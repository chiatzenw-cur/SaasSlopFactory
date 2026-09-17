"""Unit economics. The CFO states ASSUMPTIONS; this module does the arithmetic.

Why the split: a language model asked "estimate profit" produces a number that is
internally inconsistent with its own inputs, and nobody can check it. So the CFO
returns a structured assumption set (funnel rates, price, per-customer cost, channel),
this module computes every figure from those inputs, and the report prints the formula
next to each number. Any number in a report can be recomputed by hand from the
assumptions — which is the only reason a founder should believe it.

Fee rates are provider facts, not guesses.
"""

import json
import math
import os

# Paddle is a merchant of record: 5% + $0.50 per checkout transaction.
# Stripe: 2.9% + $0.30. Both are pass-through costs of taking money.
PAYMENT_FEES = {
    "paddle": {"pct": 5.0, "fixed_cents": 50},
    "stripe": {"pct": 2.9, "fixed_cents": 30},
    "none": {"pct": 0.0, "fixed_cents": 0},
}

DEFAULTS = {
    "price_cents": 1900,
    "recurring": False,
    "months_retained": 1,
    "pct_price": 0.6,
    "visitor_to_intent_pct": 5.0,
    "intent_to_checkout_pct": 40.0,
    "checkout_to_paid_pct": 80.0,
    "visitors_planned": 150,
    "channel": "organic",
    "cpc_cents": 0,
    "variable_cost_per_customer_cents": 200,
    "fixed_cost_per_month_cents": 0,
    "refund_rate_pct": 5.0,
    "payment_rail": "paddle",
}

SCENARIOS = {"pessimistic": 0.5, "base": 1.0, "optimistic": 1.5}


def normalise(a):
    out = dict(DEFAULTS)
    for k, v in (a or {}).items():
        if v is None:
            continue
        out[k] = v
    for k in ("visitor_to_intent_pct", "intent_to_checkout_pct", "checkout_to_paid_pct",
              "refund_rate_pct", "pct_price"):
        out[k] = max(0.0, min(100.0, float(out[k])))
    for k in ("price_cents", "visitors_planned", "cpc_cents",
              "variable_cost_per_customer_cents", "fixed_cost_per_month_cents"):
        out[k] = max(0, int(float(out[k])))
    out["months_retained"] = max(1, int(float(out.get("months_retained") or 1)))
    rail = str(out.get("payment_rail") or "paddle").lower()
    out["payment_rail"] = rail if rail in PAYMENT_FEES else "paddle"
    return out


def _scenario(a, build_cost_cents, scale):
    """scale multiplies the funnel conversion rates: 0.5 = half as effective."""
    r1 = min(100.0, a["visitor_to_intent_pct"] * scale) / 100.0
    r2 = min(100.0, a["intent_to_checkout_pct"] * scale) / 100.0
    r3 = min(100.0, a["checkout_to_paid_pct"] * scale) / 100.0
    visitors = a["visitors_planned"]
    months = a["months_retained"] if a["recurring"] else 1

    intents = visitors * r1
    checkouts = intents * r2
    paid = checkouts * r3
    price = a["price_cents"]
    fee = PAYMENT_FEES[a["payment_rail"]]

    revenue = paid * price * months
    fees = paid * (price * fee["pct"] / 100.0 + fee["fixed_cents"]) * months
    refunds = paid * (a["refund_rate_pct"] / 100.0) * price * months
    varcost = paid * a["variable_cost_per_customer_cents"] * months
    traffic = visitors * a["cpc_cents"]
    fixed = a["fixed_cost_per_month_cents"] * months

    gross = revenue - fees - refunds - varcost
    total_cost = traffic + varcost + fixed + build_cost_cents
    profit = revenue - fees - refunds - total_cost

    per_visitor = r1 * r2 * r3 * (price - price * fee["pct"] / 100.0 - fee["fixed_cents"]
                                  - price * a["refund_rate_pct"] / 100.0
                                  - a["variable_cost_per_customer_cents"]) * months
    net_per_visitor = per_visitor - a["cpc_cents"]
    overhead = fixed + build_cost_cents
    breakeven = (math.ceil(overhead / net_per_visitor) if net_per_visitor > 0 else None)

    return {
        "scale": scale,
        "visitors": visitors,
        "intents": round(intents, 2),
        "checkouts": round(checkouts, 2),
        "customers": round(paid, 2),
        "price_cents": price,
        "revenue_cents": round(revenue),
        "fees_cents": round(fees),
        "refunds_cents": round(refunds),
        "variable_cost_cents": round(varcost),
        "traffic_cost_cents": round(traffic),
        "fixed_cost_cents": round(fixed),
        "build_cost_cents": build_cost_cents,
        "gross_profit_cents": round(gross),
        "total_cost_cents": round(total_cost),
        "profit_cents": round(profit),
        "gross_margin_pct": round(100.0 * gross / revenue, 1) if revenue > 0 else None,
        "profit_per_visitor_cents": round(net_per_visitor, 3),
        "breakeven_visitors": breakeven,
        "verdict": "PASS" if profit > 0 else "VETO",
    }


