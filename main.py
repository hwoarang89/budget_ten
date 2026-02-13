import os
import re
import logging
from datetime import datetime, date

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
    """
    Creates tables if missing and safely migrates legacy schemas.
    This function is safe to run on every start.
    """
    with db() as conn, conn.cursor() as cur:

        # -------- expenses table (legacy-safe) --------
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

        # Ensure key columns exist (legacy)
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;")

        # Some old versions had spent_date (DATE NOT NULL). New version uses spent_at too.
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS spent_at TIMESTAMP;")
        cur.execute("UPDATE expenses SET spent_at = NOW() WHERE spent_at IS NULL;")
        cur.execute("ALTER TABLE expenses ALTER COLUMN spent_at SET NOT NULL;")

        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS spent_date DATE;")
        cur.execute("UPDATE expenses SET spent_date = CURRENT_DATE WHERE spent_date IS NULL;")
        cur.execute("ALTER TABLE expenses ALTER COLUMN spent_date SET NOT NULL;")

        # Index (safe now because spent_at guaranteed)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_expenses_chat_user_time
            ON expenses (chat_id, tg_user_id, spent_at);
        """)

        # -------- budgets table (personal per chat) --------
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

        # Unique constraint for personal budgets in a chat
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS budgets_unique_personal
            ON budgets (chat_id, tg_user_id, category, period, currency);
        """)

        conn.commit()


# ================= BUSINESS =================

def add_expense(chat_id: int, user_id: int, amount: float, currency: str, category: str):
    """
    Insert expense and satisfy both legacy columns: spent_at + spent_date.
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO expenses (
                chat_id, tg_user_id, amount, currency, category,
                spent_at, spent_date
            )
            VALUES (%s, %s, %s, %s, %s, NOW(), CURRENT_DATE);
        """, (chat_id, user_id, amount, currency, category))
        conn.commit()


def set_budget(chat_id: int, user_id: int, category: str, limit_amount: float, currency: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO budgets (chat_id, tg_user_id, category, period, limit_amount, currency)
            VALUES (%s, %s, %s, 'monthly', %s, %s)
            ON CONFLICT (chat_id, tg_user_id, category, period, currency)
            DO UPDATE SET limit_amount = EXCLUDED.limit_amount;
        """, (chat_id, user_id, category, limit_amount, currency))
        conn.commit()


def month_spent(chat_id: int, user_id: int, category: str, currency: str) -> float:
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


def get_budget(chat_id: int, user_id: int, category: str, currency: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT limit_amount FROM budgets
            WHERE chat_id=%s AND tg_user_id=%s
              AND category=%s AND period='monthly'
              AND currency=%s;
        """, (chat_id, user_id, category, currency))
        row = cur.fetchone()
        return float(row["limit_amount"]) if row else None


# ================= PARSER =================

def simple_parse(text: str):
    """
    Very simple parser:
    - finds the first integer number in text
    - category = first word of remaining text
    """
    t = (text or "").strip()
    if not t:
        return None

    m = re.search(r"(\d[\d\s]*)", t)
    if not m:
        return None

    amount = float(m.group(1).replace(" ", ""))
    rest = (t[:m.start()] + " " + t[m.end():]).strip()
    words = rest.split()
    category = words[0].lower() if words else "other"

    return {
        "amount": amount,
        "category": category,
        "currency": DEFAULT_CURRENCY
    }


# ================= HANDLERS =================

async def budget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    parts = (update.message.text or "").split()
    if len(parts) < 3:
        await update.message.reply_text("Формат: /budget категория сумма\nПример: /budget еда 3000000")
        return

    category = parts[1].lower()
    try:
        limit_amount = float(parts[2].replace(" ", ""))
    except Exception:
        await update.message.reply_text("Не смогла распознать сумму. Пример: /budget еда 3000000")
        return

    set_budget(chat_id, user_id, category, limit_amount, DEFAULT_CURRENCY)
    await update.message.reply_text(
        f"Бюджет установлен: {category} — {limit_amount:.0f} {DEFAULT_CURRENCY}"
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    parsed = simple_parse(text)
    if not parsed:
        return  # ignore unknown messages

    amount = parsed["amount"]
    category = parsed["category"]
    currency = parsed["currency"]

    add_expense(chat_id, user_id, amount, currency, category)

    spent = month_spent(chat_id, user_id, category, currency)
    limit_amt = get_budget(chat_id, user_id, category, currency)

    if limit_amt is not None:
        left = limit_amt - spent
        await update.message.reply_text(
            f"✅ Записано: {category} — {amount:.0f} {currency}\n"
            f"Осталось по бюджету в этом месяце: {left:.0f} {currency} (лимит {limit_amt:.0f})"
        )
    else:
        await update.message.reply_text(
            f"✅ Записано: {category} — {amount:.0f} {currency}\n"
            f"Бюджет не задан. Установите: /budget {category} 3000000"
        )


# ================= WEBHOOK =================

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
    app.add_handler(CommandHandler("budget", budget_cmd))
    app.add_handler(MessageHandler(filters.ALL, on_message))

    public_url = normalize_url(PUBLIC_URL)
    if not public_url:
        raise RuntimeError("Missing PUBLIC_URL (example: https://xxxx.up.railway.app)")

    # PTB webhooks mode
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{public_url}/telegram",
    )


if __name__ == "__main__":
    main()