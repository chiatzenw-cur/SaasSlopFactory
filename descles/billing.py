"""Payment rails and the only honest source of revenue.

A REVENUE row is written by a *signature-verified payment webhook*, never by a
model and never by a fixture. When no rail is connected the runtime files a setup
request and the landing page renders a visibly disabled CTA — a fake working
"Buy" button is the one thing that would make this whole system a lie.

Providers: paddle (merchant of record — the right rail for a China-based seller,
it remits VAT and handles buyer support) and stripe. Signature verification is a
local HMAC, so the gate is testable with no account at all.
"""

import hashlib
import hmac
import json
import os
import time

from . import db, events as ev, metrics as metrics_mod


class SignatureError(RuntimeError):
    pass


# ------------------------------------------------------------------ verification


def _paddle_signature(raw_body, header, secret, tolerance=300):
    """Paddle-Signature: ts=1671552777;h1=eb4d0dcf5c80dd441b2af1be896acada2df9546529abfa79ed675b1060d02983"""
    parts = dict(
        p.strip().split("=", 1) for p in (header or "").split(";") if "=" in p
    )
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
    """Stripe-Signature: t=1671552777,v1=eb4d0dcf..."""
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
    if provider == "paddle":
        secret = os.environ.get("PADDLE_WEBHOOK_SECRET")
        if not secret:
            raise SignatureError("PADDLE_WEBHOOK_SECRET not configured")
        return _paddle_signature(raw_body, headers.get("paddle-signature"), secret)
    if provider == "stripe":
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        if not secret:
            raise SignatureError("STRIPE_WEBHOOK_SECRET not configured")
        return _stripe_signature(raw_body, headers.get("stripe-signature"), secret)
    raise SignatureError(f"unknown provider {provider}")


# ------------------------------------------------------------------ payload mapping


