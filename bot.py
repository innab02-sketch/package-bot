#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
בוט טלגרם לניהול חבילות מהודעות SMS
======================================
הבוט מקבל הודעות SMS דרך שני ערוצים:
  1. Endpoint HTTP  POST /api/sms  (מ-Tasker ישירות)
  2. הודעות טקסט בצ'אט (forward ידני)

Environment variables:
  BOT_TOKEN   - Telegram bot token (required)
  GROUP_CHAT_ID - Telegram group chat ID to send packages to (required)
  PORT        - HTTP server port (default: 8080)
"""

import asyncio
import json
import logging
import sqlite3
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

from aiohttp import web

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

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN environment variable is not set!", file=sys.stderr)
    sys.exit(1)

# Group chat ID where packages are announced (set via env var or /start command)
GROUP_CHAT_ID_ENV = os.environ.get("GROUP_CHAT_ID")

# HTTP server port
PORT = int(os.environ.get("PORT", "8080"))

# Reminder interval: 3 days
REMINDER_INTERVAL_SECONDS = 3 * 24 * 60 * 60

# DB path
DB_PATH = Path(__file__).parent / "packages.db"

# Logging to stdout so Railway captures it
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Global reference to the bot Application (set in main)
_app: Application = None

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
        "INSERT INTO packages (chat_id, sms_text, sender, tracking_number, added_date) "
        "VALUES (?,?,?,?,?)",
        (chat_id, sms_text, sender, tracking_number,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
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
        "INSERT OR IGNORE INTO authorized_chats (chat_id, chat_title, added_date) "
        "VALUES (?,?,?)",
        (chat_id, chat_title, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def get_primary_chat_id():
    """Return GROUP_CHAT_ID env var (if set) or the first registered chat."""
    if GROUP_CHAT_ID_ENV:
        try:
            return int(GROUP_CHAT_ID_ENV)
        except ValueError:
            pass
    chats = get_authorized_chats()
    return chats[0] if chats else None


# ─── Keyword filter ──────────────────────────────────────────────────────────

# Strong keywords that unambiguously indicate a package/delivery SMS.
# "הזמנה" and "דואר" were intentionally removed - too generic (match restaurants, etc.).
# Multi-word phrases are matched as substrings (case-insensitive).
PACKAGE_KEYWORDS = [
    # Hebrew delivery terms
    "חבילה",
    "משלוח",
    "ממתין לאיסוף",
    "שליחויות",
    "דואר ישראל",
    "צ'יטה",
    # English delivery terms
    "delivery",
    "package",
    "shipment",
    "parcel",
    "pickup",
    "tracking",
    # Courier / delivery company names
    "cheetah",
    "dhl",
    "fedex",
    "ups",
    "israel post",
    "iherb",
    "wolt",
]

# "איסוף" alone is too generic; only match it when preceded by package-context words.
# "CN" is only meaningful when immediately followed by digits (e.g. CN100405980).
_CN_PATTERN = re.compile(r'\bCN\d+', re.IGNORECASE)
_ISUF_CONTEXT = re.compile(
    r'(?:חבילה|משלוח|parcel|package|shipment|delivery|דואר ישראל|שליחויות)'
    r'.{0,60}איסוף'
    r'|איסוף.{0,60}(?:חבילה|משלוח|parcel|package|shipment|delivery|דואר ישראל|שליחויות)',
    re.IGNORECASE | re.DOTALL,
)


def is_package_sms(text: str) -> bool:
    """Return True if the text is clearly a package/delivery notification."""
    lower = text.lower()
    # Check plain keyword list first (fast path)
    if any(kw in lower for kw in PACKAGE_KEYWORDS):
        return True
    # "ממתין לאיסוף" is already in PACKAGE_KEYWORDS; also check bare "איסוף" in context
    if "איסוף" in lower and _ISUF_CONTEXT.search(text):
        return True
    # CN followed by digits
    if _CN_PATTERN.search(text):
        return True
    return False


# ─── SMS processing (shared logic) ───────────────────────────────────────────

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


async def process_sms_and_notify(sms_text: str, chat_id: int, sender_label: str = ""):
    """
    Core logic: save package to DB and send a notification to the group.
    Called both from the HTTP endpoint and from the Telegram message handler.
    """
    global _app
    info = extract_tracking_info(sms_text)
    if sender_label:
        info["sender"] = sender_label

    package_id = add_package(chat_id, sms_text, info["sender"], info["tracking_number"])
    logger.info("Package #%s registered for chat %s", package_id, chat_id)

    confirm = f"📦 חבילה חדשה נרשמה! (#{package_id})\n\n{sms_text}"
    if info["sender"]:
        confirm += f"\nשולח: {info['sender']}"
    if info["tracking_number"]:
        confirm += f"\nמספר מעקב: {info['tracking_number']}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("נאספה!", callback_data=f"collect_{package_id}"),
            InlineKeyboardButton("עדיין לא", callback_data=f"keep_{package_id}"),
        ]
    ])

    await _app.bot.send_message(
        chat_id=chat_id,
        text=confirm,
        reply_markup=keyboard,
    )
    return package_id


# ─── aiohttp HTTP server ──────────────────────────────────────────────────────

async def handle_sms_api(request: web.Request) -> web.Response:
    """
    POST /api/sms
    Accepts ANY of:
      - JSON body:            {"text": "...", "sender": "...", "chat_id": -123}
      - Form-urlencoded body: text=...&sender=...&chat_id=...
      - Query string:         /api/sms?text=...&chat_id=...
      - Raw plain-text body:  entire body treated as SMS text

    'chat_id' is optional - falls back to GROUP_CHAT_ID env var or first registered chat.
    'sender' is optional.
    """
    try:
        content_type = (request.content_type or "").lower()

        # Read raw body once
        raw_bytes = await request.read()
        raw_body = raw_bytes.decode("utf-8", errors="replace").strip()

        logger.info(
            "[/api/sms] Incoming request | method=%s | content_type='%s' | body='%s'",
            request.method, content_type, raw_body[:300]
        )

        data = {}

        # Strategy 1: JSON
        if "application/json" in content_type and raw_body:
            try:
                data = json.loads(raw_body)
                logger.info("[/api/sms] Parsed as JSON")
            except Exception as je:
                logger.warning("[/api/sms] JSON parse failed: %s", je)

        # Strategy 2: form-urlencoded (Tasker default)
        if not data and raw_body:
            try:
                parsed = parse_qs(raw_body, keep_blank_values=True)
                if parsed:
                    data = {k: v[0] for k, v in parsed.items()}
                    logger.info("[/api/sms] Parsed as form-urlencoded: %s", data)
            except Exception as fe:
                logger.warning("[/api/sms] form-urlencoded parse failed: %s", fe)

        # Strategy 3: merge query-string params
        qs_params = dict(request.rel_url.query)
        if qs_params:
            logger.info("[/api/sms] Query params: %s", qs_params)
            for k, v in qs_params.items():
                if k not in data:
                    data[k] = v

        # Strategy 4: raw body as text fallback
        sms_text = str(data.get("text", "")).strip()
        if not sms_text and raw_body:
            # Use entire raw body as the SMS text
            sms_text = raw_body
            logger.info("[/api/sms] Using raw body as SMS text")

        sender_label = str(data.get("sender", "")).strip()

        if not sms_text:
            logger.warning("[/api/sms] Empty text. data=%s raw='%s'", data, raw_body[:100])
            return web.json_response(
                {"ok": False, "error": "Missing 'text' field"}, status=400
            )

        # Determine target chat
        raw_chat = data.get("chat_id")
        if raw_chat:
            try:
                chat_id = int(str(raw_chat).strip())
            except (ValueError, TypeError):
                return web.json_response(
                    {"ok": False, "error": f"Invalid chat_id: {raw_chat}"}, status=400
                )
        else:
            chat_id = get_primary_chat_id()

        if not chat_id:
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "No chat_id provided and no registered chat found. "
                        "Send /start in your group first, or set GROUP_CHAT_ID env var."
                    ),
                },
                status=400,
            )

        # Register the chat if not already known
        add_authorized_chat(chat_id)

        # Keyword filter — silently ignore non-package messages
        if not is_package_sms(sms_text):
            logger.info(
                "[/api/sms] Filtered out (no package keywords) | chat=%s | text='%s'",
                chat_id, sms_text[:80]
            )
            return web.json_response(
                {"ok": True, "filtered": True,
                 "reason": "No package keywords found in SMS text"}
            )

        logger.info(
            "[/api/sms] Processing | chat=%s | sender='%s' | text='%s'",
            chat_id, sender_label, sms_text[:80],
        )

        package_id = await process_sms_and_notify(sms_text, chat_id, sender_label)

        return web.json_response(
            {"ok": True, "package_id": package_id, "chat_id": chat_id}
        )

    except Exception as e:
        logger.error("[/api/sms] Unhandled error: %s", e, exc_info=True)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    """GET / - health check so Railway knows the service is up."""
    return web.json_response({"status": "ok", "bot": "package-bot"})


def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/sms", handle_sms_api)
    return app


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


# ─── Telegram command handlers ────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or "private"
    logger.info("[/start] chat_id=%s title='%s'", chat_id, chat_title)
    add_authorized_chat(chat_id, chat_title)

    text = (
        "📦 ברוכים הבאים לבוט ניהול חבילות!\n\n"
        "הבוט עוזר לכם לעקוב אחרי חבילות שמגיעות אליכם.\n\n"
        "שליחת SMS מ-Tasker:\n"
        "  POST <Railway URL>/api/sms\n"
        "  Body: {\"text\": \"...\", \"chat_id\": " + str(chat_id) + "}\n\n"
        "פקודות זמינות:\n"
        "/pending - חבילות שממתינות לאיסוף\n"
        "/all - כל החבילות\n"
        "/add [תיאור] - הוספה ידנית\n"
        "/remind - תזכורת עכשיו\n"
        "/stats - סטטיסטיקות\n"
        "/help - עזרה\n\n"
        f"Chat ID של הצ'אט הזה: {chat_id}"
    )
    await update.message.reply_text(text)
    logger.info("[/start] reply sent to chat_id=%s", chat_id)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info("[/help] chat_id=%s", chat_id)
    text = (
        "📦 עזרה - בוט ניהול חבילות\n\n"
        "Tasker שולח POST ל: <Railway URL>/api/sms\n"
        "Body (JSON): {\"text\": \"תוכן ה-SMS\", \"chat_id\": <מזהה הקבוצה>}\n\n"
        "פקודות:\n"
        "/pending - חבילות שממתינות\n"
        "/all - כל החבילות\n"
        "/add טקסט - הוספה ידנית\n"
        "/remind - תזכורת עכשיו\n"
        "/stats - סטטיסטיקות\n\n"
        "ניתן גם להעביר (forward) הודעות SMS ישירות לצ'אט הזה."
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
        pkg_id, sms_text, sender, tracking, added_date, collected, _, collected_by = pkg
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
    add_authorized_chat(chat_id, update.effective_chat.title or "")
    package_id = await process_sms_and_notify(sms_text, chat_id)
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


# ─── Plain-text message handler (manual forward from Tasker or user) ──────────

async def handle_sms_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle plain-text messages forwarded manually.
    NOTE: In groups this only fires if Group Privacy is OFF in BotFather.
    The primary SMS path is now POST /api/sms.
    """
    chat_id = update.effective_chat.id
    text = update.message.text
    if not text:
        return

    logger.info("[msg_handler] chat_id=%s text='%s'", chat_id, text[:60])

    if not is_package_sms(text):
        logger.info("[msg_handler] No package keywords - ignoring")
        return

    add_authorized_chat(chat_id, update.effective_chat.title or "")
    package_id = await process_sms_and_notify(text, chat_id)
    logger.info("[msg_handler] package #%s registered via direct message", package_id)


