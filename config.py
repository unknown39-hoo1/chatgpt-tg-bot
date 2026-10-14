"""Configuration for the ChatGPT bridge bot.

Secrets live ONLY in .env (0600). Never log them, never put them in chat.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"


def _load_dotenv() -> None:
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _sanitize_no_proxy() -> None:
    # httpx chokes on bracketed IPv6 entries like [::1] in no_proxy
    # ("Invalid port: ':1]'") -- strip them; plain hostnames stay.
    for k in ("no_proxy", "NO_PROXY"):
        v = os.environ.get(k)
        if v:
            os.environ[k] = ",".join(
                p for p in v.split(",") if "[" not in p and "]" not in p)


_sanitize_no_proxy()
_load_dotenv()

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
OWNER_ID: int = int(os.environ.get("OWNER_ID", "0") or 0)

# ChatGPT web endpoints (third-party-client style usage)
GPT_AUTH_SESSION = "https://chatgpt.com/api/auth/session"
GPT_MODELS = "https://chatgpt.com/backend-api/models"
GPT_CONVERSATION = "https://chatgpt.com/backend-api/conversation"
GPT_FILES = "https://chatgpt.com/backend-api/files"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Streaming edit throttle
EDIT_EVERY_SEC = 1.4
MAX_TG_TEXT = 4000  # stay under the 4096 limit with margin