def _dig(d, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def interpret(provider, event):
    """-> {kind, event_id, type, amount_cents, currency, project_slug, email, raw_totals}"""
    if provider == "paddle":
        data = event.get("data") or {}
        totals = _dig(data, "details", "totals") or {}
        total = totals.get("total") or totals.get("grand_total") or "0"
        slug = _dig(data, "custom_data", "project_slug") or _dig(data, "items", 0, "price", "custom_data", "project_slug")
        return {
            "kind": "revenue",
            "event_id": event.get("event_id") or _dig(data, "id"),
            "type": event.get("event_type"),
            "amount_cents": int(str(total).split(".")[0] or 0),
            "currency": data.get("currency_code") or "USD",
            "project_slug": slug,
            "email": _dig(data, "customer", "email"),
            "totals": totals,          # subtotal / tax / fee / earnings — the real payout number
            "customer_id": _dig(data, "customer_id"),
        }
    if provider == "stripe":
        obj = (event.get("data") or {}).get("object") or {}
        return {
            "kind": "revenue",
            "event_id": event.get("id"),
            "type": event.get("type"),
            "amount_cents": int(obj.get("amount_total") or 0),
            "currency": (obj.get("currency") or "usd").upper(),
            "project_slug": _dig(obj, "metadata", "project_slug"),
            "email": _dig(obj, "customer_details", "email"),
            "totals": {},
            "customer_id": obj.get("customer"),
        }
    raise SignatureError(f"unknown provider {provider}")


REVENUE_EVENTS = {
    "paddle": {"transaction.completed", "transaction.paid"},
    "stripe": {"checkout.session.completed", "payment_intent.succeeded"},
}
REVERSAL_EVENTS = {
    "paddle": {"adjustment.created", "adjustment.updated"},
    "stripe": {"charge.refunded"},
}


def handle_webhook(company_id, provider, raw_body, headers):
    """Verify -> dedupe -> interpret -> ONE revenue event. Returns a JSON-able result."""
    verify(provider, raw_body, headers)
    try:
        event = json.loads(raw_body.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        raise SignatureError(f"body is not JSON: {e}")
    info = interpret(provider, event)
    eid = info["event_id"]
    if not eid:
        raise SignatureError("event has no id — cannot dedupe, refusing to write revenue")

    dup = db.row("SELECT event_id FROM webhook_events WHERE provider=? AND event_id=?", (provider, eid))
    if dup:
        return {"ok": True, "duplicate": True, "event_id": eid, "revenue_cents": 0}

    db.ex(
        "INSERT INTO webhook_events(provider,event_id,ts,type,payload) VALUES(?,?,?,?,?)",
        (provider, eid, ev.now(), info["type"], raw_body.decode("utf-8", "replace")[:20000]),
    )

    slug = info["project_slug"]
    p = None
    if slug:
        p = db.row("SELECT * FROM projects WHERE company_id=? AND slug=?", (company_id, slug))
    if p is None:
        # attribute by the only open build, and SAY so — do not silently guess in the money
        b = db.row("SELECT * FROM builds WHERE company_id=? ORDER BY ts DESC LIMIT 1", (company_id,))
        if b:
            p = db.row("SELECT * FROM projects WHERE id=?", (b["project_id"],))
            info["attribution"] = "inferred from the most recent build — webhook carried no project_slug"
        else:
            info["attribution"] = "unattributed: no project matched and no build exists"

    amount = int(info["amount_cents"] or 0)
    if info["type"] in REVENUE_EVENTS.get(provider, set()):
        if amount <= 0:
            return {"ok": True, "event_id": eid, "revenue_cents": 0, "note": "non-positive amount, nothing written"}
        e = ev.append(company_id, "STRIPE" if provider == "stripe" else "PADDLE", "REVENUE_RECEIVED", {
            "provider": provider, "event_id": eid, "type": info["type"],
            "amount_cents": amount, "currency": info["currency"],
            "project_slug": p["slug"] if p else None,
            "totals": info.get("totals"), "attribution": info.get("attribution"),
        })
        db.ex(
            "INSERT INTO ledger(company_id,ts,kind,amount_cents,project_id,actor,memo,event_id) VALUES(?,?,?,?,?,?,?,?)",
            (company_id, ev.now(), "REVENUE", amount, p["id"] if p else None,
             f"{provider}:{info['type']}", f"{info['type']} {eid}", e["id"]),
        )
        if p:
            metrics_mod.record(company_id, "revenue_cents", amount, project_id=p["id"],
                               meta={"provider": provider, "event_id": eid})
            db.ex("UPDATE projects SET revenue_cents=revenue_cents+?, stage=CASE WHEN stage IN ('BUILT','BUILT_LOCAL','LIVE') THEN 'LIVE' ELSE stage END, updated_at=? WHERE id=?",
                  (amount, ev.now(), p["id"]))
        return {"ok": True, "event_id": eid, "revenue_cents": amount,
                "project_slug": p["slug"] if p else None}
    return {"ok": True, "event_id": eid, "ignored": info["type"], "revenue_cents": 0}


def checkout_url(project_slug=None, price_cents=None):
    """A real checkout link, or None. Never a placeholder that looks like one."""
    base = os.environ.get("PADDLE_CHECKOUT_URL")
    if base:
        sep = "&" if "?" in base else "?"
        extra = f"&custom_data[project_slug]={project_slug}" if project_slug else ""
        return base + sep + "utm_source=descles" + extra
    base = os.environ.get("STRIPE_PAYMENT_LINK")
    if base:
        return base
    return None


def rail_connected():
    return bool(os.environ.get("PADDLE_API_KEY") or os.environ.get("STRIPE_SECRET_KEY")
                or os.environ.get("PADDLE_CHECKOUT_URL") or os.environ.get("STRIPE_PAYMENT_LINK"))


def sign(provider, raw_body, secret, ts=None):
    """Test-side signer. Same maths the provider uses, so the gate is testable
    with no account and no credentials."""
    ts = int(ts or time.time())
    if provider == "paddle":
        h1 = hmac.new(secret.encode(), f"{ts}:".encode() + raw_body, hashlib.sha256).hexdigest()
        return f"ts={ts};h1={h1}"
    h1 = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={h1}"
