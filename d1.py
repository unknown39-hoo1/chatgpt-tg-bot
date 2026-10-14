"""Cloudflare D1 client mirroring db.py's interface.

Used on Render (where SQLite is ephemeral). D1 is SQLite-compatible,
so the SQL is identical. Auth via D1_API_TOKEN (user creates it in
Cloudflare dashboard: My Profile -> API Tokens -> D1 Edit).

Set D1_DATABASE_ID and D1_API_TOKEN env vars to enable.
"""
import json
import os
import time
import urllib.request
import urllib.error
from typing import Any, Optional

DB_ID = os.environ.get("D1_DATABASE_ID", "")
API_TOKEN = os.environ.get("D1_API_TOKEN", "")
ACCT = "fed5e44fd7e5093349d00659f06b7de3"
BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/d1/database/{DB_ID}/query"


def _query(sql: str, params: list | None = None) -> list:
    """Run SQL, return list of dict rows (empty for non-SELECT)."""
    if not DB_ID or not API_TOKEN:
        raise RuntimeError("D1 not configured")
    payload = json.dumps({"sql": sql, "params": params or []}).encode()
    req = urllib.request.Request(
        BASE, data=payload, method="POST",
        headers={"Authorization": f"Bearer {API_TOKEN}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        raise RuntimeError(f"D1 HTTP {e.code}: {body}")
    if not data.get("success"):
        raise RuntimeError(f"D1 error: {str(data.get('errors'))[:200]}")
    results = data.get("result", [])
    if not results:
        return []
    # result[0] has 'results' key with rows
    return results[0].get("results", [])


def _one(sql: str, params: list | None = None) -> Optional[dict]:
    rows = _query(sql, params)
    return rows[0] if rows else None


# ---------- generic kv ----------
def kv_get(key: str, default: Optional[str] = None) -> Optional[str]:
    r = _one("SELECT value FROM kv WHERE key=?", [key])
    return r["value"] if r else default


def kv_set(key: str, value: str) -> None:
    _query("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", [key, value])


# ---------- sessions ----------
def upsert_session(email: str, user_id: str, plan_type: str, session_token: str,
                   access_token: str, cookies: list) -> None:
    now = int(time.time())
    _query(
        """INSERT INTO sessions(email,user_id,plan_type,session_token,access_token,
                                cookies_json,created_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(email) DO UPDATE SET
             user_id=excluded.user_id, plan_type=excluded.plan_type,
             session_token=excluded.session_token, access_token=excluded.access_token,
             cookies_json=excluded.cookies_json, token_expires_at=0,
             health='unknown', fail_count=0""",
        [email, user_id, plan_type, session_token, access_token,
         json.dumps(cookies), now])


def get_session(email: str) -> Optional[dict]:
    return _one("SELECT * FROM sessions WHERE email=?", [email])


def list_sessions() -> list:
    return _query("SELECT * FROM sessions ORDER BY created_at")


def count_sessions() -> int:
    r = _one("SELECT COUNT(*) n FROM sessions")
    return r["n"] if r else 0


def update_session(email: str, **kw: Any) -> None:
    if not kw:
        return
    cols = ",".join(f"{k}=?" for k in kw)
    _query(f"UPDATE sessions SET {cols} WHERE email=?", [*kw.values(), email])


def healthy_sessions() -> list:
    return _query("SELECT * FROM sessions WHERE health='ok' ORDER BY last_used")


# ---------- proxies ----------
def add_proxy(url: str) -> bool:
    try:
        _query("INSERT INTO proxies(url,created_at) VALUES(?,?)",
               [url.strip(), int(time.time())])
        return True
    except RuntimeError as e:
        if "UNIQUE" in str(e):
            return False
        raise


def list_proxies() -> list:
    return _query("SELECT * FROM proxies ORDER BY id")


def update_proxy(pid: int, **kw: Any) -> None:
    if not kw:
        return
    cols = ",".join(f"{k}=?" for k in kw)
    _query(f"UPDATE proxies SET {cols} WHERE id=?", [*kw.values(), pid])


def proxy_for_session(email: str) -> Optional[dict]:
    return _one(
        """SELECT p.* FROM proxies p
           JOIN session_proxy sp ON sp.proxy_id=p.id
           WHERE sp.session_email=?""", [email])


def set_proxy_for_session(email: str, pid: int) -> None:
    _query("INSERT OR REPLACE INTO session_proxy(session_email,proxy_id) VALUES(?,?)",
           [email, pid])


def free_proxies(exclude_ids: set) -> list:
    rows = _query("SELECT * FROM proxies WHERE health != 'bad' ORDER BY fail_count, id")
    return [r for r in rows if r["id"] not in exclude_ids]


# ---------- chats ----------
def get_chat(chat_id: int) -> Optional[dict]:
    return _one("SELECT * FROM chats WHERE chat_id=?", [chat_id])


def set_chat(chat_id: int, session_email: Optional[str] = None,
             conversation_id: Optional[str] = None,
             parent_message_id: Optional[str] = None,
             model: Optional[str] = None) -> None:
    cur = _one("SELECT chat_id FROM chats WHERE chat_id=?", [chat_id])
    now = int(time.time())
    if cur is None:
        _query(
            "INSERT INTO chats(chat_id,session_email,conversation_id,parent_message_id,model,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            [chat_id, session_email, conversation_id, parent_message_id, model, now])
    else:
        sets, vals = [], []
        for k, v in (("session_email", session_email), ("conversation_id", conversation_id),
                     ("parent_message_id", parent_message_id), ("model", model)):
            if v is not None:
                sets.append(f"{k}=?")
                vals.append(v)
        sets.append("updated_at=?")
        vals.append(now)
        _query(f"UPDATE chats SET {','.join(sets)} WHERE chat_id=?", [*vals, chat_id])


def reset_chat(chat_id: int) -> None:
    _query("UPDATE chats SET conversation_id=NULL, parent_message_id=NULL,"
           " updated_at=? WHERE chat_id=?", [int(time.time()), chat_id])


def chat_count_per_session() -> dict:
    rows = _query(
        "SELECT session_email, COUNT(*) n FROM chats WHERE session_email IS NOT NULL"
        " GROUP BY session_email")
    return {r["session_email"]: r["n"] for r in rows}


def stats() -> dict:
    return {
        "sessions": (_one("SELECT COUNT(*) n FROM sessions") or {}).get("n", 0),
        "sessions_ok": (_one("SELECT COUNT(*) n FROM sessions WHERE health='ok'") or {}).get("n", 0),
        "proxies": (_one("SELECT COUNT(*) n FROM proxies") or {}).get("n", 0),
        "proxies_ok": (_one("SELECT COUNT(*) n FROM proxies WHERE health='ok'") or {}).get("n", 0),
        "chats": (_one("SELECT COUNT(*) n FROM chats") or {}).get("n", 0),
    }


def init() -> None:
    # schema already created; no-op for D1
    pass
