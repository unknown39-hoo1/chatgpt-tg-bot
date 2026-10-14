"""ChatGPT bridge bot — Bot API 10.3 native.

- Answers any chat from the owner's pool of ChatGPT web sessions
  (unlimited, imported from ELMINYAWE vault zips).
- Each Telegram chat is pinned to one healthy session (rotation across
  conversations); a dead session auto-migrates the chat.
- ChatGPT memory is always off (history_and_training_disabled) so nothing
  leaks between users.
- Every session talks through its own sticky user proxy (the server IP is
  401-blocked by ChatGPT's backend), rotating on failure.
- Native 10.3/9.4 UI: colored inline buttons (style=primary/success/danger),
  rich messages (sendRichMessage) for structured answers, stop-generation
  button while streaming.
"""
import asyncio
import html
import logging
import os
import tempfile
import time
import uuid

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import db
import fmt
import harvester
import sessions as sess
import tg_raw
from config import BOT_TOKEN, OWNER_ID, EDIT_EVERY_SEC, MAX_TG_TEXT
from gpt import GPTError

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cgpt-bot")

# chat_id -> asyncio.Task of the running generation
RUNNING: dict[int, asyncio.Task] = {}


def is_owner(uid: int) -> bool:
    return OWNER_ID and uid == OWNER_ID


def engine_on() -> bool:
    return db.kv_get("engine_on", "1") == "1"


# ---------------- styled buttons (native Bot API >= 9.4 `style`) --------
def btn(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    kw: dict = {}
    if style:
        kw["style"] = style  # primary | success | danger
    return InlineKeyboardButton(text, callback_data=data, **kw)


def stop_kb(nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("⏹ إيقاف", f"stop:{nonce}", "danger")]])


def panel_kb() -> InlineKeyboardMarkup:
    on = engine_on()
    return InlineKeyboardMarkup([
        [btn("⏸ إيقاف البوت", "eng:off", "danger") if on
         else btn("▶️ تشغيل البوت", "eng:on", "success")],
        [btn("📥 الجلسات", "sess", "primary"), btn("🌐 البروكسيات", "prox", "primary")],
        [btn("📊 إحصائيات", "stats", "primary"), btn("🆕 تصفير المحادثات", "wipe", "danger")],
    ])


def panel_text() -> str:
    st = db.stats()
    eng = "🟢 شغّال" if engine_on() else "🔴 واقف"
    return (f"🎛 <b>لوحة المالك</b>\n"
            f"المحرك: {eng}\n"
            f"الجلسات: {st['sessions']} (سليمة: {st['sessions_ok']}) | "
            f"البروكسيات: {st['proxies']} (سليمة: {st['proxies_ok']}) | "
            f"محادثات: {st['chats']}\n\n"
            f"📦 ابعت ملف <b>zip</b> (مثل المرفق) لإضافة جلسات — عدد مفتوح.\n"
            f"🌐 ابعت <b>/proxy add</b> أو ملف <b>txt</b> (سطر لكل بروكسي) لإضافة بروكسيات.\n"
            f"🚜 <b>/harvest</b> موقوف (المنصة تطلب بطاقة لكل IP) — أضف بروكسياتك بـ /proxy add.")


# ---------------- commands ----------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_owner(uid):
        await update.message.reply_text(panel_text(), parse_mode=ParseMode.HTML,
                                        reply_markup=panel_kb())
    else:
        await update.message.reply_text(
            "أهلاً! 🤖 ابعت سؤالك، صورة، أو ملف — وأنا بجاوبك.\n"
            "🆕 /new لمحادثة جديدة.")


