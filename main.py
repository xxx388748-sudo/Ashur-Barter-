import asyncio
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

from .storage import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ashur")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CONTROL_BOT_TOKEN = os.getenv("CONTROL_BOT_TOKEN", "").strip()
FERNET_KEY = os.getenv("FERNET_KEY", "").strip()
if not OWNER_ID or not CONTROL_BOT_TOKEN or not FERNET_KEY:
    raise SystemExit("أضف OWNER_ID و CONTROL_BOT_TOKEN و FERNET_KEY إلى متغيرات البيئة.")

try:
    cipher = Fernet(FERNET_KEY.encode())
except Exception as exc:
    raise SystemExit("قيمة FERNET_KEY غير صحيحة. أنشئ مفتاح Fernet جديداً.") from exc

store = Store()
child_apps = {}       # bot_id -> Application
child_tasks = {}      # bot_id -> asyncio.Task
recent = defaultdict(lambda: defaultdict(lambda: deque(maxlen=10)))
link_pattern = re.compile(r"(https?://|www\\.|t\\.me/|telegram\\.me/)", re.I)


def owner_only(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)


async def control_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🛡️ أهلاً بك في صانع آشور للحماية.\n\n"
        "إضافة بوت: أرسل /addbot ثم مسافة ثم توكن البوت من BotFather.\n"
        "/mybots — عرض البوتات المضافة\n"
        "/removebot رقم — حذف بوت من الصانع\n"
        "/help — المساعدة\n\n"
        "لا ترسل توكناتك لأي شخص. أرسلها هنا فقط إذا كنت متأكداً أن هذا بوتك الخاص."
    )


async def control_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await control_start(update, context)


async def start_child_bot(token: str, expected_id: int | None = None):
    """Start one user-owned protection bot inside the same worker process."""
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", child_start))
    app.add_handler(CommandHandler("help", child_help))
    app.add_handler(CommandHandler("status", child_status))
    app.add_handler(CommandHandler("warn", child_warn))
    app.add_handler(CommandHandler("ban", child_ban))
    app.add_handler(CommandHandler("unban", child_unban))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, moderate))
    await app.initialize()
    me = await app.bot.get_me()
    if expected_id is not None and me.id != expected_id:
        await app.shutdown()
        raise ValueError("توكن البوت لا يطابق بيانات البوت المسجلة.")
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    child_apps[me.id] = app
    log.info("Started child bot @%s (%s)", me.username, me.id)
    return me


async def stop_child_bot(bot_id: int):
    app = child_apps.pop(bot_id, None)
    if not app:
        return
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:
        log.exception("Could not stop child bot %s", bot_id)


async def addbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not owner_only(update):
        return await msg.reply_text("هذا الأمر لمالك الصانع فقط.")
    if not context.args:
        return await msg.reply_text("الصيغة:\n/addbot 123456:ABC...\nأرسل توكن بوتك الذي أنشأته عبر BotFather.")
    token = context.args[0].strip()
    if len(token) < 30 or ":" not in token:
        return await msg.reply_text("صيغة التوكن غير صحيحة. انسخه من BotFather دون مسافات.")
    try:
        probe = Application.builder().token(token).build()
        await probe.initialize()
        me = await probe.bot.get_me()
        await probe.shutdown()
        if me.id in child_apps or store.has_bot(me.id):
            return await msg.reply_text("هذا البوت مضاف مسبقاً.")
        encrypted = cipher.encrypt(token.encode()).decode()
        store.add_bot(me.id, me.username or str(me.id), encrypted)
        await start_child_bot(token, expected_id=me.id)
        await msg.reply_text(
            f"✅ تمت إضافة البوت @{me.username or me.id} وتشغيله.\n"
            "أضفه إلى مجموعتك واجعله مشرفاً بصلاحية حذف الرسائل وحظر الأعضاء."
        )
    except Exception as exc:
        log.warning("Add bot failed: %s", exc)
        await msg.reply_text("تعذرت إضافة البوت. تأكد من صحة التوكن وأنه غير مستخدم في webhook آخر.")


async def mybots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return await update.effective_message.reply_text("هذا الأمر لمالك الصانع فقط.")
    rows = store.list_bots()
    if not rows:
        return await update.effective_message.reply_text("ما أضفت أي بوت بعد. استخدم /addbot مع توكن بوتك.")
    lines = ["🤖 بوتاتك المسجلة:"]
    for i, (bot_id, username) in enumerate(rows, 1):
        state = "يعمل" if bot_id in child_apps else "متوقف"
        lines.append(f"{i}. @{username} — الرقم: {bot_id} — {state}")
    await update.effective_message.reply_text("\n".join(lines))


