"""Corporate runtime: the company as a durable state machine.

Everything an agent does becomes an event. Money only moves through
act() -> policy.authorize() -> ledger. There is no other path.
"""

import json
import re
import time
import uuid

from . import agents, billing, build as buildmod, capabilities as caps, config, db, events as ev, llm
from . import metrics as metrics_mod
from . import policy, roles, signals as sigmod

TEMPLATES = {
    "micro_saas": {
        "label": "Micro SaaS Studio",
        "loop": ["Discover", "Validate", "Board approval", "Build", "Deploy", "Acquire", "Measure", "Scale/Kill"],
        "default_mission": "Reach $1,000 MRR by shipping small software products people pay for.",
        "seed_queries": [
            "what would you pay for",
            "I wish there was an app that",
            "recommend a tool for",
            "shut up and take my money",
            "is there a simple tool that",
        ],
    },
    "leadgen": {
        "label": "Lead Generation Agency",
        "loop": ["Find prospects", "Research", "Outreach", "Qualify", "Deliver", "Invoice"],
        "default_mission": "Sell qualified B2B leads to small exporters.",
        "seed_queries": ["looking for a manufacturer", "supplier recommendation"],
    },
    "content": {
        "label": "Content Business",
        "loop": ["Research", "Create", "Distribute", "Monetize"],
        "default_mission": "Grow an audience that pays.",
        "seed_queries": ["how do you learn", "best explanation of"],
    },
}

STATUS = "ACTIVE"


def _id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _ms(t0):
    return int((time.time() - t0) * 1000)


def base_url():
    import os
    return os.environ.get("DESCLES_BASE_URL") or f"http://127.0.0.1:{config.PORT}"


def run_log(company_id, phase, ok, detail, ms):
    db.ex(
        "INSERT INTO runs(company_id,ts,phase,ok,detail,ms) VALUES(?,?,?,?,?,?)",
        (company_id, ev.now(), phase, 1 if ok else 0, str(detail)[:400], ms),
    )


# ------------------------------------------------------------------ money


def money(company_id, kind, amount_cents, actor, memo, project_id=None, event_id=None):
    db.ex(
        "INSERT INTO ledger(company_id,ts,kind,amount_cents,project_id,actor,memo,event_id) VALUES(?,?,?,?,?,?,?,?)",
        (company_id, ev.now(), kind, int(amount_cents), project_id, actor, memo[:300], event_id),
    )


def cash(company_id):
    return policy.cash_cents(company_id)


# ------------------------------------------------------------------ lifecycle


def incorporate(name, mission, capital_cents=50000, max_experiment_cents=10000,
                board_threshold_cents=20000, template="micro_saas", mode="moderate",
                model="deepseek-chat", seed_queries=None, clause_extra=None):
    db.init()
    cid = _id("co")
    charter = roles.default_charter(capital_cents, max_experiment_cents, board_threshold_cents)
    charter["mode"] = mode
    if clause_extra:
        charter["clauses"].extend(clause_extra)
    if seed_queries:
        charter["seed_queries"] = seed_queries
    db.ex(
        "INSERT INTO companies(id,name,mission,template,mode,charter,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (cid, name, mission, template, mode, json.dumps(charter, ensure_ascii=False), STATUS, ev.now()),
    )
    e = ev.append(
        cid,
        "BOARD",
        "COMPANY_INCORPORATED",
        {"name": name, "mission": mission, "charter": charter, "template": template, "board_ownership": "100%"},
    )
    money(cid, "CAPITAL", capital_cents, "BOARD", "initial capital", event_id=e["id"])
    ev.append(cid, "BOARD", "CAPITAL_ALLOCATED", {"amount_cents": capital_cents, "to": "company", "from": "shareholder"})

    for role in ("CEO", "CTO", "CFO", "CMO", "COO"):
        db.ex(
            "INSERT INTO agents(id,company_id,role,model,status,spec,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                _id("ag"),
                cid,
                role,
                model,
                "ACTIVE",
                json.dumps(roles.spec(role), ensure_ascii=False),
                ev.now(),
            ),
        )
        ev.append(cid, "SYSTEM", "AGENT_SPAWNED", {"role": role, "model": model})

    for cap, st in [(name, "CONNECTED") for name in ("web_search", "browser", "local_filesystem")]:
        db.ex(
            "INSERT OR REPLACE INTO capabilities(company_id,name,status,detail) VALUES(?,?,?,?)",
            (cid, cap, st, ""),
        )
    caps.sync(cid)
    return get(cid)


