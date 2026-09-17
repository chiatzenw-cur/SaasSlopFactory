"""Payment rails and the only honest source of revenue.

A REVENUE row is written by a *signature-verified payment webhook* or by a
*reconciled provider API transaction*, never by a model and never by a fixture.
When no rail is connected the runtime files a setup request and the landing page
renders a visibly disabled CTA — a fake working "Buy" button is the one thing that
would make this whole system a lie.

Two ways in, and the pull path matters more than it looks:

  push  POST /api/webhook/<provider>/<company>   needs a public URL (tunnel or deploy)
  pull  reconcile()                              needs only the API key — works behind NAT

Providers: paddle (merchant of record — the right rail for a China-based seller:
it remits VAT and handles buyer support) and stripe. Signature verification is a
local HMAC, so the gate is testable with no account at all.

ATTRIBUTION RULE: a payment is booked to this company only when the payload names a
project (`custom_data.project_slug`). A shared account will deliver other products'
sales to the same endpoint; crediting those to this company would inflate MRR with
money it did not earn. Unattributed events are stored and surfaced, never booked.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

from . import db, events as ev, metrics as metrics_mod


class SignatureError(RuntimeError):
    pass


# ------------------------------------------------------------------ config


def env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def webhook_secret(provider):
    if provider == "paddle":
        # visa-checker uses PADDLE_NOTIFICATION_WEBHOOK_SECRET; accept both so an
        # existing env file works without being rewritten
        return env("PADDLE_WEBHOOK_SECRET", "PADDLE_NOTIFICATION_WEBHOOK_SECRET")
    if provider == "stripe":
        return env("STRIPE_WEBHOOK_SECRET")
    return None


def paddle_env():
    raw = (env("PADDLE_ENV", "NEXT_PUBLIC_PADDLE_ENV") or "sandbox").lower()
    return "production" if raw.startswith(("prod", "live")) else "sandbox"


def api_base(provider="paddle"):
    if provider == "paddle":
        return ("https://api.paddle.com" if paddle_env() == "production"
                else "https://sandbox-api.paddle.com")
    return "https://api.stripe.com/v1"


def provision_price(project, price_cents, currency="USD", description=None):
    """Create this product's own price in the payment account, so a Buy button can
    never sell a different product's price id.

    Writes to a live catalog are refused unless explicitly opted in: a product's tax
    category freezes after its first sale, so an accidental live product is
    effectively irreversible.
    """
    key = env("PADDLE_API_KEY")
    if not key:
        return {"ok": False, "error": "no PADDLE_API_KEY"}
    if paddle_env() == "production" and not env("CONFIRM_PRODUCTION_CATALOG"):
        return {"ok": False, "error": "refusing to write to the LIVE catalog — set "
                                      "CONFIRM_PRODUCTION_CATALOG=yes if you really mean it"}
    amount = str(max(int(price_cents), 1000))
    base = api_base("paddle")
    try:
        prod = _post(base + "/products", key, {
            "name": (project["name"] or "Product")[:60],
            "description": (description or project["hypothesis"] or "")[:200],
            "tax_category": "standard",
        })
        pid = _dig(prod, "data", "id")
        if not pid:
            return {"ok": False, "error": f"product create returned {str(prod)[:200]}"}
        price = _post(base + "/prices", key, {
            "product_id": pid,
            "description": (project["name"] or "Product")[:60],
            "unit_price": {"amount": amount, "currency_code": currency},
            "quantity": {"minimum": 1, "maximum": 1},
        })
        price_id = _dig(price, "data", "id")
        if not price_id:
            return {"ok": False, "error": f"price create returned {str(price)[:200]}", "product_id": pid}
        return {"ok": True, "product_id": pid, "price_id": price_id,
                "amount": amount, "currency": currency, "env": paddle_env()}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        return {"ok": False, "status": e.code, "error": detail,
                "hint": "a fresh sandbox key can come back read-only; read e.errors"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _post(url, key, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def client_checkout_price(project_price_id=None):
    """The price id the page may sell. Only one that belongs to THIS product, or one
    the human explicitly declared for this runtime — never a borrowed env value that
    belongs to another product in a shared account."""
    if project_price_id:
        return {"price_id": project_price_id, "source": "provisioned for this product"}
    declared = env("DESCLES_PADDLE_PRICE_ID")
    if declared:
        return {"price_id": declared, "source": "declared via DESCLES_PADDLE_PRICE_ID"}
    return None


def paddle_js_config(price_id=None):
    """Client-side checkout needs only the public token + a price id for THIS product."""
    token = env("NEXT_PUBLIC_PADDLE_CLIENT_TOKEN", "PADDLE_CLIENT_TOKEN")
    if not token:
        return None
    chosen = client_checkout_price(price_id)
    if not chosen:
        return None
    return {"token": token, "price_id": chosen["price_id"], "env": paddle_env(),
            "sandbox": paddle_env() == "sandbox", "source": chosen["source"],
            "borrowed_price_id": env("NEXT_PUBLIC_PADDLE_PRICE_ID")}


def checkout_url(project_slug=None, price_cents=None):
    """A server-side checkout link, or None. Never a placeholder that looks like one."""
    base = env("PADDLE_CHECKOUT_URL")
    if base:
        sep = "&" if "?" in base else "?"
        extra = f"&custom_data[project_slug]={project_slug}" if project_slug else ""
        return base + sep + "utm_source=descles" + extra
    return env("STRIPE_PAYMENT_LINK")


def rail_connected():
    return bool(env("PADDLE_API_KEY", "STRIPE_SECRET_KEY", "PADDLE_CHECKOUT_URL",
                    "STRIPE_PAYMENT_LINK") or paddle_js_config())


def can_reconcile():
    return bool(env("PADDLE_API_KEY", "STRIPE_SECRET_KEY"))


# ------------------------------------------------------------------ verification


def _paddle_signature(raw_body, header, secret, tolerance=300):
    """Paddle-Signature: ts=1671552777;h1=eb4d0dcf5c80dd441b2af1be896acada2df9546529abfa79ed675b1060d02983"""
    parts = dict(p.strip().split("=", 1) for p in (header or "").split(";") if "=" in p)
    ts, h1 = parts.get("ts"), parts.get("h1")
    if not ts or not h1:
        raise SignatureError("malformed Paddle-Signature header")
    if abs(time.time() - int(ts)) > tolerance:
        raise SignatureError(f"signature timestamp outside {tolerance}s tolerance")
    signed = f"{ts}:".encode() + raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, h1):
        raise SignatureError("Paddle signature mismatch")
    return True


def _stripe_signature(raw_body, header, secret, tolerance=300):
    parts = dict(p.strip().split("=", 1) for p in (header or "").split(",") if "=" in p)
    ts, v1 = parts.get("t"), parts.get("v1")
    if not ts or not v1:
        raise SignatureError("malformed Stripe-Signature header")
    if abs(time.time() - int(ts)) > tolerance:
        raise SignatureError(f"signature timestamp outside {tolerance}s tolerance")
    signed = f"{ts}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        raise SignatureError("Stripe signature mismatch")
    return True


def verify(provider, raw_body, headers):
    secret = webhook_secret(provider)
    if not secret:
        raise SignatureError(f"no webhook secret configured for {provider}")
    if provider == "paddle":
        return _paddle_signature(raw_body, headers.get("paddle-signature"), secret)
    if provider == "stripe":
        return _stripe_signature(raw_body, headers.get("stripe-signature"), secret)
    raise SignatureError(f"unknown provider {provider}")


def sign(provider, raw_body, secret, ts=None):
    """Test-side signer. Same maths the provider uses, so the gate is testable
    with no account and no credentials."""
    ts = int(ts or time.time())
    if provider == "paddle":
        h1 = hmac.new(secret.encode(), f"{ts}:".encode() + raw_body, hashlib.sha256).hexdigest()
        return f"ts={ts};h1={h1}"
    h1 = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={h1}"


# ------------------------------------------------------------------ payload mapping


def _dig(d, *path, default=None):
    cur = d
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and isinstance(p, int) and len(cur) > p:
            cur = cur[p]
        else:
            return default
    return cur


def interpret_transaction(provider, tx):
    """A provider *transaction* (API object) -> the same shape a webhook yields."""
    if provider == "paddle":
        totals = _dig(tx, "details", "totals") or {}
        return {
            "event_id": f"txn:{tx.get('id')}",
            "type": "transaction.completed",
            "amount_cents": int(str(totals.get("total") or "0").split(".")[0] or 0),
            "currency": tx.get("currency_code") or "USD",
            "project_slug": _dig(tx, "custom_data", "project_slug"),
            "email": _dig(tx, "customer", "email"),
            "totals": totals,
            "provider_id": tx.get("id"),
        }
    obj = tx
    return {
        "event_id": f"txn:{obj.get('id')}",
        "type": "checkout.session.completed",
        "amount_cents": int(obj.get("amount_total") or 0),
        "currency": (obj.get("currency") or "usd").upper(),
        "project_slug": _dig(obj, "metadata", "project_slug"),
        "email": _dig(obj, "customer_details", "email"),
        "totals": {},
        "provider_id": obj.get("id"),
    }


def interpret(provider, event):
    if provider == "paddle":
        data = event.get("data") or {}
        info = interpret_transaction("paddle", data)
        info["event_id"] = event.get("event_id") or f"txn:{data.get('id')}"
        info["type"] = event.get("event_type") or "transaction.completed"
        return info
    obj = (event.get("data") or {}).get("object") or {}
    info = interpret_transaction("stripe", obj)
    info["event_id"] = event.get("id") or info["event_id"]
    info["type"] = event.get("type") or "checkout.session.completed"
    return info


REVENUE_EVENTS = {
    "paddle": {"transaction.completed", "transaction.paid"},
    "stripe": {"checkout.session.completed", "payment_intent.succeeded"},
}


# ------------------------------------------------------------------ booking


def book(company_id, provider, info, raw=""):
    """The single writer of REVENUE rows. Idempotent on (provider, event_id)."""
    eid = info["event_id"]
    if not eid:
        raise SignatureError("event has no id — cannot dedupe, refusing to write revenue")
    if db.row("SELECT event_id FROM webhook_events WHERE provider=? AND event_id=?", (provider, eid)):
        return {"ok": True, "duplicate": True, "event_id": eid, "revenue_cents": 0}

    slug = info.get("project_slug")
    p = db.row("SELECT * FROM projects WHERE company_id=? AND slug=?", (company_id, slug)) if slug else None
    amount = int(info.get("amount_cents") or 0)
    is_revenue = info.get("type") in REVENUE_EVENTS.get(provider, set())

    if not is_revenue:
        db.ex("INSERT INTO webhook_events(provider,event_id,ts,type,payload,booked,amount_cents,reason)"
              " VALUES(?,?,?,?,?,?,?,?)",
              (provider, eid, ev.now(), info.get("type"), raw[:20000], 0, amount, "not a revenue event"))
        return {"ok": True, "event_id": eid, "ignored": info.get("type"), "revenue_cents": 0}

    if p is None:
        # A shared provider account delivers other products' sales to the same
        # endpoint. Storing it is honest; booking it would inflate MRR with money
        # this company did not earn.
        why = ("payload named no project_slug" if not slug
               else f"project_slug={slug!r} matches no project in this company")
        db.ex("INSERT INTO webhook_events(provider,event_id,ts,type,payload,booked,amount_cents,reason)"
              " VALUES(?,?,?,?,?,?,?,?)",
              (provider, eid, ev.now(), info.get("type"), raw[:20000], 0, amount, "unattributed: " + why))
        ev.append(company_id, provider.upper(), "REVENUE_UNATTRIBUTED", {
            "provider": provider, "event_id": eid, "amount_cents": amount,
            "currency": info.get("currency"), "why": why, "email": info.get("email")})
        return {"ok": True, "event_id": eid, "revenue_cents": 0,
                "unattributed": amount, "reason": why}

    if amount <= 0:
        db.ex("INSERT INTO webhook_events(provider,event_id,ts,type,payload,booked,amount_cents,reason)"
              " VALUES(?,?,?,?,?,?,?,?)",
              (provider, eid, ev.now(), info.get("type"), raw[:20000], 0, amount, "non-positive amount"))
        return {"ok": True, "event_id": eid, "revenue_cents": 0, "note": "non-positive amount"}

    e = ev.append(company_id, provider.upper(), "REVENUE_RECEIVED", {
        "provider": provider, "event_id": eid, "type": info.get("type"),
        "amount_cents": amount, "currency": info.get("currency"),
        "project_slug": p["slug"], "totals": info.get("totals")})
    db.ex("INSERT INTO webhook_events(provider,event_id,ts,type,payload,booked,amount_cents,project_id,reason)"
          " VALUES(?,?,?,?,?,?,?,?,?)",
          (provider, eid, ev.now(), info.get("type"), raw[:20000], 1, amount, p["id"], "booked"))
    db.ex("INSERT INTO ledger(company_id,ts,kind,amount_cents,project_id,actor,memo,event_id) VALUES(?,?,?,?,?,?,?,?)",
          (company_id, ev.now(), "REVENUE", amount, p["id"], f"{provider}:{info.get('type')}",
           f"{info.get('type')} {eid}", e["id"]))
    metrics_mod.record(company_id, "revenue_cents", amount, project_id=p["id"],
                       meta={"provider": provider, "event_id": eid})
    db.ex("UPDATE projects SET revenue_cents=revenue_cents+?,"
          " stage=CASE WHEN stage IN ('BUILT_LOCAL','BUILDING','LIVE') THEN 'LIVE' ELSE stage END,"
          " updated_at=? WHERE id=?", (amount, ev.now(), p["id"]))
    return {"ok": True, "event_id": eid, "revenue_cents": amount, "project_slug": p["slug"]}


def handle_webhook(company_id, provider, raw_body, headers):
    verify(provider, raw_body, headers)
    try:
        event = json.loads(raw_body.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        raise SignatureError(f"body is not JSON: {e}")
    return book(company_id, provider, interpret(provider, event), raw_body.decode("utf-8", "replace")[:20000])


# ------------------------------------------------------------------ pull / reconcile


def _get(url, key):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key,
                                              "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def reconcile(company_id, provider="paddle", limit=50, created_after=None):
    """Pull completed transactions with the API key. No public URL, no tunnel:
    this is what makes the revenue path work on a machine behind NAT."""
    key = env("PADDLE_API_KEY") if provider == "paddle" else env("STRIPE_SECRET_KEY")
    if not key:
        return {"ok": False, "error": f"no {provider} API key — file a setup request instead of guessing"}
    if provider == "paddle":
        q = f"/transactions?status=completed&per_page={limit}&sort=-created_at"
        if created_after:
            q += f"&created_at[GTE]={created_after}"
        url = api_base("paddle") + q
    else:
        url = api_base("stripe") + f"/checkout/sessions?limit={limit}"
    try:
        out = _get(url, key)
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    seen, booked, unattributed, dupes = 0, 0, 0, 0
    for tx in out.get("data", []):
        seen += 1
        info = interpret_transaction(provider, tx)
        r = book(company_id, provider, info, json.dumps(tx, ensure_ascii=False)[:20000])
        if r.get("duplicate"):
            dupes += 1
        elif r.get("revenue_cents"):
            booked += r["revenue_cents"]
        elif r.get("unattributed"):
            unattributed += r["unattributed"]
    ev.append(company_id, "CFO", "REVENUE_RECONCILED", {
        "provider": provider, "seen": seen, "booked_cents": booked,
        "unattributed_cents": unattributed, "duplicates": dupes, "env": paddle_env()})
    return {"ok": True, "provider": provider, "env": paddle_env(), "seen": seen,
            "booked_cents": booked, "unattributed_cents": unattributed, "duplicates": dupes,
            "raw_count": len(out.get("data", []))}


def unattributed(company_id):
    rows = db.rows(
        "SELECT provider,event_id,ts,type,amount_cents,reason FROM webhook_events"
        " WHERE booked=0 AND amount_cents>0 ORDER BY ts DESC LIMIT 50"
    )
    return rows