async def removebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return await update.effective_message.reply_text("هذا الأمر لمالك الصانع فقط.")
    if not context.args or not context.args[0].isdigit():
        return await update.effective_message.reply_text("استخدم: /removebot رقم_البوت من قائمة /mybots")
    bot_id = int(context.args[0])
    if not store.has_bot(bot_id):
        return await update.effective_message.reply_text("لم أجد هذا الرقم.")
    await stop_child_bot(bot_id)
    store.delete_bot(bot_id)
    await update.effective_message.reply_text("تم حذف البوت من الصانع وإيقافه.")


async def child_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🛡️ أنا بوت حماية من آشور.\n"
        "أضفني إلى مجموعتك وامنحني صلاحية حذف الرسائل وحظر الأعضاء.\n"
        "/help لعرض الأوامر."
    )


async def child_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "أوامر الحماية:\n/status — حالة البوت\n"
        "/warn بالرد على رسالة عضو — تحذير\n"
        "/ban بالرد — حظر\n/unban بالرد — فك الحظر\n\n"
        "الحماية التلقائية: حذف الروابط والرسائل المتكررة بسرعة."
    )


async def child_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("🟢 بوت آشور للحماية يعمل.")


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_chat or not update.effective_user:
        return False
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def child_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await is_admin(update, context):
        return await msg.reply_text("هذا الأمر للمشرفين فقط.")
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return await msg.reply_text("استخدم الأمر بالرد على رسالة العضو.")
    target = msg.reply_to_message.from_user
    count = store.add_warning(update.effective_chat.id, target.id)
    await msg.reply_text(f"⚠️ تحذير لـ {target.full_name}: {count}/3")
    if count >= 3:
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, target.id)
            store.reset_warnings(update.effective_chat.id, target.id)
            await msg.reply_text("🚫 تم حظر العضو بعد 3 تحذيرات.")
        except Exception:
            await msg.reply_text("لم أستطع الحظر. تحقق من صلاحيات البوت.")


async def child_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await is_admin(update, context):
        return await msg.reply_text("هذا الأمر للمشرفين فقط.")
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return await msg.reply_text("استخدم الأمر بالرد على رسالة العضو.")
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, msg.reply_to_message.from_user.id)
        await msg.reply_text("🚫 تم حظر العضو.")
    except Exception:
        await msg.reply_text("فشل الحظر. تحقق من صلاحيات البوت.")


async def child_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await is_admin(update, context):
        return await msg.reply_text("هذا الأمر للمشرفين فقط.")
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return await msg.reply_text("استخدم الأمر بالرد على رسالة العضو.")
    try:
        await context.bot.unban_chat_member(
            update.effective_chat.id, msg.reply_to_message.from_user.id, only_if_banned=True
        )
        await msg.reply_text("تم فك الحظر إذا كان العضو محظوراً.")
    except Exception:
        await msg.reply_text("تعذر فك الحظر.")


async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or chat.type == "private":
        return
    try:
        if await is_admin(update, context):
            return
    except Exception:
        return
    body = msg.text or msg.caption or ""
    reason = "الرابط ممنوع" if link_pattern.search(body) else None
    now = datetime.now(timezone.utc)
    bucket = recent[context.bot.id][chat.id, user.id]
    bucket.append(now)
    while bucket and now - bucket[0] > timedelta(seconds=8):
        bucket.popleft()
    if len(bucket) >= 6:
        reason = "إرسال رسائل بسرعة"
    if reason:
        try:
            await msg.delete()
            await context.bot.send_message(chat.id, f"⚠️ تم حذف رسالة {user.full_name}: {reason}.")
        except Exception:
            log.exception("Moderation failed")


async def restore_children():
    for bot_id, username, encrypted in store.list_secrets():
        try:
            token = cipher.decrypt(encrypted.encode()).decode()
            await start_child_bot(token, expected_id=bot_id)
        except (InvalidToken, Exception):
            log.exception("Could not restore child bot %s", bot_id)


async def post_init(app: Application):
    await restore_children()


async def post_shutdown(app: Application):
    for bot_id in list(child_apps):
        await stop_child_bot(bot_id)
    store.close()


def main():
    app = Application.builder().token(CONTROL_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", control_start))
    app.add_handler(CommandHandler("help", control_help))
    app.add_handler(CommandHandler("addbot", addbot))
    app.add_handler(CommandHandler("mybots", mybots))
    app.add_handler(CommandHandler("removebot", removebot))
    log.info("ASHUR multi-bot factory starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
