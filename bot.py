#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
בוט טלגרם לניהול חבילות מהודעות SMS
======================================
הבוט מקבל הודעות SMS (דרך Tasker) עם מידע על חבילות,
שומר אותן במסד נתונים, ושולח תזכורות כל 3 ימים.

IMPORTANT: The bot token is read from the BOT_TOKEN environment variable.
           Set it in Railway -> Variables -> BOT_TOKEN
"""

import logging
import sqlite3
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# ─── הגדרות ───────────────────────────────────────────────────────────────────

# Token is ALWAYS read from environment variable - never hardcoded
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    print("FATAL ERROR: BOT_TOKEN environment variable is not set!", file=sys.stderr)
    print("Set it in Railway -> Variables -> BOT_TOKEN", file=sys.stderr)
    sys.exit(1)

# תדירות תזכורות בשניות (3 ימים = 259200 שניות)
REMINDER_INTERVAL_SECONDS = 3 * 24 * 60 * 60  # 259200

# נתיב מסד הנתונים
DB_PATH = Path(__file__).parent / "packages.db"

# הגדרת לוגים - stdout so Railway captures them
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ─── מסד נתונים ──────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            sms_text TEXT NOT NULL,
            sender TEXT DEFAULT '',
            tracking_number TEXT DEFAULT '',
            added_date TEXT NOT NULL,
            collected INTEGER DEFAULT 0,
            collected_date TEXT DEFAULT NULL,
            collected_by TEXT DEFAULT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS authorized_chats (
            chat_id INTEGER PRIMARY KEY,
            chat_title TEXT DEFAULT '',
            added_date TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    logger.info("DB initialised at %s", DB_PATH)


def add_package(chat_id, sms_text, sender="", tracking_number=""):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO packages (chat_id, sms_text, sender, tracking_number, added_date) VALUES (?,?,?,?,?)",
        (chat_id, sms_text, sender, tracking_number, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    package_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return package_id


def get_pending_packages(chat_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, sms_text, sender, tracking_number, added_date FROM packages "
        "WHERE chat_id=? AND collected=0 ORDER BY added_date DESC",
        (chat_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_packages(chat_id, limit=20):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, sms_text, sender, tracking_number, added_date, collected, "
        "collected_date, collected_by FROM packages "
        "WHERE chat_id=? ORDER BY added_date DESC LIMIT ?",
        (chat_id, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def mark_collected(package_id, collected_by):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE packages SET collected=1, collected_date=?, collected_by=? "
        "WHERE id=? AND collected=0",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), collected_by, package_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_authorized_chats():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM authorized_chats")
    chats = [row[0] for row in cursor.fetchall()]
    conn.close()
    return chats


def add_authorized_chat(chat_id, chat_title=""):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO authorized_chats (chat_id, chat_title, added_date) VALUES (?,?,?)",
        (chat_id, chat_title, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


# ─── עזר: חילוץ מידע מ-SMS ─────────────────────────────────────────────────

def extract_tracking_info(text):
    info = {"sender": "", "tracking_number": ""}
    tracking_patterns = [
        r'(?:מספר מעקב|tracking|מעקב|משלוח)[:\s]*([A-Za-z0-9\-]{6,30})',
        r'(?:מספר חבילה|package)[:\s]*([A-Za-z0-9\-]{6,30})',
        r'\b([A-Z]{2}\d{9}[A-Z]{2})\b',
        r'\b(\d{10,20})\b',
    ]
    for pattern in tracking_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            info["tracking_number"] = m.group(1)
            break
    sender_patterns = [
        r'(?:מאת|from|שולח)[:\s]*([^\n,]{2,30})',
        r'^([^\n:]{2,30})(?:\s*[-:]\s)',
    ]
    for pattern in sender_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            info["sender"] = m.group(1).strip()
            break
    return info


# ─── Error handler ────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception:", exc_info=context.error)
    tb = "".join(traceback.format_exception(
        type(context.error), context.error, context.error.__traceback__))
    logger.error("Traceback:\n%s", tb)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("אירעה שגיאה פנימית. נסה שוב.")
        except Exception:
            pass


# ─── פקודות הבוט ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or "private"
    logger.info("[/start] chat_id=%s title='%s'", chat_id, chat_title)
    add_authorized_chat(chat_id, chat_title)

    text = (
        "📦 ברוכים הבאים לבוט ניהול חבילות!\n\n"
        "הבוט עוזר לכם לעקוב אחרי חבילות שמגיעות אליכם.\n\n"
        "🔹 הודעות SMS עם מידע על חבילות יועברו לכאן דרך Tasker\n"
        "🔹 כל 3 ימים תקבלו תזכורת על חבילות שטרם נאספו\n"
        "🔹 ניתן לסמן חבילה כנאספה בלחיצת כפתור\n\n"
        "פקודות זמינות:\n"
        "/pending - חבילות שממתינות לאיסוף\n"
        "/all - כל החבילות\n"
        "/add [תיאור] - הוספה ידנית\n"
        "/remind - תזכורת עכשיו\n"
        "/stats - סטטיסטיקות\n"
        "/help - עזרה\n\n"
        f"Chat ID: {chat_id}"
    )
    await update.message.reply_text(text)
    logger.info("[/start] reply sent to chat_id=%s", chat_id)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("[/help] chat_id=%s", chat_id)
    text = (
        "📦 עזרה - בוט ניהול חבילות\n\n"
        "איך זה עובד?\n"
        "1. Tasker מזהה הודעות SMS עם המילה 'חבילה'\n"
        "2. ההודעה נשלחת אוטומטית לבוט\n"
        "3. הבוט שומר את המידע ומתזכר אתכם כל 3 ימים\n"
        "4. כשהחבילה נאספה - לוחצים על הכפתור\n\n"
        "פקודות:\n"
        "/pending - חבילות שממתינות\n"
        "/all - כל החבילות\n"
        "/add טקסט - הוספה ידנית\n"
        "/remind - תזכורת עכשיו\n"
        "/stats - סטטיסטיקות\n\n"
        "טיפ: ניתן גם להעביר (forward) הודעות SMS ישירות לצ'אט הזה"
    )
    await update.message.reply_text(text)


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("[/pending] chat_id=%s", chat_id)
    packages = get_pending_packages(chat_id)
    if not packages:
        await update.message.reply_text("אין חבילות ממתינות לאיסוף!")
        return
    await update.message.reply_text(f"📦 {len(packages)} חבילות ממתינות לאיסוף:")
    for pkg in packages:
        pkg_id, sms_text, sender, tracking, added_date = pkg
        await _send_package_card(chat_id, pkg_id, sms_text, sender, tracking, added_date, context)


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("[/all] chat_id=%s", chat_id)
    packages = get_all_packages(chat_id)
    if not packages:
        await update.message.reply_text("אין חבילות במערכת עדיין.")
        return
    lines = [f"📦 כל החבילות ({len(packages)} אחרונות):\n"]
    for pkg in packages:
        pkg_id, sms_text, sender, tracking, added_date, collected, collected_date, collected_by = pkg
        status = "נאספה" if collected else "ממתינה"
        date_short = added_date[:10] if added_date else "?"
        line = f"{'✅' if collected else '⏳'} #{pkg_id} | {date_short} | {status}\n  {sms_text[:60]}"
        if collected and collected_by:
            line += f"\n  נאספה ע\"י {collected_by}"
        lines.append(line)
    await update.message.reply_text("\n\n".join(lines))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("[/add] chat_id=%s args=%s", chat_id, context.args)
    if not context.args:
        await update.message.reply_text(
            "כדי להוסיף חבילה ידנית:\n/add תיאור החבילה\n\nלדוגמה:\n/add חבילה מאמזון - ספרים"
        )
        return
    sms_text = " ".join(context.args)
    info = extract_tracking_info(sms_text)
    package_id = add_package(chat_id, sms_text, info["sender"], info["tracking_number"])
    await update.message.reply_text(f"חבילה #{package_id} נוספה בהצלחה!\n\n{sms_text}")
    logger.info("[/add] package #%s added in chat %s", package_id, chat_id)


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("[/remind] chat_id=%s", chat_id)
    await send_reminder_to_chat(chat_id, context)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("[/stats] chat_id=%s", chat_id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM packages WHERE chat_id=?", (chat_id,))
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM packages WHERE chat_id=? AND collected=0", (chat_id,))
    pending = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM packages WHERE chat_id=? AND collected=1", (chat_id,))
    collected = cursor.fetchone()[0]
    conn.close()
    await update.message.reply_text(
        f"סטטיסטיקות חבילות\n\n"
        f"סה\"כ: {total}\n"
        f"ממתינות: {pending}\n"
        f"נאספו: {collected}"
    )


# ─── טיפול בהודעות SMS (מ-Tasker) ───────────────────────────────────────────

async def handle_sms_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle plain-text messages forwarded from Tasker.
    NOTE: In groups this only fires if Group Privacy is OFF in BotFather.
    To disable: BotFather -> /mybots -> Bot Settings -> Group Privacy -> Turn off
    """
    chat_id = update.effective_chat.id
    text = update.message.text
    if not text:
        return

    logger.info("[sms_handler] chat_id=%s text_preview='%s'", chat_id, text[:60])

    package_keywords = ["חבילה", "משלוח", "delivery", "package", "shipment", "הזמנה", "דואר"]
    if not any(kw in text.lower() for kw in package_keywords):
        logger.info("[sms_handler] No package keywords - ignoring")
        return

    add_authorized_chat(chat_id, update.effective_chat.title or "")
    info = extract_tracking_info(text)
    package_id = add_package(chat_id, text, info["sender"], info["tracking_number"])

    confirm = f"📦 חבילה חדשה נרשמה! (#{package_id})\n\n{text}"
    if info["sender"]:
        confirm += f"\nשולח: {info['sender']}"
    if info["tracking_number"]:
        confirm += f"\nמספר מעקב: {info['tracking_number']}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("נאספה!", callback_data=f"collect_{package_id}")]
    ])
    await update.message.reply_text(confirm, reply_markup=keyboard)
    logger.info("[sms_handler] package #%s registered in chat %s", package_id, chat_id)


