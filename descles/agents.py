"""The C-suite. Each agent is a *function of company state*, not a chat partner.

No agent has an actuator. Agents return structured proposals; runtime.act() is
the only way anything happens, and it always goes through policy.authorize().
"""

import json
import re

from . import config, llm, roles

COMMON = """You are the {role} of {company}, a company with this mission: {mission}

You are not a chatbot and nobody wants your encouragement. You emit exactly one JSON object.
Hard rules:
 1. You only act inside the corporate charter. The charter is enforced in code, not by you — a
    request outside your authority is simply refused and logged, so do not waste a turn on it.
 2. NEVER invent evidence. Any factual claim about the market must cite a `signal_id` that appears
    in the input. If you have no signal, say "unsupported" in the field instead of guessing.
 3. Money is in integer cents. No floats, no dollar signs.
 4. Be specific and falsifiable. "Huge market opportunity" is a failed answer.

Your role specification:
{spec}

Return JSON only, matching the schema. No prose before or after."""

SCHEMAS = {
    "CMO": """{
  "opportunities": [
    {
      "name": "<= 3 words product name",
      "one_liner": "one sentence: what it does, for whom",
      "problem": "the concrete pain, in the user's words",
      "target_user": "narrow segment, not 'everyone'",
      "signal_ids": ["hn:123", "hn:456"],
      "evidence_quote": "verbatim substring from one cited signal, <= 200 chars",
      "why_now": "what changed recently",
      "competition_note": "who else solves this, and how bad they are",
      "monetization": "who pays, for what, one-time or recurring",
      "price_guess_cents": 1900
    }
  ],
  "rejected_ideas": [{"name": "...", "why": "..."}]
}""",
    "CTO": """{
  "project_slug": "same slug you were given",
  "mvp_scope": ["3-6 bullets, each a thing a single agent can build in one sitting"],
  "mvp_hours": 12,
  "build_cost_cents": 3600,
  "stack": ["python", "sqlite", "vercel"],
  "time_to_launch_days": 2,
  "build_risk": "the one thing most likely to make this fail to ship",
  "explicitly_out_of_scope": ["..."]
}""",
    "CFO": """{
  "project_slug": "same slug you were given",
  "price_cents": 1900,
  "expected_cac_cents": 800,
  "expected_conversion_pct": 4.5,
  "payback_months": 1.5,
  "max_test_spend_cents": 8000,
  "worst_case_loss_cents": 8000,
  "verdict": "PASS",
  "veto_reason": null,
  "reason": "the arithmetic, stated with the numbers you used"
}""",
    "COO": """{
  "project_slug": "same slug you were given",
  "validation_plan": ["3-5 concrete steps, cheapest first"],
  "cost_cents": 7000,
  "duration_days": 7,
  "success_criterion": "measurable, e.g. '>=5% of 150 visitors click Buy'",
  "kill_criterion": "measurable, e.g. '<3% pricing intent after 150 visitors'",
  "measurement": "how the number gets collected without a human in the loop"
}""",
    "CEO": """{
  "action": "allocate_capital | kill_project | advance_project | request_board | idle",
  "project_slug": "slug or null",
  "amount_cents": 7200,
  "rationale": "why this and not the alternatives, referencing the CFO and COO numbers",
  "success_criterion": "copied from the COO plan, unchanged",
  "evidence_signal_ids": ["hn:123"],
  "risk_ack": "the worst realistic outcome and its cost",
  "board_ask": "one line a shareholder can decide on"
}""",
}


def _spec_text(role):
    s = roles.spec(role)
    return json.dumps({k: v for k, v in s.items() if k != "permits"}, ensure_ascii=False, indent=1) + \
        "\npermits: " + json.dumps(s["permits"], ensure_ascii=False)


def _sys(company, role):
    return COMMON.format(
        role=role,
        company=company["name"],
        mission=company["mission"],
        spec=_spec_text(role),
    )


