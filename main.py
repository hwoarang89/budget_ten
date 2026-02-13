import os
import re
import json
import base64
import logging
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, Tuple

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))

DEFAULT_CURRENCY = (os.getenv("DEFAULT_CURRENCY", "UZS") or "UZS").strip().upper()

# Optional: allow only one forum topic (thread). If empty/0 -> all topics.
ALLOWED_THREAD_ID = os.getenv("ALLOWED_THREAD_ID", "").strip()
ALLOWED_THREAD_ID = int(ALLOWED_THREAD_ID) if ALLOWED_THREAD_ID.isdigit() else 0

# Optional: receipt recognition (photo)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini").strip()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("budget-bot")


# =========================
# DB
# =========================

def db():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        cursor_factory=RealDictCursor,
    )


def init_db():
    """
    Creates tables if missing and safely migrates legacy schemas.
    Safe to run on every start.
    """
    with db() as conn, conn.cursor() as cur:
        # ---- expenses ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                tg_user_id BIGINT,
                amount NUMERIC NOT NULL,
                currency TEXT NOT NULL,
                category TEXT NOT NULL,
                note TEXT,
                spent_at TIMESTAMP,
                spent_date DATE
            );
        """)

        # Legacy-safe columns
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS note TEXT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS spent_at TIMESTAMP;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS spent_date DATE;")

        # Ensure non-null timestamps/dates
        cur.execute("UPDATE expenses SET spent_at = NOW() WHERE spent_at IS NULL;")
        cur.execute("UPDATE expenses SET spent_date = CURRENT_DATE WHERE spent_date IS NULL;")
        cur.execute("ALTER TABLE expenses ALTER COLUMN spent_at SET NOT NULL;")
        cur.execute("ALTER TABLE expenses ALTER COLUMN spent_date SET NOT NULL;")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_expenses_chat_user_time
            ON expenses (chat_id, tg_user_id, spent_at);
        """)

        # ---- budgets ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                tg_user_id BIGINT,
                category TEXT NOT NULL,
                period TEXT NOT NULL,      -- 'daily' | 'monthly'
                limit_amount NUMERIC NOT NULL,
                currency TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)

        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;")
        cur.execute("UPDATE budgets SET created_at = NOW() WHERE created_at IS NULL;")
        cur.execute("ALTER TABLE budgets ALTER COLUMN created_at SET NOT NULL;")

        # Unique budget per (chat,user,category,period,currency)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS budgets_unique_personal
            ON budgets (chat_id, tg_user_id, category, period, currency);
        """)

        conn.commit()


# =========================
# BUSINESS
# =========================

def add_expense(chat_id: int, user_id: int, amount: float, currency: str, category: str, note: str = ""):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO expenses (
                chat_id, tg_user_id, amount, currency, category, note,
                spent_at, spent_date
            )
            VALUES (%s, %s, %s, %s, %s, %s, NOW(), CURRENT_DATE);
        """, (chat_id, user_id, amount, currency, category, note))
        conn.commit()


def set_budget(chat_id: int, user_id: int, category: str, period: str, limit_amount: float, currency: str):
    period = period.lower().strip()
    if period not in ("daily", "monthly"):
        raise ValueError("period must be daily or monthly")

    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO budgets (chat_id, tg_user_id, category, period, limit_amount, currency)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, tg_user_id, category, period, currency)
            DO UPDATE SET limit_amount = EXCLUDED.limit_amount, created_at = NOW();
        """, (chat_id, user_id, category, period, limit_amount, currency))
        conn.commit()


def get_budget(chat_id: int, user_id: int, category: str, period: str, currency: str) -> Optional[float]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT limit_amount
            FROM budgets
            WHERE chat_id=%s AND tg_user_id=%s AND category=%s AND period=%s AND currency=%s;
        """, (chat_id, user_id, category, period, currency))
        row = cur.fetchone()
        return float(row["limit_amount"]) if row else None


