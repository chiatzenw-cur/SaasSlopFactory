"""LLM adapter. OpenAI-compatible chat with strict JSON output."""

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import config


class LLMError(RuntimeError):
    pass


def available(model):
    _, keyenv = config.provider_for(model)
    return bool(os.environ.get(keyenv))


def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    # first balanced { ... }
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start : i + 1])
                except Exception:
                    start = None
    raise LLMError("model did not return JSON: " + text[:300])


def chat_json(model, system, user, max_tokens=2000, temperature=0.3, retries=3, timeout=120):
    base, keyenv = config.provider_for(model)
    key = os.environ.get(keyenv)
    if not key:
        raise LLMError(f"{keyenv} not set")
    url = base.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body).encode()
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            out = json.loads(raw)
            txt = out["choices"][0]["message"]["content"]
            parsed = _extract_json(txt)
            parsed["_usage"] = out.get("usage", {})
            parsed["_model"] = model
            return parsed
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            last = f"HTTP {e.code}: {detail}"
            if e.code in (400, 401, 403, 404):
                # 400 may be response_format unsupported -> retry once without it
                if e.code == 400 and "response_format" in detail:
                    body.pop("response_format", None)
                    data = json.dumps(body).encode()
                    continue
                raise LLMError(last)
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"failed after {retries} attempts: {last}")