def _ask(company, role, model, user, schema_key, fallback):
    if not llm.available(model):
        out = fallback()
        out["_simulated"] = True
        out["_note"] = f"{config.provider_for(model)[1]} not set — deterministic fallback, NOT model output"
        return out
    user = user + "\n\nReturn JSON matching exactly this schema:\n" + SCHEMAS[schema_key]
    try:
        return llm.chat_json(model, _sys(company, role), user)
    except llm.LLMError as e:
        out = fallback()
        out["_simulated"] = True
        out["_note"] = f"LLM call failed ({e}) — deterministic fallback"
        return out


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "project"


def _signals_block(sigs, limit=40):
    lines = []
    for s in sigs[:limit]:
        lines.append(
            f"[{s['id']}] ({s['source']}, {s.get('created_at') or 'n/a'}, {s.get('points',0)} pts)\n"
            f"  title: {s['title']}\n  quote: {s['text'][:400]}\n  url: {s['url']}"
        )
    return "\n".join(lines) if lines else "(no signals returned by any source — report that honestly)"


# ---------------------------------------------------------------- agents


def cmo_discover(company, model, signals, killed, existing, mission_queries):
    user = f"""Buying signals were swept from live sources. Every signal below is real and fetchable.

QUERIES SWEPT: {json.dumps(mission_queries)}

SIGNALS:
{_signals_block(signals)}

ALREADY IN PORTFOLIO (do not re-propose): {json.dumps(existing)}
ALREADY KILLED (do not re-propose): {json.dumps(killed)}

Find the 3 best opportunities to build a small profitable software product from.
Prefer: people explicitly asking for a tool, paying for a worse alternative, or hacking it in a spreadsheet.
Disqualify anything where the only evidence is a general "X is hard" complaint with no buyer."""

    def fb():
        picked = signals[:3]
        opps = []
        for i, s in enumerate(picked):
            q = re.sub(r"[^a-z0-9 ]", " ", (s.get("query") or "opportunity").lower())
            words = [w for w in q.split() if len(w) > 3][:3]
            name = (" ".join(words).title() + f" #{i+1}").strip()[:28]
            opps.append(
                {
                    "name": name or f"Opportunity {i+1}",
                    "one_liner": (s["text"][:120] or "derived from a live signal"),
                    "problem": s["text"][:200],
                    "target_user": "the person who wrote the cited signal",
                    "signal_ids": [s["id"]],
                    "evidence_quote": s["text"][:180],
                    "why_now": "signal is recent and unprompted",
                    "competition_note": "unsupported — no competitor sweep run",
                    "monetization": "unsupported",
                    "price_guess_cents": 1900,
                }
            )
        return {"opportunities": opps, "rejected_ideas": []}

    return _ask(company, "CMO", model, user, "CMO", fb)


def cto_estimate(company, model, opp):
    user = "Estimate the smallest shippable MVP for this opportunity.\n\nOPPORTUNITY:\n" + json.dumps(opp, ensure_ascii=False, indent=1) + \
        "\n\nAssume agent labour at $30/hour equivalent, and that a single agent session is 4 hours."

    def fb():
        return {
            "project_slug": slugify(opp.get("name", "")),
            "mvp_scope": ["single-page product", "sqlite storage", "stripe test checkout", "one landing page"],
            "mvp_hours": 12,
            "build_cost_cents": 3600,
            "stack": ["python", "sqlite"],
            "time_to_launch_days": 3,
            "build_risk": "unvalidated demand; the build itself is trivial",
            "explicitly_out_of_scope": ["accounts", "teams", "mobile app"],
        }

    return _ask(company, "CTO", model, user, "CTO", fb)


