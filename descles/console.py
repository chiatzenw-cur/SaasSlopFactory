"""Local corporate console. stdlib only, no build step, no CDN.

  python server.py            -> http://127.0.0.1:7310
"""

import json
import mimetypes
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import billing, capabilities as caps, config, db, events as ev, policy, roles, runtime, secrets
from . import signals as sigmod

WEB = config.ROOT / "descles" / "web"
GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
    b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def _err(msg):
    return {"error": str(msg)}


def _company_for_webhook(cid=None):
    if cid:
        return runtime.get(cid)
    cs = runtime.companies()
    return runtime.get(cs[0]["id"]) if len(cs) == 1 else None


def handle_get(path, q):
    if path == "/api/health":
        return {
            "ok": True,
            "db": str(config.DB_PATH),
            "models": {m: bool(os.environ.get(k)) for m, (_, k) in config.PROVIDERS.items()},
            "companies": len(runtime.companies()),
            "payment_rail": billing.rail_connected(),
            "deploy_host": bool(os.environ.get("VERCEL_TOKEN")),
            "base_url": runtime.base_url(),
        }
    if path == "/api/companies":
        return {"companies": runtime.companies()}
    if path == "/api/templates":
        return {"templates": runtime.TEMPLATES}
    if path == "/api/sources":
        return {"sources": sigmod.source_report(),
                "by_template": sigmod.SOURCES_BY_TEMPLATE}
    if path == "/api/why":
        # the boundaries, stated by the code that enforces them
        return {
            "roles": {
                r: {"permits": s["permits"], "objectives": s["objectives"], "metrics": s["metrics"]}
                for r, s in roles.ROLE_SPECS.items()
            },
            "clauses": roles.CLAUSE_FORBIDS,
            "capabilities": {
                k: {kk: vv for kk, vv in v.items() if kk in ("label", "mode", "why", "human_kind", "url", "steps")}
                for k, v in caps.CAPABILITIES.items()
            },
        }
    if path.startswith("/api/company/"):
        rest = path[len("/api/company/"):].split("/")
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
        if sub == "report":
            slug = rest[2] if len(rest) > 2 else None
            r = runtime.shareholder_report(cid) if slug in (None, "shareholder") else runtime.project_report(cid, slug)
            return r or _err("no report yet — the CFO writes one when a project is evaluated")
        if sub == "project":
            slug = rest[2] if len(rest) > 2 else None
            for p in s["projects"]:
                if p["slug"] == slug or p["id"] == slug:
                    return p
            return _err("no such project")
        return _err("unknown subpath")
    return _err("not found")