def companies():
    return db.rows("SELECT id,name,mission,template,mode,status,created_at FROM companies ORDER BY created_at DESC")


def get(company_id):
    r = db.row("SELECT * FROM companies WHERE id=?", (company_id,))
    if not r:
        return None
    r["charter"] = json.loads(r["charter"])
    return r


def get_by_slug_prefix(prefix):
    for c in companies():
        if c["id"] == prefix or c["id"].startswith(prefix):
            return get(c["id"])
    return None


def project_by_slug(company_id, slug):
    return db.row("SELECT * FROM projects WHERE company_id=? AND slug=?", (company_id, slug))


def evaluations(project_id):
    out = {}
    for r in db.rows("SELECT * FROM evaluations WHERE project_id=? ORDER BY id ASC", (project_id,)):
        try:
            out[r["role"]] = {**json.loads(r["payload"]), "_simulated": bool(r["simulated"])}
        except Exception:  # noqa: BLE001
            pass
    return out


def state(company_id):
    """The projection handed to agents and to the console."""
    c = get(company_id)
    if not c:
        return None
    projs = []
    for p in db.rows("SELECT * FROM projects WHERE company_id=? ORDER BY created_at ASC", (company_id,)):
        evals = evaluations(p["id"])
        sp = db.row(
            "SELECT COALESCE(SUM(-amount_cents),0) s FROM ledger WHERE project_id=? AND amount_cents<0",
            (p["id"],),
        )
        rv = db.row(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM ledger WHERE project_id=? AND kind='REVENUE'",
            (p["id"],),
        )
        p["spent_cents"] = int(sp["s"]) if sp else 0
        p["revenue_cents"] = int(rv["s"]) if rv else 0
        p["evidence"] = json.loads(p["evidence"] or "[]")
        p["signals"] = json.loads(p["signals"] or "[]")
        p["cto"] = evals.get("CTO", {})
        p["cfo"] = evals.get("CFO", {})
        p["coo"] = evals.get("COO", {})
        p["evaluations"] = evals
        projs.append(p)
    charter = c["charter"]
    pend = db.rows(
        "SELECT * FROM board_requests WHERE company_id=? AND status='PENDING' ORDER BY ts ASC", (company_id,)
    )
    for r in pend:
        r["payload"] = json.loads(r["payload"] or "{}")
    rev = db.row("SELECT COALESCE(SUM(amount_cents),0) s FROM ledger WHERE company_id=? AND kind='REVENUE'", (company_id,))
    spend = db.row(
        "SELECT COALESCE(SUM(-amount_cents),0) s FROM ledger WHERE company_id=? AND amount_cents<0 AND kind!='CAPITAL'",
        (company_id,),
    )
    dec = db.rows("SELECT * FROM decisions WHERE company_id=? ORDER BY id DESC LIMIT 20", (company_id,))
    for d in dec:
        d["payload"] = json.loads(d["payload"] or "{}")
    return {
        "company": {k: v for k, v in c.items() if k != "charter"},
        "charter": charter,
        "template": TEMPLATES.get(c["template"], {}),
        "cash_cents": cash(company_id),
        "mrr_cents": int(rev["s"]) if rev else 0,
        "total_spend_cents": int(spend["s"]) if spend else 0,
        "projects": projs,
        "portfolio": {
            "active": [p["slug"] for p in projs if p["stage"] in
                       ("DISCOVERED", "VALIDATED", "AWAITING_BOARD", "FUNDED", "BUILT_LOCAL",
                        "BUILDING", "LIVE", "SCALING")],
            "killed": [p["slug"] for p in projs if p["stage"] == "KILLED"],
            "shelved": [p["slug"] for p in projs if p["stage"] == "SHELVED"],
        },
        "board_requests": pend,
        "decisions": dec,
        "agents": db.rows("SELECT role,model,status FROM agents WHERE company_id=? ORDER BY rowid", (company_id,)),
        "capabilities": [
            {**r, "human_kind": (caps.CAPABILITIES.get(r["name"]) or {}).get("human_kind"),
             "label": (caps.CAPABILITIES.get(r["name"]) or {}).get("label", r["name"])}
            for r in db.rows("SELECT name,status,detail FROM capabilities WHERE company_id=? ORDER BY rowid", (company_id,))
        ],
        "setup_requests": _setup_rows(company_id),
        "builds": db.rows("SELECT * FROM builds WHERE company_id=? ORDER BY ts DESC", (company_id,)),
        "funnels": {p["slug"]: metrics_mod.funnel(company_id, p["id"]) for p in projs},
        "webhooks": db.rows(
            "SELECT provider,event_id,ts,type FROM webhook_events WHERE 1=1 ORDER BY ts DESC LIMIT 20"
        ),
        "events": ev.chain(company_id, 60),
        "actions": db.rows("SELECT * FROM actions WHERE company_id=? ORDER BY id DESC LIMIT 60", (company_id,)),
        "runs": db.rows("SELECT * FROM runs WHERE company_id=? ORDER BY id DESC LIMIT 40", (company_id,)),
        "ledger": db.rows("SELECT * FROM ledger WHERE company_id=? ORDER BY id DESC LIMIT 60", (company_id,)),
        "authority": {
            "ceo_unilateral_limit_cents": min(
                roles.spec("CEO")["permits"]["allocate_budget"]["limit_cents"],
                charter["max_experiment_spend_cents"],
            ),
            "board_approval_threshold_cents": charter["board_approval_threshold_cents"],
            "max_experiment_spend_cents": charter["max_experiment_spend_cents"],
        },
    }


