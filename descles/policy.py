"""The company constitution, enforced. Every actuator call goes through authorize().

The model may *request* anything. Only this module decides what happens.
"""

import json

from . import db, events as ev, roles
from .roles import ALLOW, APPROVAL, DENY


def _today_spend(company_id, actor, tool):
    r = db.row(
        "SELECT COALESCE(SUM(ABS(amount_cents)),0) s FROM ledger WHERE company_id=? AND actor=? "
        "AND memo LIKE ? AND substr(ts,1,10)=substr(?,1,10)",
        (company_id, actor, tool + "%", ev.now()),
    )
    return int(r["s"]) if r else 0


def cash_cents(company_id):
    r = db.row("SELECT COALESCE(SUM(amount_cents),0) s FROM ledger WHERE company_id=?", (company_id,))
    return int(r["s"]) if r else 0


def committed_cents(company_id):
    """Cash already handed to projects that has not been consumed yet."""
    r = db.row(
        "SELECT COALESCE(SUM(amount_cents),0) s FROM ledger WHERE company_id=? AND kind='ALLOCATION' "
        "AND project_id IS NOT NULL",
        (company_id,),
    )
    return int(r["s"]) if r else 0


def authorize(company_id, actor_role, tool, amount_cents=0, ctx=None):
    """Request one action. Returns (verdict, reason, amount_cents).

    Verdicts: ALLOW / DENY / APPROVAL.  Never raises for a denial — a denial is
    a record, and the caller must handle it as such.
    """
    ctx = ctx or {}
    company = db.row("SELECT * FROM companies WHERE id=?", (company_id,))
    if not company:
        return DENY, "no such company", 0
    charter = json.loads(company["charter"])
    spec = roles.spec(actor_role)
    if not spec and actor_role not in ("BOARD", "SYSTEM"):
        return DENY, f"unknown role {actor_role}", 0

    # 1. charter clauses are absolute — they beat every role permit
    for clause in charter.get("clauses", []):
        if tool in roles.CLAUSE_FORBIDS.get(clause, []):
            return DENY, f"charter clause {clause} forbids {tool}", 0

    # board and system are outside the corporate authority structure
    if actor_role in ("BOARD", "SYSTEM"):
        return ALLOW, "board authority", amount_cents

    permit = spec["permits"].get(tool)
    if permit is None:
        return DENY, f"{actor_role} has no permit for {tool}", 0
    if permit == roles.DENY:
        return DENY, f"{actor_role} is forbidden from {tool}", 0
    if permit == roles.APPROVAL:
        return APPROVAL, f"{actor_role} requires board approval for {tool}", amount_cents

    if isinstance(permit, dict):
        limit = int(permit.get("limit_cents", 0))
        # the charter's per-experiment cap is a ceiling on what anyone may do
        # unilaterally — it does not stop the board from authorising more.
        limit = min(limit, int(charter.get("max_experiment_spend_cents", limit)))
        hard = permit.get("hard_cents")
        daily = permit.get("daily_cents")
        if hard and amount_cents > int(hard):
            return DENY, (
                f"{tool} amount ${amount_cents/100:.2f} is above {actor_role}'s absolute ceiling "
                f"${int(hard)/100:.2f} — the board must amend the charter to change this"
            ), amount_cents
        # cash is the real wall: nobody spends money the company does not have
        if cash_cents(company_id) - amount_cents < 0:
            return DENY, (
                f"insufficient cash (${cash_cents(company_id)/100:.2f} available, "
                f"${amount_cents/100:.2f} requested)"
            ), amount_cents
        if daily and _today_spend(company_id, spec["title"], tool) + amount_cents > daily:
            return DENY, f"{tool} would exceed the ${daily/100:.2f}/day cap", amount_cents
        if amount_cents > limit:
            return APPROVAL, (
                f"${amount_cents/100:.2f} exceeds {actor_role} unilateral limit "
                f"${limit/100:.2f} -> board approval required"
            ), amount_cents
        return ALLOW, f"within {actor_role} permit for {tool}", amount_cents

    return ALLOW, f"{actor_role} permitted {tool}", amount_cents


def record(company_id, actor, tool, args, verdict, reason, event_id=None):
    return db.ex(
        "INSERT INTO actions(company_id,ts,actor,tool,args,verdict,reason,event_id) VALUES(?,?,?,?,?,?,?,?)",
        (company_id, ev.now(), actor, tool, json.dumps(args, ensure_ascii=False), verdict, reason, event_id),
    )


def requires_board(company_id, amount_cents, tool):
    company = db.row("SELECT charter FROM companies WHERE id=?", (company_id,))
    charter = json.loads(company["charter"])
    thresh = int(charter.get("board_approval_threshold_cents", 20000))
    maxexp = int(charter.get("max_experiment_spend_cents", 10000))
    return amount_cents > thresh or (tool in ("allocate_budget", "spend_ads") and amount_cents > maxexp)
