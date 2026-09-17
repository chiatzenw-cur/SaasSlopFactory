"""Live reconcile against the real Paddle sandbox API, using the keys you already have.

  DESCLES_RUNTIME_ENV=<path to a .env with PADDLE_API_KEY> DESCLES_DB=data/live_reconcile.db \
    python scripts/test_live_reconcile.py

Read-only GET of /transactions. Nothing is written to any real company: this runs
against a throwaway DB, and prints COUNTS ONLY — never a customer email or an amount
that belongs to another product.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from descles import billing, config, db, runtime  # noqa: E402

loaded, seen = config.load_keys()
print("env files         :", ", ".join(seen) if seen else "(none — pass DESCLES_RUNTIME_ENV)")
print("keys loaded       :", ", ".join(loaded) if loaded else "(none)")
print("paddle env        :", billing.paddle_env())
print("api base          :", billing.api_base("paddle"))
print("can reconcile     :", billing.can_reconcile())
print("checkout mode     :", "link" if billing.checkout_url() else
      ("paddle_js" if billing.paddle_js_config() else "disabled"))
print()

db.init()
for t in ("companies", "agents", "events", "ledger", "projects", "board_requests", "decisions",
          "actions", "runs", "evaluations", "capabilities", "setup_requests", "webhook_events",
          "metrics", "builds"):
    db.ex(f"DELETE FROM {t}")
c = runtime.incorporate("Reconcile Probe Co", "read-only probe", capital_cents=10000)

out = runtime.reconcile_revenue(c["id"], "paddle")
print("reconcile ->", {k: v for k, v in out.items() if k != "error"})
if not out.get("ok"):
    print("  error:", str(out.get("error"))[:300])
else:
    st = runtime.state(c["id"])
    print(f"  seen={out['seen']} booked={out['booked_cents']}c unattributed={out['unattributed_cents']}c "
          f"duplicates={out['duplicates']}")
    print(f"  company MRR after reconcile: {st['mrr_cents']}c  (must stay 0 unless a sale named this company)")
    print(f"  unattributed rows: {len(st['unattributed'])}")

    print("\n  idempotency: reconcile twice, nothing may be booked twice")
    again = runtime.reconcile_revenue(c["id"], "paddle")
    print(f"  second run -> seen={again.get('seen')} booked={again.get('booked_cents')}c "
          f"duplicates={again.get('duplicates')}")
    print(f"  MRR still: {runtime.state(c['id'])['mrr_cents']}c")