# ─── Callback handler ─────────────────────────────────────────────────────────

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


# ─── Reminders ────────────────────────────────────────────────────────────────

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
            logger.error("Error for chat %s: %s", chat_id, e, exc_info=True)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run_web_server(web_app: web.Application):
    """Run the aiohttp web server."""
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("HTTP server listening on 0.0.0.0:%d", PORT)
    logger.info("SMS endpoint: POST http://0.0.0.0:%d/api/sms", PORT)
    return runner


async def main_async():
    global _app

    logger.info("=" * 60)
    logger.info("Starting Telegram Package Bot")
    logger.info("DB path: %s", DB_PATH)
    logger.info("HTTP port: %d", PORT)
    logger.info("GROUP_CHAT_ID env: %s", GROUP_CHAT_ID_ENV or "(not set - use /start first)")
    logger.info("Reminder interval: every %d hours", REMINDER_INTERVAL_SECONDS // 3600)
    logger.info("=" * 60)

    init_db()

    # Build Telegram application
    _app = Application.builder().token(BOT_TOKEN).build()

    _app.add_error_handler(error_handler)

    _app.add_handler(CommandHandler("start",   cmd_start))
    _app.add_handler(CommandHandler("help",    cmd_help))
    _app.add_handler(CommandHandler("pending", cmd_pending))
    _app.add_handler(CommandHandler("all",     cmd_all))
    _app.add_handler(CommandHandler("add",     cmd_add))
    _app.add_handler(CommandHandler("remind",  cmd_remind))
    _app.add_handler(CommandHandler("stats",   cmd_stats))
    _app.add_handler(CallbackQueryHandler(handle_callback))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sms_message))

    _app.job_queue.run_repeating(
        scheduled_reminder,
        interval=REMINDER_INTERVAL_SECONDS,
        first=60,
        name="package_reminder",
    )

    # Build and start HTTP server
    web_app = build_web_app()
    runner = await run_web_server(web_app)

    # Start Telegram polling
    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    logger.info("Bot is running. Press Ctrl+C to stop.")

    # Keep running until interrupted
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down...")
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        await runner.cleanup()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
