"""Signal sources, with their legality stated in the registry.

A company that mines demand must not be built on a source that can kill it. Every
source below therefore carries a tier and a commercial-use verdict, and the three
that are forbidden are listed WITH the reason rather than silently omitted —
"we do not use Reddit" is a design decision the shareholder is entitled to see.

Tiers
  official_api    provider-published API, commercial use permitted
  public_feed     provider-published feed/RSS, commercial use permitted
  licensed_only   the data exists, but commercial use needs a contract
  forbidden       commercial use prohibited by the provider's terms
  undocumented    no published API; widely used, no permission granted — labelled as such

The B2B blind spot this fixes: a lot of B2B/SaaS products are not on any app store,
so app-store review mining sees nothing. For those, the machine-readable buying
signals are JOB POSTINGS (somebody already allocated salary to the problem),
competitor GitHub issues (people publicly saying what is broken), and search/
community demand — not reviews.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "descles-runtime/0.1 (+local corporate runtime)"
_cache = {}
TTL = 900


# ------------------------------------------------------------------ registry

REGISTRY = {
    "hn": {"label": "Hacker News (Algolia)", "tier": "official_api", "auth": None,
           "commercial_ok": True, "use": "requests, complaints, Show HN, hiring threads"},
    "stackexchange": {"label": "Stack Exchange API", "tier": "official_api", "auth": "optional key (300/day without)",
                      "commercial_ok": True, "use": "developer questions that name the missing tool",
                      "note": "content is CC-BY-SA — attribution required if republished"},
    "github_issues": {"label": "GitHub issues & discussions", "tier": "official_api",
                      "auth": "optional token (60/hr without)", "commercial_ok": True,
                      "use": "the strongest B2B pain signal: people saying exactly what is broken"},
    "itunes_reviews": {"label": "Apple App Store reviews (RSS)", "tier": "official_api", "auth": None,
                       "commercial_ok": True, "use": "only for products that HAVE an app"},
    "itunes_search": {"label": "Apple App Store supply", "tier": "official_api", "auth": None,
                      "commercial_ok": True, "use": "who already ships in this space"},
    "youtube_api": {"label": "YouTube Data API", "tier": "official_api", "auth": "free key",
                    "commercial_ok": True, "use": "comments under tutorials = workarounds people hate"},
    "producthunt": {"label": "Product Hunt GraphQL", "tier": "official_api", "auth": "free token",
                    "commercial_ok": True, "use": "launch supply and reception"},
    "jobboards": {"label": "RemoteOK / WeWorkRemotely / HN hiring", "tier": "public_feed", "auth": None,
                  "commercial_ok": True,
                  "use": "B2B budget signal — a company hiring a person for a task has already funded the problem"},
    "google_trends": {"label": "Google Trends RSS", "tier": "public_feed", "auth": None,
                      "commercial_ok": True, "use": "what is rising this week"},
    "google_autocomplete": {"label": "Google autocomplete", "tier": "undocumented", "auth": None,
                            "commercial_ok": None,
                            "use": "search demand and exact phrasing",
                            "note": "no published API and no permission granted; widely used, low practical risk, "
                                    "but it is not a licence and could change without notice"},
    "youtube_autocomplete": {"label": "YouTube autocomplete", "tier": "undocumented", "auth": None,
                             "commercial_ok": None, "use": "same, for video intent"},
    "reddit": {"label": "Reddit", "tier": "licensed_only", "auth": "paid Data API licence",
               "commercial_ok": False,
               "why": "Reddit's Data API Terms require a commercial licence and prohibit scraping as a "
                      "contractual matter. GummySearch (135k users, ~$5k/mo) could not reach a licence "
                      "agreement and shut down to new signups on 2025-11-30. A commercial product built "
                      "here can be forced to close the same way.",
               "use_instead": ["hn", "stackexchange", "github_issues"]},
    "g2_capterra": {"label": "G2 / Capterra reviews", "tier": "forbidden", "auth": None,
                    "commercial_ok": False,
                    "why": "no public API; their terms prohibit scraping. These are exactly the B2B review "
                           "sites you would want, and they are exactly the ones you cannot take.",
                    "use_instead": ["github_issues", "jobboards", "stackexchange"]},
    "google_play": {"label": "Google Play reviews", "tier": "forbidden", "auth": None,
                    "commercial_ok": False,
                    "why": "no public reviews API; scraping violates the terms. The Play Developer API exposes "
                           "reviews only for apps you own.",
                    "use_instead": ["itunes_reviews", "github_issues"]},
    "amazon": {"label": "Amazon reviews", "tier": "forbidden", "auth": None, "commercial_ok": False,
               "why": "no public reviews API; scraping violates the terms",
               "use_instead": ["hn", "jobboards"]},
}


_OPENER = {"key": None, "obj": None}


def _opener():
    """Cache one opener. Building a fresh urllib opener per call exhausts ephemeral
    ports (WinError 10048) during a sweep of hundreds of requests."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY")
    if _OPENER["obj"] is not None and _OPENER["key"] == proxy:
        return _OPENER["obj"]
    if proxy:
        obj = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        obj = urllib.request.build_opener()
    _OPENER["key"], _OPENER["obj"] = proxy, obj
    return obj