def handle_post(path, raw, body):
    if path == "/api/companies":
        name = (body.get("name") or "").strip() or "Untitled Co"
        tpl = body.get("template") or "micro_saas"
        mission = (body.get("mission") or "").strip() or runtime.TEMPLATES[tpl]["default_mission"]
        sq = [s.strip() for s in (body.get("seed_queries") or "").splitlines() if s.strip()]
        c = runtime.incorporate(
            name, mission,
            capital_cents=int(round(float(body.get("capital") or 500) * 100)),
            max_experiment_cents=int(round(float(body.get("max_experiment") or 100) * 100)),
            board_threshold_cents=int(round(float(body.get("board_threshold") or 200) * 100)),
            template=tpl, mode=body.get("mode") or "moderate",
            model=(body.get("model") or "deepseek-chat").strip(),
            seed_queries=sq or None,
        )
        return {"ok": True, "company": c, "state": runtime.state(c["id"])}

    parts = path.strip("/").split("/")

    # ---- payment webhooks: the only path that can write a REVENUE row
    if len(parts) >= 3 and parts[0] == "api" and parts[1] == "webhook":
        provider = parts[2]
        cid = parts[3] if len(parts) > 3 else None
        c = _company_for_webhook(cid)
        if not c:
            return {"error": "cannot resolve company for webhook — use /api/webhook/<provider>/<company_id>"}
        try:
            return billing.handle_webhook(c["id"], provider, raw, {k.lower(): v for k, v in body.get("_headers", {}).items()})
        except billing.SignatureError as e:
            return {"error": f"signature rejected: {e}"}

    if len(parts) == 4 and parts[0] == "api" and parts[1] == "company":
        cid, sub = parts[2], parts[3]
        if sub == "tick":
            res = runtime.tick(cid, spend_round=True, queries=body.get("queries"))
            return {"ok": res.get("ok", False), "log": res.get("log"), "state": res.get("state")}
        if sub == "sync":
            caps.sync(cid)
            return {"ok": True, "state": runtime.state(cid)}
        if sub == "sync-revenue":
            out = runtime.reconcile_revenue(cid, body.get("provider"))
            return {**out, "state": runtime.state(cid)}

    if len(parts) == 5 and parts[0] == "api" and parts[1] == "company" and parts[3] == "project":
        cid, slug, what = parts[2], body.get("slug"), parts[4]
        p = runtime.project_by_slug(cid, slug)
        if not p:
            return _err("no such project")
        if what == "evaluate":
            return {"ok": True, "result": runtime.evaluate(cid, p["id"]), "state": runtime.state(cid)}
        if what == "build":
            return {"ok": True, "result": runtime.build_project(cid, p["id"]), "state": runtime.state(cid)}
        if what == "deploy":
            return {"ok": True, "result": runtime.deploy_project(cid, p["id"]), "state": runtime.state(cid)}
        if what == "gtm":
            return {"ok": True, "result": runtime.plan_gtm(cid, p["id"]), "state": runtime.state(cid)}
        if what == "recompute":
            return {"ok": True, "result": runtime.recompute_economics(cid, p["id"]), "state": runtime.state(cid)}
        return _err("unknown project action")

    if len(parts) == 4 and parts[0] == "api" and parts[1] == "board" and parts[3] == "decide":
        cid = body.get("company_id")
        if not cid:
            return _err("company_id required")
        out = runtime.board_decide(cid, parts[2], bool(body.get("approve")), body.get("note") or "")
        return {**out, "state": runtime.state(cid)}

    if len(parts) == 4 and parts[0] == "api" and parts[1] == "setup" and parts[3] in ("grant", "dismiss", "submit"):
        cid = body.get("company_id")
        if not cid:
            return _err("company_id required")
        rid = parts[2]
        if parts[3] == "submit":
            r = db.row("SELECT * FROM setup_requests WHERE id=? AND company_id=?", (rid, cid))
            if not r:
                return _err("no such setup request")
            values = {k: v for k, v in (body.get("values") or {}).items()
                      if k in {f["name"] for f in secrets.FIELDS.get(r["capability"], [])}}
            stored = secrets.store(values)
            caps.sync(cid)
            probe = secrets.verify(r["capability"])
            ev.append(cid, "BOARD", "SETUP_SUBMITTED", {
                "capability": r["capability"], "request_id": rid,
                "fields_stored": stored, "probe": probe})
            if probe.get("ok"):
                out = caps.grant(cid, rid, probe.get("detail") or "verified via API")
                out["verified"] = probe
            else:
                out = {"ok": False, "verified": probe, "stored": stored,
                       "error": "stored, but the provider rejected it — nothing was marked connected"}
            return {**out, "state": runtime.state(cid), "fields": secrets.fields_for(r["capability"])}
        if parts[3] == "grant":
            out = caps.grant(cid, rid, body.get("note") or "")
        else:
            db.ex("UPDATE setup_requests SET status='DISMISSED', resolved_at=?, note=? WHERE id=? AND company_id=?",
                  (ev.now(), body.get("note") or "", rid, cid))
            out = {"ok": True, "status": "DISMISSED"}
        return {**out, "state": runtime.state(cid)}

    if path == "/api/action":
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
        p = u.path
        try:
            if p in ("/", "/index.html"):
                f = WEB / "index.html"
                if not f.is_file():
                    return self._send(500, "missing web/index.html", "text/plain")
                return self._send(200, f.read_bytes(), "text/html")

            # ---- a company's built product, served for real
            if p.startswith("/p/"):
                slug = p[len("/p/"):].strip("/") or "index.html"
                f = (config.DATA / "portfolio" / slug / "index.html")
                if not f.is_file():
                    return self._send(404, "no build for " + slug, "text/plain")
                return self._send(200, f.read_bytes(), "text/html")

            if p.startswith("/api/p/"):
                bits = p[len("/api/p/"):].split("/")
                slug = bits[0]
                what = bits[1] if len(bits) > 1 else ""
                cs = runtime.companies()
                cid = u.query.split("c=")[-1] if "c=" in u.query else (cs[0]["id"] if len(cs) == 1 else None)
                if not cid:
                    return self._send(404, "cannot resolve company", "text/plain")
                if what == "pixel.gif":
                    src = None
                    if "src=" in u.query:
                        src = u.query.split("src=")[-1].split("&")[0][:40]
                    runtime.record_page_view(cid, slug, src)
                    return self._send(200, GIF, "image/gif")
                if what == "intent":
                    out = runtime.record_pricing_intent(cid, slug)
                    url = billing.checkout_url(slug)
                    if url:
                        self.send_response(302)
                        self.send_header("Location", url)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    page = (
                        "<html><body style='font:16px system-ui;background:#0b0d10;color:#e6edf5;padding:60px'>"
                        "<h2>Payment is not connected</h2>"
                        "<p>Your click was recorded as pricing intent"
                        f" ({json.dumps(out.get('funnel', {}))}).</p>"
                        "<p>No payment rail is configured, so there is nowhere to send you. "
                        "The company has filed a setup request.</p></body></html>"
                    )
                    return self._send(200, page, "text/html")
                return self._send(404, "unknown", "text/plain")

            if p.startswith("/api/"):
                return self._send(200, handle_get(p, u.query))

            f = (WEB / p.lstrip("/")).resolve()
            if str(f).startswith(str(WEB)) and f.is_file():
                ct = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                return self._send(200, f.read_bytes(), ct)
            return self._send(404, _err("not found"))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, _err(f"{type(e).__name__}: {e}"))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        # the raw body is what a signature is computed over — never re-serialise it
        body["_headers"] = {k: v for k, v in self.headers.items()}
        try:
            return self._send(200, handle_post(urlparse(self.path).path, raw, body))
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
    if not billing.rail_connected():
        print("payment rail     ->  NOT CONNECTED (Buy buttons render disabled; runtime files a setup request)")
    if not os.environ.get("VERCEL_TOKEN"):
        print("deploy host      ->  NOT CONNECTED (builds stay local; runtime files a setup request)")
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
