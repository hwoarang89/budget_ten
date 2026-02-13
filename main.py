import os
import re
import json
import base64
import logging
from datetime import datetime, date
from typing import Optional, Tuple, Dict, Any

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update, Bot
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# ---------------------------
# Config
# ---------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()  # e.g. https://xxxx.up.railway.app
PORT = int(os.getenv("PORT", "8080"))

DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "UZS").strip()

# Optional: restrict bot to only one topic (message_thread_id) in forum groups
ALLOWED_THREAD_ID = os.getenv("ALLOWED_THREAD_ID", "").strip()
ALLOWED_THREAD_ID = int(ALLOWED_THREAD_ID) if ALLOWED_THREAD_ID.isdigit() else 0

# OpenAI model (keep it small/cheap)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("budget-bot")


# ---------------------------
# DB helpers
# ---------------------------

def db_conn():
    # Railway gives DATABASE_URL like postgres://...
    return psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor)


def init_db():
    """Create base tables if not exist."""
    with db_conn() as conn, conn.cursor() as cur:
        # users observed in chats (optional but useful)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                username TEXT,
                full_name TEXT,
                last_seen TIMESTAMP NOT NULL DEFAULT NOW(),
                PRIMARY KEY (chat_id, tg_user_id)
            );
            """
        )

        # expenses (personal per chat)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                amount NUMERIC NOT NULL,
                currency TEXT NOT NULL,
                category TEXT NOT NULL,
                note TEXT,
                spent_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_expenses_chat_user_time ON expenses (chat_id, tg_user_id, spent_at);")

        # budgets (personal per chat)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS budgets (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                category TEXT NOT NULL,
                period TEXT NOT NULL, -- 'monthly'
                limit_amount NUMERIC NOT NULL,
                currency TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """
        )
        # Unique per (chat,user,category,period,currency)
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS budgets_unique_personal
            ON budgets (chat_id, tg_user_id, category, period, currency);
            """
        )
        conn.commit()


def migrate_legacy_if_needed():
    """
    If you previously had tables without chat_id, we patch them here.
    Safe to run every start.
    """
    with db_conn() as conn, conn.cursor() as cur:
        # expenses: add chat_id if missing
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;")

        # If old data existed without chat_id, try to set chat_id=tg_user_id as a fallback
        cur.execute("UPDATE expenses SET chat_id = tg_user_id WHERE chat_id IS NULL AND tg_user_id IS NOT NULL;")

        # budgets: add chat_id if missing
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;")
        cur.execute("UPDATE budgets SET chat_id = tg_user_id WHERE chat_id IS NULL AND tg_user_id IS NOT NULL;")

        # Make sure NOT NULL where possible (only if no NULLs remain)
        cur.execute("SELECT COUNT(*) AS c FROM expenses WHERE chat_id IS NULL OR tg_user_id IS NULL;")
        if int(cur.fetchone()["c"]) == 0:
            cur.execute("ALTER TABLE expenses ALTER COLUMN chat_id SET NOT NULL;")
            cur.execute("ALTER TABLE expenses ALTER COLUMN tg_user_id SET NOT NULL;")

        cur.execute("SELECT COUNT(*) AS c FROM budgets WHERE chat_id IS NULL OR tg_user_id IS NULL;")
        if int(cur.fetchone()["c"]) == 0:
            cur.execute("ALTER TABLE budgets ALTER COLUMN chat_id SET NOT NULL;")
            cur.execute("ALTER TABLE budgets ALTER COLUMN tg_user_id SET NOT NULL;")

        # ensure unique index exists
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS budgets_unique_personal
            ON budgets (chat_id, tg_user_id, category, period, currency);
            """
        )
        conn.commit()


