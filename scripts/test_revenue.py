"""Money-path test. Run against a throwaway DB.

  DESCLES_DB=data/test_revenue.db PADDLE_WEBHOOK_SECRET=test_secret python scripts/test_revenue.py

Tests the half of the payment rail that exists without an account: the signature
gate is a local HMAC, so verification, idempotency and the ledger write are all
provable here. It writes NOTHING to a real company's books.
"""
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("PADDLE_WEBHOOK_SECRET", "test_secret")
os.environ.setdefault("DESCLES_DB", "data/test_revenue.db")

from descles import billing, build as buildmod, db, events as ev, metrics, runtime  # noqa: E402

SECRET = os.environ["PADDLE_WEBHOOK_SECRET"]
fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  BAD ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


db.init()
for t in ("companies", "agents", "events", "ledger", "projects", "board_requests", "decisions",
          "actions", "runs", "evaluations", "capabilities", "setup_requests", "webhook_events",
          "metrics", "builds"):
    db.ex(f"DELETE FROM {t}")

c = runtime.incorporate("Money Test Co", "Prove the revenue path.", capital_cents=10000)
cid = c["id"]
p = db.row("SELECT * FROM projects WHERE company_id=?", (cid,))
# a project that has been funded and built — the state a real payment arrives in
db.ex(
    "INSERT INTO projects(id,company_id,slug,name,stage,hypothesis,evidence,signals,success_criterion,"
    "kill_criterion,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
    ("pr_test", cid, "widget", "Widget", "BUILT_LOCAL", "people want a widget", "[]", "[]",
     ">=5% pricing intent", "<3%", ev.now(), ev.now()),
)
db.ex("INSERT INTO evaluations(company_id,project_id,role,ts,payload) VALUES(?,?,?,?,?)",
       (cid, "pr_test", "CFO", ev.now(), json.dumps({"price_cents": 1900, "verdict": "PASS"})))
project = db.row("SELECT * FROM projects WHERE id='pr_test'")
out = buildmod.build(runtime.get(cid), project, {
    "headline": "A widget that does the thing", "subheadline": "For people who need the thing",
    "bullets": ["one job", "fast", "cancel anytime"], "cta_label": "Buy widget",
    "faq": [{"q": "Refunds?", "a": "14 days"}], "footer_note": "autonomous co", "price_cents": 1900,
}, runtime.base_url())
print(f"\n[build] {out['path']}\n        checkout={out['checkout']!r}  (no rail connected -> CTA must be disabled)")
html = pathlib.Path(out["index"]).read_text(encoding="utf-8")
check("CTA is visibly disabled, not a dead link", "cta off" in html and "PAYMENT NOT CONNECTED" in html)
check("page reports its own traffic", "/pixel.gif" in html)
check("no fake testimonials/customer claims", "testimonial" not in html.lower())

setup = runtime.state(cid)["setup_requests"]
check("a setup request was filed for the missing rail",
      any(r["capability"] == "payment_rail" for r in setup), f"{len(setup)} request(s)")

print("\n[webhook] signed Paddle transaction.completed")
event = {
    "event_id": "evt_test_0001",
    "event_type": "transaction.completed",
    "data": {
        "id": "txn_test_0001",
        "currency_code": "USD",
        "custom_data": {"project_slug": "widget"},
        "details": {"totals": {"subtotal": "1900", "tax": "0", "total": "1900", "fee": "145", "earnings": "1755"}},
        "customer": {"email": "buyer@example.com"},
    },
}
raw = json.dumps(event).encode()
headers = {"paddle-signature": billing.sign("paddle", raw, SECRET)}
res = billing.handle_webhook(cid, "paddle", raw, headers)
print("        ->", res)
check("revenue recorded", res.get("revenue_cents") == 1900, str(res))
st = runtime.state(cid)
check("MRR moved off zero", st["mrr_cents"] == 1900, f"mrr={st['mrr_cents']}")
check("ledger has a REVENUE row", any(l["kind"] == "REVENUE" for l in st["ledger"]))
check("project attributed", res.get("project_slug") == "widget")
check("project revenue_cents updated", [x for x in st["projects"] if x["slug"] == "widget"][0]["revenue_cents"] == 1900)

print("\n[webhook] replay the same event_id (Paddle is at-least-once)")
res2 = billing.handle_webhook(cid, "paddle", raw, headers)
check("duplicate detected", res2.get("duplicate") is True)
check("no double-counted revenue", runtime.state(cid)["mrr_cents"] == 1900,
      f"mrr={runtime.state(cid)['mrr_cents']}")

print("\n[webhook] forged signature")
bad = {"paddle-signature": billing.sign("paddle", raw, "wrong_secret")}
try:
    billing.handle_webhook(cid, "paddle", raw, bad)
    check("forged signature rejected", False, "ACCEPTED — the gate is open")
except billing.SignatureError as e:
    check("forged signature rejected", True, str(e))

print("\n[webhook] replayed old ts (replay window)")
old = {"paddle-signature": billing.sign("paddle", raw, SECRET, ts=int(time.time()) - 4000)}
try:
    billing.handle_webhook(cid, "paddle", raw, old)
    check("stale timestamp rejected", False, "ACCEPTED")
except billing.SignatureError as e:
    check("stale timestamp rejected", True, str(e))

print("\n[webhook] body tampered after signing")
try:
    billing.handle_webhook(cid, "paddle", raw.replace(b"1900", b"9900"), headers)
    check("tampered body rejected", False, "ACCEPTED")
except billing.SignatureError as e:
    check("tampered body rejected", True, str(e))

print("\n[funnel] the measurement the COO's criterion is judged on")
for _ in range(40):
    runtime.record_page_view(cid, "widget")
for _ in range(3):
    runtime.record_pricing_intent(cid, "widget")
f = metrics.funnel(cid, "pr_test")
check("views counted", f["views"] == 40, str(f))
check("pricing intent counted", f["intents"] == 3, str(f))
check("intent rate computed", abs((f["intent_rate_pct"] or 0) - 7.5) < 0.01, str(f["intent_rate_pct"]))

print("\n[chain]", ev.verify(cid))
check("event chain intact", ev.verify(cid)["ok"])
print("\nledger:")
for l in db.rows("SELECT kind,amount_cents,actor,memo FROM ledger WHERE company_id=? ORDER BY id", (cid,)):
    print(f"  {l['kind']:9} {l['amount_cents']/100:>8.2f}  {l['actor'][:22]:22} {l['memo'][:50]}")

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
