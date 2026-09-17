#!/usr/bin/env python
"""descles — the corporate runtime CLI.

  python cli.py company create --name "TinyFish Labs" --capital 500
  python cli.py company list
  python cli.py status <company>
  python cli.py tick <company>
  python cli.py board <company>            # show pending board requests
  python cli.py approve <company> <req_id> [--note ...]
  python cli.py reject  <company> <req_id> [--note ...]
  python cli.py verify  <company>
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from descles import config, runtime, events as ev  # noqa: E402

B = "\033[1m"
D = "\033[2m"
G = "\033[32m"
Y = "\033[33m"
R = "\033[31m"
C = "\033[36m"
X = "\033[0m"


def money(c):
    return f"${(c or 0)/100:,.2f}"


def find(prefix):
    c = runtime.get_by_slug_prefix(prefix)
    if not c:
        print(f"{R}no company matching {prefix!r}{X}")
        sys.exit(1)
    return c


def cmds_create(a):
    c = runtime.incorporate(
        a.name, a.mission, capital_cents=int(a.capital * 100),
        max_experiment_cents=int(a.max_experiment * 100),
        board_threshold_cents=int(a.board_threshold * 100),
        template=a.template, mode=a.mode, model=a.model,
    )
    print(f"{G}✓{X} Company incorporated.\n")
    print(f"  {B}{c['name']}{X}  {D}{c['id']}{X}")
    print(f"  mission        {c['mission']}")
    print(f"  board ownership 100%      capital {money(int(a.capital*100))}")
    for ag in ("CEO", "CTO", "CFO", "CMO", "COO"):
        print(f"  management     {ag:4} spawned  {D}{a.model}{X}")
    print(f"\n  open  {C}http://127.0.0.1:{config.PORT}/#/c/{c['id']}{X}")


def cmds_list(a):
    for c in runtime.companies():
        s = runtime.state(c["id"])
        print(f"{B}{c['name']:22}{X} {money(s['cash_cents']):>10}  mrr {money(s['mrr_cents']):>8}  "
              f"live {len(s['portfolio']['active'])}  killed {len(s['portfolio']['killed'])}  {D}{c['id']}{X}")


def cmds_status(a):
    c = find(a.company)
    s = runtime.state(c["id"])
    print(f"\n{B}{c['name']}{X}  {D}{c['id']}{X}")
    print("─" * 62)
    print(f"  Cash          {money(s['cash_cents'])}")
    print(f"  MRR           {money(s['mrr_cents'])}")
    print(f"  Deployed      {money(s['total_spend_cents'])}")
    print(f"  Experiments   {len(s['projects'])}")
    print(f"  Board requests {len(s['board_requests'])}")
    for role in ("CEO", "CTO", "CFO", "CMO", "COO"):
        ag = [x for x in s["agents"] if x["role"] == role]
        if ag:
            print(f"  {role:4}          {G}ACTIVE{X}  {D}{ag[0]['model']}{X}")
    if s["projects"]:
        print("\n  PORTFOLIO")
        for p in s["projects"]:
            tag = {"KILLED": R, "SCALING": G, "LIVE": G, "FUNDED": G}.get(p["stage"], Y)
            print(f"    {p['slug']:26} {tag}{p['stage']:14}{X} spent {money(p['spent_cents']):>9}  "
                  f"cfo {(p['cfo'] or {}).get('verdict','—')}")
    for r in s["board_requests"]:
        print(f"\n  {Y}BOARD REQUEST{X} {D}{r['id']}{X}")
        print(f"    {r['title']}   {B}{money(r['amount_cents'])}{X}")
    print()


def cmds_tick(a):
    c = find(a.company)
    print(f"{D}running operating cycle for {c['name']}…{X}")
    r = runtime.tick(c["id"])
    for step in r["log"]:
        if step.get("phase") == "sweep":
            continue
        det = step.get("detail")
        if isinstance(det, dict):
            det = ", ".join(f"{k}={json.dumps(v)[:60]}" for k, v in list(det.items())[:4])
        print(f"  {step['phase']:18} {str(det)[:150]}")
    cmds_status(a)


def cmds_board(a):
    c = find(a.company)
    s = runtime.state(c["id"])
    if not s["board_requests"]:
        print("no pending board requests")
        return
    for r in s["board_requests"]:
        ar = r["payload"].get("args", {})
        print(f"\n{B}{r['title']}{X}   {B}{money(r['amount_cents'])}{X}")
        print(f"  id            {r['id']}")
        print(f"  filed         {r['ts']} by {r['payload'].get('role')}")
        if ar.get("success_criterion"):
            print(f"  success       {ar['success_criterion']}")
        if ar.get("kill_criterion"):
            print(f"  kill          {ar['kill_criterion']}")
        if ar.get("risk_ack"):
            print(f"  risk          {ar['risk_ack']}")
        cfo = ar.get("cfo") or {}
        if cfo:
            print(f"  cfo review    {cfo.get('verdict')} worst case {money(cfo.get('worst_case_loss_cents'))}")
        for e in (ar.get("evidence") or [])[:3]:
            print(f"  evidence      {e.get('signal_id')} {e.get('url')}")
        plan = (ar.get("coo") or {}).get("validation_plan") or []
        for x in plan:
            print(f"                · {x}")


def _decide(a, approve):
    c = find(a.company)
    out = runtime.board_decide(c["id"], a.request_id, approve, a.note or "")
    print(json.dumps(out, ensure_ascii=False, indent=1))


def cmds_verify(a):
    c = find(a.company)
    print(json.dumps(ev.verify(c["id"]), indent=1))


def main():
    p = argparse.ArgumentParser(prog="descles")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("company", help="company commands")
    cs = c.add_subparsers(dest="sub", required=True)
    cc = cs.add_parser("create")
    cc.add_argument("--name", default="TinyFish Labs")
    cc.add_argument("--mission", default="Reach $1,000 MRR with tiny software products.")
    cc.add_argument("--capital", type=float, default=500)
    cc.add_argument("--max-experiment", type=float, default=100)
    cc.add_argument("--board-threshold", type=float, default=200)
    cc.add_argument("--template", default="micro_saas")
    cc.add_argument("--mode", default="moderate")
    cc.add_argument("--model", default="deepseek-chat")
    cc.set_defaults(fn=cmds_create)
    cl = cs.add_parser("list")
    cl.set_defaults(fn=cmds_list)

    for name, fn in (("status", cmds_status), ("tick", cmds_tick), ("board", cmds_board), ("verify", cmds_verify)):
        s = sub.add_parser(name)
        s.add_argument("company")
        s.set_defaults(fn=fn)
    ap = sub.add_parser("approve")
    ap.add_argument("company")
    ap.add_argument("request_id")
    ap.add_argument("--note", default="")
    ap.set_defaults(fn=lambda a: _decide(a, True))
    rj = sub.add_parser("reject")
    rj.add_argument("company")
    rj.add_argument("request_id")
    rj.add_argument("--note", default="")
    rj.set_defaults(fn=lambda a: _decide(a, False))

    a = p.parse_args()
    config.load_keys()
    a.fn(a)


if __name__ == "__main__":
    main()
