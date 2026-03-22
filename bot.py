#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
בוט טלגרם לניהול חבילות מהודעות SMS
======================================
הבוט מקבל הודעות SMS (דרך Tasker) עם מידע על חבילות,
שומר אותן במסד נתונים, ושולח תזכורות כל 3 ימים.
"""

import logging
import sqlite3
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─── הגדרות ───────────────────────────────────────────────────────────────────

BOT_TOKEN = "8766003392:AAH1TmcrbuvTLBuul6DVsNhP6DnBgdtpdrM"

# תדירות תזכורות בשניות (3 ימים = 259200 שניות)
REMINDER_INTERVAL_SECONDS = 3 * 24 * 60 * 60  # 259200

# נתיב מסד הנתונים
DB_PATH = Path(__file__).parent / "packages.db"

# הגדרת לוגים
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── מסד נתונים ──────────────────────────────────────────────────────────────

def init_db():
    """אתחול מסד הנתונים ויצירת טבלאות."""
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
    logger.info("מסד הנתונים אותחל בהצלחה")


def add_package(chat_id: int, sms_text: str, sender: str = "", tracking_number: str = "") -> int:
    """הוספת חבילה חדשה למסד הנתונים."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO packages (chat_id, sms_text, sender, tracking_number, added_date)
           VALUES (?, ?, ?, ?, ?)""",
        (chat_id, sms_text, sender, tracking_number, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    package_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return package_id


def get_pending_packages(chat_id: int) -> list:
    """קבלת כל החבילות שטרם נאספו עבור צ'אט מסוים."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, sms_text, sender, tracking_number, added_date FROM packages WHERE chat_id = ? AND collected = 0 ORDER BY added_date DESC",
        (chat_id,),
    )
    packages = cursor.fetchall()
    conn.close()
    return packages


def get_all_packages(chat_id: int, limit: int = 20) -> list:
    """קבלת כל החבילות (כולל שנאספו) עבור צ'אט מסוים."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, sms_text, sender, tracking_number, added_date, collected, collected_date, collected_by FROM packages WHERE chat_id = ? ORDER BY added_date DESC LIMIT ?",
        (chat_id, limit),
    )
    packages = cursor.fetchall()
    conn.close()
    return packages


def mark_collected(package_id: int, collected_by: str) -> bool:
    """סימון חבילה כנאספה."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE packages SET collected = 1, collected_date = ?, collected_by = ?
           WHERE id = ? AND collected = 0""",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), collected_by, package_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_authorized_chats() -> list:
    """קבלת רשימת כל הצ'אטים המורשים."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM authorized_chats")
    chats = [row[0] for row in cursor.fetchall()]
    conn.close()
    return chats


