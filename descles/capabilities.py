"""Capabilities and the human-in-the-loop boundary.

Principle: the company runs fully autonomously. A human is asked for exactly two
kinds of things — **identity** and **money** — and when that happens the runtime
files a SETUP REQUEST, which is not the same object as a BOARD REQUEST:

  BOARD REQUEST  = "may I spend this money?"        (a decision, seconds to make)
  SETUP REQUEST  = "I cannot proceed without you"   (an action only a human can take)

A missing capability parks the one project that needs it. It never stalls the
whole company and it never degrades into a fake success.
"""

import json
import os

from . import db, events as ev

# mode: builtin = always on · key = needs a credential · manual = needs a human action
CAPABILITIES = {
    "web_search": {
        "label": "Web search / public sources", "mode": "builtin",
        "why": "reading live buying signals", "human_kind": None,
    },
    "browser": {
        "label": "Browser", "mode": "builtin",
        "why": "reading pages that are not APIs", "human_kind": None,
    },
    "local_filesystem": {
        "label": "Local filesystem", "mode": "builtin",
        "why": "building the product on your machine", "human_kind": None,
    },
    "payment_rail": {
        "label": "Payment rail", "mode": "key", "env": ["PADDLE_API_KEY", "STRIPE_SECRET_KEY"],
        "why": "the only honest source of a REVENUE row — someone has to actually be able to pay",
        "human_kind": "money",
        "steps": [
            "If you already sell with Paddle, point the runtime at that env file: DESCLES_RUNTIME_ENV=<path> "
            "(PADDLE_API_KEY, PADDLE_NOTIFICATION_WEBHOOK_SECRET, NEXT_PUBLIC_PADDLE_CLIENT_TOKEN, NEXT_PUBLIC_PADDLE_PRICE_ID).",
            "Otherwise create a Paddle sandbox vendor (sandbox-vendors.paddle.com) — separate account, separate keys; "
            "sandbox auto-approves domains and needs no paperwork.",
            "Catalog > Products: one product + one price (>= $10). A new sandbox key can come back read-only — probe a write.",
            "The Buy button only needs the PUBLIC client token + price id; the runtime renders Paddle.js itself.",
            "Revenue is then reconciled by POLLING the API (no public URL, no tunnel). "
            "Webhooks are optional: add destination <public URL>/api/webhook/paddle/<company_id> with traffic_source=all.",
        ],
        "url": "https://sandbox-vendors.paddle.com",
    },
    "public_url": {
        "label": "Public URL", "mode": "key", "env": ["DESCLES_PUBLIC_URL"],
        "why": "webhooks are push, so the provider has to be able to reach this machine — "
               "only needed if you want push instead of the API poll, and a localhost page cannot "
               "receive a visitor who is not you",
        "human_kind": "money",
        "steps": [
            "Install cloudflared (no account needed) and put it on PATH — the runtime then opens its own tunnel, or",
            "Run your own tunnel and set DESCLES_PUBLIC_URL=https://...",
        ],
        "url": "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
    },
    "deploy_host": {
        "label": "Deploy host", "mode": "key", "env": ["VERCEL_TOKEN"],
        "why": "a landing page on localhost cannot receive a visitor who is not you",
        "human_kind": "money",
        "steps": [
            "Create a Vercel token (vercel.com/account/tokens) and put VERCEL_TOKEN in the env file.",
            "Nothing else: the runtime deploys the built page itself.",
        ],
        "url": "https://vercel.com/account/tokens",
    },
    "ad_account": {
        "label": "Ad account", "mode": "key", "env": ["GOOGLE_ADS_TOKEN", "META_ADS_TOKEN"],
        "why": "paid acquisition is one of the tests the COO may pick — and real ad spend is real money",
        "human_kind": "money",
        "steps": ["Add an ads token plus a payment method. The charter still caps every spend per role."],
        "url": None,
    },
    "email": {
        "label": "Outbound email", "mode": "key", "env": ["SMTP_HOST"],
        "why": "email is the one channel that can burn a domain irreversibly — the charter requires approval per send",
        "human_kind": "identity",
        "steps": ["Add SMTP credentials; every send still opens a board request."],
        "url": None,
    },
    "github": {
        "label": "GitHub", "mode": "key", "env": ["GITHUB_TOKEN"],
        "why": "publishing source and opening PRs", "human_kind": "identity",
        "steps": ["Add a GITHUB_TOKEN with repo scope."], "url": "https://github.com/settings/tokens",
    },
    "domain_registrar": {
        "label": "Domain registrar", "mode": "key", "env": ["GODADDY_API_KEY", "NAMECHEAP_API_KEY"],
        "why": "a real product usually wants a real name", "human_kind": "money",
        "steps": ["Add a registrar API key. Domestic .com in China requires a verified registrant identity."],
        "url": None,
    },
    "play_console": {
        "label": "Play Console", "mode": "manual",
        "why": "shipping to a store needs a verified developer identity, not a key",
        "human_kind": "identity",
        "steps": ["Register a Play developer account ($25) and complete identity verification."],
        "url": "https://play.google.com/console",
    },
}


