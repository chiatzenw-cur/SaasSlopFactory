"""Local corporate console. stdlib only, no build step, no CDN.

  python server.py            -> http://127.0.0.1:7310
"""

import json
import mimetypes
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import config, db, events as ev, policy, roles, runtime

WEB = config.ROOT / "descles" / "web"


def _err(msg):
    return {"error": str(msg)}


def handle_get(path, q):
    if path == "/api/health":
        return {
            "ok": True,
            "db": str(config.DB_PATH),
            "models": {m: bool(os.environ.get(k)) for m, (_, k) in config.PROVIDERS.items()},
            "companies": len(runtime.companies()),
        }
    if path == "/api/companies":
        return {"companies": runtime.companies()}
    if path == "/api/templates":
        return {"templates": runtime.TEMPLATES}
    if path.startswith("/api/company/"):
        rest = path[len("/api/company/") :].split("/")
        cid = rest[0]
        s = runtime.state(cid)
        if not s:
            return _err("no such company")
        if len(rest) == 1:
            return s
        sub = rest[1]
        if sub == "events":
            return {"events": ev.chain(cid, 500), "verify": ev.verify(cid)}
        if sub == "verify":
            return ev.verify(cid)
        if sub == "project":
            slug = rest[2] if len(rest) > 2 else None
            for p in s["projects"]:
                if p["slug"] == slug or p["id"] == slug:
                    return p
            return _err("no such project")
        return _err("unknown subpath")
    return _err("not found")


def handle_post(path, body):
    if path == "/api/companies":
        name = (body.get("name") or "").strip() or "Untitled Co"
        tpl = body.get("template") or "micro_saas"
        mission = (body.get("mission") or "").strip() or runtime.TEMPLATES[tpl]["default_mission"]
        sq = [s.strip() for s in (body.get("seed_queries") or "").splitlines() if s.strip()]
        c = runtime.incorporate(
            name,
            mission,
            capital_cents=int(round(float(body.get("capital") or 500) * 100)),
            max_experiment_cents=int(round(float(body.get("max_experiment") or 100) * 100)),
            board_threshold_cents=int(round(float(body.get("board_threshold") or 200) * 100)),
            template=tpl,
            mode=body.get("mode") or "moderate",
            model=(body.get("model") or "deepseek-chat").strip(),
            seed_queries=sq or None,
        )
        return {"ok": True, "company": c, "state": runtime.state(c["id"])}
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[0] == "api" and parts[1] == "company" and parts[3] == "tick":
        res = runtime.tick(parts[2], spend_round=True, queries=body.get("queries"))
        return {"ok": res.get("ok", False), "log": res.get("log"), "state": res.get("state")}
    if len(parts) == 5 and parts[0] == "api" and parts[1] == "company" and parts[3] == "project" and parts[4] == "evaluate":
        cid = parts[2]
        slug = body.get("slug")
        p = runtime.project_by_slug(cid, slug)
        if not p:
            return _err("no such project")
        out = runtime.evaluate(cid, p["id"])
        return {"ok": True, "result": out, "state": runtime.state(cid)}
    if len(parts) == 4 and parts[0] == "api" and parts[1] == "board" and parts[3] == "decide":
        rid = parts[2]
        cid = body.get("company_id")
        if not cid:
            return _err("company_id required")
        out = runtime.board_decide(cid, rid, bool(body.get("approve")), body.get("note") or "")
        return {**out, "state": runtime.state(cid)}
    if path == "/api/action":
        # raw actuator probe: used to demonstrate that the constitution bites
        cid, role, tool = body.get("company_id"), body.get("role"), body.get("tool")
        amount = int(body.get("amount_cents") or 0)
        args = body.get("args") or {"title": f"manual {tool} by {role}"}
        if not (cid and role and tool):
            return _err("company_id, role, tool required")
        v = runtime.act(cid, role, tool, args, amount, memo=body.get("memo") or f"manual {tool}")
        return {**v, "state": runtime.state(cid)}
    return _err("not found")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "descles-runtime"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload, ctype="application/json"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            f = WEB / "index.html"
            if not f.is_file():
                return self._send(500, "missing web/index.html", "text/plain")
            return self._send(200, f.read_bytes(), "text/html")
        if u.path.startswith("/api/"):
            try:
                return self._send(200, handle_get(u.path, u.query))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._send(500, _err(f"{type(e).__name__}: {e}"))
        p = (WEB / u.path.lstrip("/")).resolve()
        if str(p).startswith(str(WEB)) and p.is_file():
            ct = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            return self._send(200, p.read_bytes(), ct)
        return self._send(404, _err("not found"))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        try:
            return self._send(200, handle_post(urlparse(self.path).path, body))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, _err(f"{type(e).__name__}: {e}"))


def serve(port=None):
    port = port or config.PORT
    db.init()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"descles runtime  ->  {url}")
    print(f"db               ->  {config.DB_PATH}")
    missing = [k for m, (_, k) in config.PROVIDERS.items() if m in ("deepseek", "qwen") and not os.environ.get(k)]
    if missing:
        print(f"WARNING: {', '.join(missing)} not set — agents fall back to SIMULATED mode (clearly labelled)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
