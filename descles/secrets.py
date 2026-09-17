"""Zero-config credential intake.

The human should never be told to open an env file and retype five variable names.
They paste the token into the setup card, the runtime stores it, reads it into the
process, and **probes the provider API with it right now** — so "connected" means a
real call succeeded, not that a field was filled in.

A pasted secret is never echoed back; only a 4-character fingerprint is shown.
Stored in data/secrets.env (gitignored, plaintext, localhost-only server).
"""

import json
import os
import urllib.error
import urllib.request

from . import config, db, events as ev

SECRETS = config.DATA / "secrets.env"

# capability -> the fields its setup card asks for
FIELDS = {
    "payment_rail": [
        {"name": "PADDLE_API_KEY", "label": "Paddle API key", "secret": True,
         "placeholder": "pdl_sdbx_apikey_…", "help": "sandbox-vendors.paddle.com → Developer tools → Authentication"},
        {"name": "NEXT_PUBLIC_PADDLE_CLIENT_TOKEN", "label": "Paddle client token", "secret": False,
         "placeholder": "live_… / test_…", "help": "public by design; it opens the checkout overlay"},
        {"name": "NEXT_PUBLIC_PADDLE_ENV", "label": "Environment", "secret": False,
         "placeholder": "sandbox", "help": "sandbox or production"},
    ],
    "deploy_host": [
        {"name": "VERCEL_TOKEN", "label": "Vercel token", "secret": True,
         "placeholder": "vercel_…", "help": "vercel.com/account/tokens — deploys the built page"},
    ],
    "public_url": [
        {"name": "DESCLES_PUBLIC_URL", "label": "Public URL", "secret": False,
         "placeholder": "https://xxx.trycloudflare.com", "help": "only needed for push webhooks"},
    ],
    "ad_account": [
        {"name": "GOOGLE_ADS_TOKEN", "label": "Google Ads token", "secret": True, "placeholder": "", "help": ""},
    ],
    "email": [
        {"name": "SMTP_HOST", "label": "SMTP host", "secret": False, "placeholder": "smtp.example.com", "help": ""},
        {"name": "SMTP_USER", "label": "SMTP user", "secret": False, "placeholder": "", "help": ""},
        {"name": "SMTP_PASS", "label": "SMTP password", "secret": True, "placeholder": "", "help": ""},
    ],
    "github": [
        {"name": "GITHUB_TOKEN", "label": "GitHub token", "secret": True,
         "placeholder": "ghp_…", "help": "github.com/settings/tokens, repo scope"},
    ],
}


def fingerprint(v):
    v = str(v or "")
    return ("…" + v[-4:]) if len(v) > 4 else "…"


def load():
    """Read data/secrets.env into the environment at startup. Never prints values."""
    if not SECRETS.is_file():
        return []
    loaded = []
    for line in SECRETS.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k and v:
            os.environ[k] = v
            loaded.append(k)
    return loaded


def store(values):
    """Persist non-empty values and set them in this process. Returns what was stored."""
    have = {}
    if SECRETS.is_file():
        for line in SECRETS.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                have[k.strip()] = v.strip()
    stored = []
    for k, v in (values or {}).items():
        v = str(v or "").strip()
        if not v:
            continue
        if not k.replace("_", "").isalnum():
            continue
        have[k] = v
        os.environ[k] = v
        stored.append(k)
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    body = "# written by the descles runtime setup cards. local, plaintext, gitignored.\n"
    body += "".join(f"{k}={v}\n" for k, v in sorted(have.items()))
    SECRETS.write_text(body, encoding="utf-8")
    try:
        os.chmod(SECRETS, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return stored


# ------------------------------------------------------------------ probes


def _get(url, key=None, timeout=20):
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def verify(capability):
    """A real call to the provider. 'Connected' must mean the API answered."""
    try:
        if capability == "deploy_host":
            if not os.environ.get("VERCEL_TOKEN"):
                return {"ok": False, "error": "VERCEL_TOKEN not set"}
            u = _get("https://api.vercel.com/v2/user", os.environ["VERCEL_TOKEN"])
            return {"ok": True, "detail": f"authenticated as {(u.get('user') or {}).get('username') or (u.get('user') or {}).get('email') or 'account'}"}
        if capability == "payment_rail":
            key = os.environ.get("PADDLE_API_KEY")
            if not key:
                return {"ok": False, "error": "PADDLE_API_KEY not set"}
            from . import billing
            env = billing.paddle_env()
            u = _get(f"{billing.api_base('paddle')}/products?per_page=1", key)
            n = len(u.get("data", []))
            mode = "paddle.js" if os.environ.get("NEXT_PUBLIC_PADDLE_CLIENT_TOKEN") else "API only (no client token yet)"
            return {"ok": True, "detail": f"{env} catalog reachable, {n} product(s) visible, checkout via {mode}"}
        if capability == "public_url":
            u = os.environ.get("DESCLES_PUBLIC_URL")
            if not u:
                return {"ok": False, "error": "DESCLES_PUBLIC_URL not set"}
            body = _get(u.rstrip("/") + "/api/health")
            return {"ok": bool(body.get("ok")), "detail": f"reached {u}" if body.get("ok") else "unexpected payload"}
        if capability == "github":
            if not os.environ.get("GITHUB_TOKEN"):
                return {"ok": False, "error": "GITHUB_TOKEN not set"}
            u = _get("https://api.github.com/user", os.environ["GITHUB_TOKEN"])
            return {"ok": True, "detail": f"github user {u.get('login')}"}
        if capability == "email":
            return ({"ok": True, "detail": "SMTP host stored — sends still need a board approval each"}
                    if os.environ.get("SMTP_HOST") else {"ok": False, "error": "SMTP_HOST not set"})
        if capability == "ad_account":
            return ({"ok": True, "detail": "ad token stored — the charter still caps every spend per role"}
                    if os.environ.get("GOOGLE_ADS_TOKEN") else {"ok": False, "error": "no ad token"})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        hint = ""
        if e.code in (401, 403):
            hint = " — the token was rejected or lacks permission"
        return {"ok": False, "status": e.code, "error": f"HTTP {e.code}{hint}: {detail}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "detail": "no probe defined"}


def fields_for(capability):
    out = []
    for f in FIELDS.get(capability, []):
        v = os.environ.get(f["name"])
        out.append({**f, "set": bool(v), "fingerprint": fingerprint(v) if v else None})
    return out