def status(company_id, name):
    spec = CAPABILITIES.get(name)
    if not spec:
        return "UNKNOWN"
    if spec["mode"] == "builtin":
        return "CONNECTED"
    if spec["mode"] == "manual":
        row = db.row("SELECT status FROM capabilities WHERE company_id=? AND name=?", (company_id, name))
        return (row["status"] if row else None) or "NOT_CONNECTED"
    if name == "payment_rail":
        from . import billing
        # the rail is usable when the company can both charge and read back its own
        # money: a client token for checkout, plus an API key to provision prices and
        # reconcile. Requiring a hand-declared price id would understate what it can do.
        if billing.paddle_js_config() or billing.checkout_url():
            return "CONNECTED"
        if billing.can_reconcile() and db.row(
            "SELECT id FROM builds WHERE company_id=? AND price_id IS NOT NULL LIMIT 1", (company_id,)
        ):
            return "CONNECTED"
        return "NOT_CONNECTED"
    if any(os.environ.get(k) for k in spec["env"]):
        return "CONNECTED"
    return "NOT_CONNECTED"


def connected(company_id, name):
    return status(company_id, name) == "CONNECTED"


def sync(company_id):
    """Refresh the capability table from the environment + human confirmations."""
    keep = ", ".join("'%s'" % n for n in CAPABILITIES)
    db.ex(f"DELETE FROM capabilities WHERE company_id=? AND name NOT IN ({keep})", (company_id,))
    for name, spec in CAPABILITIES.items():
        st = status(company_id, name)
        detail = "" if st == "CONNECTED" else "; ".join(spec.get("steps") or [])[:300]
        db.ex(
            "INSERT OR REPLACE INTO capabilities(company_id,name,status,detail) VALUES(?,?,?,?)",
            (company_id, name, st, detail),
        )
        # a request whose capability is now satisfied must not keep asking: a stale
        # pending item is a false statement about what the company is waiting for
        if st == "CONNECTED":
            stale = db.rows(
                "SELECT id FROM setup_requests WHERE company_id=? AND capability=? AND status='PENDING'",
                (company_id, name),
            )
            for r in stale:
                db.ex("UPDATE setup_requests SET status='RESOLVED', resolved_at=?, note=? WHERE id=?",
                      (ev.now(), "capability became available", r["id"]))
                ev.append(company_id, "SYSTEM", "SETUP_AUTO_RESOLVED",
                          {"capability": name, "request_id": r["id"]})


def pending(company_id):
    return db.rows(
        "SELECT * FROM setup_requests WHERE company_id=? AND status='PENDING' ORDER BY ts ASC",
        (company_id,),
    )


def require(company_id, name, why_extra="", project_id=None):
    """Ask a human for a capability. Idempotent: one open request per capability.

    Returns True when the capability is already available (nothing was filed).
    """
    if connected(company_id, name):
        return True
    spec = CAPABILITIES.get(name, {})
    if not spec:
        return True
    existing = db.row(
        "SELECT id FROM setup_requests WHERE company_id=? AND capability=? AND status='PENDING'",
        (company_id, name),
    )
    if existing:
        return False
    rid = "setup_" + ev.now().replace("-", "").replace(":", "")[:15] + "_" + name[:4]
    why = (spec.get("why", "") + (" — " + why_extra if why_extra else "")).strip()
    db.ex(
        "INSERT INTO setup_requests(id,company_id,ts,capability,title,why,steps,url,status,project_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            rid, company_id, ev.now(), name,
            f"Connect {spec.get('label', name)}",
            why,
            json.dumps(spec.get("steps") or [], ensure_ascii=False),
            spec.get("url"), "PENDING", project_id,
        ),
    )
    ev.append(company_id, "SYSTEM", "SETUP_REQUESTED", {
        "capability": name, "human_kind": spec.get("human_kind"),
        "why": spec.get("why"), "project_id": project_id, "request_id": rid,
    })
    return False


def grant(company_id, request_id, note=""):
    r = db.row("SELECT * FROM setup_requests WHERE id=? AND company_id=?", (request_id, company_id))
    if not r or r["status"] != "PENDING":
        return {"ok": False, "error": "no such pending setup request"}
    db.ex("UPDATE setup_requests SET status='GRANTED', resolved_at=?, note=? WHERE id=?",
          (ev.now(), note[:400], request_id))
    db.ex("INSERT OR REPLACE INTO capabilities(company_id,name,status,detail) VALUES(?,?,?,?)",
          (company_id, r["capability"], "CONNECTED", note[:300]))
    ev.append(company_id, "BOARD", "SETUP_GRANTED", {"capability": r["capability"], "request_id": request_id, "note": note})
    return {"ok": True, "capability": r["capability"]}