def ceo_state(company_id):
    """Compact state for the CEO prompt — money and evidence, no chatter."""
    s = state(company_id)
    return {
        "company": s["company"]["name"],
        "mission": s["company"]["mission"],
        "charter": s["charter"],
        "cash_cents": s["cash_cents"],
        "mrr_cents": s["mrr_cents"],
        "authority": s["authority"],
        "projects": [
            {
                "slug": p["slug"],
                "name": p["name"],
                "stage": p["stage"],
                "spent_cents": p["spent_cents"],
                "hypothesis": p["hypothesis"],
                "signal_ids": [x.get("id") for x in p["signals"]][:3],
                "cto": {k: p["cto"].get(k) for k in ("mvp_hours", "build_cost_cents", "build_risk")} if p["cto"] else {},
                "cfo": {k: p["cfo"].get(k) for k in ("verdict", "veto_reason", "max_test_spend_cents", "worst_case_loss_cents")} if p["cfo"] else {},
                "coo": {k: p["coo"].get(k) for k in ("cost_cents", "success_criterion", "kill_criterion")} if p["coo"] else {},
            }
            for p in s["projects"]
            if p["stage"] not in ("KILLED", "SHELVED")
        ],
        "recent_decisions": [{"summary": d["summary"], "authority": d["authority"]} for d in s["decisions"][:5]],
    }


def _model(company_id, role):
    r = db.row("SELECT model FROM agents WHERE company_id=? AND role=?", (company_id, role))
    return r["model"] if r else "deepseek-chat"


def _save_eval(company_id, project_id, role, payload):
    db.ex(
        "INSERT INTO evaluations(company_id,project_id,role,ts,payload,simulated) VALUES(?,?,?,?,?,?)",
        (company_id, project_id, role, ev.now(), json.dumps(payload, ensure_ascii=False),
         1 if payload.get("_simulated") else 0),
    )


