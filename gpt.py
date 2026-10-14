"""ChatGPT backend client, third-party-app style.

Uses the owner's own exported web sessions (session cookie + tokens):
  - refresh(): GET /api/auth/session with the session cookies -> fresh access token
  - models():   GET /backend-api/models
  - ask_stream(): POST /backend-api/conversation (SSE)
  - upload_file(): POST /backend-api/files

Every request for a session goes through that session's assigned user proxy
(sticky), so OpenAI always sees one consistent IP per account. TLS is always
verified, so a proxy can never read the tokens.

Memory: every conversation request sets history_and_training_disabled=true
(temp-chat semantics) -> ChatGPT memory is never read nor written, so nothing
leaks between Telegram users. Conversation continuity is kept only in our own
DB, per Telegram chat.

Never log tokens. Errors are reported by type/status only.
"""
import asyncio
import json
import time
import uuid
from typing import AsyncIterator, Optional

import httpx

from config import (GPT_AUTH_SESSION, GPT_CONVERSATION, GPT_FILES, GPT_MODELS, UA)


def _headers(token: Optional[str] = None) -> dict:
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-device-id": str(uuid.uuid4()),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class GPTError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}"[:300])
        self.kind = kind          # auth | blocked | rate | network | bad
        self.detail = detail[:300]


