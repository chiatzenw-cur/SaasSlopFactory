import json
import time

from descles import db, runtime, policy, events as ev, signals as sigmod

db.init()
print("db:", db.config.DB_PATH)

# --- 1. real signal source, no company needed
t0 = time.time()
sigs, sources = sigmod.sweep(["what would you pay for", "I wish there was an app that"], per_query=8)
ok = [k for k, v in sources.items() if v.get("ok")]
print(f"\n[signals] {len(sigs)} signals in {int((time.time()-t0)*1000)}ms; sources ok={len(ok)}/{len(sources)}")
for k, v in list(sources.items())[:3]:
    print("   ", k, v.get("ok"), v.get("status"), v.get("n"))
if sigs:
    s = sigs[0]
    print("   sample:", s["id"], "|", s["title"][:70], "|", s["url"])

# --- 2. incorporate
c = runtime.incorporate(
    "TinyFish Labs",
    "Reach $1,000 MRR by shipping small software products people pay for.",
    capital_cents=50000, max_experiment_cents=10000, board_threshold_cents=20000,
    mode="moderate", model="deepseek-chat",
)
print("\n[incorporate]", c["id"], c["name"], "cash:", runtime.cash(c["id"]))

# --- 3. policy engine unit checks (the constitution actually bites)
tests = [
("CEO", "allocate_budget", 5000, "ALLOW"),
("CEO", "allocate_budget", 15000, "APPROVAL"),
("CEO", "allocate_budget", 30000, "APPROVAL"),
("CEO", "allocate_budget", 99999, "DENY"),
("CEO", "borrow_money", 100, "DENY"),
("CEO", "bank_transfer", 100, "DENY"),
("CFO", "allocate_budget", 100, "DENY"),
("CFO", "alter_revenue_numbers", 0, "DENY"),
("CMO", "mass_unsolicited_email", 0, "DENY"),
("CMO", "send_email", 0, "APPROVAL"),
("CMO", "spend_ads", 4000, "ALLOW"),
("CTO", "spend_cloud", 9000, "APPROVAL"),
("CTO", "spend_cloud", 25000, "DENY"),
("COO", "run_experiment", 4000, "ALLOW"),
("BOARD", "allocate_budget", 40000, "ALLOW"),
]
print("\n[policy] role/tool/amount -> verdict")
bad = 0
for role, tool, amt, want in tests:
    v, why, _ = policy.authorize(c["id"], role, tool, amt)
    flag = "ok " if v == want else "BAD"
    if v != want:
        bad += 1
    print(f"  {flag} {role:4} {tool:24} ${amt/100:>7.2f} -> {v:8} {why[:70]}")
print(f"  {bad} unexpected verdict(s)")
print("  cash after all that:", runtime.cash(c["id"]), "(no money should have moved)")

# --- 4. one tick, simulated mode
t0 = time.time()
res = runtime.tick(c["id"])
print(f"\n[tick] {int((time.time()-t0)*1000)}ms")
for step in res["log"]:
    print("   ", step.get("phase"), "->", str(step.get("detail"))[:160])
s = res["state"]
print(f"\n[state] cash=${s['cash_cents']/100:.2f} mrr=${s['mrr_cents']/100:.2f} "
      f"projects={len(s['projects'])} pending_requests={len(s['board_requests'])}")
for p in s["projects"]:
    print("   project", p["slug"], p["stage"], "cfo:", (p["cfo"] or {}).get("verdict"),
          "coo_cost:", (p["coo"] or {}).get("cost_cents"))
for r in s["board_requests"]:
    print("   BOARD REQUEST:", r["title"], r["amount_cents"])
print("   decisions:", len(s["decisions"]), "actions:", len(s["actions"]), "events:", len(s["events"]))
print("   chain valid:", ev.verify(c["id"]))