def detected_rail():
    """The rail this company can actually use. A model that assumes a payment provider
    the company does not have produces a fee line that will never be paid."""
    if os.environ.get("PADDLE_API_KEY") or os.environ.get("PADDLE_CHECKOUT_URL"):
        return "paddle"
    if os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("STRIPE_PAYMENT_LINK"):
        return "stripe"
    return None


def money(c):
    c = c or 0
    return f"-${abs(c)/100:,.2f}" if c < 0 else f"${c/100:,.2f}"


def model(assumptions, build_cost_cents=0, rail=None):
    """-> {assumptions, scenarios{...}, checks[], kill, verdict}"""
    a = normalise(assumptions)
    fixed_rail = rail or detected_rail()
    if fixed_rail:
        a["payment_rail"] = fixed_rail
        a["payment_rail_enforced"] = True
    sc = {name: _scenario(a, int(build_cost_cents), s) for name, s in SCENARIOS.items()}
    base = sc["base"]

    checks = [
        {"label": "intent", "formula": f"{base['visitors']} × {a['visitor_to_intent_pct']}%",
         "value": base["intents"]},
        {"label": "checkouts", "formula": f"{base['intents']} × {a['intent_to_checkout_pct']}%",
         "value": base["checkouts"]},
        {"label": "paying customers", "formula": f"{base['checkouts']} × {base['scale']*a['checkout_to_paid_pct']}%",
         "value": base["customers"]},
        {"label": "revenue", "formula": f"{base['customers']} × ${a['price_cents']/100:.2f}"
                                         + (f" × {a['months_retained']}mo" if a["recurring"] else ""),
         "value": base["revenue_cents"]},
        {"label": f"{a['payment_rail']} fees",
         "formula": f"{base['customers']} × (${a['price_cents']/100:.2f} × {PAYMENT_FEES[a['payment_rail']]['pct']}%"
                    f" + ${PAYMENT_FEES[a['payment_rail']]['fixed_cents']/100:.2f})",
         "value": base["fees_cents"]},
        {"label": "refunds", "formula": f"{base['customers']} × {a['refund_rate_pct']}% × price",
         "value": base["refunds_cents"]},
        {"label": "per-customer cost",
         "formula": f"{base['customers']} × ${a['variable_cost_per_customer_cents']/100:.2f}",
         "value": base["variable_cost_cents"]},
        {"label": "traffic cost", "formula": f"{base['visitors']} × ${a['cpc_cents']/100:.2f}",
         "value": base["traffic_cost_cents"]},
        {"label": "profit", "formula": "revenue − fees − refunds − costs (variable + traffic + fixed + build)",
         "value": base["profit_cents"]},
    ]
    if base["breakeven_visitors"] is not None:
        checks.append({"label": "break-even visitors",
                       "formula": f"ceil((fixed ${a['fixed_cost_per_month_cents']/100:.2f} + build "
                                  f"${build_cost_cents/100:.2f}) / ${base['profit_per_visitor_cents']/100:.2f} per visitor)",
                       "value": base["breakeven_visitors"]})

    # CFO veto rule, applied in code so a confident paragraph cannot override it
    kill = []
    if base["profit_cents"] <= 0:
        kill.append(f"base case loses ${-base['profit_cents']/100:.2f}")
    if sc["pessimistic"]["profit_cents"] <= 0 and base["profit_cents"] > 0:
        kill.append("only profitable if the funnel performs as assumed — half the rate loses money")
    if base["customers"] < 1 and a["visitors_planned"] > 0:
        kill.append(f"{a['visitors_planned']} visitors is not enough for a single customer at these rates")
    if base["gross_margin_pct"] is not None and base["gross_margin_pct"] < 50:
        kill.append(f"gross margin {base['gross_margin_pct']}% is below 50%")

    return {
        "assumptions": a,
        "scenarios": sc,
        "checks": checks,
        "kill": kill,
        "verdict": "PASS" if (base["profit_cents"] > 0 and not kill) else "VETO",
    }


