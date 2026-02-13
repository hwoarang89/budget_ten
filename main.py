import os
import re
import json
import base64
import logging
from datetime import datetime, date
from typing import Optional, Dict, Any, List, Tuple

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update, MessageEntity
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

# Only respond in groups/supergroups; and only when mentioned or replied-to (to save tokens)
MENTION_ONLY = (os.getenv("MENTION_ONLY", "1").strip() != "0")

# Optional: restrict to one forum topic id (thread). If empty/0 -> all topics.
ALLOWED_THREAD_ID = os.getenv("ALLOWED_THREAD_ID", "").strip()
ALLOWED_THREAD_ID = int(ALLOWED_THREAD_ID) if ALLOWED_THREAD_ID.isdigit() else 0

# OpenAI (text understanding)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1-mini").strip()

# OpenAI (receipt photo)
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini").strip()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("budget-bot")

# Cache bot username once fetched
BOT_USERNAME_CACHE: Optional[str] = os.getenv("TELEGRAM_BOT_USERNAME", "").strip() or None


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
    """Create tables if missing and safely migrate legacy schema."""
    with db() as conn, conn.cursor() as cur:
        # expenses
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
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS note TEXT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS spent_at TIMESTAMP;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS spent_date DATE;")

        cur.execute("UPDATE expenses SET spent_at = NOW() WHERE spent_at IS NULL;")
        cur.execute("UPDATE expenses SET spent_date = CURRENT_DATE WHERE spent_date IS NULL;")
        cur.execute("ALTER TABLE expenses ALTER COLUMN spent_at SET NOT NULL;")
        cur.execute("ALTER TABLE expenses ALTER COLUMN spent_date SET NOT NULL;")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_expenses_chat_user_time
            ON expenses (chat_id, tg_user_id, spent_at);
        """)

        # budgets
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
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;")
        cur.execute("UPDATE budgets SET created_at = NOW() WHERE created_at IS NULL;")
        cur.execute("ALTER TABLE budgets ALTER COLUMN created_at SET NOT NULL;")

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS budgets_unique_personal
            ON budgets (chat_id, tg_user_id, category, period, currency);
        """)
        conn.commit()


# =========================
# BUSINESS (DB ops)
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


def breakdown_today(chat_id: int, user_id: int) -> List[Dict[str, Any]]:
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


def breakdown_month(chat_id: int, user_id: int) -> List[Dict[str, Any]]:
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


def list_budgets(chat_id: int, user_id: int) -> List[Dict[str, Any]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT category, period, limit_amount, currency
            FROM budgets
            WHERE chat_id=%s AND tg_user_id=%s
            ORDER BY period, category;
        """, (chat_id, user_id))
        return cur.fetchall()


# =========================
# Telegram helpers
# =========================

def is_group(update: Update) -> bool:
    c = update.effective_chat
    return bool(c and c.type in ("group", "supergroup"))

def allowed_topic(update: Update) -> bool:
    if not ALLOWED_THREAD_ID:
        return True
    m = update.effective_message
    return bool(m and m.message_thread_id == ALLOWED_THREAD_ID)

def _extract_bot_mentions(msg_text: str, entities: Optional[List[MessageEntity]], bot_username: str) -> bool:
    if not msg_text or not entities or not bot_username:
        return False
    target = f"@{bot_username.lower()}"
    for e in entities:
        if e.type == "mention":
            frag = msg_text[e.offset : e.offset + e.length]
            if frag.lower() == target:
                return True
    return False

def _strip_bot_mention(text: str, bot_username: str) -> str:
    if not text or not bot_username:
        return text
    # Remove "@botname" anywhere, collapse spaces
    t = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s+", " ", t)
    return t

def should_process_message(update: Update, bot_username: str) -> bool:
    if not is_group(update):
        return False
    if not allowed_topic(update):
        return False
    if not MENTION_ONLY:
        return True

    msg = update.effective_message
    if not msg:
        return False

    # Process if message explicitly mentions bot
    if _extract_bot_mentions(msg.text or "", msg.entities, bot_username):
        return True

    # Or if user replies to a bot message
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot:
        if (msg.reply_to_message.from_user.username or "").lower() == bot_username.lower():
            return True

    return False


# =========================
# OpenAI text understanding
# =========================

INTENT_SYSTEM = f"""
You are a group expense tracker assistant.
Return ONLY JSON (no markdown, no extra text).

User message may ask to:
1) add expense
2) set daily or monthly budget
3) show today's breakdown
4) show month's breakdown
5) show my budgets + remaining
6) help

Use this schema:

Expense:
{{"type":"expense","amount":12345,"currency":"{DEFAULT_CURRENCY}","category":"кофе","note":"optional short note"}}

Budget:
{{"type":"budget","period":"daily"|"monthly","category":"кофе","limit_amount":50000,"currency":"{DEFAULT_CURRENCY}"}}

Report:
{{"type":"report","period":"today"|"month"|"my"}}