def upsert_member(chat_id: int, tg_user_id: int, username: Optional[str], full_name: Optional[str]):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO members (chat_id, tg_user_id, username, full_name, last_seen)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (chat_id, tg_user_id)
            DO UPDATE SET username=EXCLUDED.username, full_name=EXCLUDED.full_name, last_seen=NOW();
            """,
            (chat_id, tg_user_id, username, full_name),
        )
        conn.commit()


def add_expense(chat_id: int, tg_user_id: int, amount: float, currency: str, category: str, note: str = ""):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO expenses (chat_id, tg_user_id, amount, currency, category, note)
            VALUES (%s, %s, %s, %s, %s, %s);
            """,
            (chat_id, tg_user_id, amount, currency, category, note),
        )
        conn.commit()


def set_budget_monthly(chat_id: int, tg_user_id: int, category: str, limit_amount: float, currency: str):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO budgets (chat_id, tg_user_id, category, period, limit_amount, currency)
            VALUES (%s, %s, %s, 'monthly', %s, %s)
            ON CONFLICT (chat_id, tg_user_id, category, period, currency)
            DO UPDATE SET limit_amount=EXCLUDED.limit_amount, created_at=NOW();
            """,
            (chat_id, tg_user_id, category, limit_amount, currency),
        )
        conn.commit()


def month_spent(chat_id: int, tg_user_id: int, category: str, currency: str) -> float:
    today = date.today()
    month_start = today.replace(day=1)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS s
            FROM expenses
            WHERE chat_id=%s AND tg_user_id=%s AND category=%s AND currency=%s
              AND spent_at >= %s;
            """,
            (chat_id, tg_user_id, category, currency, datetime(month_start.year, month_start.month, 1)),
        )
        return float(cur.fetchone()["s"])


def get_budget_monthly(chat_id: int, tg_user_id: int, category: str, currency: str) -> Optional[float]:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT limit_amount
            FROM budgets
            WHERE chat_id=%s AND tg_user_id=%s AND category=%s AND period='monthly' AND currency=%s
            """,
            (chat_id, tg_user_id, category, currency),
        )
        row = cur.fetchone()
        return float(row["limit_amount"]) if row else None


def list_my_budgets(chat_id: int, tg_user_id: int) -> list:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT category, period, limit_amount, currency
            FROM budgets
            WHERE chat_id=%s AND tg_user_id=%s
            ORDER BY category;
            """,
            (chat_id, tg_user_id),
        )
        return cur.fetchall()


# ---------------------------
# Parsing
# ---------------------------

SYSTEM_PROMPT = f"""
You are a finance assistant for tracking expenses in a Telegram group.
Task: parse a user's message about an expense or a budget command.
Return ONLY JSON.

If the message is an expense, return:
{{
  "type": "expense",
  "amount": number,
  "currency": "{DEFAULT_CURRENCY}" or other currency code if explicitly stated,
  "category": "short category (e.g., food, taxi, coffee, health, home, other)",
  "note": "short note"
}}

If the message sets a monthly budget, return:
{{
  "type": "budget",
  "category": "category",
  "limit_amount": number,
  "currency": "{DEFAULT_CURRENCY}" or other currency code if explicitly stated
}}

If unclear, return:
{{ "type": "unknown" }}
""".strip()