def verdict_from_cfo(assumptions, max_test_spend_cents, build_cost_cents, cash_cents):
    """The CFO's sign-off, computed. Money that cannot be lost is not a plan."""
    m = model(assumptions, build_cost_cents)
    a = m["assumptions"]
    worst = m["scenarios"]["pessimistic"]["total_cost_cents"]
    if worst > max_test_spend_cents:
        m["kill"].append(f"worst-case spend ${worst/100:.2f} exceeds the CFO ceiling "
                         f"${max_test_spend_cents/100:.2f}")
    if worst > cash_cents:
        m["kill"].append(f"worst-case spend ${worst/100:.2f} exceeds cash ${cash_cents/100:.2f}")
    if m["kill"]:
        m["verdict"] = "VETO"
    m["worst_case_loss_cents"] = min(worst, cash_cents)
    m["max_test_spend_cents"] = max_test_spend_cents
    m["summary"] = (
        f"{a['visitors_planned']} visitors @ {a['visitor_to_intent_pct']}% intent → "
        f"{m['scenarios']['base']['customers']:.1f} customers @ ${a['price_cents']/100:.2f} = "
        f"${m['scenarios']['base']['revenue_cents']/100:.2f} revenue, "
        f"${m['scenarios']['base']['profit_cents']/100:.2f} profit "
        f"(worst case −${worst/100:.2f})"
    )
    return m