def add_authorized_chat(chat_id: int, chat_title: str = ""):
    """הוספת צ'אט מורשה."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO authorized_chats (chat_id, chat_title, added_date) VALUES (?, ?, ?)",
        (chat_id, chat_title, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


# ─── סינון הודעות SMS ────────────────────────────────────────────────────────

# מילות מפתח שיווקיות — אם אחת מהן קיימת, ההודעה נדחית מיד
MARKETING_KEYWORDS = [
    "קנו", "קנייה", "קישור לרכישה", "לרכישה",
    "מבצע", "הנחה", "קופון", "מתנה", "בתוקף עד",
    "לחצו כאן", "לחץ כאן", "http://", "https://",
    "קנה עכשיו", "קנו עכשיו", "shop", "store",
]

# מילות מפתח חזקות — מספיק אחת מהן כדי לאשר שזו הודעת חבילה
STRONG_KEYWORDS = [
    "חבילה ממתינה", "חבילה הגיעה", "ממתין לאיסוף",
    "נמצאת בנקודת איסוף", "נמצא בנקודת איסוף",
    "מוכנה לאיסוף", "מוכן לאיסוף",
    "הזמנתכם", "הזמנתך", "לאסוף את ההזמנה",
    "קוד אימות מסירה", "קוד מסירה",
    "tms",
    "tracking", "מספר מעקב",
    "נשלחה אליך", "נשלח אליך", "בדרך אליך", "נמסרה", "נמסר",
    "courier", "dhl", "fedex", "ups", "tnt",
    "צ'יטה", "דואר ישראל", "iherb",
]

# מילות מפתח חלשות — צריך לפחות 2 מהן (ללא מילות שיווק) כדי לאשר
WEAK_KEYWORDS = [
    "חבילה", "משלוח", "delivery", "package", "shipment",
    "איסוף", "נקודת איסוף",
]


def is_delivery_sms(text: str) -> bool:
    """
    בודק האם הודעת SMS עוסקת באמת בחבילה/משלוח ולא בשיווק.

    הלוגיקה:
    1. אם ההודעה מכילה מילת שיווק → דחייה מיידית.
    2. אם ההודעה מכילה מילת מפתח חזקה → אישור מיידי.
    3. אם ההודעה מכילה לפחות 2 מילות מפתח חלשות → אישור.
    4. אחרת → דחייה.
    """
    lower = text.lower()

    # שלב 1 — סינון שיווקי
    for kw in MARKETING_KEYWORDS:
        if kw.lower() in lower:
            logger.info(f"הודעה נדחתה (שיווקית) — מילת מפתח: '{kw}'")
            return False

    # שלב 2 — מילת מפתח חזקה
    for kw in STRONG_KEYWORDS:
        if kw.lower() in lower:
            logger.info(f"הודעה אושרה (מילה חזקה: '{kw}')")
            return True

    # שלב 3 — לפחות 2 מילות מפתח חלשות
    weak_matches = [kw for kw in WEAK_KEYWORDS if kw.lower() in lower]
    if len(weak_matches) >= 2:
        logger.info(f"הודעה אושרה (מילים חלשות: {weak_matches})")
        return True

    logger.info("הודעה נדחתה — לא עמדה בתנאי הסינון")
    return False


# ─── עזר: חילוץ מידע מ-SMS ─────────────────────────────────────────────────

def extract_tracking_info(text: str) -> dict:
    """חילוץ מספר מעקב ושולח מתוך הודעת SMS."""
    info = {"sender": "", "tracking_number": ""}

    # ניסיון לחלץ מספר מעקב (רצף של אותיות ומספרים ארוך)
    tracking_patterns = [
        r'(?:מספר מעקב|tracking|מעקב|משלוח)[:\s]*([A-Za-z0-9\-]{6,30})',
        r'(?:מספר חבילה|package)[:\s]*([A-Za-z0-9\-]{6,30})',
        r'\b([A-Z]{2}\d{9}[A-Z]{2})\b',  # פורמט בינלאומי
        r'\b(\d{10,20})\b',  # רצף מספרים ארוך
    ]

    for pattern in tracking_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            info["tracking_number"] = match.group(1)
            break

    # ניסיון לחלץ שם השולח/חברת משלוח
    sender_patterns = [
        r'(?:מאת|from|שולח)[:\s]*([^\n,]{2,30})',
        r'^([^\n:]{2,30})(?:\s*[-:]\s)',
    ]

    for pattern in sender_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            info["sender"] = match.group(1).strip()
            break

    return info


# ─── פקודות הבוט ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /start - רישום הצ'אט והצגת הודעת פתיחה."""
    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or "צ'אט פרטי"

    add_authorized_chat(chat_id, chat_title)

    welcome_text = (
        "📦 *ברוכים הבאים לבוט ניהול חבילות!*\n\n"
        "הבוט הזה עוזר לכם לעקוב אחרי חבילות שמגיעות אליכם\\.\n\n"
        "🔹 הודעות SMS עם מידע על חבילות יועברו לכאן אוטומטית דרך Tasker\n"
        "🔹 כל 3 ימים תקבלו תזכורת על חבילות שטרם נאספו\n"
        "🔹 ניתן לסמן חבילה כנאספה בלחיצת כפתור\n\n"
        "*פקודות זמינות:*\n"
        "/start \\- רישום הצ'אט והצגת הודעה זו\n"
        "/pending \\- הצגת חבילות שממתינות לאיסוף\n"
        "/all \\- הצגת כל החבילות \\(כולל שנאספו\\)\n"
        "/add \\- הוספת חבילה ידנית\n"
        "/remind \\- שליחת תזכורת עכשיו\n"
        "/help \\- עזרה\n\n"
        f"✅ הצ'אט הזה נרשם בהצלחה \\(ID: `{chat_id}`\\)"
    )

    await update.message.reply_text(welcome_text, parse_mode="MarkdownV2")
    logger.info(f"צ'אט נרשם: {chat_id} ({chat_title})")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /help - הצגת עזרה."""
    help_text = (
        "📦 *עזרה \\- בוט ניהול חבילות*\n\n"
        "*איך זה עובד?*\n"
        "1\\. Tasker מזהה הודעות SMS עם המילה \\'חבילה\\'\n"
        "2\\. ההודעה נשלחת אוטומטית לבוט\n"
        "3\\. הבוט שומר את המידע ומתזכר אתכם כל 3 ימים\n"
        "4\\. כשהחבילה נאספה \\- לוחצים על הכפתור\n\n"
        "*פקודות:*\n"
        "/pending \\- חבילות שממתינות\n"
        "/all \\- כל החבילות\n"
        "/add טקסט \\- הוספה ידנית\n"
        "/remind \\- תזכורת עכשיו\n"
        "/stats \\- סטטיסטיקות\n\n"
        "*טיפ:* ניתן גם להעביר \\(forward\\) הודעות SMS ישירות לצ'אט הזה"
    )
    await update.message.reply_text(help_text, parse_mode="MarkdownV2")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /pending - הצגת חבילות ממתינות."""
    chat_id = update.effective_chat.id
    packages = get_pending_packages(chat_id)

    if not packages:
        await update.message.reply_text("✅ אין חבילות ממתינות לאיסוף! 🎉")
        return

    await update.message.reply_text(
        f"📦 *{len(packages)} חבילות ממתינות לאיסוף:*",
        parse_mode="MarkdownV2",
    )

    for pkg in packages:
        pkg_id, sms_text, sender, tracking, added_date = pkg
        await _send_package_card(update.effective_chat.id, pkg_id, sms_text, sender, tracking, added_date, context)


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /all - הצגת כל החבילות."""
    chat_id = update.effective_chat.id
    packages = get_all_packages(chat_id)

    if not packages:
        await update.message.reply_text("📭 אין חבילות במערכת עדיין.")
        return

    text_lines = [f"📦 *כל החבילות \\({len(packages)} אחרונות\\):*\n"]

    for pkg in packages:
        pkg_id, sms_text, sender, tracking, added_date, collected, collected_date, collected_by = pkg
        status = "✅ נאספה" if collected else "⏳ ממתינה"
        # Escape special characters for MarkdownV2
        safe_sms = _escape_md(sms_text[:60])
        safe_date = _escape_md(added_date[:10])
        line = f"{status} \\| *#{pkg_id}* \\| {safe_date}\n└ {safe_sms}"
        if collected and collected_by:
            safe_by = _escape_md(collected_by)
            line += f"\n└ נאספה ע\\'י {safe_by}"
        text_lines.append(line)

    await update.message.reply_text("\n\n".join(text_lines), parse_mode="MarkdownV2")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /add - הוספת חבילה ידנית."""
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "📝 כדי להוסיף חבילה ידנית, כתבו:\n/add תיאור החבילה\n\nלדוגמה:\n/add חבילה מאמזון - ספרים"
        )
        return

    sms_text = " ".join(context.args)
    info = extract_tracking_info(sms_text)
    package_id = add_package(chat_id, sms_text, info["sender"], info["tracking_number"])

    await update.message.reply_text(
        f"✅ חבילה #{package_id} נוספה בהצלחה!\n\n📝 {sms_text}"
    )
    logger.info(f"חבילה #{package_id} נוספה ידנית בצ'אט {chat_id}")


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /remind - שליחת תזכורת עכשיו."""
    chat_id = update.effective_chat.id
    await send_reminder_to_chat(chat_id, context)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /stats - סטטיסטיקות."""
    chat_id = update.effective_chat.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM packages WHERE chat_id = ?", (chat_id,))
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM packages WHERE chat_id = ? AND collected = 0", (chat_id,))
    pending = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM packages WHERE chat_id = ? AND collected = 1", (chat_id,))
    collected = cursor.fetchone()[0]

    conn.close()

    text = (
        f"📊 *סטטיסטיקות חבילות*\n\n"
        f"📦 סה\\'כ חבילות: {total}\n"
        f"⏳ ממתינות לאיסוף: {pending}\n"
        f"✅ נאספו: {collected}"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


# ─── טיפול בהודעות SMS (מ-Tasker) ───────────────────────────────────────────

async def handle_sms_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בהודעת SMS שהועברה מ-Tasker."""
    chat_id = update.effective_chat.id
    text = update.message.text

    if not text:
        return

    # בדיקה אם ההודעה עוסקת בחבילה אמיתית (ולא שיווקית)
    if not is_delivery_sms(text):
        return

    # רישום הצ'אט אם לא רשום
    add_authorized_chat(chat_id, update.effective_chat.title or "")

    # חילוץ מידע
    info = extract_tracking_info(text)

    # שמירה במסד הנתונים
    package_id = add_package(chat_id, text, info["sender"], info["tracking_number"])

    # שליחת אישור
    confirm_text = f"📦 *חבילה חדשה נרשמה\\!* \\(#{package_id}\\)\n\n"
    safe_text = _escape_md(text)
    confirm_text += f"📝 {safe_text}\n"

    if info["sender"]:
        safe_sender = _escape_md(info["sender"])
        confirm_text += f"📤 שולח: {safe_sender}\n"
    if info["tracking_number"]:
        safe_tracking = _escape_md(info["tracking_number"])
        confirm_text += f"🔢 מספר מעקב: `{safe_tracking}`\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ נאספה!", callback_data=f"collect_{package_id}")]
    ])

    await update.message.reply_text(confirm_text, parse_mode="MarkdownV2", reply_markup=keyboard)
    logger.info(f"חבילה #{package_id} נרשמה מ-SMS בצ'אט {chat_id}")


# ─── טיפול בלחיצות כפתור ────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בלחיצת כפתור inline."""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("collect_"):
        package_id = int(data.replace("collect_", ""))
        user = query.from_user
        user_name = user.first_name or user.username or "משתמש"

        if mark_collected(package_id, user_name):
            await query.edit_message_text(
                f"✅ חבילה #{package_id} סומנה כנאספה!\n"
                f"👤 נאספה על ידי: {user_name}\n"
                f"📅 תאריך: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
            logger.info(f"חבילה #{package_id} נאספה ע\"י {user_name}")
        else:
            await query.edit_message_text(
                f"ℹ️ חבילה #{package_id} כבר סומנה כנאספה קודם לכן."
            )

    elif data.startswith("keep_"):
        package_id = int(data.replace("keep_", ""))
        await query.answer("👍 החבילה נשארת ברשימת ההמתנה", show_alert=False)


# ─── תזכורות ─────────────────────────────────────────────────────────────────

async def send_reminder_to_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """שליחת תזכורת על חבילות ממתינות לצ'אט מסוים."""
    packages = get_pending_packages(chat_id)

    if not packages:
        await context.bot.send_message(
            chat_id=chat_id,
            text="✅ אין חבילות ממתינות! הכל נאסף. 🎉"
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔔 *תזכורת: יש {len(packages)} חבילות שממתינות לאיסוף\\!*\n\nהאם אספתם אותן?",
        parse_mode="MarkdownV2",
    )

    for pkg in packages:
        pkg_id, sms_text, sender, tracking, added_date = pkg
        await _send_package_card(chat_id, pkg_id, sms_text, sender, tracking, added_date, context)


async def _send_package_card(chat_id: int, pkg_id: int, sms_text: str, sender: str, tracking: str, added_date: str, context: ContextTypes.DEFAULT_TYPE):
    """שליחת כרטיס חבילה עם כפתורי פעולה."""
    # חישוב ימים מאז ההוספה
    try:
        added = datetime.strptime(added_date, "%Y-%m-%d %H:%M:%S")
        days_ago = (datetime.now() - added).days
        days_text = f"{days_ago} ימים" if days_ago > 0 else "היום"
    except ValueError:
        days_text = "לא ידוע"

    card_text = f"📦 *חבילה \\#{pkg_id}*\n"
    card_text += f"📅 נוספה לפני: {_escape_md(days_text)}\n"

    if sender:
        card_text += f"📤 שולח: {_escape_md(sender)}\n"
    if tracking:
        card_text += f"🔢 מעקב: `{_escape_md(tracking)}`\n"

    card_text += f"\n📝 {_escape_md(sms_text[:100])}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ נאספה!", callback_data=f"collect_{pkg_id}"),
            InlineKeyboardButton("⏳ עדיין לא", callback_data=f"keep_{pkg_id}"),
        ]
    ])

    await context.bot.send_message(
        chat_id=chat_id,
        text=card_text,
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


async def scheduled_reminder(context: ContextTypes.DEFAULT_TYPE):
    """משימה מתוזמנת - שליחת תזכורות לכל הצ'אטים המורשים."""
    logger.info("מריץ תזכורת מתוזמנת...")
    chats = get_authorized_chats()

    for chat_id in chats:
        try:
            await send_reminder_to_chat(chat_id, context)
        except Exception as e:
            logger.error(f"שגיאה בשליחת תזכורת לצ'אט {chat_id}: {e}")


# ─── עזר ─────────────────────────────────────────────────────────────────────

def _escape_md(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


# ─── הפעלת הבוט ─────────────────────────────────────────────────────────────

def main():
    """הפעלת הבוט."""
    # אתחול מסד הנתונים
    init_db()

    # יצירת אפליקציית הבוט
    app = Application.builder().token(BOT_TOKEN).build()

    # רישום פקודות
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # טיפול בלחיצות כפתור
    app.add_handler(CallbackQueryHandler(handle_callback))

    # טיפול בהודעות טקסט (SMS מ-Tasker)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sms_message))

    # הגדרת תזכורת מתוזמנת כל 3 ימים
    job_queue = app.job_queue
    job_queue.run_repeating(
        scheduled_reminder,
        interval=REMINDER_INTERVAL_SECONDS,
        first=10,  # תזכורת ראשונה 10 שניות אחרי ההפעלה (לבדיקה)
        name="package_reminder",
    )

    logger.info("🚀 הבוט מופעל...")
    logger.info(f"📂 מסד נתונים: {DB_PATH}")
    logger.info(f"⏰ תזכורות כל {REMINDER_INTERVAL_SECONDS // 3600} שעות")

    # הפעלת הבוט
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