def _set_stage(company_id, project_id, stage, **cols):
    sets = ["stage=?", "updated_at=?"]
    args = [stage, ev.now()]
    for k, v in cols.items():
        sets.append(f"{k}=?")
        args.append(v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False))
    args.append(project_id)
    db.ex(f"UPDATE projects SET {','.join(sets)} WHERE id=?", args)


# ------------------------------------------------------------------ the only actuator


def act(company_id, role, tool, args, amount_cents=0, memo=""):
    """Every effect goes through here. Returns the policy verdict."""
    verdict, reason, amt = policy.authorize(company_id, role, tool, amount_cents, args)
    rec = policy.record(company_id, role, tool, args, verdict, reason)
    out = {"verdict": verdict, "reason": reason, "amount_cents": amt, "action_id": rec.lastrowid}

    if verdict == "APPROVAL":
        # a request, not a spend. no money moves.
        rid = _id("req")
        db.ex(
            "INSERT INTO board_requests(id,company_id,ts,kind,title,amount_cents,payload,status) VALUES(?,?,?,?,?,?,?,?)",
            (
                rid, company_id, ev.now(), tool,
                args.get("title") or f"{role} requests {tool}",
                int(amount_cents), json.dumps({"role": role, "tool": tool, "args": args}, ensure_ascii=False),
                "PENDING",
            ),
        )
        ev.append(company_id, role, "BOARD_REQUEST_FILED", {"request_id": rid, "tool": tool, "amount_cents": int(amount_cents), "reason": reason})
        out["board_request_id"] = rid
    elif verdict == "ALLOW" and tool in ("allocate_budget", "spend_ads", "spend_cloud", "spend_ops", "run_experiment"):
        if amount_cents:
            e = ev.append(company_id, role, "CAPITAL_RELEASED", {
                "tool": tool, "amount_cents": int(amount_cents),
                "project_slug": args.get("project_slug"), "memo": memo})
            money(company_id, "SPEND", -int(amount_cents), roles.spec(role)["title"] if roles.spec(role) else role,
                  memo or tool, project_id=args.get("project_id"), event_id=e["id"])
            out["event_id"] = e["id"]
    elif verdict == "DENY":
        ev.append(company_id, role, "ACTION_DENIED", {"tool": tool, "amount_cents": int(amount_cents), "reason": reason})
    return out


def file_board_decision(company_id, actor, kind, summary, rationale, payload, authority):
    cur = db.ex(
        "INSERT INTO decisions(company_id,ts,actor,kind,summary,rationale,payload,authority) VALUES(?,?,?,?,?,?,?,?)",
        (company_id, ev.now(), actor, kind, summary[:300], rationale[:1200],
         json.dumps(payload, ensure_ascii=False), authority),
    )
    ev.append(company_id, actor, "CEO_DECISION", {"summary": summary, "kind": kind, "authority": authority, "payload": payload})
    return cur.lastrowid


# ------------------------------------------------------------------ the operating loop


def _queries(company_id, extra=None):
    c = get(company_id)
    tpl = TEMPLATES.get(c["template"], {})
    qs = list(c["charter"].get("seed_queries") or tpl.get("seed_queries") or ["what would you pay for"])
    if extra:
        qs = list(extra) + qs
    return qs[:6]


def sweep(company_id, queries=None):
    t0 = time.time()
    qs = _queries(company_id, queries)
    sigs, sources = sigmod.sweep(qs, per_query=15)
    ok = any(v.get("ok") for v in sources.values())
    ev.append(company_id, "CMO", "SIGNALS_SWEPT", {
        "queries": qs, "n_signals": len(sigs),
        "sources": {k: {"ok": v.get("ok"), "status": v.get("status"), "n": v.get("n")} for k, v in sources.items()},
    })
    run_log(company_id, "sweep", ok, f"{len(sigs)} signals from {sum(1 for v in sources.values() if v.get('ok'))} ok sources", _ms(t0))
    return sigs, sources


