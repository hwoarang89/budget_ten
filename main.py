import os
import re
import json
import base64
import logging
from datetime import datetime, date

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

# ================= CONFIG =================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini").strip()

DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "UZS").strip() or "UZS"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("budget-bot")


# ================= DB =================

def db():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        cursor_factory=RealDictCursor,
    )


def init_db():
    with db() as conn, conn.cursor() as cur:

        # EXPENSES TABLE
        cur.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                tg_user_id BIGINT,
                amount NUMERIC NOT NULL,
                currency TEXT NOT NULL,
                category TEXT NOT NULL,
                note TEXT
            );
        """)

        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;")

        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS spent_at TIMESTAMP;")
        cur.execute("UPDATE expenses SET spent_at = NOW() WHERE spent_at IS NULL;")
        cur.execute("ALTER TABLE expenses ALTER COLUMN spent_at SET NOT NULL;")

        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS spent_date DATE;")
        cur.execute("UPDATE expenses SET spent_date = CURRENT_DATE WHERE spent_date IS NULL;")
        cur.execute("ALTER TABLE expenses ALTER COLUMN spent_date SET NOT NULL;")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_expenses_chat_user_time
            ON expenses (chat_id, tg_user_id, spent_at);
        """)

        # BUDGETS TABLE
        cur.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                tg_user_id BIGINT,
                category TEXT NOT NULL,
                period TEXT NOT NULL,
                limit_amount NUMERIC NOT NULL,
                currency TEXT NOT NULL
            );
        """)

        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;")

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS budgets_unique_personal
            ON budgets (chat_id, tg_user_id, category, period, currency);
        """)

        conn.commit()


# ================= BUSINESS =================

def add_expense(chat_id, user_id, amount, currency, category, note=""):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO expenses (
                chat_id, tg_user_id, amount, currency, category,
                note, spent_at, spent_date
            )
            VALUES (%s, %s, %s, %s, %s, %s, NOW(), CURRENT_DATE);
        """, (chat_id, user_id, amount, currency, category, note))
        conn.commit()


def set_budget(chat_id, user_id, category, limit_amount, currency):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO budgets (chat_id, tg_user_id, category, period, limit_amount, currency)
            VALUES (%s, %s, %s, 'monthly', %s, %s)
            ON CONFLICT (chat_id, tg_user_id, category, period, currency)
            DO UPDATE SET limit_amount = EXCLUDED.limit_amount;
        """, (chat_id, user_id, category, limit_amount, currency))
        conn.commit()


def month_spent(chat_id, user_id, category, currency):
    month_start = date.today().replace(day=1)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) AS s
            FROM expenses
            WHERE chat_id=%s AND tg_user_id=%s
              AND category=%s AND currency=%s
              AND spent_at >= %s;
        """, (chat_id, user_id, category, currency,
              datetime(month_start.year, month_start.month, 1)))
        return float(cur.fetchone()["s"])


def get_budget(chat_id, user_id, category, currency):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT limit_amount FROM budgets
            WHERE chat_id=%s AND tg_user_id=%s
              AND category=%s AND period='monthly'
              AND currency=%s;
        """, (chat_id, user_id, category, currency))
        row = cur.fetchone()
        return float(row["limit_amount"]) if row else None


# ================= TEXT PARSER =================

def simple_parse(text):
    m = re.search(r"(\d[\d\s]*)", text)
    if not m:
        return None

    amount = float(m.group(1).replace(" ", ""))
    rest = (text[:m.start()] + " " + text[m.end():]).strip()
    words = rest.split()
    category = words[0].lower() if words else "other"

    return {
        "amount": amount,
        "category": category,
        "currency": DEFAULT_CURRENCY
    }


# ================= PHOTO RECEIPT OCR =================

RECEIPT_PROMPT = f"""
Extract expense data from this receipt image.
Return JSON only.

Format:
{{"type":"expense","amount":12345,"currency":"UZS","category":"продукты","note":"STORE NAME"}}

If amount cannot be confidently detected:
{{"type":"unknown"}}
"""


async def parse_receipt(image_bytes: bytes):
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
                    {"type": "input_text", "text": "Extract total amount from this receipt."},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}
                ]
            }
        ],
        "text": {"format": {"type": "json_object"}}
    }

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if r.status_code >= 400:
            logger.error("OpenAI Vision error: %s", r.text[:300])
            return {"type": "unknown"}
        data = r.json()

    output_text = ""
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                output_text += c.get("text", "")

    try:
        return json.loads(output_text)
    except Exception:
        return {"type": "unknown"}


# ================= HANDLERS =================

async def budget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split()
    if len(parts) < 3:
        await update.message.reply_text("Формат: /budget категория сумма")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    category = parts[1].lower()
    limit_amount = float(parts[2])

    set_budget(chat_id, user_id, category, limit_amount, DEFAULT_CURRENCY)

    await update.message.reply_text(
        f"Бюджет установлен: {category} — {limit_amount:.0f} {DEFAULT_CURRENCY}"
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.text.startswith("/"):
        return

    parsed = simple_parse(update.message.text)
    if not parsed:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    amount = parsed["amount"]
    category = parsed["category"]
    currency = parsed["currency"]

    add_expense(chat_id, user_id, amount, currency, category)

    spent = month_spent(chat_id, user_id, category, currency)
    limit_amt = get_budget(chat_id, user_id, category, currency)

    if limit_amt:
        left = limit_amt - spent
        await update.message.reply_text(
            f"Записано: {category} — {amount:.0f} {currency}\n"
            f"Осталось: {left:.0f} (лимит {limit_amt:.0f})"
        )
    else:
        await update.message.reply_text(
            f"Записано: {category} — {amount:.0f} {currency}\n"
            f"Бюджет не задан."
        )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    parsed = await parse_receipt(bytes(image_bytes))
    if parsed.get("type") != "expense":
        await update.message.reply_text("Не смогла распознать чек.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    amount = float(parsed.get("amount"))
    category = parsed.get("category", "другое")
    currency = parsed.get("currency", DEFAULT_CURRENCY)
    note = parsed.get("note", "")

    add_expense(chat_id, user_id, amount, currency, category, note)

    await update.message.reply_text(
        f"Чек записан: {amount:.0f} {currency}\nКатегория: {category}"
    )


# ================= MAIN =================

def normalize_url(u):
    u = u.strip().rstrip("/")
    if not u.startswith("https://"):
        u = "https://" + u
    return u


def main():
    if not TELEGRAM_BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or DATABASE_URL")

    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("budget", budget_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    public_url = normalize_url(PUBLIC_URL)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{public_url}/telegram",
    )


if __name__ == "__main__":
    main()