# ─── טיפול בלחיצות כפתור ────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    logger.info("[callback] data='%s' user=%s", data, query.from_user.id)

    if data.startswith("collect_"):
        package_id = int(data.replace("collect_", ""))
        user = query.from_user
        user_name = user.first_name or user.username or "משתמש"
        if mark_collected(package_id, user_name):
            await query.edit_message_text(
                f"חבילה #{package_id} סומנה כנאספה!\n"
                f"נאספה ע\"י: {user_name}\n"
                f"תאריך: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
            logger.info("[callback] package #%s collected by %s", package_id, user_name)
        else:
            await query.edit_message_text(f"חבילה #{package_id} כבר סומנה כנאספה.")
    elif data.startswith("keep_"):
        await query.answer("החבילה נשארת ברשימת ההמתנה", show_alert=False)


# ─── תזכורות ─────────────────────────────────────────────────────────────────

async def send_reminder_to_chat(chat_id, context):
    packages = get_pending_packages(chat_id)
    if not packages:
        await context.bot.send_message(chat_id=chat_id, text="אין חבילות ממתינות! הכל נאסף.")
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"תזכורת: יש {len(packages)} חבילות שממתינות לאיסוף!",
    )
    for pkg in packages:
        pkg_id, sms_text, sender, tracking, added_date = pkg
        await _send_package_card(chat_id, pkg_id, sms_text, sender, tracking, added_date, context)