class GPTSession:
    """One ChatGPT web session (one email from the vault)."""

    def __init__(self, email: str, session_token: str, access_token: str,
                 cookies: list, proxy_url: Optional[str] = None):
        self.email = email
        self.session_token = session_token
        self.access_token = access_token
        self.cookies = cookies or []
        self.proxy_url = proxy_url
        self._client: Optional[httpx.AsyncClient] = None

    def _client_for(self) -> httpx.AsyncClient:
        if self._client is None:
            kw: dict = {"timeout": httpx.Timeout(30.0, read=120.0),
                        "verify": True, "follow_redirects": True}
            if self.proxy_url:
                # direct proxy use: each new proxy IP triggers one platform
                # approval card (one-time "Always allow"). No way around it.
                kw["proxy"] = self.proxy_url
                kw["trust_env"] = False
            self._client = httpx.AsyncClient(**kw)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _cookie_header(self) -> str:
        return "; ".join(f"{c['name']}={c['value']}" for c in self.cookies
                         if c.get("name") and c.get("value"))

    # ---------- auth ----------
    async def refresh(self) -> str:
        """Refresh the access token via the session cookie. Returns new token."""
        c = self._client_for()
        try:
            r = await c.get(GPT_AUTH_SESSION,
                            headers={**_headers(), "Cookie": self._cookie_header()})
        except httpx.HTTPError as e:
            raise GPTError("network", type(e).__name__)
        if r.status_code in (401, 403):
            # IP-flagged exits here even with a valid cookie
            raise GPTError("blocked", f"auth session -> {r.status_code}")
        if r.status_code != 200:
            raise GPTError("auth", f"auth session -> {r.status_code}")
        try:
            data = r.json()
        except Exception:
            raise GPTError("auth", "bad auth-session json")
        tok = data.get("accessToken") or ""
        if not tok:
            raise GPTError("auth", "empty accessToken (session dead?)")
        self.access_token = tok
        return tok

    async def models(self) -> list:
        c = self._client_for()
        try:
            r = await c.get(GPT_MODELS, headers=_headers(self.access_token))
        except httpx.HTTPError as e:
            raise GPTError("network", type(e).__name__)
        if r.status_code in (401, 403):
            raise GPTError("blocked", f"models -> {r.status_code}")
        if r.status_code == 429:
            raise GPTError("rate", "models -> 429")
        if r.status_code != 200:
            raise GPTError("bad", f"models -> {r.status_code}")
        try:
            data = r.json()
        except Exception:
            raise GPTError("bad", "bad models json")
        models = data.get("models", data if isinstance(data, list) else [])
        return [m for m in models if isinstance(m, dict)]

    # ---------- files ----------
    async def upload_file(self, path: str, filename: str, mime: str) -> str:
        """Upload to ChatGPT backend. Returns file_id like 'file-...'."""
        c = self._client_for()
        with open(path, "rb") as f:
            content = f.read()
        files = {"file": (filename, content, mime)}
        try:
            r = await c.post(GPT_FILES, headers=_headers(self.access_token), files=files)
        except httpx.HTTPError as e:
            raise GPTError("network", type(e).__name__)
        if r.status_code in (401, 403):
            raise GPTError("blocked", f"files -> {r.status_code}")
        if r.status_code not in (200, 201):
            raise GPTError("bad", f"files -> {r.status_code}: {r.text[:120]}")
        try:
            fid = r.json().get("file_id", "")
        except Exception:
            raise GPTError("bad", "bad files json")
        if not fid:
            raise GPTError("bad", "empty file_id")
        return fid

    async def download_asset(self, file_id: str) -> bytes:
        c = self._client_for()
        url = f"https://chatgpt.com/backend-api/files/{file_id}/content"
        try:
            r = await c.get(url, headers=_headers(self.access_token))
        except httpx.HTTPError as e:
            raise GPTError("network", type(e).__name__)
        if r.status_code != 200:
            raise GPTError("bad", f"asset -> {r.status_code}")
        return r.content

    # ---------- conversation ----------
    def _body(self, prompt: str, model: str, conversation_id: Optional[str],
              parent_id: Optional[str], attachments: list) -> dict:
        msg_id = str(uuid.uuid4())
        if attachments:
            parts: list = []
            if prompt:
                parts.append({"content_type": "text", "text": prompt})
            for a in attachments:
                p: dict = {"content_type": "image_asset_pointer",
                           "asset_pointer": f"file-service://{a['file_id']}"}
                if a.get("size"):
                    p["size_bytes"] = a["size"]
                if a.get("width"):
                    p["width"] = a["width"]
                    p["height"] = a.get("height", 0)
                parts.append(p)
            content = {"content_type": "multimodal_text", "parts": parts}
        else:
            content = {"content_type": "text", "parts": [prompt]}
        return {
            "action": "next",
            "messages": [{
                "id": msg_id,
                "author": {"role": "user"},
                "content": content,
                "metadata": {},
            }],
            "parent_message_id": parent_id or str(uuid.uuid4()),
            "model": model,
            "conversation_id": conversation_id,
            "history_and_training_disabled": True,  # temp semantics: memory never read/written
            "timezone_offset_min": -180,
            "system_hints": [],
            "paragen_stream_enabled": False,
            "paragen_cot_summary_display_override": "allow",
        }

    async def ask_stream(self, prompt: str, model: str,
                         conversation_id: Optional[str] = None,
                         parent_id: Optional[str] = None,
                         attachments: Optional[list] = None
                         ) -> AsyncIterator[dict]:
        """Yield events: {'type':'delta','text':...}, then
        {'type':'done','conversation_id':...,'message_id':...,'assets':[...]}."""
        c = self._client_for()
        body = self._body(prompt, model, conversation_id, parent_id, attachments or [])
        req = c.build_request("POST", GPT_CONVERSATION,
                              headers={**_headers(self.access_token),
                                       "Content-Type": "application/json",
                                       "Accept": "text/event-stream"},
                              json=body)
        try:
            r = await c.send(req, stream=True)
        except httpx.HTTPError as e:
            raise GPTError("network", type(e).__name__)
        if r.status_code in (401, 403):
            await r.aclose()
            raise GPTError("blocked", f"conversation -> {r.status_code}")
        if r.status_code == 429:
            await r.aclose()
            raise GPTError("rate", "conversation -> 429")
        if r.status_code != 200:
            txt = (await r.aread())[:200].decode("utf-8", "ignore")
            await r.aclose()
            raise GPTError("bad", f"conversation -> {r.status_code}: {txt}")

        text = ""
        conv_id = conversation_id
        msg_id: Optional[str] = None
        assets: list = []
        try:
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                if "conversation_id" in ev and not conv_id:
                    conv_id = ev["conversation_id"]
                msg = ev.get("message") or {}
                if msg.get("id"):
                    msg_id = msg["id"]
                content = (msg.get("content") or {})
                parts = content.get("parts") or []
                for p in parts:
                    if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer":
                        ap = p.get("asset_pointer", "")
                        if ap.startswith("file-service://"):
                            fid = ap.split("file-service://", 1)[1]
                            if fid not in assets:
                                assets.append(fid)
                # text delta
                new_text = ""
                for p in parts:
                    if isinstance(p, str):
                        new_text += p
                    elif isinstance(p, dict) and p.get("content_type") == "text":
                        new_text += p.get("text", "")
                if new_text and new_text != text:
                    text = new_text
                    yield {"type": "delta", "text": text}
        finally:
            await r.aclose()
        yield {"type": "done", "conversation_id": conv_id,
               "message_id": msg_id, "assets": assets, "text": text}
