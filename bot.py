import logging
import json
import os
from datetime import timedelta

import psycopg2
from psycopg2.extras import RealDictCursor, Json

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    JobQueue,
    filters,
)

# ========= POSTGRES STORAGE =========

DATABASE_URL = os.getenv("DATABASE_URL")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    """Create user_data table if it doesn't exist."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_data (
            user_id BIGINT PRIMARY KEY,
            data JSONB NOT NULL
        );
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def load_user_data(user_id: int) -> dict:
    """Load one user's data from DB, or return default structure."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT data FROM user_data WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row and row.get("data"):
        data = row["data"]
    else:
        data = {
            "channels": [],
            "messages": [],
            "settings": {
                "batch_size": 1,
                "interval_minutes": 5,
                "next_message_index": 0,
                "running": False,
            },
        }
    return data


def save_user_data(user_id: int, data: dict):
    """Upsert one user's data."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO user_data (user_id, data)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data
        """,
        (user_id, Json(data)),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_all_users():
    """Get all (user_id, data) rows for auto-resume."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, data FROM user_data")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ========= PER-USER HELPERS =========

def get_user_data(user_id: int) -> dict:
    return load_user_data(user_id)


def get_user_settings(user_id: int) -> dict:
    data = get_user_data(user_id)
    settings = data.setdefault(
        "settings",
        {
            "batch_size": 1,
            "interval_minutes": 5,
            "next_message_index": 0,
            "running": False,
        },
    )
    # guarantee persistence even if settings was missing
    save_user_data(user_id, data)
    return settings


# ========= HELPERS =========

def add_channel_entry(user_id: int, chat):
    data = get_user_data(user_id)
    channels = data.setdefault("channels", [])

    # Avoid duplicates by id
    for ch in channels:
        if ch["id"] == chat.id:
            return False

    channels.append(
        {
            "id": chat.id,
            "title": chat.title or getattr(chat, "full_name", chat.title),
            "username": getattr(chat, "username", None),
        }
    )
    save_user_data(user_id, data)
    return True


def channels_text(user_id: int) -> str:
    data = get_user_data(user_id)
    channels = data.get("channels", [])
    if not channels:
        return "No channels added yet."
    lines = []
    for idx, ch in enumerate(channels, start=1):
        uname = f"@{ch['username']}" if ch.get("username") else ""
        lines.append(f"{idx}. {ch['title']} {uname} (id={ch['id']})")
    return "\n".join(lines)


def messages_text(user_id: int) -> str:
    data = get_user_data(user_id)
    messages = data.get("messages", [])
    if not messages:
        return "No messages saved yet."
    lines = []
    for idx, m in enumerate(messages, start=1):
        lines.append(f"{idx}. [{m['type']}] {m.get('preview', '')}")
    return "\n".join(lines)


def add_message_entry(user_id: int, message):
    data = get_user_data(user_id)
    msgs = data.setdefault("messages", [])

    msg_type = "text"
    if message.photo:
        msg_type = "photo"
    elif message.document:
        msg_type = "document"
    elif message.video:
        msg_type = "video"
    elif message.audio:
        msg_type = "audio"
    elif message.voice:
        msg_type = "voice"
    elif message.sticker:
        msg_type = "sticker"

    text = message.text or message.caption or ""
    preview = (text[:40] + "…") if len(text) > 40 else text

    msgs.append(
        {
            "from_chat_id": message.chat_id,
            "message_id": message.message_id,
            "type": msg_type,
            "preview": preview,
        }
    )
    save_user_data(user_id, data)


async def auto_sender(context: ContextTypes.DEFAULT_TYPE):
    """Job that sends messages for ONE user (user_id stored in job.data)."""
    user_id = context.job.data
    data = get_user_data(user_id)
    channels = data.get("channels", [])
    messages = data.get("messages", [])
    settings = data.setdefault(
        "settings",
        {
            "batch_size": 1,
            "interval_minutes": 5,
            "next_message_index": 0,
            "running": False,
        },
    )

    if not channels or not messages:
        save_user_data(user_id, data)
        return

    batch_size = settings.get("batch_size", 1)
    idx = settings.get("next_message_index", 0)
    total = len(messages)

    for _ in range(batch_size):
        msg = messages[idx]
        for ch in channels:
            try:
                await context.bot.copy_message(
                    chat_id=ch["id"],
                    from_chat_id=msg["from_chat_id"],
                    message_id=msg["message_id"],
                )
            except Exception as e:
                logging.warning(
                    "Failed to send to %s for user %s: %s", ch["id"], user_id, e
                )

        idx = (idx + 1) % total

    settings["next_message_index"] = idx
    save_user_data(user_id, data)


# ========= HANDLERS =========

WELCOME_TEXT = (
    "👋 Welcome!\n"
    "I’m your simple auto-message bot.\n\n"
    "Commands:\n"
    "/start - Start the bot\n"
    "/addchannel - Add your channel\n"
    "/listchannel - List added channels\n"
    "/removechannel - Remove a channel \n\n"
    "/addmessage - Add a message/template to send\n"
    "/listmessage - List saved messages\n"
    "/removemessage - Remove a message \n"
    "/clearmessage - Remove all messages\n\n"
    "/setbatch - Set messages per round\n"
    "/setinterval - Set minutes between rounds\n\n"
    "/startbot - Start auto messaging\n"
    "/stopbot - Stop auto messaging\n\n"
    "/status - Show current status\n"
    "/mydata - Show raw stored data"
)



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT)


# ---------- CHANNELS ----------

async def addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "To add a channel for *you*:\n"
        "1️⃣ Add this bot as admin in that channel.\n"
        "2️⃣ Forward *any* message from that channel to me.\n\n"
        "When you forward it, I’ll save the channel under *your* account."
    )


async def capture_forwarded_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    msg = update.message
    if not msg.forward_from_chat:
        return

    chat = msg.forward_from_chat
    if chat.type not in ("channel", "supergroup"):
        return

    user_id = update.effective_user.id
    added = add_channel_entry(user_id, chat)
    if added:
        await msg.reply_text(
            f"✅ Added channel for you:\n{chat.title} (id={chat.id})\n\n"
            "Use /listchannel to see your channels."
        )
    else:
        await msg.reply_text("ℹ️ That channel is already in *your* list.")


async def listchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(channels_text(user_id))


async def removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)

    if not data.get("channels"):
        await update.message.reply_text("You have no channels to remove.")
        return

    context.user_data["awaiting_remove_channel_index"] = True
    await update.message.reply_text(
        "These are *your* channels:\n\n"
        f"{channels_text(user_id)}\n\n"
        "Send the *number* of the channel you want to remove.\n"
        "Send 0 to cancel."
    )


# ---------- MESSAGES ----------

async def addmessage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["adding_messages"] = True
    await update.message.reply_text(
        "You are now in *add message* mode (for your own list).\n\n"
        "Send me the message you want to save.\n"
        "It can be *text*, *photo with caption*, *video*, etc.\n"
        "Every message you send will be saved until you send /done."
    )


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("adding_messages"):
        context.user_data["adding_messages"] = False
        await update.message.reply_text(
            "✅ Done adding messages.\nUse /listmessage to see your messages."
        )
    else:
        await update.message.reply_text("You are not in add-message mode.")


async def listmessage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(messages_text(user_id))


async def removemessage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)

    if not data.get("messages"):
        await update.message.reply_text("You have no messages to remove.")
        return

    context.user_data["awaiting_remove_message_index"] = True
    await update.message.reply_text(
        "These are *your* saved messages:\n\n"
        f"{messages_text(user_id)}\n\n"
        "Send the *number* of the message you want to remove.\n"
        "Send 0 to cancel."
    )


async def clearmessage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)
    count = len(data.get("messages", []))
    data["messages"] = []
    data.setdefault("settings", {})["next_message_index"] = 0
    save_user_data(user_id, data)
    await update.message.reply_text(f"✅ Cleared {count} of *your* messages.")


# ---------- SETTINGS (PER USER) ----------

async def setbatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    context.user_data["awaiting_batch_size"] = True
    await update.message.reply_text(
        f"Your current batch size: {settings.get('batch_size', 1)}\n\n"
        "Send the new *batch size* (number > 0).\n"
        "Send 0 to cancel."
    )


async def setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    context.user_data["awaiting_interval_minutes"] = True
    await update.message.reply_text(
        f"Your current interval: {settings.get('interval_minutes', 5)} minute(s)\n\n"
        "Send the new *interval in minutes* (number > 0).\n"
        "Send 0 to cancel."
    )


# ---------- START / STOP / STATUS / DATA ----------

async def startbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)
    settings = data.setdefault("settings", {})
    channels = data.get("channels", [])
    messages = data.get("messages", [])

    if settings.get("running"):
        await update.message.reply_text("Your auto messaging is already running.")
        return

    if not channels:
        await update.message.reply_text("Add at least one channel first with /addchannel.")
        return

    if not messages:
        await update.message.reply_text("Add at least one message first with /addmessage.")
        return

    minutes = settings.get("interval_minutes", 5)

    job_queue = context.job_queue
    if job_queue is None:
        await update.message.reply_text(
            "⚠️ Background job queue is not available in this deployment.\n"
            "Cannot start auto messaging."
        )
        return

    job_queue.run_repeating(
        auto_sender,
        interval=timedelta(minutes=minutes),
        first=0,
        name=f"auto_sender_{user_id}",
        data=user_id,
    )

    settings["running"] = True
    save_user_data(user_id, data)
    await update.message.reply_text(
        f"✅ Your auto messaging started.\n"
        f"Interval: {minutes} minute(s), batch: {settings.get('batch_size', 1)} message(s) per round."
    )


async def stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)
    settings = data.setdefault("settings", {})

    job_queue = context.job_queue
    if job_queue is None:
        await update.message.reply_text(
            "⚠️ Background job queue is not available in this deployment.\n"
            "Nothing to stop."
        )
        return

    jobs = job_queue.get_jobs_by_name(f"auto_sender_{user_id}")
    for j in jobs:
        j.schedule_removal()

    settings["running"] = False
    save_user_data(user_id, data)
    await update.message.reply_text("⏹ Your auto messaging stopped.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)
    settings = data.setdefault("settings", {})
    running = "✅ RUNNING" if settings.get("running") else "⏹ STOPPED"
    text = (
        f"Your status: {running}\n\n"
        f"Your channels: {len(data.get('channels', []))}\n"
        f"Your messages: {len(data.get('messages', []))}\n"
        f"Your batch size: {settings.get('batch_size', 1)}\n"
        f"Your interval: {settings.get('interval_minutes', 5)} minute(s)\n"
        f"Next message index: {settings.get('next_message_index', 0) + 1}"
    )
    await update.message.reply_text(text)


async def mydata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    if len(raw) < 3800:
        await update.message.reply_text(f"```json\n{raw}\n```", parse_mode="Markdown")
    else:
        filename = f"mydata_{user_id}.json"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(raw)
        await update.message.reply_document(open(filename, "rb"), filename=filename)


# ---------- MAIN PRIVATE MESSAGE HANDLER (INTERACTIVE INPUT PER USER) ----------

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles per-user interactive flows:
    - removechannel index
    - removemessage index
    - setbatch value
    - setinterval value
    - addmessage templates
    """
    if not update.message:
        return

    msg = update.message
    text = (msg.text or "").strip()
    user_id = update.effective_user.id
    data = get_user_data(user_id)
    settings = data.setdefault("settings", {})

    # 1) Remove channel index
    if context.user_data.get("awaiting_remove_channel_index"):
        if not text.isdigit():
            await msg.reply_text("Please send a valid number. Or send 0 to cancel.")
            return
        idx = int(text)
        if idx == 0:
            context.user_data["awaiting_remove_channel_index"] = False
            await msg.reply_text("❌ Channel removal cancelled.")
            return
        idx -= 1
        channels = data.get("channels", [])
        if not (0 <= idx < len(channels)):
            await msg.reply_text("Invalid index. Send a correct number or 0 to cancel.")
            return
        ch = channels.pop(idx)
        save_user_data(user_id, data)
        context.user_data["awaiting_remove_channel_index"] = False
        await msg.reply_text(
            f"✅ Removed your channel: {ch['title']} (id={ch['id']})"
        )
        return

    # 2) Remove message index
    if context.user_data.get("awaiting_remove_message_index"):
        if not text.isdigit():
            await msg.reply_text("Please send a valid number. Or send 0 to cancel.")
            return
        idx = int(text)
        if idx == 0:
            context.user_data["awaiting_remove_message_index"] = False
            await msg.reply_text("❌ Message removal cancelled.")
            return
        idx -= 1
        messages = data.get("messages", [])
        if not (0 <= idx < len(messages)):
            await msg.reply_text("Invalid index. Send a correct number or 0 to cancel.")
            return
        m = messages.pop(idx)
        save_user_data(user_id, data)
        context.user_data["awaiting_remove_message_index"] = False
        await msg.reply_text(
            f"✅ Removed your message #{idx+1}: [{m['type']}] {m.get('preview', '')}"
        )
        return

    # 3) Set batch size
    if context.user_data.get("awaiting_batch_size"):
        if not text.isdigit():
            await msg.reply_text("Please send a valid number. Or send 0 to cancel.")
            return
        val = int(text)
        if val == 0:
            context.user_data["awaiting_batch_size"] = False
            await msg.reply_text("❌ Batch size change cancelled.")
            return
        if val <= 0:
            await msg.reply_text(
                "Batch size must be greater than 0. Try again or send 0 to cancel."
            )
            return
        settings["batch_size"] = val
        save_user_data(user_id, data)
        context.user_data["awaiting_batch_size"] = False
        await msg.reply_text(f"✅ Your batch size is now {val} messages per round.")
        return

    # 4) Set interval minutes
    if context.user_data.get("awaiting_interval_minutes"):
        try:
            val = float(text)
        except ValueError:
            await msg.reply_text("Please send a valid number. Or send 0 to cancel.")
            return
        if val == 0:
            context.user_data["awaiting_interval_minutes"] = False
            await msg.reply_text("❌ Interval change cancelled.")
            return
        if val <= 0:
            await msg.reply_text(
                "Interval must be greater than 0. Try again or send 0 to cancel."
            )
            return
        settings["interval_minutes"] = val
        save_user_data(user_id, data)
        context.user_data["awaiting_interval_minutes"] = False
        await msg.reply_text(
            f"✅ Your interval is now {val} minute(s) between rounds.\n"
            "Stop and start your bot again (/stopbot then /startbot) to apply."
        )
        return

    # 5) Add message templates
    if context.user_data.get("adding_messages"):
        add_message_entry(user_id, msg)
        await msg.reply_text("✅ Saved this message template to *your* list.")
        return

    # Optional: generic reply for random messages
    # await msg.reply_text("Use /start to see available commands.")
    return