def fallback_parse(text: str) -> Dict[str, Any]:
    """
    Very simple parser if OpenAI is unavailable.
    - Extract first number as amount (supports spaces as thousand separators)
    - Category = remaining words (first word) or "other"
    """
    t = (text or "").strip()
    if not t:
        return {"type": "unknown"}

    # budget command fallback: "/budget food 3000000"
    m = re.match(r"^/budget\s+(.+)$", t, flags=re.IGNORECASE)
    if m:
        rest = m.group(1).strip()
        parts = rest.split()
        if len(parts) >= 2:
            cat = parts[0].lower()
            num = " ".join(parts[1:])
            num = re.sub(r"[^\d., ]", "", num)
            num = num.replace(" ", "").replace(",", ".")
            try:
                limit_amount = float(num)
                return {"type": "budget", "category": cat, "limit_amount": limit_amount, "currency": DEFAULT_CURRENCY}
            except Exception:
                return {"type": "unknown"}

    # expense: find number
    num_match = re.search(r"(\d[\d\s]*)(?:[.,](\d+))?", t)
    if not num_match:
        return {"type": "unknown"}

    whole = num_match.group(1).replace(" ", "")
    frac = num_match.group(2)
    num_str = whole + (("." + frac) if frac else "")
    try:
        amount = float(num_str)
    except Exception:
        return {"type": "unknown"}

    # remove the amount part to get category/note
    before = t[:num_match.start()].strip()
    after = t[num_match.end():].strip()
    words = (before + " " + after).strip().split()
    category = (words[0].lower() if words else "other")
    note = " ".join(words[1:]) if len(words) > 1 else ""
    return {"type": "expense", "amount": amount, "currency": DEFAULT_CURRENCY, "category": category, "note": note}


async def openai_parse_text(user_text: str) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        return fallback_parse(user_text)

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
            # JSON-mode hint
            {"role": "user", "content": [{"type": "input_text", "text": "Return JSON only. " + (user_text or "")}]},
        ],
        "text": {"format": {"type": "json_object"}},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if r.status_code >= 400:
            logger.warning("OpenAI error %s: %s", r.status_code, r.text[:500])
            return fallback_parse(user_text)
        data = r.json()

    out_text = ""
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                out_text += c.get("text", "")

    try:
        return json.loads(out_text) if out_text else fallback_parse(user_text)
    except Exception:
        return fallback_parse(user_text)


# ---------------------------
# Telegram handlers
# ---------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я на связи.\n\n"
        "• Расход: например `кофе 28000` или `такси 35 000`\n"
        "• Бюджет: `/budget еда 3000000`\n"
        "• Мои бюджеты: `/my`",
        parse_mode="Markdown",
        message_thread_id=update.message.message_thread_id if update.message else None,
    )


async def my_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    budgets = list_my_budgets(chat_id, user_id)
    if not budgets:
        await update.message.reply_text(
            "У вас пока нет бюджетов. Установите: `/budget еда 3000000`",
            parse_mode="Markdown",
            message_thread_id=update.message.message_thread_id if update.message else None,
        )
        return

    lines = ["Ваши бюджеты (месяц):"]
    for b in budgets:
        cat = b["category"]
        cur = b["currency"]
        limit_amt = float(b["limit_amount"])
        spent = month_spent(chat_id, user_id, cat, cur)
        left = limit_amt - spent
        lines.append(f"• {cat}: лимит {limit_amt:.0f} {cur}, потрачено {spent:.0f} {cur}, осталось {left:.0f} {cur}")

    await update.message.reply_text(
        "\n".join(lines),
        message_thread_id=update.message.message_thread_id if update.message else None,
    )


