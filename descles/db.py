import sqlite3
import threading

from . import config

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS companies(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, mission TEXT, template TEXT,
  mode TEXT, charter TEXT NOT NULL, status TEXT DEFAULT 'ACTIVE', created_at TEXT
);
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, company_id TEXT, role TEXT, model TEXT, status TEXT DEFAULT 'ACTIVE',
  spec TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT, seq INTEGER, ts TEXT,
  actor TEXT, type TEXT, payload TEXT, prev_hash TEXT, hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_company ON events(company_id, seq);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT, ts TEXT, kind TEXT,
  amount_cents INTEGER NOT NULL, project_id TEXT, actor TEXT, memo TEXT, event_id INTEGER
);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, company_id TEXT, slug TEXT, name TEXT, stage TEXT,
  hypothesis TEXT, evidence TEXT, signals TEXT, spent_cents INTEGER DEFAULT 0,
  revenue_cents INTEGER DEFAULT 0, success_criterion TEXT, kill_criterion TEXT,
  killed_reason TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS board_requests(
  id TEXT PRIMARY KEY, company_id TEXT, ts TEXT, kind TEXT, title TEXT,
  amount_cents INTEGER, payload TEXT, status TEXT DEFAULT 'PENDING',
  decided_at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT, ts TEXT, actor TEXT,
  kind TEXT, summary TEXT, rationale TEXT, payload TEXT, authority TEXT
);
CREATE TABLE IF NOT EXISTS actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT, ts TEXT, actor TEXT,
  tool TEXT, args TEXT, verdict TEXT, reason TEXT, event_id INTEGER
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT, ts TEXT, phase TEXT,
  ok INTEGER, detail TEXT, ms INTEGER
);
CREATE TABLE IF NOT EXISTS evaluations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT, project_id TEXT, role TEXT,
  ts TEXT, payload TEXT, simulated INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS capabilities(
  company_id TEXT, name TEXT, status TEXT, detail TEXT, PRIMARY KEY(company_id, name)
);
CREATE TABLE IF NOT EXISTS setup_requests(
  id TEXT PRIMARY KEY, company_id TEXT, ts TEXT, capability TEXT, title TEXT,
  why TEXT, steps TEXT, url TEXT, status TEXT DEFAULT 'PENDING', project_id TEXT,
  resolved_at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS webhook_events(
  provider TEXT, event_id TEXT, ts TEXT, type TEXT, payload TEXT,
  booked INTEGER DEFAULT 0, amount_cents INTEGER, project_id TEXT, reason TEXT,
  PRIMARY KEY(provider, event_id)
);
CREATE TABLE IF NOT EXISTS metrics(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT, project_id TEXT, ts TEXT,
  name TEXT, value REAL, meta TEXT
);
CREATE TABLE IF NOT EXISTS builds(
  id TEXT PRIMARY KEY, company_id TEXT, project_id TEXT, slug TEXT, ts TEXT,
  path TEXT, url TEXT, files TEXT, copy TEXT, deploy_url TEXT, deploy_state TEXT,
  price_id TEXT, price_source TEXT
);
"""


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(str(config.DB_PATH), timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def init():
    c = conn()
    c.executescript(SCHEMA)
    _migrate(c)
    return c


def _migrate(c):
    """sqlite has no ADD COLUMN IF NOT EXISTS; older DBs must still open."""
    for table, cols in (
        ("webhook_events", (("booked", "INTEGER DEFAULT 0"), ("amount_cents", "INTEGER"),
                            ("project_id", "TEXT"), ("reason", "TEXT"))),
        ("builds", (("price_id", "TEXT"), ("price_source", "TEXT"))),
    ):
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        if not have:
            continue
        for col, ddl in cols:
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    return c


def rows(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def row(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def ex(sql, args=()):
    return conn().execute(sql, args)


if __name__ == "__main__":
    init()
    print("schema ok ->", config.DB_PATH)