async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    await update.message.reply_text(panel_text(), parse_mode=ParseMode.HTML,
                                    reply_markup=panel_kb())


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.reset_chat(update.effective_chat.id)
    await update.message.reply_text("🆕 محادثة جديدة — تفضل اسأل.")


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if ctx.args:
        db.kv_set("default_model", ctx.args[0].strip())
        await update.message.reply_text(f"✅ الموديل الافتراضي: <code>{html.escape(ctx.args[0])}</code>",
                                        parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(
            f"الموديل الحالي: <code>{html.escape(sess.default_model())}</code>\n"
            "الاستخدام: <code>/model gpt-5-mini</code>", parse_mode=ParseMode.HTML)


async def cmd_proxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not ctx.args:
        rows = db.list_proxies()
        if not rows:
            await update.message.reply_text("لا بروكسيات بعد. ابعت <code>/proxy add http://user:pass@host:port</code>"
                                            " أو ملف txt.", parse_mode=ParseMode.HTML)
            return
        lines = [f"{'🟢' if p['health']=='ok' else '🔴' if p['health']=='bad' else '⚪️'} "
                 f"<code>{p['id']}</code> — فشل: {p['fail_count']}" for p in rows]
        await update.message.reply_text("🌐 البروكسيات:\n" + "\n".join(lines),
                                        parse_mode=ParseMode.HTML)
        return
    sub = ctx.args[0].lower()
    if sub == "add" and len(ctx.args) > 1:
        url = ctx.args[1].strip()
        if "://" not in url:
            url = "http://" + url
        new = db.add_proxy(url)
        await update.message.reply_text("✅ انضاف البروكسي." if new else "⚠️ هالبروكسي موجود already.")
        # health-check existing sessions now that a proxy exists
        asyncio.create_task(_recheck_sessions(ctx))
    elif sub == "del" and len(ctx.args) > 1:
        await update.message.reply_text("🗑 استخدم ملف الإعدادات لحذف بروكسي (قريباً).")
    else:
        await update.message.reply_text("الاستخدام: <code>/proxy add http://user:pass@host:port</code>",
                                        parse_mode=ParseMode.HTML)


async def _recheck_sessions(ctx: ContextTypes.DEFAULT_TYPE):
    res = await sess.check_all_sessions()
    ok = sum(1 for v in res.values() if v == "ok")
    try:
        await ctx.bot.send_message(OWNER_ID,
                                   f"🔄 فحص الجلسات: {ok}/{len(res)} سليمة.")
    except Exception:
        pass


# ---------------- proxy harvester (DISABLED by platform limit) ----------------
# Bulk free-proxy harvesting is disabled: the platform requires one approval
# card per proxy IP, so a bulk harvest floods the owner with hundreds of
# cards. Proxies are added manually via /proxy add (one card each, one-time).
HARVEST_RUNNING = False


async def cmd_harvest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    await update.message.reply_text(
        "⛔ الحصاد الجماعي موقوف: المنصة بتطلب بطاقة سماح لكل IP بروكسي، "
        "والحصاد كان يولّد مئات البطاقات.\n\n"
        "الحل الشغال: ابعت بروكسياتك الخاصة بـ <code>/proxy add</code> "
        "(كل واحد بيطلب سماح مرة وحدة بس)، والبوت بيوزع الجلسات عليهم تلقائياً.",
        parse_mode="HTML")


async def _do_harvest(ctx: ContextTypes.DEFAULT_TYPE, status_id: int, want: int):
    global HARVEST_RUNNING
    last_push = 0.0

    async def progress(stage: str, done: int, total: int, working: int):
        nonlocal last_push
        now = time.monotonic()
        if now - last_push < 20 and done < total:
            return
        last_push = now
        name = {"harvest": "📥 جمع", "stage1": "🔌 فحص الاتصال",
                "stage2": "🤖 فحص ChatGPT"}.get(stage, stage)
        try:
            await ctx.bot.edit_message_text(
                f"🌐 الحصاد شغّال…\n{name}: {done}/{total} — شغال: {working}",
                OWNER_ID, status_id)
        except Exception:
            pass

    try:
        stats = await harvester.run_harvest(want, progress)
        w = len(stats.get("working", []))
        await ctx.bot.edit_message_text(
            f"✅ خلص الحصاد ({stats.get('seconds', 0)}s)\n"
            f"مرشحين: {stats.get('candidates', 0)} | متصلين: {stats.get('alive', 0)}\n"
            f"🌐 شغال مع ChatGPT: {w} (انضاف {stats.get('added', 0)} جديد)\n"
            f"المجموع بالبوت: {db.stats()['proxies']}",
            OWNER_ID, status_id)
        # sessions may now have proxies -> recheck
        await _recheck_sessions(ctx)
    except Exception as e:
        log.warning("harvest failed: %s", type(e).__name__)
        try:
            await ctx.bot.edit_message_text("⚠️ فشل الحصاد، جرّب /harvest مرة تانية.",
                                            OWNER_ID, status_id)
        except Exception:
            pass
    finally:
        HARVEST_RUNNING = False


# ---------------- documents (owner imports / user files) ----------------
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = update.effective_user.id
    doc = msg.document
    name = (doc.file_name or "").lower()

    if is_owner(uid) and name.endswith(".zip"):
        f = await doc.get_file()
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            await f.download_to_drive(tf.name)
            path = tf.name
        try:
            res = sess.ingest_zip(path)
        finally:
            os.unlink(path)
        n = len(res["added"])
        await msg.reply_text(f"📥 تسجّل {n} جلسة جديدة ✅ (المجموع: {db.count_sessions()})")
        if n:
            asyncio.create_task(_recheck_sessions(ctx))
        return

    if is_owner(uid) and name.endswith(".txt"):
        f = await doc.get_file()
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            await f.download_to_drive(tf.name)
            path = tf.name
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
            res = sess.ingest_proxies_text(text)
        finally:
            os.unlink(path)
        await msg.reply_text(f"🌐 انضاف {res['added']} بروكسي ✅ (مكرر: {res['dup']})")
        asyncio.create_task(_recheck_sessions(ctx))
        return

    # ordinary user file -> send to ChatGPT
    await _handle_attachment(update, ctx, doc, kind="document")


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    await _handle_attachment(update, ctx, photo, kind="photo")


async def _handle_attachment(update, ctx, tg_file_obj, kind: str):
    msg = update.message
    if not engine_on() and not is_owner(update.effective_user.id):
        await msg.reply_text("⏸ البوت متوقف مؤقتاً.")
        return
    if not await _wants_answer(update, ctx):
        return
    caption = msg.caption or ("شو في بهالصورة؟" if kind == "photo" else "اشرحلي هالملف")
    f = await tg_file_obj.get_file()
    suffix = ""
    if kind == "document":
        suffix = os.path.splitext(tg_file_obj.file_name or "")[1][:8]
    with tempfile.NamedTemporaryFile(suffix=suffix or ".bin", delete=False) as tf:
        await f.download_to_drive(tf.name)
        path = tf.name
    try:
        await _ask(update, ctx, caption, attachments=[path])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------- text ----------------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not engine_on() and not is_owner(update.effective_user.id):
        await update.message.reply_text("⏸ البوت متوقف مؤقتاً.")
        return
    if not await _wants_answer(update, ctx):
        return
    await _ask(update, ctx, update.message.text)


async def _wants_answer(update: Update, ctx) -> bool:
    """In groups: only answer on mention or reply-to-bot."""
    chat = update.effective_chat
    if chat.type == "private":
        return True
    msg = update.message
    me = ctx.bot.username or ""
    if msg.reply_to_message and msg.reply_to_message.from_user.is_bot:
        return True
    text = msg.text or msg.caption or ""
    if f"@{me}" in text:
        return True
    for e in (msg.entities or []) + (msg.caption_entities or []):
        if e.type == "mention":
            if text[e.offset:e.offset + e.length] == f"@{me}":
                return True
    return False


# ---------------- core Q&A ----------------
async def _ask(update: Update, ctx, prompt: str, attachments: list | None = None):
    chat_id = update.effective_chat.id
    msg = update.message
    email = sess.assign_chat(chat_id)
    if not email:
        st = db.stats()
        hint = ""
        if st["sessions"] and not st["sessions_ok"]:
            hint = "\n(الجلسات موجودة بس محظورة — ضيف بروكسيات من اللوحة 🌐)"
        await msg.reply_text("⚠️ لا توجد جلسات شغالة حالياً." + hint)
        return

    nonce = uuid.uuid4().hex[:8]
    placeholder = await msg.reply_text("⏳ عم فكّر…", reply_markup=stop_kb(nonce),
                                       reply_to_message_id=msg.message_id)
    task = asyncio.create_task(
        _generate(ctx, chat_id, placeholder.message_id, prompt, attachments or []))
    RUNNING[(chat_id, nonce)] = task
    task.add_done_callback(lambda t: RUNNING.pop((chat_id, nonce), None))
    try:
        await task
    except asyncio.CancelledError:
        try:
            await ctx.bot.edit_message_text("⏹ توقّف التوليد.", chat_id, placeholder.message_id)
        except Exception:
            pass


async def _generate(ctx, chat_id: int, ph_id: int, prompt: str, attachments: list[str]):
    chat = db.get_chat(chat_id)
    model = (chat["model"] if chat and chat["model"] else None) or sess.default_model()
    email = (chat["session_email"] if chat else None) or sess.assign_chat(chat_id)

    async def run_once(em: str):
        gpt = sess.make_gpt(em)
        if not gpt:
            raise GPTError("auth", "no session")
        try:
            # refresh token if needed
            if not gpt.access_token:
                await gpt.refresh()
            if model == "auto":
                models = await gpt.models()
                use_model = _pick_model(models)
            else:
                use_model = model
            # upload attachments
            atts = []
            for p in attachments:
                fid = await gpt.upload_file(p, os.path.basename(p) or "file",
                                            _mime(p))
                atts.append({"file_id": fid, "size": os.path.getsize(p)})
            # stream
            c = db.get_chat(chat_id)
            conv_id = c["conversation_id"] if c else None
            parent = c["parent_message_id"] if c else None
            full = ""
            last_edit = 0.0
            done_ev: dict = {}
            async for ev in gpt.ask_stream(prompt, use_model, conv_id, parent, atts):
                if ev["type"] == "delta":
                    full = ev["text"]
                    now = time.monotonic()
                    if now - last_edit > EDIT_EVERY_SEC and full.strip():
                        last_edit = now
                        await _safe_edit(ctx, chat_id, ph_id,
                                         fmt.md_to_html(full[:MAX_TG_TEXT]))
                else:
                    done_ev = ev
            final_text = (done_ev.get("text") or full).strip()
            if done_ev.get("conversation_id"):
                db.set_chat(chat_id, conversation_id=done_ev["conversation_id"],
                            parent_message_id=done_ev.get("message_id"),
                            model=use_model if model == "auto" else None,
                            session_email=em)
            db.update_session(em, last_used=int(time.time()))
            # images generated?
            for fid in done_ev.get("assets", []):
                try:
                    blob = await gpt.download_asset(fid)
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                        tf.write(blob)
                        ip = tf.name
                    await ctx.bot.send_photo(chat_id, open(ip, "rb"))
                    os.unlink(ip)
                except Exception as e:
                    log.warning("asset send failed: %s", type(e).__name__)
            await _finalize(ctx, chat_id, ph_id, final_text or "…")
        finally:
            await gpt.close()

    try:
        await run_once(email)
    except GPTError as e:
        log.warning("gpt error %s on %s", e.kind, email[:6])
        if e.kind in ("blocked", "auth", "rate"):
            db.update_session(email, health="blocked" if e.kind != "auth" else "dead",
                              last_check=int(time.time()))
            # migrate chat to another healthy session and retry once
            db.set_chat(chat_id, session_email=None)
            email2 = sess.assign_chat(chat_id)
            if email2 and email2 != email:
                await _safe_edit(ctx, chat_id, ph_id, "🔄 عم جرّب جلسة تانية…")
                try:
                    await run_once(email2)
                    return
                except GPTError as e2:
                    e = e2
        await _safe_edit(ctx, chat_id, ph_id, _friendly_error(e))
    except Exception as e:
        log.warning("generate failed: %s", type(e).__name__)
        await _safe_edit(ctx, chat_id, ph_id, "⚠️ صار خطأ غير متوقع، جرّب مرة تانية.")


def _pick_model(models: list) -> str:
    slugs = [m.get("slug", "") for m in models if m.get("slug")]
    for want in ("gpt-5-mini", "gpt-5", "gpt-4o-mini", "gpt-4o", "o4-mini"):
        for s in slugs:
            if want in s:
                return s
    return slugs[0] if slugs else "gpt-5-mini"


def _mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif", ".pdf": "application/pdf",
            ".txt": "text/plain", ".mp4": "video/mp4"}.get(ext, "application/octet-stream")


