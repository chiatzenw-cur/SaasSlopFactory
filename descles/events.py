"""Append-only hash-chained event log. Company state is a projection of this."""

import hashlib
import json
from datetime import datetime, timezone

from . import db


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canon(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def append(company_id, actor, type_, payload):
    r = db.row(
        "SELECT seq, hash FROM events WHERE company_id=? ORDER BY seq DESC LIMIT 1",
        (company_id,),
    )
    seq = (r["seq"] + 1) if r else 1
    prev = r["hash"] if r else "GENESIS"
    ts = now()
    h = hashlib.sha256(
        canon({"prev": prev, "seq": seq, "ts": ts, "actor": actor, "type": type_, "payload": payload}).encode()
    ).hexdigest()[:32]
    cur = db.ex(
        "INSERT INTO events(company_id,seq,ts,actor,type,payload,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?)",
        (company_id, seq, ts, actor, type_, canon(payload), prev, h),
    )
    return {"id": cur.lastrowid, "seq": seq, "hash": h, "ts": ts}


def chain(company_id, limit=200):
    return db.rows(
        "SELECT * FROM events WHERE company_id=? ORDER BY seq DESC LIMIT ?", (company_id, limit)
    )


def verify(company_id):
    """Recompute the chain. Returns {ok, n, broken_at}."""
    evs = db.rows("SELECT * FROM events WHERE company_id=? ORDER BY seq ASC", (company_id,))
    prev = "GENESIS"
    for e in evs:
        h = hashlib.sha256(
            canon(
                {
                    "prev": prev,
                    "seq": e["seq"],
                    "ts": e["ts"],
                    "actor": e["actor"],
                    "type": e["type"],
                    "payload": json.loads(e["payload"]),
                }
            ).encode()
        ).hexdigest()[:32]
        if h != e["hash"] or e["prev_hash"] != prev:
            return {"ok": False, "n": len(evs), "broken_at": e["seq"]}
        prev = h
    return {"ok": True, "n": len(evs), "broken_at": None}