def discover(company_id, signals=None, queries=None):
    t0 = time.time()
    c = get(company_id)
    if signals is None:
        signals, _ = sweep(company_id, queries)
    killed = [p["slug"] for p in db.rows("SELECT slug FROM projects WHERE company_id=? AND stage='KILLED'", (company_id,))]
    existing = [p["slug"] for p in db.rows("SELECT slug FROM projects WHERE company_id=? AND stage NOT IN ('KILLED')", (company_id,))]
    out = agents.cmo_discover(c, _model(company_id, "CMO"), signals, killed, existing, _queries(company_id, queries))
    by_id = {s["id"]: s for s in signals}
    created = []
    for opp in out.get("opportunities", [])[:3]:
        slug = agents.slugify(opp.get("name", ""))
        if slug in existing or slug in killed or project_by_slug(company_id, slug):
            slug = slug + "-" + uuid.uuid4().hex[:3]
        cited = [i for i in (opp.get("signal_ids") or []) if i in by_id]
        evidence = [
            {"signal_id": i, "url": by_id[i]["url"], "source": by_id[i]["source"],
             "quote": by_id[i]["text"][:300], "title": by_id[i]["title"]}
            for i in cited
        ]
        uncited = [i for i in (opp.get("signal_ids") or []) if i not in by_id]
        if uncited:
            evidence.append({"signal_id": None, "url": None, "source": "UNVERIFIED",
                             "quote": f"model cited signal ids that do not exist: {uncited}"})
        pid = _id("pr")
        db.ex(
            "INSERT INTO projects(id,company_id,slug,name,stage,hypothesis,evidence,signals,success_criterion,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pid, company_id, slug, opp.get("name", slug)[:60], "DISCOVERED",
             (opp.get("one_liner") or "") + " | " + (opp.get("problem") or "")[:400],
             json.dumps(evidence, ensure_ascii=False),
             json.dumps([by_id[i] for i in cited], ensure_ascii=False),
             None, ev.now(), ev.now()),
        )
        ev.append(company_id, "CMO", "OPPORTUNITY_DISCOVERED", {
            "slug": slug, "name": opp.get("name"), "one_liner": opp.get("one_liner"),
            "target_user": opp.get("target_user"), "signal_ids": cited,
            "price_guess_cents": opp.get("price_guess_cents"), "why_now": opp.get("why_now"),
            "uncited_signal_ids": uncited,
        })
        created.append({"project_id": pid, "slug": slug, "opp": opp})
    run_log(company_id, "discover", bool(created), f"{len(created)} opportunities"
            + (" (SIMULATED)" if out.get("_simulated") else ""), _ms(t0))
    return created, out


def _setup_rows(company_id):
    out = []
    for r in db.rows(
        "SELECT * FROM setup_requests WHERE company_id=? ORDER BY (status='PENDING') DESC, ts DESC",
        (company_id,),
    ):
        import json as _j
        try:
            r["steps"] = _j.loads(r["steps"] or "[]")
        except Exception:  # noqa: BLE001
            r["steps"] = []
        out.append(r)
    return out


def build_project(company_id, project_id):
    """FUNDED -> a real artifact on disk. No capability needed, so this is never
    blocked: the only human input the build itself needs is none."""
    t0 = time.time()
    c = get(company_id)
    p = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p:
        return {"ok": False, "error": "no such project"}
    evl = evaluations(p["id"])
    opp = {
        "name": p["name"],
        "one_liner": p["hypothesis"],
        "problem": p["hypothesis"],
        "signal_ids": [s.get("id") for s in json.loads(p["signals"] or "[]")],
        "monetization": "paid",
    }
    copy = agents.cto_landing(c, _model(company_id, "CTO"), opp,
                              evl.get("CFO", {}), evl.get("CTO", {}), evl.get("COO", {}))
    out = buildmod.build(c, p, copy, base_url())
    run_log(company_id, "build", True, f"{p['slug']} -> {out['path']}"
            + ("" if out["checkout"] else " (payment rail NOT connected)"), _ms(t0))
    return {"ok": True, **out, "slug": p["slug"]}