async def _send_package_card(chat_id, pkg_id, sms_text, sender, tracking, added_date, context):
    try:
        added = datetime.strptime(added_date, "%Y-%m-%d %H:%M:%S")
        days_ago = (datetime.now() - added).days
        days_text = f"{days_ago} ימים" if days_ago > 0 else "היום"
    except ValueError:
        days_text = "לא ידוע"

    card = f"📦 חבילה #{pkg_id}\nנוספה לפני: {days_text}\n"
    if sender:
        card += f"שולח: {sender}\n"
    if tracking:
        card += f"מעקב: {tracking}\n"
    card += f"\n{sms_text[:100]}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("נאספה!", callback_data=f"collect_{pkg_id}"),
            InlineKeyboardButton("עדיין לא", callback_data=f"keep_{pkg_id}"),
        ]
    ])
    await context.bot.send_message(chat_id=chat_id, text=card, reply_markup=keyboard)


async def scheduled_reminder(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running scheduled reminder...")
    chats = get_authorized_chats()
    logger.info("Sending reminders to %d chats", len(chats))
    for chat_id in chats:
        try:
            await send_reminder_to_chat(chat_id, context)
        except TelegramError as e:
            logger.error("Telegram error for chat %s: %s", chat_id, e)
        except Exception as e:
            logger.error("Unexpected error for chat %s: %s", chat_id, e, exc_info=True)


# ─── הפעלת הבוט ─────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 60)
    logger.info("Starting Telegram Package Bot")
    logger.info("DB path: %s", DB_PATH)
    logger.info("Reminder interval: every %d hours", REMINDER_INTERVAL_SECONDS // 3600)
    logger.info("=" * 60)

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Error handler must be first
    app.add_error_handler(error_handler)

    # Commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("all",     cmd_all))
    app.add_handler(CommandHandler("add",     cmd_add))
    app.add_handler(CommandHandler("remind",  cmd_remind))
    app.add_handler(CommandHandler("stats",   cmd_stats))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Plain-text messages (SMS forwarded from Tasker)
    # In groups: only fires when Group Privacy is OFF in BotFather
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sms_message))

    # Scheduled reminder every 3 days
    app.job_queue.run_repeating(
        scheduled_reminder,
        interval=REMINDER_INTERVAL_SECONDS,
        first=60,
        name="package_reminder",
    )

    logger.info("All handlers registered. Starting polling...")
    logger.info("NOTE: For the bot to read plain messages in groups,")
    logger.info("  Group Privacy must be OFF.")
    logger.info("  BotFather -> /mybots -> Bot Settings -> Group Privacy -> Turn off")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