Help:
{{"type":"help"}}

If unclear:
{{"type":"unknown"}}

Rules:
- category: short russian word/phrase (1-2 words), lowercase
- amount/limit_amount: number
- currency: default "{DEFAULT_CURRENCY}" unless user clearly states another
""".strip()


async def openai_intent(text: str) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        return {"type": "unknown"}

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_TEXT_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": INTENT_SYSTEM}]},
            {"role": "user", "content": [{"type": "input_text", "text": text}]},
        ],
        "text": {"format": {"type": "json_object"}},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if r.status_code >= 400:
            logger.error("OpenAI text error %s: %s", r.status_code, r.text[:300])
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
# Receipt photo recognition (optional)
# =========================

RECEIPT_PROMPT = f"""
Extract expense data from this receipt image.
Return JSON only.

Format:
{{"type":"expense","amount":12345,"currency":"{DEFAULT_CURRENCY}","category":"продукты","note":"STORE"}}

If cannot confidently detect total amount:
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
            logger.error("OpenAI vision error %s: %s", r.status_code, r.text[:300])
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
# Replies (formatting)
# =========================

def fmt_breakdown(title: str, rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return f"{title}\nПока нет записей."
    lines = [title]
    total = 0.0
    for r in rows:
        spent = float(r["spent"])
        total += spent
        lines.append(f"• {r['category']}: {spent:.0f} {r['currency']}")
    lines.append(f"\nИтого: {total:.0f} {DEFAULT_CURRENCY}")
    return "\n".join(lines)

def fmt_my_budgets(chat_id: int, user_id: int) -> str:
    rows = list_budgets(chat_id, user_id)
    if not rows:
        return ("Бюджеты не заданы.\n"
                "Примеры:\n"
                "• @бот бюджет на день кофе 50000\n"
                "• @бот бюджет на месяц еда 3000000")
    lines = ["Ваши бюджеты и остатки:"]
    for r in rows:
        cat = r["category"]
        period = r["period"]
        cur = r["currency"]
        limit_amt = float(r["limit_amount"])
        if period == "daily":
            spent = spent_today(chat_id, user_id, cat, cur)
            label = "день"
        else:
            spent = spent_month(chat_id, user_id, cat, cur)
            label = "месяц"
        left = limit_amt - spent
        lines.append(f"• {cat} ({label}): лимит {limit_amt:.0f} {cur}, потрачено {spent:.0f} {cur}, осталось {left:.0f} {cur}")
    return "\n".join(lines)

def fmt_after_expense(chat_id: int, user_id: int, category: str, currency: str, amount: float) -> str:
    d_limit = get_budget(chat_id, user_id, category, "daily", currency)
    m_limit = get_budget(chat_id, user_id, category, "monthly", currency)
    d_spent = spent_today(chat_id, user_id, category, currency)
    m_spent = spent_month(chat_id, user_id, category, currency)

    lines = [f"✅ Записано: {category} — {amount:.0f} {currency}"]

    if d_limit is not None:
        lines.append(f"День: потрачено {d_spent:.0f} {currency}, лимит {d_limit:.0f}, осталось {d_limit - d_spent:.0f}")
    else:
        lines.append(f"День: бюджет не задан")

    if m_limit is not None:
        lines.append(f"Месяц: потрачено {m_spent:.0f} {currency}, лимит {m_limit:.0f}, осталось {m_limit - m_spent:.0f}")
    else:
        lines.append(f"Месяц: бюджет не задан")

    return "\n".join(lines)


# =========================
# Handlers
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME_CACHE
    # cache bot username if missing
    if not BOT_USERNAME_CACHE:
        BOT_USERNAME_CACHE = (context.bot.username or "").strip()

    await update.effective_message.reply_text(
        "Я работаю в группе и отвечаю, когда меня упоминают.\n\n"
        f"Примеры:\n"
        f"• @{BOT_USERNAME_CACHE} кофе 1000\n"
        f"• @{BOT_USERNAME_CACHE} бюджет на день кофе 50000\n"
        f"• @{BOT_USERNAME_CACHE} бюджет на месяц еда 3000000\n"
        f"• @{BOT_USERNAME_CACHE} покажи расходы за сегодня\n"
        f"• @{BOT_USERNAME_CACHE} покажи расходы за месяц\n"
        f"• @{BOT_USERNAME_CACHE} мои бюджеты\n\n"
        "Можно прислать фото чека и упомянуть бота в подписи."
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME_CACHE
    if not update.effective_message:
        return

    # cache username
    if not BOT_USERNAME_CACHE:
        BOT_USERNAME_CACHE = (context.bot.username or "").strip()
    bot_username = BOT_USERNAME_CACHE or ""
    if not bot_username:
        return

    if not should_process_message(update, bot_username):
        return

    text = (update.effective_message.text or "").strip()
    # remove mention to reduce noise for model
    text_clean = _strip_bot_mention(text, bot_username).strip()
    if not text_clean:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    intent = await openai_intent(text_clean)

    t = str(intent.get("type") or "unknown").lower()

    if t == "help":
        await update.effective_message.reply_text(
            f"Примеры:\n"
            f"• @{bot_username} кофе 1000\n"
            f"• @{bot_username} бюджет на день кофе 50000\n"
            f"• @{bot_username} бюджет на месяц еда 3000000\n"
            f"• @{bot_username} покажи расходы за сегодня\n"
            f"• @{bot_username} покажи расходы за месяц\n"
            f"• @{bot_username} мои бюджеты"
        )
        return

    if t == "budget":
        try:
            period = str(intent.get("period") or "").lower().strip()
            category = str(intent.get("category") or "другое").lower().strip()
            limit_amount = float(intent.get("limit_amount"))
            currency = str(intent.get("currency") or DEFAULT_CURRENCY).upper()
            if period not in ("daily", "monthly"):
                raise ValueError("bad period")
        except Exception:
            await update.effective_message.reply_text(
                "Не поняла бюджет. Пример:\n"
                f"@{bot_username} бюджет на день кофе 50000\n"
                f"@{bot_username} бюджет на месяц еда 3000000"
            )
            return

        set_budget(chat_id, user_id, category, period, limit_amount, currency)
        label = "день" if period == "daily" else "месяц"
        await update.effective_message.reply_text(
            f"Бюджет установлен ({label}): {category} — {limit_amount:.0f} {currency}"
        )
        return

    if t == "report":
        period = str(intent.get("period") or "").lower().strip()
        if period == "today":
            rows = breakdown_today(chat_id, user_id)
            await update.effective_message.reply_text(fmt_breakdown("Ваши расходы за сегодня:", rows))
            return
        if period == "month":
            rows = breakdown_month(chat_id, user_id)
            await update.effective_message.reply_text(fmt_breakdown("Ваши расходы за месяц:", rows))
            return
        if period == "my":
            await update.effective_message.reply_text(fmt_my_budgets(chat_id, user_id))
            return

        await update.effective_message.reply_text(
            "Уточните период: сегодня / месяц / мои бюджеты.\n"
            f"Пример: @{bot_username} покажи расходы за сегодня"
        )
        return

    if t == "expense":
        try:
            amount = float(intent.get("amount"))
            currency = str(intent.get("currency") or DEFAULT_CURRENCY).upper()
            category = str(intent.get("category") or "другое").lower().strip()
            note = str(intent.get("note") or "").strip()
        except Exception:
            await update.effective_message.reply_text(
                "Не смогла распознать расход. Пример:\n"
                f"@{bot_username} кофе 1000"
            )
            return

        add_expense(chat_id, user_id, amount, currency, category, note)
        await update.effective_message.reply_text(fmt_after_expense(chat_id, user_id, category, currency, amount))
        return

    # unknown
    await update.effective_message.reply_text(
        "Не поняла запрос.\n"
        f"Пример: @{bot_username} кофе 1000\n"
        f"Или: @{bot_username} покажи расходы за сегодня"
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME_CACHE
    if not update.effective_message or not update.effective_message.photo:
        return

    if not BOT_USERNAME_CACHE:
        BOT_USERNAME_CACHE = (context.bot.username or "").strip()
    bot_username = BOT_USERNAME_CACHE or ""
    if not bot_username:
        return

    # For photos: process only if caption mentions bot OR reply to bot (token saving)
    if not is_group(update) or not allowed_topic(update):
        return

    msg = update.effective_message
    caption = (msg.caption or "").strip()

    mentioned = _extract_bot_mentions(caption, msg.caption_entities, bot_username) if caption else False
    replied = bool(msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot
                   and (msg.reply_to_message.from_user.username or "").lower() == bot_username.lower())
    if MENTION_ONLY and not (mentioned or replied):
        return

    if not OPENAI_API_KEY:
        await msg.reply_text("Распознавание фото отключено: не задан OPENAI_API_KEY.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    photo = msg.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    parsed = await parse_receipt(bytes(image_bytes))
    if parsed.get("type") != "expense":
        await msg.reply_text("Не удалось надёжно распознать сумму на фото. Напишите расход текстом, упомянув бота.")
        return

    try:
        amount = float(parsed.get("amount"))
    except Exception:
        await msg.reply_text("Не удалось корректно распознать сумму. Напишите расход текстом, упомянув бота.")
        return

    currency = str(parsed.get("currency") or DEFAULT_CURRENCY).upper()
    category = str(parsed.get("category") or "другое").lower().strip()
    note = str(parsed.get("note") or "").strip()

    add_expense(chat_id, user_id, amount, currency, category, note)
    await msg.reply_text("🧾 Чек записан\n" + fmt_after_expense(chat_id, user_id, category, currency, amount))


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

    # Keep /start for onboarding (doesn't call OpenAI)
    app.add_handler(CommandHandler("start", start_cmd))

    # Mention-only natural language in group
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