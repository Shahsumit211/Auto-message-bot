import logging
import json
import os
from datetime import timedelta

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    JobQueue,
    filters,
)


DATA_FILE = "bot_data.json"

# ========= STORAGE =========

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Simple migration: if no "users" key, start fresh
    if "users" not in data:
        data = {"users": {}}
    return data


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(DATA, f, ensure_ascii=False, indent=2)


DATA = load_data()


def get_user_data(user_id: int):
    """Get or create the block for this user."""
    users = DATA.setdefault("users", {})
    key = str(user_id)
    if key not in users:
        users[key] = {
            "channels": [],
            "messages": [],
            "settings": {
                "batch_size": 1,
                "interval_minutes": 5,
                "next_message_index": 0,
                "running": False,
            },
        }
    return users[key]


def get_user_settings(user_id: int):
    ud = get_user_data(user_id)
    return ud.setdefault("settings", {
        "batch_size": 1,
        "interval_minutes": 5,
        "next_message_index": 0,
        "running": False,
    })


# ========= HELPERS =========

def add_channel_entry(user_id: int, chat):
    user_data = get_user_data(user_id)
    channels = user_data["channels"]
    # Avoid duplicates by id
    for ch in channels:
        if ch["id"] == chat.id:
            return False
    channels.append({
        "id": chat.id,
        "title": chat.title or getattr(chat, "full_name", chat.title),
        "username": getattr(chat, "username", None),
    })
    save_data()
    return True


def channels_text(user_id: int) -> str:
    user_data = get_user_data(user_id)
    channels = user_data["channels"]
    if not channels:
        return "No channels added yet."
    lines = []
    for idx, ch in enumerate(channels, start=1):
        uname = f"@{ch['username']}" if ch.get("username") else ""
        lines.append(f"{idx}. {ch['title']} {uname} (id={ch['id']})")
    return "\n".join(lines)


def messages_text(user_id: int) -> str:
    user_data = get_user_data(user_id)
    messages = user_data["messages"]
    if not messages:
        return "No messages saved yet."
    lines = []
    for idx, m in enumerate(messages, start=1):
        lines.append(f"{idx}. [{m['type']}] {m.get('preview','')}")
    return "\n".join(lines)


def add_message_entry(user_id: int, message):
    user_data = get_user_data(user_id)
    msgs = user_data["messages"]

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

    msgs.append({
        "from_chat_id": message.chat_id,
        "message_id": message.message_id,
        "type": msg_type,
        "preview": preview,
    })
    save_data()


async def auto_sender(context: ContextTypes.DEFAULT_TYPE):
    """Job that sends messages for ONE user (user_id stored in job.data)."""
    user_id = context.job.data
    user_data = get_user_data(user_id)
    channels = user_data["channels"]
    messages = user_data["messages"]
    settings = user_data["settings"]

    if not channels or not messages:
        # Nothing to do for this user
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
                logging.warning("Failed to send to %s for user %s: %s", ch["id"], user_id, e)

        idx = (idx + 1) % total

    settings["next_message_index"] = idx
    save_data()


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
    user_data = get_user_data(user_id)

    if not user_data["channels"]:
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
    user_data = get_user_data(user_id)

    if not user_data["messages"]:
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
    user_data = get_user_data(user_id)
    count = len(user_data["messages"])
    user_data["messages"].clear()
    user_data["settings"]["next_message_index"] = 0
    save_data()
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
    user_data = get_user_data(user_id)
    settings = user_data["settings"]

    if settings.get("running"):
        await update.message.reply_text("Your auto messaging is already running.")
        return

    if not user_data["channels"]:
        await update.message.reply_text("Add at least one channel first with /addchannel.")
        return

    if not user_data["messages"]:
        await update.message.reply_text("Add at least one message first with /addmessage.")
        return

    minutes = settings.get("interval_minutes", 5)

    # ✅ use context.job_queue instead of context.application.job_queue
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
    save_data()
    await update.message.reply_text(
        f"✅ Your auto messaging started.\n"
        f"Interval: {minutes} minute(s), batch: {settings.get('batch_size',1)} message(s) per round."
    )



async def stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    settings = user_data["settings"]

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
    save_data()
    await update.message.reply_text("⏹ Your auto messaging stopped.")



async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    settings = user_data["settings"]
    running = "✅ RUNNING" if settings.get("running") else "⏹ STOPPED"
    text = (
        f"Your status: {running}\n\n"
        f"Your channels: {len(user_data['channels'])}\n"
        f"Your messages: {len(user_data['messages'])}\n"
        f"Your batch size: {settings.get('batch_size', 1)}\n"
        f"Your interval: {settings.get('interval_minutes', 5)} minute(s)\n"
        f"Next message index: {settings.get('next_message_index', 0) + 1}"
    )
    await update.message.reply_text(text)


async def mydata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    raw = json.dumps(user_data, ensure_ascii=False, indent=2)
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
    user_data = get_user_data(user_id)
    settings = user_data["settings"]

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
        channels = user_data["channels"]
        if not (0 <= idx < len(channels)):
            await msg.reply_text("Invalid index. Send a correct number or 0 to cancel.")
            return
        ch = channels.pop(idx)
        save_data()
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
        messages = user_data["messages"]
        if not (0 <= idx < len(messages)):
            await msg.reply_text("Invalid index. Send a correct number or 0 to cancel.")
            return
        m = messages.pop(idx)
        save_data()
        context.user_data["awaiting_remove_message_index"] = False
        await msg.reply_text(
            f"✅ Removed your message #{idx+1}: [{m['type']}] {m.get('preview','')}"
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
            await msg.reply_text("Batch size must be greater than 0. Try again or send 0 to cancel.")
            return
        settings["batch_size"] = val
        save_data()
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
            await msg.reply_text("Interval must be greater than 0. Try again or send 0 to cancel.")
            return
        settings["interval_minutes"] = val
        save_data()
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

    print("✅ Bot starting with multi-user interactive version...")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    # Re-start jobs for all users whose bots were running
    users = DATA.get("users", {})
    jq = application.job_queue
    if jq is not None:
        for user_id_str, user_data in users.items():
            settings = user_data.get("settings", {})
            if settings.get("running"):
                try:
                    user_id = int(user_id_str)
                except ValueError:
                    continue
                minutes = settings.get("interval_minutes", 5)
                jq.run_repeating(
                    auto_sender,
                    interval=timedelta(minutes=minutes),
                    first=0,
                    name=f"auto_sender_{user_id}",
                    data=user_id,
                )
    else:
        logging.warning("Job queue is None; auto-resume of jobs is disabled.")


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
        MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, capture_forwarded_channel)
    )

    # ALL other private messages for interactive input & addmessage
    application.add_handler(
        MessageHandler(filters.ALL & filters.ChatType.PRIVATE, handle_private_message)
    )

    application.run_polling()


if __name__ == "__main__":
    main()