def spent_today(chat_id: int, user_id: int, category: str, currency: str) -> float:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0) AS s
            FROM expenses
            WHERE chat_id=%s AND tg_user_id=%s AND category=%s AND currency=%s AND spent_at >= %s;
        """, (chat_id, user_id, category, currency, today_start))
        return float(cur.fetchone()["s"])


def spent_month(chat_id: int, user_id: int, category: str, currency: str) -> float:
    today = date.today()
    month_start = datetime(today.year, today.month, 1)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0) AS s
            FROM expenses
            WHERE chat_id=%s AND tg_user_id=%s AND category=%s AND currency=%s AND spent_at >= %s;
        """, (chat_id, user_id, category, currency, month_start))
        return float(cur.fetchone()["s"])


def breakdown_today(chat_id: int, user_id: int) -> list:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT category, currency, COALESCE(SUM(amount), 0) AS spent
            FROM expenses
            WHERE chat_id=%s AND tg_user_id=%s AND spent_at >= %s
            GROUP BY category, currency
            ORDER BY spent DESC;
        """, (chat_id, user_id, today_start))
        return cur.fetchall()


def breakdown_month(chat_id: int, user_id: int) -> list:
    today = date.today()
    month_start = datetime(today.year, today.month, 1)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT category, currency, COALESCE(SUM(amount), 0) AS spent
            FROM expenses
            WHERE chat_id=%s AND tg_user_id=%s AND spent_at >= %s
            GROUP BY category, currency
            ORDER BY spent DESC;
        """, (chat_id, user_id, month_start))
        return cur.fetchall()


def list_budgets(chat_id: int, user_id: int) -> list:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT category, period, limit_amount, currency
            FROM budgets
            WHERE chat_id=%s AND tg_user_id=%s
            ORDER BY period, category;
        """, (chat_id, user_id))
        return cur.fetchall()


# =========================
# PARSING (TEXT EXPENSE)
# =========================

def parse_text_expense(text: str) -> Optional[Dict[str, Any]]:
    """
    Extracts first number as amount; category is first word of remaining text.
    Accepts: "кофе 1000", "1000 кофе", "такси 35 000".
    """
    t = (text or "").strip()
    if not t:
        return None

    m = re.search(r"(\d[\d\s]*)(?:[.,](\d+))?", t)
    if not m:
        return None

    whole = m.group(1).replace(" ", "")
    frac = m.group(2)
    num_str = whole + (("." + frac) if frac else "")
    try:
        amount = float(num_str)
    except Exception:
        return None

    rest = (t[:m.start()] + " " + t[m.end():]).strip()
    words = rest.split()
    category = words[0].lower() if words else "другое"
    note = " ".join(words[1:]) if len(words) > 1 else ""

    return {"amount": amount, "currency": DEFAULT_CURRENCY, "category": category, "note": note}


# =========================
# PHOTO RECEIPT RECOGNITION (OPTIONAL)
# =========================

RECEIPT_PROMPT = f"""
Extract expense data from this receipt image.
Return JSON only.

Format:
{{"type":"expense","amount":12345,"currency":"{DEFAULT_CURRENCY}","category":"продукты","note":"STORE NAME"}}

