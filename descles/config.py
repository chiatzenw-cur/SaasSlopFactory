import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path(os.environ.get("DESCLES_DATA", ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = pathlib.Path(os.environ.get("DESCLES_DB", DATA / "runtime.db"))
PORT = int(os.environ.get("CONSOLE_PORT", "7310"))

# model prefix -> (base_url, api key env var)
PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "kimi": ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"),
    "gpt": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "o1": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "claude": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
}


def load_keys(path=None):
    """Load API keys from an env file. Never prints values."""
    candidates = [path, os.environ.get("DESCLES_RUNTIME_ENV"), ROOT / ".keys.env"]
    loaded = []
    for c in candidates:
        if not c:
            continue
        p = pathlib.Path(c)
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip().strip('"').strip("'")
            if not k or not v:
                continue
            if k not in os.environ:
                os.environ[k] = v
                loaded.append(k)
        break
    return loaded


def provider_for(model):
    m = (model or "").lower()
    for prefix, (base, keyenv) in PROVIDERS.items():
        if m.startswith(prefix):
            return base, keyenv
    return PROVIDERS["deepseek"]