def get(url, timeout=20, headers=None, cache=True, attempts=3):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    if cache and url in _cache:
        ts, out = _cache[url]
        if time.time() - ts < TTL:
            return out
    out = None
    for i in range(attempts):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers=h)
            with _opener().open(req, timeout=timeout) as r:
                raw = r.read()
            out = {"ok": True, "status": 200, "ms": int((time.time() - t0) * 1000), "body": raw}
        except urllib.error.HTTPError as e:
            out = {"ok": False, "status": e.code, "ms": int((time.time() - t0) * 1000), "body": b"",
                   "error": str(e)}
            break  # a 403/429 is a finding — do not retry it away
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "status": None, "ms": int((time.time() - t0) * 1000), "body": b"",
                   "error": f"{type(e).__name__}: {e}"}
            if "10048" in str(e) or "10055" in str(e):
                time.sleep(0.7 * (i + 1))
                continue
            break
        break
    if cache:
        _cache[url] = (time.time(), out)
    return out


def _clean(html):
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = (txt.replace("&#x27;", "'").replace("&quot;", '"').replace("&amp;", "&")
              .replace("&gt;", ">").replace("&lt;", "<").replace("&#x2F;", "/").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", txt).strip()


def _phrase_hit(text, phrase):
    words = re.findall(r"[a-z0-9']+", phrase.lower())
    if not words:
        return False
    hay = re.findall(r"[a-z0-9']+", text.lower())
    i = 0
    for w in hay:
        if w == words[i]:
            i += 1
            if i == len(words):
                return True
    return False


def _window(text, phrase, before=220, after=480):
    words = re.findall(r"[a-z0-9']+", phrase.lower())
    if not words:
        return text
    hay = [(m.start(), m.group(0).lower()) for m in re.finditer(r"[A-Za-z0-9']+", text)]
    i = 0
    for pos, w in hay:
        if w == words[i]:
            i += 1
            if i == len(words):
                end = pos + len(w)
                return ("…" if pos - before > 0 else "") + text[max(0, pos - before): end + after].strip()
        else:
            i = 1 if w == words[0] else 0
    return text


# ------------------------------------------------------------------ fetchers


def hn(query, tags="comment", limit=20):
    q = urllib.parse.quote(query)
    url = (f"https://hn.algolia.com/api/v1/search?query={q}&tags={tags}"
           f"&hitsPerPage={limit}&numericFilters=created_at_i%3E{int(time.time())-3*365*86400}")
    r = get(url)
    if not r["ok"]:
        return [], {"ok": False, "status": r.get("status"), "error": r.get("error"), "ms": r["ms"]}
    try:
        data = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return [], {"ok": False, "status": 200, "error": f"bad json: {e}", "ms": r["ms"]}
    out, scanned = [], 0
    for h in data.get("hits", []):
        scanned += 1
        text = _clean(h.get("comment_text") or h.get("story_text") or "")
        if len(text) < 40:
            continue
        if tags == "comment" and not _phrase_hit(text, query):
            continue
        oid = h.get("objectID")
        out.append({"id": f"hn:{oid}", "source": "hn", "kind": tags,
                    "title": _clean(h.get("title") or h.get("story_title") or "")[:160],
                    "text": _window(text, query)[:700],
                    "url": f"https://news.ycombinator.com/item?id={oid}",
                    "points": h.get("points") or 0, "created_at": h.get("created_at"), "query": query})
    return out, {"ok": True, "status": 200, "n": len(out), "scanned": scanned, "ms": r["ms"]}


def stackexchange(query, limit=15, site="stackoverflow"):
    """Official API. Bodies come back as markdown; questions are demand, not opinion."""
    url = ("https://api.stackexchange.com/2.3/search/advanced?"
           + urllib.parse.urlencode({"order": "desc", "sort": "relevance", "q": query,
                                     "site": site, "pagesize": limit, "filter": "withbody"}))
    if os.environ.get("STACKEXCHANGE_KEY"):
        url += "&key=" + os.environ["STACKEXCHANGE_KEY"]
    r = get(url)
    if not r["ok"]:
        return [], {"ok": False, "status": r.get("status"), "error": r.get("error"), "ms": r["ms"]}
    try:
        data = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return [], {"ok": False, "status": 200, "error": f"bad json: {e}", "ms": r["ms"]}
    if "error_message" in data:
        return [], {"ok": False, "status": 200, "error": data["error_message"], "ms": r["ms"]}
    out = []
    for it in data.get("items", []):
        body = _clean(it.get("body") or "")
        if len(body) < 60:
            continue
        out.append({"id": f"se:{it.get('question_id')}", "source": "stackexchange", "kind": site,
                    "title": _clean(it.get("title") or "")[:160],
                    "text": body[:700], "url": it.get("link"),
                    "points": it.get("score") or 0, "created_at": None, "query": query,
                    "answers": it.get("answer_count")})
    return out, {"ok": True, "status": 200, "n": len(out), "ms": r["ms"]}


def github_issues(query, limit=15, repo=None):
    """Official API (60 req/hr unauthenticated, 10 searches/min). For B2B this is the
    best pain source there is: an open issue on a competitor is a customer saying what
    is broken. Query is quoted so search does not fan out into unrelated repos."""
    terms = " ".join(f'"{t}"' for t in str(query).split() if len(t) > 3) or str(query)
    scope = f" repo:{repo}" if repo else ""
    q = urllib.parse.quote(f"{terms} is:issue in:title,body{scope}")
    url = f"https://api.github.com/search/issues?q={q}&sort=reactions&order=desc&per_page={limit}"
    headers = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = "Bearer " + tok
    r = get(url, headers=headers)
    if not r["ok"]:
        hint = " — unauthenticated GitHub search allows 10 requests/minute" if r.get("status") == 403 else ""
        return [], {"ok": False, "status": r.get("status"), "error": (r.get("error") or "") + hint, "ms": r["ms"]}
    try:
        data = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return [], {"ok": False, "status": 200, "error": f"bad json: {e}", "ms": r["ms"]}
    out = []
    for it in data.get("items", []):
        body = _clean(it.get("body") or "")
        if len(body) < 60:
            continue
        repo_name = (it.get("repository_url") or "").replace("https://api.github.com/repos/", "")
        out.append({"id": f"gh:{it.get('id')}", "source": "github", "kind": "issue",
                    "title": _clean(it.get("title") or "")[:160], "text": body[:700],
                    "url": it.get("html_url"), "points": (it.get("reactions") or {}).get("total_count") or 0,
                    "created_at": (it.get("created_at") or "")[:10], "query": query,
                    "repo": repo_name, "comments": it.get("comments")})
    return out, {"ok": True, "status": 200, "n": len(out), "ms": r["ms"]}


def itunes_search(term, limit=10):
    """Official: who already ships in this space."""
    url = ("https://itunes.apple.com/search?"
           + urllib.parse.urlencode({"term": term, "entity": "software", "limit": limit}))
    r = get(url)
    if not r["ok"]:
        return [], {"ok": False, "status": r.get("status"), "error": r.get("error"), "ms": r["ms"]}
    try:
        data = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return [], {"ok": False, "status": 200, "error": f"bad json: {e}", "ms": r["ms"]}
    out = []
    for it in data.get("results", []):
        out.append({"id": f"itunes:{it.get('trackId')}", "source": "itunes", "kind": "app",
                    "title": it.get("trackName") or "", "text": _clean(it.get("description") or "")[:400],
                    "url": it.get("trackViewUrl"), "points": it.get("userRatingCount") or 0,
                    "created_at": (it.get("currentVersionReleaseDate") or "")[:10], "query": term,
                    "rating": it.get("averageUserRating"), "price": it.get("formattedPrice")})
    return out, {"ok": True, "status": 200, "n": len(out), "ms": r["ms"]}


def itunes_reviews(app_id, limit=40):
    """Official RSS. Only for products that actually have an app — which is the point:
    a lot of B2B/SaaS has none, so this source is the wrong one for them."""
    url = f"https://itunes.apple.com/us/rss/customerreviews/id={app_id}/sortBy=mostRecent/json"
    r = get(url)
    if not r["ok"]:
        return [], {"ok": False, "status": r.get("status"), "error": r.get("error"), "ms": r["ms"]}
    try:
        data = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return [], {"ok": False, "status": 200, "error": f"bad json: {e}", "ms": r["ms"]}
    out = []
    for e in (data.get("feed", {}).get("entry") or [])[:limit]:
        if "im:rating" not in e:
            continue
        body = (e.get("content") or {}).get("label") or ""
        out.append({"id": f"itunesrev:{e.get('id', {}).get('label')}", "source": "itunes_reviews",
                    "kind": "review", "title": (e.get("title") or {}).get("label") or "",
                    "text": _clean(body)[:600], "url": (e.get("author") or {}).get("uri", {}).get("label"),
                    "points": int((e.get("im:rating") or {}).get("label") or 0),
                    "created_at": None, "query": str(app_id)})
    return out, {"ok": True, "status": 200, "n": len(out), "ms": r["ms"]}


def autocomplete(query, engine="google"):
    """Undocumented endpoint. Labelled as such everywhere it is used: it is not a
    licence, and search demand is intent, not payment."""
    ds = "&ds=yt" if engine == "youtube" else ""
    url = ("https://suggestqueries.google.com/complete/search?client=firefox"
           f"{ds}&q={urllib.parse.quote(query)}")
    r = get(url)
    if not r["ok"]:
        return [], {"ok": False, "status": r.get("status"), "error": r.get("error"), "ms": r["ms"]}
    try:
        data = json.loads(r["body"].decode("utf-8", "replace"))
        suggestions = data[1]
    except Exception as e:  # noqa: BLE001
        return [], {"ok": False, "status": 200, "error": f"unexpected shape: {e}", "ms": r["ms"]}
    out = []
    for i, s in enumerate(suggestions or []):
        s = str(s).strip()
        if len(s) < 6:
            continue
        out.append({"id": f"ac:{engine}:{urllib.parse.quote(s)[:40]}", "source": engine + "_autocomplete",
                    "kind": "search_suggestion", "title": s, "text": f"people search: {s}",
                    "url": f"https://www.google.com/search?q={urllib.parse.quote(s)}",
                    "points": max(0, 10 - i), "created_at": None, "query": query})
    return out, {"ok": True, "status": 200, "n": len(out), "ms": r["ms"], "tier": "undocumented"}


def jobboards(query, limit=15):
    """Public, keyless budget signal: the monthly "Who is hiring" threads. A company
    hiring a person for a task has already funded the problem — which no review site
    tells you, and which is the signal that actually matters for B2B.

    RemoteOK's /api now answers with HTML, so it is reported dead rather than parsed.
    """
    status = {}
    out = []
    # 1. find the most recent hiring thread
    q = urllib.parse.quote("Ask HN: Who is hiring?")
    r = get(f"https://hn.algolia.com/api/v1/search_by_date?query={q}&tags=story&hitsPerPage=1")
    story_id = None
    if r["ok"]:
        try:
            hits = json.loads(r["body"].decode("utf-8", "replace")).get("hits", [])
            if hits:
                story_id = hits[0]["objectID"]
        except Exception as e:  # noqa: BLE001
            status["hn_hiring_thread"] = {"ok": False, "status": 200, "error": str(e), "ms": r["ms"]}
    else:
        status["hn_hiring_thread"] = {"ok": False, "status": r.get("status"), "error": r.get("error"), "ms": r["ms"]}
    if story_id:
        r2 = get(f"https://hn.algolia.com/api/v1/search?tags=comment,story_{story_id}&hitsPerPage=400")
        if not r2["ok"]:
            status["hn_hiring_comments"] = {"ok": False, "status": r2.get("status"),
                                            "error": r2.get("error"), "ms": r2["ms"]}
        else:
            try:
                hits = json.loads(r2["body"].decode("utf-8", "replace")).get("hits", [])
                scanned = 0
                for h in hits:
                    text = _clean(h.get("comment_text") or "")
                    if len(text) < 80:
                        continue
                    scanned += 1
                    if query and not _phrase_hit(text, query):
                        continue
                    oid = h.get("objectID")
                    out.append({"id": f"hnjob:{oid}", "source": "jobboards", "kind": "job",
                                "title": text[:90], "text": _window(text, query)[:700],
                                "url": f"https://news.ycombinator.com/item?id={oid}",
                                "points": 0, "created_at": h.get("created_at"), "query": query,
                                "thread": f"https://news.ycombinator.com/item?id={story_id}"})
                    if len(out) >= limit:
                        break
                status["hn_hiring_comments"] = {"ok": True, "status": 200, "n": len(out),
                                                "scanned": scanned, "ms": r2["ms"], "thread": story_id}
            except Exception as e:  # noqa: BLE001
                status["hn_hiring_comments"] = {"ok": False, "status": 200, "error": str(e), "ms": r2["ms"]}
    # 2. RemoteOK is reported, not silently dropped
    r3 = get("https://remoteok.com/api")
    status["remoteok"] = {"ok": False, "status": r3.get("status"),
                          "error": "endpoint no longer returns JSON — reported dead, not parsed"}
    return out, status


FETCHERS = {
    "hn": lambda q, n: hn(q, "comment", n),
    "stackexchange": lambda q, n: stackexchange(q, n),
    "github_issues": lambda q, n: github_issues(q, n),
    "itunes_search": lambda q, n: itunes_search(q, n),
    "google_autocomplete": lambda q, n: autocomplete(q, "google"),
    "youtube_autocomplete": lambda q, n: autocomplete(q, "youtube"),
    "jobboards": lambda q, n: jobboards(q, n),
}

# what the CMO may sweep, by company template. Reddit/G2/Play are absent on purpose.
SOURCES_BY_TEMPLATE = {
    "micro_saas": ["hn", "google_autocomplete", "stackexchange", "github_issues", "itunes_search"],
    "leadgen": ["jobboards", "hn", "github_issues"],
    "content": ["hn", "youtube_autocomplete", "google_autocomplete"],
    "b2b_saas": ["github_issues", "jobboards", "stackexchange", "hn"],
}


def sweep(queries, sources=None, per_query=15):
    """Run every allowed source for every query. A dead source reports its status."""
    sources = sources or ["hn"]
    seen, status = {}, {}
    for q in queries:
        for name in sources:
            fn = FETCHERS.get(name)
            if not fn:
                status[f"{name}"] = {"ok": False, "error": "not implemented", "tier": "?"}
                continue
            try:
                sigs, st = fn(q, per_query)
            except Exception as e:  # noqa: BLE001
                sigs, st = [], {"ok": False, "error": f"{type(e).__name__}: {e}"}
            st = {**st, "tier": (REGISTRY.get(name) or {}).get("tier"),
                  "commercial_ok": (REGISTRY.get(name) or {}).get("commercial_ok")}
            status[f"{name}:{q}"] = st
            for s in sigs:
                seen.setdefault(s["id"], s)
    return list(seen.values()), status


def source_report():
    """What this runtime will and will not mine, and why — for the console."""
    rows = []
    for name, spec in REGISTRY.items():
        rows.append({"name": name, "implemented": name in FETCHERS, **spec})
    return rows