Rules:
- amount: total paid amount (number)
- category: short russian category (e.g., продукты, еда, кафе, такси, аптека, дом, другое)
- note: short merchant/store name if visible, else empty string
If you cannot confidently detect the total:
{{"type":"unknown"}}
""".strip()


async def parse_receipt(image_bytes: bytes) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        return {"type": "unknown"}

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "model": OPENAI_VISION_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": RECEIPT_PROMPT}]},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Extract the total amount from this receipt. Return JSON only."},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
                ],
            },
        ],
        "text": {"format": {"type": "json_object"}},
    }

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if r.status_code >= 400:
            logger.error("OpenAI Vision error %s: %s", r.status_code, r.text[:300])
            return {"type": "unknown"}
        data = r.json()

    out = ""
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                out += c.get("text", "")

    try:
        return json.loads(out) if out else {"type": "unknown"}
    except Exception:
        return {"type": "unknown"}


# =========================
# TELEGRAM HELPERS
# =========================

def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    return chat.type in ("group", "supergroup")

def allowed_topic(update: Update) -> bool:
    if not ALLOWED_THREAD_ID:
        return True
    msg = update.effective_message
    if not msg:
        return False
    return (msg.message_thread_id == ALLOWED_THREAD_ID)


# =========================
# HANDLERS
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_chat(update):
        await update.effective_message.reply_text("Этот бот предназначен для работы в группах. Добавьте бота в группу.")
        return
    await update.effective_message.reply_text(
        "Команды:\n"
        "• /budgetd <категория> <сумма> — дневной бюджет\n"
        "• /budgetm <категория> <сумма> — месячный бюджет\n"
        "• /my — мои бюджеты и остатки\n"
        "• /today — мои расходы за сегодня (разбивка)\n"
        "• /month — мои расходы за месяц (разбивка)\n\n"
        "Расход текстом: «кофе 1000» или «1000 кофе».\n"
        "Можно отправлять фото чека (если задан OPENAI_API_KEY)."
    )


async def budgetd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_chat(update) or not allowed_topic(update):
        return

    parts = (update.effective_message.text or "").split()
    if len(parts) < 3:
        await update.effective_message.reply_text("Формат: /budgetd категория сумма\nПример: /budgetd кофе 50000")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    category = parts[1].lower()

    try:
        limit_amount = float(parts[2].replace(" ", "").replace(",", "."))
    except Exception:
        await update.effective_message.reply_text("Не смогла распознать сумму. Пример: /budgetd кофе 50000")
        return

    set_budget(chat_id, user_id, category, "daily", limit_amount, DEFAULT_CURRENCY)
    await update.effective_message.reply_text(f"Дневной бюджет установлен: {category} — {limit_amount:.0f} {DEFAULT_CURRENCY}")


async def budgetm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_chat(update) or not allowed_topic(update):
        return

    parts = (update.effective_message.text or "").split()
    if len(parts) < 3:
        await update.effective_message.reply_text("Формат: /budgetm категория сумма\nПример: /budgetm еда 3000000")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    category = parts[1].lower()

    try:
        limit_amount = float(parts[2].replace(" ", "").replace(",", "."))
    except Exception:
        await update.effective_message.reply_text("Не смогла распознать сумму. Пример: /budgetm еда 3000000")
        return

    set_budget(chat_id, user_id, category, "monthly", limit_amount, DEFAULT_CURRENCY)
    await update.effective_message.reply_text(f"Месячный бюджет установлен: {category} — {limit_amount:.0f} {DEFAULT_CURRENCY}")


async def my_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_chat(update) or not allowed_topic(update):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    rows = list_budgets(chat_id, user_id)
    if not rows:
        await update.effective_message.reply_text("Бюджеты не заданы. Пример: /budgetd кофе 50000 или /budgetm еда 3000000")
        return

    lines = ["Ваши бюджеты и остатки:"]
    for r in rows:
        cat = r["category"]
        period = r["period"]
        cur = r["currency"]
        limit_amt = float(r["limit_amount"])

        if period == "daily":
            spent = spent_today(chat_id, user_id, cat, cur)
        else:
            spent = spent_month(chat_id, user_id, cat, cur)

        left = limit_amt - spent
        label = "день" if period == "daily" else "месяц"
        lines.append(f"• {cat} ({label}): лимит {limit_amt:.0f} {cur}, потрачено {spent:.0f} {cur}, осталось {left:.0f} {cur}")

    await update.effective_message.reply_text("\n".join(lines))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_chat(update) or not allowed_topic(update):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    rows = breakdown_today(chat_id, user_id)

    if not rows:
        await update.effective_message.reply_text("Сегодня расходов пока нет.")
        return

    lines = ["Ваши расходы за сегодня:"]
    total = 0.0
    for r in rows:
        spent = float(r["spent"])
        total += spent
        lines.append(f"• {r['category']}: {spent:.0f} {r['currency']}")

    lines.append(f"\nИтого: {total:.0f} {DEFAULT_CURRENCY}")
    await update.effective_message.reply_text("\n".join(lines))


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_chat(update) or not allowed_topic(update):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    rows = breakdown_month(chat_id, user_id)

    if not rows:
        await update.effective_message.reply_text("В этом месяце расходов пока нет.")
        return

    lines = ["Ваши расходы за месяц:"]
    total = 0.0
    for r in rows:
        spent = float(r["spent"])
        total += spent
        lines.append(f"• {r['category']}: {spent:.0f} {r['currency']}")

    lines.append(f"\nИтого: {total:.0f} {DEFAULT_CURRENCY}")
    await update.effective_message.reply_text("\n".join(lines))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if not is_group_chat(update) or not allowed_topic(update):
        return

    text = (update.effective_message.text or "").strip()
    if not text or text.startswith("/"):
        return

    parsed = parse_text_expense(text)
    if not parsed:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    amount = float(parsed["amount"])
    currency = str(parsed["currency"]).upper()
    category = str(parsed["category"]).lower()
    note = str(parsed.get("note", "")).strip()

    add_expense(chat_id, user_id, amount, currency, category, note)

    # Compute daily + monthly remainder (if budgets exist)
    d_limit = get_budget(chat_id, user_id, category, "daily", currency)
    m_limit = get_budget(chat_id, user_id, category, "monthly", currency)

    d_spent = spent_today(chat_id, user_id, category, currency)
    m_spent = spent_month(chat_id, user_id, category, currency)

    lines = [
        f"✅ Записано: {category} — {amount:.0f} {currency}",
    ]

    if d_limit is not None:
        lines.append(f"День: потрачено {d_spent:.0f} {currency}, лимит {d_limit:.0f}, осталось {d_limit - d_spent:.0f}")
    else:
        lines.append(f"День: бюджет не задан (/budgetd {category} 50000)")

    if m_limit is not None:
        lines.append(f"Месяц: потрачено {m_spent:.0f} {currency}, лимит {m_limit:.0f}, осталось {m_limit - m_spent:.0f}")
    else:
        lines.append(f"Месяц: бюджет не задан (/budgetm {category} 3000000)")

    await update.effective_message.reply_text("\n".join(lines))


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_message.photo:
        return
    if not is_group_chat(update) or not allowed_topic(update):
        return

    if not OPENAI_API_KEY:
        await update.effective_message.reply_text("Распознавание фото отключено: не задан OPENAI_API_KEY.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    photo = update.effective_message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    parsed = await parse_receipt(bytes(image_bytes))
    if parsed.get("type") != "expense":
        await update.effective_message.reply_text("Не удалось надёжно распознать сумму на фото. Напишите расход текстом, например: «еда 120000».")
        return

    try:
        amount = float(parsed.get("amount"))
    except Exception:
        await update.effective_message.reply_text("Не удалось корректно распознать сумму. Напишите расход текстом, например: «еда 120000».")
        return

    currency = str(parsed.get("currency") or DEFAULT_CURRENCY).upper()
    category = str(parsed.get("category") or "другое").lower()
    note = str(parsed.get("note") or "").strip()

    add_expense(chat_id, user_id, amount, currency, category, note)

    # Show daily + monthly remainder as well
    d_limit = get_budget(chat_id, user_id, category, "daily", currency)
    m_limit = get_budget(chat_id, user_id, category, "monthly", currency)
    d_spent = spent_today(chat_id, user_id, category, currency)
    m_spent = spent_month(chat_id, user_id, category, currency)

    lines = [
        "🧾 Чек распознан и записан",
        f"Категория: {category}",
        f"Сумма: {amount:.0f} {currency}",
        f"Примечание: {note or '-'}",
        ""
    ]

    if d_limit is not None:
        lines.append(f"День: потрачено {d_spent:.0f} {currency}, лимит {d_limit:.0f}, осталось {d_limit - d_spent:.0f}")
    else:
        lines.append(f"День: бюджет не задан (/budgetd {category} 50000)")

    if m_limit is not None:
        lines.append(f"Месяц: потрачено {m_spent:.0f} {currency}, лимит {m_limit:.0f}, осталось {m_limit - m_spent:.0f}")
    else:
        lines.append(f"Месяц: бюджет не задан (/budgetm {category} 3000000)")

    await update.effective_message.reply_text("\n".join(lines))


# =========================
# WEBHOOK
# =========================

def normalize_url(u: str) -> str:
    u = (u or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith("https://"):
        u = "https://" + u
    return u


def main():
    if not TELEGRAM_BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or DATABASE_URL")

    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("budgetd", budgetd_cmd))
    app.add_handler(CommandHandler("budgetm", budgetm_cmd))
    app.add_handler(CommandHandler("my", my_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("month", month_cmd))

    # Messages
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    public_url = normalize_url(PUBLIC_URL)
    if not public_url:
        raise RuntimeError("Missing PUBLIC_URL (example: https://xxxx.up.railway.app)")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{public_url}/telegram",
    )


if __name__ == "__main__":
    main()