async def _safe_edit(ctx, chat_id: int, msg_id: int, text: str):
    try:
        await ctx.bot.edit_message_text(text, chat_id, msg_id,
                                        parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True)
    except Exception:
        pass


async def _finalize(ctx, chat_id: int, msg_id: int, text: str):
    """Prefer a native rich message (10.1+); fall back to chunked HTML."""
    chunks = fmt.chunk_text(text, MAX_TG_TEXT)
    first = chunks[0]
    # try rich edit for the first chunk
    try:
        blocks = fmt.md_to_rich_blocks(first)
        res = await tg_raw.edit_message_rich(chat_id, msg_id, blocks)
        if res.get("ok"):
            for ch in chunks[1:]:
                await ctx.bot.send_message(chat_id, fmt.md_to_html(ch),
                                           parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=True)
            return
    except Exception as e:
        log.warning("rich edit failed: %s", type(e).__name__)
    # fallback: HTML edits
    await _safe_edit(ctx, chat_id, msg_id, fmt.md_to_html(first))
    for ch in chunks[1:]:
        try:
            await ctx.bot.send_message(chat_id, fmt.md_to_html(ch),
                                       parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
        except Exception:
            pass


def _friendly_error(e: GPTError) -> str:
    return {
        "blocked": "🚫 الجلسة محظورة من هالشبكة. المالك عم يضيف بروكسيات — جرّب بعد شوي.",
        "rate": "⏳ في ضغط على الجلسة حالياً، جرّب بعد دقيقة.",
        "auth": "🔑 الجلسة انتهت. المالك لازم يحدّث ملفات الجلسات.",
        "network": "📡 مشكلة اتصال، جرّب مرة تانية.",
    }.get(e.kind, "⚠️ صار خطأ، جرّب مرة تانية.")


# ---------------- callbacks ----------------
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    if data.startswith("stop:"):
        nonce = data.split(":", 1)[1]
        task = RUNNING.pop((chat_id, nonce), None)
        if task and not task.done():
            task.cancel()
            await q.answer("⏹ توقّف")
        else:
            await q.answer("خلص التوليد")
        return

    if not is_owner(uid):
        await q.answer("للمالك فقط 🔒")
        return

    if data == "eng:on":
        db.kv_set("engine_on", "1")
        await q.answer("✅ اشتغل البوت")
    elif data == "eng:off":
        db.kv_set("engine_on", "0")
        await q.answer("⏸ وقف البوت")
    elif data == "sess":
        rows = db.list_sessions()
        if not rows:
            await q.answer("لا جلسات بعد — ابعت zip")
            return
        icon = {"ok": "🟢", "blocked": "🔴", "dead": "⚫"}.get
        lines = [f"{icon(r['health'], '⚪️')} <code>{html.escape(r['email'][:18])}</code>"
                 for r in rows[:30]]
        more = f"\n…و {len(rows)-30} غيرها" if len(rows) > 30 else ""
        try:
            await q.message.reply_text("📥 الجلسات:\n" + "\n".join(lines) + more,
                                       parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await q.answer()
        return
    elif data == "prox":
        rows = db.list_proxies()
        lines = [f"{'🟢' if p['health']=='ok' else '🔴' if p['health']=='bad' else '⚪️'} "
                 f"<code>{p['id']}</code>" for p in rows] or ["لا بروكسيات بعد"]
        await q.message.reply_text("🌐 البروكسيات:\n" + "\n".join(lines) +
                                   "\n\nأضف بـ <code>/proxy add …</code> أو ملف txt.",
                                   parse_mode=ParseMode.HTML)
        await q.answer()
        return
    elif data == "stats":
        st = db.stats()
        await q.message.reply_text(
            f"📊 إحصائيات\nجلسات: {st['sessions']} (سليمة {st['sessions_ok']})\n"
            f"بروكسيات: {st['proxies']} (سليمة {st['proxies_ok']})\n"
            f"محادثات نشطة: {st['chats']}")
        await q.answer()
        return
    elif data == "wipe":
        await q.answer("🆕 كل محادثة بتتصفّر بأمر /new من صاحبها")
        return

    # refresh panel
    try:
        await q.message.edit_text(panel_text(), parse_mode=ParseMode.HTML,
                                  reply_markup=panel_kb())
    except Exception:
        pass
    await q.answer()


# ---------------- background ----------------
async def job_health(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        res = await sess.check_all_sessions()
        ok = sum(1 for v in res.values() if v == "ok")
        log.warning("health: %d/%d ok", ok, len(res))
    except Exception as e:
        log.warning("health job: %s", type(e).__name__)
    # auto top-up DISABLED: bulk harvesting floods the owner with one
    # approval card per proxy IP (platform limit). Proxies are added
    # manually via /proxy add instead.
    pass


async def _auto_harvest(ctx: ContextTypes.DEFAULT_TYPE):
    global HARVEST_RUNNING
    try:
        stats = await harvester.run_harvest(1000)
        w = len(stats.get("working", []))
        if w:
            await ctx.bot.send_message(
                OWNER_ID, f"🌐 حصاد تلقائي: انضاف {w} بروكسي شغال ✅")
            await _recheck_sessions(ctx)
    except Exception as e:
        log.warning("auto harvest failed: %s", type(e).__name__)
    finally:
        HARVEST_RUNNING = False


def main():
    if not BOT_TOKEN or not OWNER_ID:
        raise SystemExit("BOT_TOKEN / OWNER_ID missing in .env")
    db.init()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("proxy", cmd_proxy))
    app.add_handler(CommandHandler("harvest", cmd_harvest))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.job_queue.run_repeating(job_health, interval=600, first=60)

    log.warning("bot starting (owner=%d)", OWNER_ID)
    webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
    if webhook_url:
        # Render mode: Telegram POSTs updates to /webhook; service wakes on demand
        port = int(os.environ.get("PORT", "8443"))
        log.warning("webhook mode: %s/webhook (port %d)", webhook_url, port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="webhook",
            webhook_url=f"{webhook_url}/webhook",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