def deploy_project(company_id, project_id):
    t0 = time.time()
    c = get(company_id)
    p = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
    b = db.row("SELECT * FROM builds WHERE project_id=? ORDER BY ts DESC LIMIT 1", (project_id,))
    if not b:
        return {"ok": False, "error": "nothing built yet"}
    out = buildmod.deploy(c, p, b, base_url())
    run_log(company_id, "deploy", bool(out.get("ok")), json.dumps(out)[:200], _ms(t0))
    return out


def record_page_view(company_id, slug):
    p = project_by_slug(company_id, slug)
    if not p:
        return {"ok": False, "error": "no such project"}
    metrics_mod.record(company_id, "page_view", 1, project_id=p["id"], meta={"slug": slug})
    return {"ok": True, "funnel": metrics_mod.funnel(company_id, p["id"])}


def record_pricing_intent(company_id, slug):
    """The COO's success criterion is a pricing-intent rate. This is the event that
    measures it — a click on a real CTA, not a model's estimate."""
    p = project_by_slug(company_id, slug)
    if not p:
        return {"ok": False, "error": "no such project"}
    metrics_mod.record(company_id, "pricing_intent", 1, project_id=p["id"], meta={"slug": slug})
    ev.append(company_id, "VISITOR", "PRICING_INTENT", {"slug": slug})
    f = metrics_mod.funnel(company_id, p["id"])
    crit = p["success_criterion"] or ""
    m = re.search(r"([\d.]+)\s*%", crit)
    if m and f["intent_rate_pct"] is not None:
        target = float(m.group(1))
        f["target_pct"] = target
        f["verdict"] = "ON TRACK" if f["intent_rate_pct"] >= target else "BELOW CRITERION"
    return {"ok": True, "funnel": f}


def evaluate(company_id, project_id):
    """CTO cost -> CFO unit economics -> COO validation plan. Each is an evaluation row."""
    t0 = time.time()
    p = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
    opp = {
        "name": p["name"],
        "one_liner": p["hypothesis"],
        "signal_ids": [s.get("id") for s in json.loads(p["signals"] or "[]")],
        "evidence_quote": (json.loads(p["evidence"] or "[]") or [{}])[0].get("quote", ""),
        "monetization": "subscription",
    }
    cto = agents.cto_estimate(get(company_id), _model(company_id, "CTO"), opp)
    _save_eval(company_id, project_id, "CTO", cto)
    ev.append(company_id, "CTO", "MVP_ESTIMATED", {"slug": p["slug"], **{k: cto.get(k) for k in
             ("mvp_hours", "build_cost_cents", "time_to_launch_days", "build_risk")}})

    cfo = agents.cfo_economics(get(company_id), _model(company_id, "CFO"), opp, cto, cash(company_id))
    _save_eval(company_id, project_id, "CFO", cfo)
    ev.append(company_id, "CFO", "UNIT_ECONOMICS", {"slug": p["slug"], **{k: cfo.get(k) for k in
             ("verdict", "veto_reason", "price_cents", "expected_cac_cents", "max_test_spend_cents", "worst_case_loss_cents")}})

    if str(cfo.get("verdict", "")).upper() != "PASS":
        _set_stage(company_id, project_id, "KILLED", killed_reason=f"CFO veto: {cfo.get('veto_reason') or cfo.get('reason')}")
        ev.append(company_id, "CFO", "PROJECT_KILLED", {"slug": p["slug"], "reason": cfo.get("veto_reason") or cfo.get("reason")})
        run_log(company_id, "evaluate", True, f"{p['slug']}: CFO veto", _ms(t0))
        return {"stage": "KILLED", "cfo": cfo}

    coo = agents.coo_validation_plan(get(company_id), _model(company_id, "COO"), opp, cfo, cto)
    _save_eval(company_id, project_id, "COO", coo)
    ev.append(company_id, "COO", "VALIDATION_PLANNED", {"slug": p["slug"], **{k: coo.get(k) for k in
             ("cost_cents", "success_criterion", "kill_criterion", "duration_days")}})
    _set_stage(company_id, project_id, "VALIDATED", success_criterion=coo.get("success_criterion"),
               kill_criterion=coo.get("kill_criterion"))
    run_log(company_id, "evaluate", True, f"{p['slug']}: validated, planned cost {coo.get('cost_cents')}c", _ms(t0))
    return {"stage": "VALIDATED", "cto": cto, "cfo": cfo, "coo": coo}


