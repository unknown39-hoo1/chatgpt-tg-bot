"""SQLite persistence: sessions, proxies, chat assignments, settings.

Session tokens and proxy credentials are sensitive: the DB file is created
with 0600 permissions. Never print row contents that include tokens.

On Render (D1_DATABASE_ID + D1_API_TOKEN set), all calls delegate to
Cloudflare D1 via d1.py (SQLite-compatible, persistent).
"""
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from config import DB_PATH

# D1 delegate: if configured, re-export d1's functions and skip SQLite
if os.environ.get("D1_DATABASE_ID") and os.environ.get("D1_API_TOKEN"):
    from d1 import (  # noqa: F401
        kv_get, kv_set, upsert_session, get_session, list_sessions,
        count_sessions, update_session, healthy_sessions, add_proxy,
        list_proxies, update_proxy, proxy_for_session, set_proxy_for_session,
        free_proxies, get_chat, set_chat, reset_chat, chat_count_per_session,
        stats, init,
    )
else:


    def _conn() -> sqlite3.Connection:
        new = not DB_PATH.exists()
        c = sqlite3.connect(str(DB_PATH))
        c.row_factory = sqlite3.Row
        if new:
            os.chmod(DB_PATH, 0o600)
        return c


    def init() -> None:
        c = _conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions(
                email TEXT PRIMARY KEY,
                user_id TEXT,
                plan_type TEXT,
                session_token TEXT,
                access_token TEXT,
                token_expires_at INTEGER DEFAULT 0,
                cookies_json TEXT,
                health TEXT DEFAULT 'unknown',   -- ok | blocked | dead | unknown
                fail_count INTEGER DEFAULT 0,
                last_check INTEGER DEFAULT 0,
                last_used INTEGER DEFAULT 0,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS proxies(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                health TEXT DEFAULT 'unknown',   -- ok | bad | unknown
                fail_count INTEGER DEFAULT 0,
                last_check INTEGER DEFAULT 0,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS session_proxy(
                session_email TEXT PRIMARY KEY,
                proxy_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS chats(
                chat_id INTEGER PRIMARY KEY,
                session_email TEXT,
                conversation_id TEXT,
                parent_message_id TEXT,
                model TEXT,
                updated_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS kv(
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        c.commit()
        c.close()


    # ---------- generic kv ----------
    def kv_get(key: str, default: Optional[str] = None) -> Optional[str]:
        c = _conn()
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        c.close()
        return r["value"] if r else default


    def kv_set(key: str, value: str) -> None:
        c = _conn()
        c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value))
        c.commit()
        c.close()


    # ---------- sessions ----------
    def upsert_session(email: str, user_id: str, plan_type: str, session_token: str,
                       access_token: str, cookies: list) -> None:
        now = int(time.time())
        c = _conn()
        c.execute(
            """INSERT INTO sessions(email,user_id,plan_type,session_token,access_token,
                                    cookies_json,created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(email) DO UPDATE SET
                 user_id=excluded.user_id, plan_type=excluded.plan_type,
                 session_token=excluded.session_token, access_token=excluded.access_token,
                 cookies_json=excluded.cookies_json, token_expires_at=0,
                 health='unknown', fail_count=0""",
            (email, user_id, plan_type, session_token, access_token,
             json.dumps(cookies), now),
        )
        c.commit()
        c.close()


    def get_session(email: str) -> Optional[sqlite3.Row]:
        c = _conn()
        r = c.execute("SELECT * FROM sessions WHERE email=?", (email,)).fetchone()
        c.close()
        return r


    def list_sessions() -> list:
        c = _conn()
        rows = c.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()
        c.close()
        return rows


    def count_sessions() -> int:
        c = _conn()
        n = c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
        c.close()
        return n


    def update_session(email: str, **kw: Any) -> None:
        if not kw:
            return
        cols = ",".join(f"{k}=?" for k in kw)
        c = _conn()
        c.execute(f"UPDATE sessions SET {cols} WHERE email=?", (*kw.values(), email))
        c.commit()
        c.close()


    def healthy_sessions() -> list:
        c = _conn()
        rows = c.execute(
            "SELECT * FROM sessions WHERE health='ok' ORDER BY last_used").fetchall()
        c.close()
        return rows


    # ---------- proxies ----------
    def add_proxy(url: str) -> bool:
        """Returns True if newly added."""
        c = _conn()
        try:
            c.execute(
                "INSERT INTO proxies(url,created_at) VALUES(?,?)",
                (url.strip(), int(time.time())))
            c.commit()
            added = True
        except sqlite3.IntegrityError:
            added = False
        c.close()
        return added


    def list_proxies() -> list:
        c = _conn()
        rows = c.execute("SELECT * FROM proxies ORDER BY id").fetchall()
        c.close()
        return rows


    def update_proxy(pid: int, **kw: Any) -> None:
        if not kw:
            return
        cols = ",".join(f"{k}=?" for k in kw)
        c = _conn()
        c.execute(f"UPDATE proxies SET {cols} WHERE id=?", (*kw.values(), pid))
        c.commit()
        c.close()


    def proxy_for_session(email: str) -> Optional[sqlite3.Row]:
        c = _conn()
        r = c.execute(
            """SELECT p.* FROM proxies p
               JOIN session_proxy sp ON sp.proxy_id=p.id
               WHERE sp.session_email=?""", (email,)).fetchone()
        c.close()
        return r


    def set_proxy_for_session(email: str, pid: int) -> None:
        c = _conn()
        c.execute("INSERT OR REPLACE INTO session_proxy(session_email,proxy_id) VALUES(?,?)",
                  (email, pid))
        c.commit()
        c.close()


    def free_proxies(exclude_ids: set) -> list:
        c = _conn()
        rows = c.execute(
            "SELECT * FROM proxies WHERE health != 'bad' ORDER BY fail_count, id").fetchall()
        c.close()
        return [r for r in rows if r["id"] not in exclude_ids]


    # ---------- chats ----------
    def get_chat(chat_id: int) -> Optional[sqlite3.Row]:
        c = _conn()
        r = c.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        c.close()
        return r


    def set_chat(chat_id: int, session_email: Optional[str] = None,
                 conversation_id: Optional[str] = None,
                 parent_message_id: Optional[str] = None,
                 model: Optional[str] = None) -> None:
        c = _conn()
        cur = c.execute("SELECT chat_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        now = int(time.time())
        if cur is None:
            c.execute(
                "INSERT INTO chats(chat_id,session_email,conversation_id,parent_message_id,model,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (chat_id, session_email, conversation_id, parent_message_id, model, now))
        else:
            sets, vals = [], []
            for k, v in (("session_email", session_email), ("conversation_id", conversation_id),
                         ("parent_message_id", parent_message_id), ("model", model)):
                if v is not None:
                    sets.append(f"{k}=?")
                    vals.append(v)
            sets.append("updated_at=?")
            vals.append(now)
            c.execute(f"UPDATE chats SET {','.join(sets)} WHERE chat_id=?", (*vals, chat_id))
        c.commit()
        c.close()


    def reset_chat(chat_id: int) -> None:
        c = _conn()
        c.execute("UPDATE chats SET conversation_id=NULL, parent_message_id=NULL,"
                  " updated_at=? WHERE chat_id=?", (int(time.time()), chat_id))
        c.commit()
        c.close()


    def chat_count_per_session() -> dict:
        c = _conn()
        rows = c.execute(
            "SELECT session_email, COUNT(*) n FROM chats WHERE session_email IS NOT NULL"
            " GROUP BY session_email").fetchall()
        c.close()
        return {r["session_email"]: r["n"] for r in rows}


    def stats() -> dict:
        c = _conn()
        s = {
            "sessions": c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"],
            "sessions_ok": c.execute("SELECT COUNT(*) n FROM sessions WHERE health='ok'").fetchone()["n"],
            "proxies": c.execute("SELECT COUNT(*) n FROM proxies").fetchone()["n"],
            "proxies_ok": c.execute("SELECT COUNT(*) n FROM proxies WHERE health='ok'").fetchone()["n"],
            "chats": c.execute("SELECT COUNT(*) n FROM chats").fetchone()["n"],
        }
        c.close()
        return s
