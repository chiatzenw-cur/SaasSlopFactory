"""BUILD + DEPLOY. The model writes the copy; the code writes the page.

A landing page is the cheapest experiment that can falsify a hypothesis, so it is
the first real artifact the company produces. Two things are never faked here:

  * the CTA is only a working link when a payment rail is connected — otherwise it
    renders a visibly disabled state and the runtime files a setup request;
  * the page reports its own traffic back (a pixel, not a JS dependency), so the
    COO's success criterion is measured from events rather than asserted.
"""

import base64
import json
import os
import re
import urllib.error
import urllib.request

from . import capabilities as caps, config, db, events as ev, metrics as metrics_mod

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} — {headline}</title>
<meta name="description" content="{subheadline}">
<style>
:root{{--bg:#0b0d10;--fg:#e6edf5;--dim:#8b98a6;--accent:#5aa9ff;--card:#141a21;--line:#242c36}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
.wrap{{max-width:720px;margin:0 auto;padding:64px 24px 96px}}
h1{{font-size:38px;line-height:1.15;margin:0 0 16px;letter-spacing:-.02em}}
.sub{{font-size:19px;color:var(--dim);margin:0 0 32px}}
.price{{display:inline-block;font-size:15px;color:var(--dim);border:1px solid var(--line);border-radius:8px;padding:8px 14px;margin-bottom:24px}}
.cta{{display:inline-block;background:var(--accent);color:#04121f;font-weight:700;padding:15px 30px;border-radius:9px;text-decoration:none;font-size:17px}}
.cta.off{{background:#2a323c;color:#6b7683;cursor:not-allowed}}
.note{{font-size:13px;color:var(--dim);margin-top:12px}}
ul{{padding-left:20px}}li{{margin:9px 0}}
.faq{{margin-top:44px;border-top:1px solid var(--line);padding-top:24px}}
.faq h3{{font-size:15px;margin:20px 0 6px}}.faq p{{margin:0;color:var(--dim);font-size:15px}}
footer{{margin-top:56px;border-top:1px solid var(--line);padding-top:20px;font-size:13px;color:var(--dim)}}
.warn{{margin-top:14px;border:1px solid #5b4611;background:#191609;color:#fbbf24;border-radius:8px;padding:10px 14px;font-size:14px}}
</style></head><body><div class="wrap">
<h1>{headline}</h1>
<p class="sub">{subheadline}</p>
<div class="price">{price_label}</div>
<div>{cta}</div>
<div class="note">{cta_note}</div>
<ul>{bullets}</ul>
<div class="faq">{faq}</div>
<footer>{footer}<br>{disclosure}</footer>
</div>
<img src="{pixel}" width="1" height="1" alt="" style="position:absolute;left:-9999px">
</body></html>
"""


SOURCE_TAG = """
<script>
// attribute the visit to the channel that sent it; without JS the view is still counted
(function(){
  var s = location.search.match(/[?&]src=([A-Za-z0-9_-]{1,40})/);
  var img = document.querySelector('img[src*="pixel.gif"]');
  if (s && img) { img.src = img.src + '?src=' + s[1]; }
})();
</script>"""


def _copy_fallback(opp):
    name = opp.get("name") or "The product"
    return {
        "headline": (opp.get("one_liner") or name)[:110],
        "subheadline": (opp.get("problem") or "")[:220],
        "bullets": [
            "Does one job, does it fast",
            "No account needed to try it",
            "Cancel in one click",
        ],
        "cta_label": "Buy now",
        "faq": [
            {"q": "What do I get?", "a": "Access to the tool, immediately after payment."},
            {"q": "Refunds?", "a": "Full refund within 14 days, no questions."},
        ],
        "footer_note": "Built and operated by an autonomous company.",
    }


def _render(company, project, copy, base_url, checkout, paddle_js=None):
    price = int(copy.get("price_cents") or 0)
    slug = project["slug"]
    label = copy.get("cta_label") or "Buy now"
    script = ""
    if checkout:
        cta = f'<a class="cta" id="cta" href="{base_url}/api/p/{slug}/intent">{label}</a>'
        note = "You will be taken to the payment page."
        warn = ""
    elif paddle_js:
        # client-side checkout: only the PUBLIC token and price id are needed, so the
        # Buy button can be real without a server-side checkout link
        cta = f'<a class="cta" id="cta" href="#">{label} — ${price/100:.2f}</a>'
        note = "Payment opens in an overlay on this page."
        warn = ""
        script = """
<script src="https://cdn.paddle.com/paddle/v2/paddle.js"></script>
<script>
(function(){
  var P = window.Paddle;
  var a = document.getElementById("cta");
  if(!P){ a.className = "cta off"; a.textContent = "payment library blocked"; return; }
  %s
  P.Initialize({token: %s});
  a.addEventListener("click", function(ev){
    ev.preventDefault();
    try { navigator.sendBeacon(%s); } catch(e) {}
    P.Checkout.open({items:[{priceId: %s}], customData:{project_slug: %s}});
  });
})();
</script>""" % (
            "P.Environment.set('sandbox');" if paddle_js.get("sandbox") else "",
            json.dumps(paddle_js["token"]),
            json.dumps(f"{base_url}/api/p/{slug}/intent"),
            json.dumps(paddle_js["price_id"]),
            json.dumps(slug),
        )
    else:
        cta = f'<span class="cta off">{label} — not available yet</span>'
        note = "Payment is not connected yet. This button is disabled on purpose; the company has filed a setup request."
        warn = '<div class="warn">PAYMENT NOT CONNECTED — no payment rail is configured for this company.</div>'
    bullets = "".join(f"<li>{b}</li>" for b in (copy.get("bullets") or []))
    faq = "".join(f"<h3>{f.get('q','')}</h3><p>{f.get('a','')}</p>" for f in (copy.get("faq") or []))
    disclosure = (
        f"Operated by {company['name']}. Prices in USD. "
        "Nothing on this page is an endorsement or a guarantee of any outcome."
    )
    return PAGE.format(
        name=project["name"],
        headline=copy.get("headline") or project["name"],
        subheadline=copy.get("subheadline") or "",
        price_label=(f"{price/100:.2f} USD" if price else "price not set"),
        cta=cta,
        cta_note=note,
        bullets=bullets,
        faq=faq,
        footer=copy.get("footer_note") or "",
        disclosure=disclosure,
        pixel=f"{base_url}/api/p/{slug}/pixel.gif",
    ) + warn + script + SOURCE_TAG


def build(company, project, copy, base_url, price=None):
    """Write the artifact to disk. Returns the build row. No network, no excuses."""
    from . import billing

    slug = re.sub(r"[^a-z0-9-]", "", project["slug"].lower()) or "product"
    out = config.DATA / "portfolio" / slug
    out.mkdir(parents=True, exist_ok=True)
    checkout = billing.checkout_url(slug, copy.get("price_cents"))
    pj = billing.paddle_js_config((price or {}).get("price_id")) if not checkout else None
    if pj and (price or {}).get("source"):
        pj["source"] = price["source"]
    html = _render(company, project, copy, base_url, checkout, pj)
    (out / "index.html").write_text(html, encoding="utf-8")
    spec = {
        "slug": slug,
        "name": project["name"],
        "hypothesis": project["hypothesis"],
        "success_criterion": project["success_criterion"],
        "kill_criterion": project["kill_criterion"],
        "copy": copy,
        "checkout_connected": bool(checkout or pj),
        "checkout_mode": "link" if checkout else ("paddle_js" if pj else "disabled"),
        "price_id": (pj or {}).get("price_id"),
        "price_source": (pj or {}).get("source"),
        "price_id_borrowed": (pj or {}).get("borrowed_price_id"),
    }
    (out / "product.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "README.md").write_text(
        f"# {project['name']}\n\n{project['hypothesis']}\n\n"
        f"- success: {project['success_criterion']}\n- kill: {project['kill_criterion']}\n"
        f"- local preview: {base_url}/p/{slug}\n",
        encoding="utf-8",
    )
    bid = "build_" + ev.now().replace("-", "").replace(":", "") + "_" + slug[:10]
    files = ["index.html", "product.json", "README.md"]
    db.ex(
        "INSERT INTO builds(id,company_id,project_id,slug,ts,path,url,files,copy,deploy_state,price_id,price_source)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (bid, company["id"], project["id"], slug, ev.now(), str(out),
         f"{base_url}/p/{slug}", json.dumps(files), json.dumps(copy, ensure_ascii=False), "LOCAL",
         (pj or {}).get("price_id"), (pj or {}).get("source")),
    )
    ev.append(company["id"], "CTO", "PRODUCT_BUILT", {
        "slug": slug, "path": str(out), "files": files,
        "checkout_connected": bool(checkout or pj), "build_id": bid,
        "checkout_mode": spec["checkout_mode"], "price_id": spec["price_id"],
        "price_source": spec["price_source"]})
    db.ex("UPDATE projects SET stage='BUILT_LOCAL', updated_at=? WHERE id=?", (ev.now(), project["id"]))
    if not capabilities_ok(company["id"]):
        caps.require(company["id"], "payment_rail",
                     "the page is built but its Buy button cannot lead anywhere", project["id"])
    return {"build_id": bid, "path": str(out), "index": str(out / "index.html"),
            "url": f"{base_url}/p/{slug}", "checkout": checkout}


def capabilities_ok(company_id):
    from . import billing
    return billing.rail_connected()


# ------------------------------------------------------------------ deploy


def deploy(company, project, build_row, base_url):
    """Deploy the built page. Without a host capability this files a setup request
    and the project stays BUILT_LOCAL — it does not pretend to be live."""
    token = os.environ.get("VERCEL_TOKEN")
    if not token:
        caps.require(company["id"], "deploy_host",
                     f"{project['name']} is built and has a local preview but no visitor can reach it",
                     project["id"])
        db.ex("UPDATE builds SET deploy_state='BLOCKED_NO_HOST' WHERE id=?", (build_row["id"],))
        ev.append(company["id"], "CTO", "DEPLOY_BLOCKED", {
            "slug": build_row["slug"], "reason": "no deploy host capability", "local_url": build_row["url"]})
        return {"ok": False, "blocked": "deploy_host", "local_url": build_row["url"]}

    root = config.DATA / "portfolio" / build_row["slug"]
    files = []
    for f in json.loads(build_row["files"] or "[]"):
        p = root / f
        if p.is_file():
            files.append({"file": f, "data": p.read_text(encoding="utf-8")})
    body = {
        "name": f"descles-{build_row['slug']}",
        "files": files,
        "target": "production",
        "projectSettings": {"framework": None},
    }
    req = urllib.request.Request(
        "https://api.vercel.com/v13/deployments?forceNew=1",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        ev.append(company["id"], "CTO", "DEPLOY_FAILED", {"slug": build_row["slug"], "status": e.code, "detail": detail})
        db.ex("UPDATE builds SET deploy_state='FAILED' WHERE id=?", (build_row["id"],))
        return {"ok": False, "error": f"HTTP {e.code}: {detail}"}
    except Exception as e:  # noqa: BLE001
        ev.append(company["id"], "CTO", "DEPLOY_FAILED", {"slug": build_row["slug"], "detail": str(e)[:300]})
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    url = "https://" + (out.get("url") or out.get("alias", [""])[0] or "")
    db.ex("UPDATE builds SET deploy_state='DEPLOYED', deploy_url=? WHERE id=?", (url, build_row["id"]))
    db.ex("UPDATE projects SET stage='LIVE', updated_at=? WHERE id=?", (ev.now(), project["id"]))
    ev.append(company["id"], "CTO", "PRODUCT_DEPLOYED", {"slug": build_row["slug"], "url": url, "deployment_id": out.get("id")})
    return {"ok": True, "url": url}