# ---------- MAIN ----------

def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN environment variable is not set.")
        return

    # Init DB table
    init_db()

    print("✅ Bot starting with multi-user interactive version (Postgres)...")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    # Auto-resume jobs for users whose bots were running
    jq = application.job_queue
    users = get_all_users()
    for row in users:
        user_id = row["user_id"]
        data = row.get("data") or {}
        settings = data.get("settings", {})
        if settings.get("running"):
            minutes = settings.get("interval_minutes", 5)
            jq.run_repeating(
                auto_sender,
                interval=timedelta(minutes=minutes),
                first=0,
                name=f"auto_sender_{user_id}",
                data=user_id,
            )

    # Command handlers
    application.add_handler(CommandHandler("start", start))

    application.add_handler(CommandHandler("addchannel", addchannel))
    application.add_handler(CommandHandler("listchannel", listchannel))
    application.add_handler(CommandHandler("removechannel", removechannel))

    application.add_handler(CommandHandler("addmessage", addmessage))
    application.add_handler(CommandHandler("done", done))
    application.add_handler(CommandHandler("listmessage", listmessage))
    application.add_handler(CommandHandler("removemessage", removemessage))
    application.add_handler(CommandHandler("clearmessage", clearmessage))

    application.add_handler(CommandHandler("setbatch", setbatch))
    application.add_handler(CommandHandler("setinterval", setinterval))

    application.add_handler(CommandHandler("startbot", startbot))
    application.add_handler(CommandHandler("stopbot", stopbot))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("mydata", mydata))

    # Forwarded messages from channels for /addchannel
    application.add_handler(
        MessageHandler(
            filters.FORWARDED & filters.ChatType.PRIVATE, capture_forwarded_channel
        )
    )

    # ALL other private messages for interactive input & addmessage
    application.add_handler(
        MessageHandler(filters.ALL & filters.ChatType.PRIVATE, handle_private_message)
    )

    application.run_polling()


if __name__ == "__main__":
    main()