def report_md(project, m, cfo_extra=None):
    """A report a shareholder can read and re-check."""
    a, sc = m["assumptions"], m["scenarios"]
    L = []
    L.append(f"# Unit economics — {project['name']}")
    L.append("")
    L.append(f"**Hypothesis.** {project.get('hypothesis') or ''}")
    L.append("")
    L.append(f"**Verdict: {m['verdict']}**" + (f" — {len(m['kill'])} reason(s) to kill" if m["kill"] else ""))
    L.append("")
    if m["kill"]:
        for k in m["kill"]:
            L.append(f"- ⚠︎ {k}")
        L.append("")
    L.append("## Assumptions (the only things that were assumed)")
    L.append("")
    L.append("| input | value | note |")
    L.append("|---|---|---|")
    L.append(f"| price | {money(a['price_cents'])} | {'recurring, ' + str(a['months_retained']) + ' month(s)' if a['recurring'] else 'one-time'} |")
    L.append(f"| visitors planned | {a['visitors_planned']} | channel: {a['channel']} |")
    L.append(f"| cost per visitor | {money(a['cpc_cents'])} | ${a['cpc_cents']/100:.2f} — 0 means organic reach only |")
    L.append(f"| visit → intent | {a['visitor_to_intent_pct']}% | |")
    L.append(f"| intent → checkout | {a['intent_to_checkout_pct']}% | |")
    L.append(f"| checkout → paid | {a['checkout_to_paid_pct']}% | |")
    L.append(f"| refund rate | {a['refund_rate_pct']}% | |")
    L.append(f"| cost per customer | {money(a['variable_cost_per_customer_cents'])} | compute/API/TTS per paying user |")
    L.append(f"| fixed per month | {money(a['fixed_cost_per_month_cents'])} | hosting, domain |")
    L.append(f"| payment rail | {a['payment_rail']} | fee {PAYMENT_FEES[a['payment_rail']]['pct']}% + {money(PAYMENT_FEES[a['payment_rail']]['fixed_cents'])} per transaction |")
    L.append("")
    L.append("## Three scenarios")
    L.append("")
    L.append("| | pessimistic (½ rates) | base | optimistic (×1.5 rates) |")
    L.append("|---|---|---|---|")
    for label, key, fmt in (("visitors", "visitors", lambda v: f"{v}"),
                            ("pricing intent", "intents", lambda v: f"{v:.1f}"),
                            ("checkouts", "checkouts", lambda v: f"{v:.1f}"),
                            ("**paying customers**", "customers", lambda v: f"**{v:.1f}**"),
                            ("revenue", "revenue_cents", money),
                            ("payment fees", "fees_cents", money),
                            ("refunds", "refunds_cents", money),
                            ("per-customer cost", "variable_cost_cents", money),
                            ("traffic cost", "traffic_cost_cents", money),
                            ("build cost", "build_cost_cents", money),
                            ("**profit**", "profit_cents", lambda v: f"**{money(v)}**"),
                            ("gross margin", "gross_margin_pct", lambda v: (f"{v}%" if v is not None else "—"))):
        row = " | ".join(fmt(sc[s][key]) for s in ("pessimistic", "base", "optimistic"))
        L.append(f"| {label} | {row} |")
    L.append("")
    L.append("## Every number, recomputable")
    L.append("")
    for c in m["checks"]:
        v = c["value"]
        shown = money(v) if isinstance(v, (int, float)) and abs(v) > 3 and "pct" not in c["label"] else v
        L.append(f"- **{c['label']}**: `{c['formula']}` = {shown}")
    L.append("")
    if m.get("breakeven_visitors") is not None or sc["base"]["breakeven_visitors"]:
        L.append(f"Break-even needs **{sc['base']['breakeven_visitors']} visitors** "
                 f"({sc['base']['profit_per_visitor_cents']/100:.4f} profit per visitor).")
        L.append("")
    L.append(f"Worst case loss: **{money(m.get('worst_case_loss_cents'))}** "
             f"(capped at cash). Experiment ceiling: {money(m.get('max_test_spend_cents'))}.")
    if cfo_extra:
        L.append("")
        L.append("## CFO note")
        L.append("")
        L.append(cfo_extra)
    return "\n".join(L)


def portfolio_report(company, projects, cash_cents, ledger):
    """The shareholder view: what the whole portfolio is expected to return."""
    L = [f"# {company['name']} — shareholder report", ""]
    spend = sum(-r["amount_cents"] for r in ledger if r["amount_cents"] < 0)
    revenue = sum(r["amount_cents"] for r in ledger if r["kind"] == "REVENUE")
    L.append(f"- cash: **{money(cash_cents)}** · deployed: {money(spend)} · revenue: {money(revenue)}")
    L.append(f"- projects: {len(projects)} ("
             + ", ".join(f"{p['stage'].lower()} {p['slug']}" for p in projects) + ")")
    L.append("")
    L.append("| project | stage | price | expected customers | expected revenue | expected profit | worst case |")
    L.append("|---|---|---|---|---|---|---|")
    tot_profit = tot_rev = tot_worst = 0
    for p in projects:
        cfo = p.get("cfo") or {}
        m = cfo.get("model") if isinstance(cfo.get("model"), dict) else None
        if not m:
            L.append(f"| {p['slug']} | {p['stage']} | — | — | — | — | — |")
            continue
        b = m["scenarios"]["base"]
        w = m["scenarios"]["pessimistic"]
        tot_profit += b["profit_cents"]
        tot_rev += b["revenue_cents"]
        tot_worst += m.get("worst_case_loss_cents") or 0
        L.append(f"| {p['slug']} | {p['stage']} | {money(b['price_cents'])} | {b['customers']:.1f} | "
                 f"{money(b['revenue_cents'])} | {money(b['profit_cents'])} | −{money(m.get('worst_case_loss_cents'))} |")
    L.append(f"| **total** | | | | **{money(tot_rev)}** | **{money(tot_profit)}** | **−{money(tot_worst)}** |")
    L.append("")
    if tot_profit <= 0:
        L.append("> Expected profit across the portfolio is not positive. That is a fact about the "
                 "assumptions, not a reason to hope: either the funnel rates or the price has to change.")
    return "\n".join(L)
