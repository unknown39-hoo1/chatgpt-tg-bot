"""Raw Bot API calls for 10.1+/10.3 methods python-telegram-bot doesn't expose
(sendRichMessage, rich editMessageText). Plain HTTPS, through the normal
egress proxy (env), with the bot token from .env.
"""
import httpx

from config import BOT_TOKEN

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def _call(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{API}/{method}", json=payload)
        try:
            data = r.json()
        except Exception:
            return {"ok": False, "description": f"http {r.status_code}"}
        return data


async def send_rich_message(chat_id: int, blocks: list,
                            reply_to: int | None = None,
                            reply_markup: dict | None = None) -> dict:
    payload: dict = {"chat_id": chat_id,
                     "rich_message": {"blocks": blocks}}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _call("sendRichMessage", payload)


async def edit_message_rich(chat_id: int, message_id: int, blocks: list,
                            reply_markup: dict | None = None) -> dict:
    payload: dict = {"chat_id": chat_id, "message_id": message_id,
                     "rich_message": {"blocks": blocks}}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await _call("editMessageText", payload)


async def get_me() -> dict:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{API}/getMe")
        return r.json()
