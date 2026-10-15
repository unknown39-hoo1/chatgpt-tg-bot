"""Session pool management.

- ingest_zip(path): read an ELMINYAWE vault zip (per-email JSON session files)
  and register every healthy session. Unlimited sessions.
- assign_chat(chat_id): pin a chat to the least-loaded healthy session
  (rotation across conversations). Reassigns if the session died.
- ensure_proxy(email): sticky proxy per session; assigns a free healthy proxy,
  rotates away from bad ones.
- health_check(): refresh token + models through the session proxy.
"""
import asyncio
import json
import time
import zipfile
from typing import Optional

import db
from gpt import GPTError, GPTSession


# ---------------- ingestion ----------------
def _parse_session_file(data: dict) -> Optional[dict]:
    if not data.get("ok") or not data.get("email"):
        return None
    if not data.get("session_token") and not data.get("cookies"):
        return None
    return {
        "email": data["email"],
        "user_id": data.get("user_id", ""),
        "plan_type": data.get("plan_type", "free"),
        "session_token": data.get("session_token", ""),
        "access_token": data.get("access_token", ""),
        "cookies": data.get("cookies", []),
    }


def ingest_zip(path: str) -> dict:
    """Returns {'added': [...emails], 'skipped': n}."""
    added, skipped = [], 0
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(".json"):
                continue
            try:
                data = json.loads(z.read(name).decode("utf-8"))
            except Exception:
                skipped += 1
                continue
            if isinstance(data, dict) and "email" in data:
                s = _parse_session_file(data)
                if s:
                    db.upsert_session(**s)
                    added.append(s["email"])
                else:
                    skipped += 1
            elif isinstance(data, dict):
                # mail_sessions.json style: {email: {...}} -> nothing to import
                skipped += 1
            else:
                skipped += 1
    return {"added": added, "skipped": skipped}


def ingest_proxies_text(text: str) -> dict:
    added, dup = 0, 0
    for line in text.splitlines():
        line = line.strip().strip(",").strip('"').strip("'")
        if not line or line.startswith("#"):
            continue
        if "://" not in line:
            line = "http://" + line
        if db.add_proxy(line):
            added += 1
        else:
            dup += 1
    return {"added": added, "dup": dup}


# ---------------- proxy assignment ----------------
def ensure_proxy(email: str) -> Optional[str]:
    """Sticky proxy per session; rotate if the current one is bad."""
    cur = db.proxy_for_session(email)
    if cur and cur["health"] != "bad":
        return cur["url"]
    # collect actually-used proxy ids
    used_ids: set = set()
    for s in db.list_sessions():
        p = db.proxy_for_session(s["email"])
        if p and s["email"] != email:
            used_ids.add(p["id"])
    cands = db.free_proxies(used_ids)
    # prefer an unused proxy, else least-failing
    if cands:
        pick = cands[0]
        db.set_proxy_for_session(email, pick["id"])
        return pick["url"]
    return cur["url"] if cur else None


def mark_proxy_bad(proxy_id: int) -> None:
    p = next((x for x in db.list_proxies() if x["id"] == proxy_id), None)
    if not p:
        return
    db.update_proxy(proxy_id, health="bad" if p["fail_count"] + 1 >= 2 else p["health"],
                    fail_count=p["fail_count"] + 1, last_check=int(time.time()))


# ---------------- session objects ----------------
def make_gpt(email: str) -> Optional[GPTSession]:
    row = db.get_session(email)
    if not row:
        return None
    proxy = ensure_proxy(email)
    cookies = json.loads(row["cookies_json"] or "[]")
    return GPTSession(email=row["email"], session_token=row["session_token"] or "",
                      access_token=row["access_token"] or "",
                      cookies=cookies, proxy_url=proxy)


async def health_check(email: str) -> str:
    """Refresh + models through the session proxy (or direct if no proxy). Returns new health."""
    gpt = make_gpt(email)
    if not gpt:
        return "dead"
    # Try direct connection even without proxy (user order 2026-10-15)
    try:
        await gpt.refresh()
        models = await gpt.models()
        db.update_session(email, health="ok" if models else "blocked",
                          fail_count=0, last_check=int(time.time()),
                          access_token=gpt.access_token,
                          token_expires_at=int(time.time()) + 3600)
        p = db.proxy_for_session(email)
        if p:
            db.update_proxy(p["id"], health="ok", fail_count=0,
                            last_check=int(time.time()))
        health = "ok" if models else "blocked"
    except GPTError as e:
        if e.kind == "blocked":
            db.update_session(email, health="blocked", last_check=int(time.time()),
                              fail_count=(db.get_session(email)["fail_count"] + 1))
            p = db.proxy_for_session(email)
            if p:
                mark_proxy_bad(p["id"])
                p2 = next((x for x in db.list_proxies() if x["id"] == p["id"]), None)
                if p2 and p2["health"] == "bad":
                    # unassign so ensure_proxy() rotates to a fresh proxy
                    import sqlite3 as _sq
                    from config import DB_PATH as _dbp
                    _c = _sq.connect(str(_dbp))
                    _c.execute("DELETE FROM session_proxy WHERE session_email=?", (email,))
                    _c.commit(); _c.close()
            health = "blocked"
        elif e.kind == "auth":
            db.update_session(email, health="dead", last_check=int(time.time()))
            health = "dead"
        else:
            db.update_session(email, last_check=int(time.time()))
            health = db.get_session(email)["health"]
    finally:
        await gpt.close()
    return health


# ---------------- chat assignment (rotation) ----------------
def assign_chat(chat_id: int) -> Optional[str]:
    """Pin chat to the least-loaded healthy session. Returns email or None."""
    chat = db.get_chat(chat_id)
    if chat and chat["session_email"]:
        s = db.get_session(chat["session_email"])
        if s and s["health"] == "ok":
            return s["email"]
    ok = db.healthy_sessions()
    if not ok:
        return None
    load = db.chat_count_per_session()
    ok.sort(key=lambda r: load.get(r["email"], 0))
    email = ok[0]["email"]
    db.set_chat(chat_id, session_email=email)
    return email


def default_model() -> str:
    return db.kv_get("default_model", "auto") or "auto"


async def check_all_sessions() -> dict:
    out = {}
    for s in db.list_sessions():
        out[s["email"]] = await health_check(s["email"])
        await asyncio.sleep(1)
    return out