def cfo_economics(company, model, opp, cto, cash):
    user = (
        "Judge the unit economics of this experiment before a dollar is spent.\n\n"
        + "OPPORTUNITY:\n" + json.dumps(opp, ensure_ascii=False, indent=1)
        + "\n\nCTO ESTIMATE:\n" + json.dumps(cto, ensure_ascii=False, indent=1)
        + f"\n\nCOMPANY CASH: {cash} cents (this is the absolute ceiling on worst_case_loss_cents)"
        + "\n\nVeto the project if expected CAC is not covered by a plausible price, or if the worst-case"
        " loss exceeds available cash. You cannot allocate budget — you can only approve or veto spend."
    )

    def fb():
        price = int(opp.get("price_guess_cents") or 1900)
        loss = min(7000, max(1000, cash // 8))
        return {
            "project_slug": slugify(opp.get("name", "")),
            "price_cents": price,
            "expected_cac_cents": 900,
            "expected_conversion_pct": 4.0,
            "payback_months": 1.0,
            "max_test_spend_cents": loss,
            "worst_case_loss_cents": loss,
            "verdict": "PASS" if cash > loss else "VETO",
            "veto_reason": None if cash > loss else "worst case loss exceeds cash",
            "reason": f"assumed CAC 900c vs price {price}c; worst case {loss}c <= cash {cash}c",
        }

    out = _ask(company, "CFO", model, user, "CFO", fb)
    # the hard wall: CFO arithmetic does not get to exceed cash or the charter
    out["worst_case_loss_cents"] = min(int(out.get("worst_case_loss_cents") or 0), cash)
    if int(out.get("max_test_spend_cents") or 0) > cash:
        out["verdict"] = "VETO"
        out["veto_reason"] = "max_test_spend exceeds company cash"
    return out


def coo_validation_plan(company, model, opp, cfo, cto):
    user = (
        "Design the cheapest experiment that can falsify this hypothesis. Do not plan the build.\n\n"
        + "OPPORTUNITY:\n" + json.dumps(opp, ensure_ascii=False, indent=1)
        + "\n\nCFO LIMITS:\n" + json.dumps(cfo, ensure_ascii=False, indent=1)
        + "\n\nCTO SCOPE:\n" + json.dumps(cto, ensure_ascii=False, indent=1)
        + f"\n\nYour budget ceiling is {cfo.get('max_test_spend_cents')} cents."
    )

    def fb():
        return {
            "project_slug": slugify(opp.get("name", "")),
            "validation_plan": [
                "publish a one-page offer at a real price",
                "drive 150 visitors from the cited communities (organic, no paid)",
                "instrument the checkout click as the pricing-intent event",
                "report the number after 7 days or 150 visitors, whichever first",
            ],
            "cost_cents": min(5000, int(cfo.get("max_test_spend_cents") or 5000)),
            "duration_days": 7,
            "success_criterion": ">=5% of 150 visitors click Buy",
            "kill_criterion": "<3% pricing intent after 150 visitors",
            "measurement": "landing page click event -> sqlite, no human in the loop",
        }

    return _ask(company, "COO", model, user, "COO", fb)


def ceo_decide(company, model, state):
    user = (
        "You are deciding what this company does next. Below is the entire company state.\n\n"
        + json.dumps(state, ensure_ascii=False, indent=1)
        + "\n\nRules: you may allocate at most "
        + f"{state['authority']['ceo_unilateral_limit_cents']} cents without board approval; anything "
        f"above goes to the board as a request and does NOT move money now. You may kill projects whose "
        "kill_criterion has been met. Do not propose work you are not permitted to do. If nothing is worth "
        "doing, say idle."
    )

    def fb():
        cands = [p for p in state["projects"] if p["stage"] in ("DISCOVERED", "VALIDATED")]
        if not cands:
            return {
                "action": "idle",
                "project_slug": None,
                "amount_cents": 0,
                "rationale": "no validated candidate in portfolio",
                "success_criterion": None,
                "evidence_signal_ids": [],
                "risk_ack": "opportunity cost only",
                "board_ask": "none",
            }
        p = cands[0]
        amount = min(int(p.get("cfo", {}).get("max_test_spend_cents") or 5000), state["authority"]["ceo_unilateral_limit_cents"])
        return {
            "action": "allocate_capital",
            "project_slug": p["slug"],
            "amount_cents": amount,
            "rationale": f"cheapest validated candidate ({p['name']}); CFO worst case {p.get('cfo',{}).get('worst_case_loss_cents')}c",
            "success_criterion": p.get("coo", {}).get("success_criterion"),
            "evidence_signal_ids": [s for s in (p.get("signal_ids") or [])][:3],
            "risk_ack": f"lose {amount}c if the validation fails",
            "board_ask": f"Approve ${amount/100:.2f} to validate {p['name']}",
        }

    return _ask(company, "CEO", model, user, "CEO", fb)