def decide(company_id):
    """CEO picks a move; the policy engine decides whether it is a spend or a request."""
    t0 = time.time()
    st = ceo_state(company_id)
    d = agents.ceo_decide(get(company_id), _model(company_id, "CEO"), st)
    action = (d.get("action") or "idle").lower()
    slug = d.get("project_slug")
    amount = int(d.get("amount_cents") or 0)
    p = project_by_slug(company_id, slug) if slug else None
    if p and action in ("allocate_capital", "advance_project"):
        # nobody funds a hypothesis that has not passed the CTO/CFO/COO gate
        if not evaluations(p["id"]).get("CFO"):
            ev.append(company_id, "SYSTEM", "EVALUATION_FORCED", {"slug": slug, "why": "CEO tried to fund unevaluated project"})
            evaluate(company_id, p["id"])
            p = db.row("SELECT * FROM projects WHERE id=?", (p["id"],))
            if p["stage"] == "KILLED":
                action = "idle"
    simulated = bool(d.get("_simulated"))
    outcome = {"action": action, "amount_cents": amount, "simulated": simulated}

    if action == "kill_project" and p:
        v = act(company_id, "CEO", "kill_project", {"project_slug": slug, "project_id": p["id"], "title": f"Kill {p['name']}"}, 0)
        _set_stage(company_id, p["id"], "KILLED", killed_reason=d.get("rationale"))
        ev.append(company_id, "CEO", "PROJECT_KILLED", {"slug": slug, "reason": d.get("rationale")})
        outcome.update(v)
    elif action in ("allocate_capital", "advance_project") and p and amount > 0:
        args = {
            "project_slug": slug, "project_id": p["id"], "amount_cents": amount,
            "title": f"[${amount/100:.2f}] {p['name']}: {d.get('board_ask') or 'validation budget'}",
            "success_criterion": d.get("success_criterion"),
            "kill_criterion": p["kill_criterion"],
            "evidence_signal_ids": d.get("evidence_signal_ids"),
            "risk_ack": d.get("risk_ack"),
            "cfo": {k: evaluations(p["id"]).get("CFO", {}).get(k) for k in
                    ("verdict", "max_test_spend_cents", "worst_case_loss_cents", "price_cents", "expected_cac_cents")},
            "coo": {k: evaluations(p["id"]).get("COO", {}).get(k) for k in
                    ("validation_plan", "cost_cents", "success_criterion", "kill_criterion", "duration_days")},
            "evidence": json.loads(p["evidence"] or "[]"),
        }
        v = act(company_id, "CEO", "allocate_budget", args, amount, memo=f"validation budget for {p['name']}")
        outcome.update(v)
        if v["verdict"] == "ALLOW":
            _set_stage(company_id, p["id"], "FUNDED")
        elif v["verdict"] == "APPROVAL":
            _set_stage(company_id, p["id"], "AWAITING_BOARD")
    else:
        action = "idle"
        outcome["action"] = "idle"

    summary = d.get("board_ask") or d.get("rationale") or action
    file_board_decision(company_id, "CEO", action, summary, d.get("rationale") or "",
                        {"decision": d, "outcome": outcome}, outcome.get("verdict", "N/A"))
    run_log(company_id, "decide", True, f"{action} {outcome.get('verdict','')} "
            f"{('SIMULATED' if simulated else '')}", _ms(t0))
    return outcome


