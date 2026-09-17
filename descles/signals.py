"""Real buying-signal sources. Evidence must be fetchable, not invented.

v1 sources (no API key required):
  hn        Hacker News via Algolia  (stories + comments, real quotes, real URLs)
  rss       any RSS/Atom feed
Reddit/ProductHunt need OAuth -> not in v1. A failed source reports ok:false and
a status code; it never degrades into a silent empty list.
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


def _opener():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY")
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def get(url, timeout=20, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    key = url
    if key in _cache:
        return _cache[key]
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=h)
        with _opener().open(req, timeout=timeout) as r:
            raw = r.read()
        out = {"ok": True, "status": 200, "ms": int((time.time() - t0) * 1000), "body": raw}
    except urllib.error.HTTPError as e:
        out = {"ok": False, "status": e.code, "ms": int((time.time() - t0) * 1000), "body": b"", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "status": None, "ms": int((time.time() - t0) * 1000), "body": b"", "error": f"{type(e).__name__}: {e}"}
    _cache[key] = out
    return out


def _clean(html):
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = (
        txt.replace("&#x27;", "'").replace("&quot;", '"').replace("&amp;", "&")
        .replace("&gt;", ">").replace("&lt;", "<").replace("&#x2F;", "/").replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", txt).strip()


def _phrase_hit(text, phrase):
    """Loose in-order phrase match: 'what would you pay for' hits
    'what would you actually pay for'. Keeps the sweeper from returning
    tangentially-related traffic as a buying signal."""
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
    """Centre the excerpt on the matched phrase — the evidence should show the
    buying intent, not the first 700 characters of a thread."""
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
                return ("…" if pos - before > 0 else "") + text[max(0, pos - before) : end + after].strip()
        else:
            i = 1 if w == words[0] else 0
    return text


def hn(query, tags="comment", limit=20):
    """Returns (signals, status). Only in-order phrase hits count as signals."""
    q = urllib.parse.quote(query)
    url = (
        f"https://hn.algolia.com/api/v1/search?query={q}&tags={tags}"
        f"&hitsPerPage={limit}&numericFilters=created_at_i%3E{int(time.time())-3*365*86400}"
    )
    r = get(url)
    if not r["ok"]:
        return [], {"ok": False, "status": r.get("status"), "error": r.get("error"), "ms": r["ms"]}
    try:
        data = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return [], {"ok": False, "status": 200, "error": f"bad json: {e}", "ms": r["ms"]}
    out = []
    scanned = 0
    for h in data.get("hits", []):
        scanned += 1
        text = _clean(h.get("comment_text") or h.get("story_text") or "")
        if len(text) < 40:
            continue
        title = _clean(h.get("title") or h.get("story_title") or "")
        if tags == "comment" and not _phrase_hit(text, query):
            continue
        oid = h.get("objectID")
        out.append(
            {
                "id": f"hn:{oid}",
                "source": "hackernews",
                "kind": tags,
                "title": title[:160],
                "text": _window(text, query)[:700],
                "url": f"https://news.ycombinator.com/item?id={oid}",
                "points": h.get("points") or 0,
                "created_at": h.get("created_at"),
                "query": query,
                "match": "phrase",
            }
        )
    return out, {"ok": True, "status": 200, "n": len(out), "scanned": scanned, "ms": r["ms"], "url": url}


def rss(feed_url, limit=15):
    r = get(feed_url)
    if not r["ok"]:
        return [], {"ok": False, "status": r.get("status"), "error": r.get("error"), "ms": r["ms"]}
    try:
        root = ET.fromstring(r["body"])
    except Exception as e:  # noqa: BLE001
        return [], {"ok": False, "status": 200, "error": f"bad xml: {e}", "ms": r["ms"]}
    out = []
    for i, item in enumerate(root.iter()):
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        t = item.find("{*}title")
        l = item.find("{*}link")
        d = item.find("{*}description") if item.find("{*}description") is not None else item.find("{*}summary")
        href = (l.get("href") if l is not None and l.get("href") else (l.text if l is not None else "")) or ""
        out.append(
            {
                "id": f"rss:{feed_url}#{i}",
                "source": "rss",
                "kind": "feed",
                "title": _clean(t.text if t is not None else "")[:160],
                "text": _clean(d.text if d is not None else "")[:500],
                "url": href,
                "points": 0,
                "created_at": None,
                "query": feed_url,
            }
        )
        if len(out) >= limit:
            break
    return out, {"ok": True, "status": 200, "n": len(out), "ms": r["ms"], "url": feed_url}


def sweep(queries, per_query=15, include_stories=True):
    """Sweep every query across the real sources. Dedupes by signal id."""
    seen = {}
    sources = {}
    for q in queries:
        sigs, st = hn(q, "comment", per_query)
        sources[f"hn:comment:{q}"] = st
        for s in sigs:
            seen.setdefault(s["id"], s)
        if include_stories:
            sigs2, st2 = hn(q, "story", max(5, per_query // 3))
            sources[f"hn:story:{q}"] = st2
            for s in sigs2:
                seen.setdefault(s["id"], s)
    return list(seen.values()), sources
