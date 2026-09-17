"""Numbers the company collects about itself. The COO's success criterion is
measured here, from real events — never estimated by the model that proposed it.
"""

import json

from . import db, events as ev


def record(company_id, name, value, project_id=None, meta=None):
    db.ex(
        "INSERT INTO metrics(company_id,project_id,ts,name,value,meta) VALUES(?,?,?,?,?,?)",
        (company_id, project_id, ev.now(), name, float(value), json.dumps(meta or {}, ensure_ascii=False)),
    )


def summarize(company_id, project_id):
    rows = db.rows(
        "SELECT name, COUNT(*) n, SUM(value) total, MAX(ts) last FROM metrics"
        " WHERE company_id=? AND project_id=? GROUP BY name",
        (company_id, project_id),
    )
    return {r["name"]: {"n": r["n"], "total": r["total"], "last": r["last"]} for r in rows}


def funnel(company_id, project_id):
    views = db.row(
        "SELECT COUNT(*) n FROM metrics WHERE company_id=? AND project_id=? AND name='page_view'",
        (company_id, project_id),
    )
    intents = db.row(
        "SELECT COUNT(*) n FROM metrics WHERE company_id=? AND project_id=? AND name='pricing_intent'",
        (company_id, project_id),
    )
    rev = db.row(
        "SELECT COALESCE(SUM(value),0) s, COUNT(*) n FROM metrics WHERE company_id=? AND project_id=? AND name='revenue_cents'",
        (company_id, project_id),
    )
    v = int(views["n"] if views else 0)
    i = int(intents["n"] if intents else 0)
    return {
        "views": v,
        "intents": i,
        "intent_rate_pct": round(100.0 * i / v, 2) if v else None,
        "revenue_cents": int(rev["s"] if rev else 0),
        "orders": int(rev["n"] if rev else 0),
        "by_source": by_source(company_id, project_id),
    }


def by_source(company_id, project_id):
    """Which channel actually produced the traffic. Without this, GTM is a story."""
    try:
        rows = db.rows(
            "SELECT COALESCE(json_extract(meta,'$.src'),'direct') src, name, COUNT(*) n"
            " FROM metrics WHERE company_id=? AND project_id=? AND name IN ('page_view','pricing_intent')"
            " GROUP BY src, name ORDER BY src",
            (company_id, project_id),
        )
    except Exception:  # noqa: BLE001  (no JSON1)
        return {}
    out = {}
    for r in rows:
        d = out.setdefault(r["src"], {"views": 0, "intents": 0})
        d["views" if r["name"] == "page_view" else "intents"] = int(r["n"])
    for d in out.values():
        d["intent_rate_pct"] = round(100.0 * d["intents"] / d["views"], 2) if d["views"] else None
    return out