async def budget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text or ""

    # /budget <category> <amount>
    parts = text.split()
    if len(parts) < 3:
        await update.message.reply_text(
            "Формат: `/budget <категория> <сумма>`\nПример: `/budget еда 3000000`",
            parse_mode="Markdown",
            message_thread_id=update.message.message_thread_id if update.message else None,
        )
        return

    category = parts[1].lower()
    num = " ".join(parts[2:])
    num = re.sub(r"[^\d., ]", "", num).replace(" ", "").replace(",", ".")
    try:
        limit_amount = float(num)
    except Exception:
        await update.message.reply_text(
            "Не смогла распознать сумму. Пример: `/budget еда 3000000`",
            parse_mode="Markdown",
            message_thread_id=update.message.message_thread_id if update.message else None,
        )
        return

    set_budget_monthly(chat_id, user_id, category, limit_amount, DEFAULT_CURRENCY)
    await update.message.reply_text(
        f"Бюджет установлен: {category} — {limit_amount:.0f} {DEFAULT_CURRENCY} в месяц",
        message_thread_id=update.message.message_thread_id if update.message else None,
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    # Topics restriction
    if ALLOWED_THREAD_ID and update.message.message_thread_id != ALLOWED_THREAD_ID:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    username = user.username
    full_name = (user.full_name or "").strip()

    upsert_member(chat_id, user_id, username, full_name)

    text = (update.message.text or "").strip()
    if not text:
        return

    # ignore commands (they have their handlers)
    if text.startswith("/"):
        return

    parsed = await openai_parse_text(text)

    if parsed.get("type") == "budget":
        category = str(parsed.get("category") or "other").lower()
        currency = str(parsed.get("currency") or DEFAULT_CURRENCY).upper()
        try:
            limit_amount = float(parsed.get("limit_amount"))
        except Exception:
            await update.message.reply_text(
                "Не смогла распознать сумму бюджета. Пример: `/budget еда 3000000`",
                parse_mode="Markdown",
                message_thread_id=update.message.message_thread_id,
            )
            return

        set_budget_monthly(chat_id, user_id, category, limit_amount, currency)
        await update.message.reply_text(
            f"Бюджет установлен: {category} — {limit_amount:.0f} {currency} в месяц",
            message_thread_id=update.message.message_thread_id,
        )
        return

    if parsed.get("type") != "expense":
        # fallback attempt
        parsed = fallback_parse(text)
        if parsed.get("type") != "expense":
            await update.message.reply_text(
                "Не поняла. Пример расхода: `кофе 28000`",
                parse_mode="Markdown",
                message_thread_id=update.message.message_thread_id,
            )
            return

    try:
        amount = float(parsed.get("amount"))
    except Exception:
        await update.message.reply_text(
            "Не смогла распознать сумму. Пример: `кофе 28000`",
            parse_mode="Markdown",
            message_thread_id=update.message.message_thread_id,
        )
        return

    currency = str(parsed.get("currency") or DEFAULT_CURRENCY).upper()
    category = str(parsed.get("category") or "other").lower()
    note = str(parsed.get("note") or "").strip()

    add_expense(chat_id, user_id, amount, currency, category, note)

    spent = month_spent(chat_id, user_id, category, currency)
    limit_amt = get_budget_monthly(chat_id, user_id, category, currency)

    if limit_amt is None:
        await update.message.reply_text(
            f"✅ Записано: {category} — {amount:.0f} {currency}\n"
            f"Бюджет для категории не задан. Установите: `/budget {category} 3000000`",
            parse_mode="Markdown",
            message_thread_id=update.message.message_thread_id,
        )
    else:
        left = float(limit_amt) - float(spent)
        await update.message.reply_text(
            f"✅ Записано: {category} — {amount:.0f} {currency}\n"
            f"В этом месяце по категории: потрачено {spent:.0f} {currency}, осталось {left:.0f} {currency} (лимит {float(limit_amt):.0f})",
            message_thread_id=update.message.message_thread_id,
        )


# ---------------------------
# Webhook bootstrap
# ---------------------------

def normalize_public_url(u: str) -> str:
    u = (u or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith("https://"):
        u = "https://" + u
    return u


def main():
    if not TELEGRAM_BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError("Missing env vars: TELEGRAM_BOT_TOKEN / DATABASE_URL")

    init_db()
    migrate_legacy_if_needed()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("my", my_cmd))
    application.add_handler(CommandHandler("budget", budget_cmd))
    application.add_handler(MessageHandler(filters.ALL, on_message))

    public_url = normalize_public_url(PUBLIC_URL)
    if not public_url:
        raise RuntimeError("Missing env var: PUBLIC_URL (example: https://xxxx.up.railway.app)")

    # Set webhook explicitly to avoid any ambiguity
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    bot.delete_webhook(drop_pending_updates=True)
    bot.set_webhook(url=f"{public_url}/telegram")

    logger.info("Webhook set to %s/telegram", public_url)

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{public_url}/telegram",
    )


if __name__ == "__main__":
    main()