def board_decide(company_id, request_id, approve, note=""):
    r = db.row("SELECT * FROM board_requests WHERE id=? AND company_id=?", (request_id, company_id))
    if not r:
        return {"ok": False, "error": "no such request"}
    if r["status"] != "PENDING":
        return {"ok": False, "error": f"already {r['status']}"}
    payload = json.loads(r["payload"] or "{}")
    args = payload.get("args", {})
    db.ex("UPDATE board_requests SET status=?, decided_at=?, note=? WHERE id=?",
          ("APPROVED" if approve else "REJECTED", ev.now(), note[:400], request_id))
    if not approve:
        ev.append(company_id, "BOARD", "BOARD_REJECTED", {"request_id": request_id, "title": r["title"], "note": note})
        if args.get("project_id"):
            _set_stage(company_id, args["project_id"], "SHELVED", killed_reason=f"board rejected: {note or 'no note'}")
        policy.record(company_id, "BOARD", payload.get("tool", "allocate_budget"), args, "DENY", "board rejected")
        return {"ok": True, "status": "REJECTED"}

    amount = int(r["amount_cents"] or 0)
    if amount > cash(company_id):
        return {"ok": False, "error": f"insufficient cash: ${cash(company_id)/100:.2f} < ${amount/100:.2f}"}
    e = ev.append(company_id, "BOARD", "BOARD_APPROVED", {
        "request_id": request_id, "title": r["title"], "amount_cents": amount, "note": note})
    ev.append(company_id, "BOARD", "CAPITAL_RELEASED", {
        "tool": payload.get("tool"), "amount_cents": amount,
        "project_slug": args.get("project_slug"), "memo": r["title"][:200]})
    money(company_id, "SPEND", -amount, "BOARD", f"{r['title']}"[:200],
          project_id=args.get("project_id"), event_id=e["id"])
    policy.record(company_id, "BOARD", payload.get("tool", "allocate_budget"), args, "ALLOW",
                  f"board approved ${amount/100:.2f}", e["id"])
    if args.get("project_id"):
        _set_stage(company_id, args["project_id"], "FUNDED")
    return {"ok": True, "status": "APPROVED", "amount_cents": amount}


def tick(company_id, spend_round=True, queries=None):
    """One operating cycle: discover -> evaluate -> decide -> build -> deploy.

    A missing human capability parks ONE project; it never stops the company and it
    never turns into a fake success. Every phase is bounded so a tick stays cheap.
    """
    db.init()
    log = []
    s = state(company_id)
    if not s:
        return {"ok": False, "error": "no such company"}
    caps.sync(company_id)

    by_stage = {}
    for p in s["projects"]:
        by_stage.setdefault(p["stage"], []).append(p)

    # 1. nothing yet -> find something to do
    if not s["projects"]:
        created, _raw = discover(company_id, queries=queries)
        log.append({"phase": "discover", "detail": [c["slug"] for c in created]})

    s = state(company_id)
    by_stage = {}
    for p in s["projects"]:
        by_stage.setdefault(p["stage"], []).append(p)

    # 2. evaluate what has not been through the gate
    for p in (by_stage.get("DISCOVERED") or [])[: (2 if spend_round else 1)]:
        log.append({"phase": "evaluate", "detail": evaluate(company_id, p["id"])})

    # 3. decide (the board has to answer before the CEO may move again)
    if s["board_requests"]:
        log.append({"phase": "halt", "detail": f"{len(s['board_requests'])} board request(s) pending — the CEO cannot pre-empt the board"})
    elif (by_stage.get("VALIDATED") or by_stage.get("FUNDED") or by_stage.get("BUILT_LOCAL")
          or by_stage.get("LIVE") or by_stage.get("BUILDING")):
        log.append({"phase": "decide", "detail": decide(company_id)})

    # 4. build what has been funded — this needs no human, so it is never blocked
    s = state(company_id)
    funded = [p for p in s["projects"] if p["stage"] == "FUNDED"]
    if funded:
        log.append({"phase": "build", "detail": build_project(company_id, funded[0]["id"])})

    # 5. deploy — needs a host, so this is where a setup request can appear
    s = state(company_id)
    for p in [x for x in s["projects"] if x["stage"] == "BUILT_LOCAL"][:1]:
        log.append({"phase": "deploy", "detail": deploy_project(company_id, p["id"])})

    return {"ok": True, "log": log, "state": state(company_id)}
