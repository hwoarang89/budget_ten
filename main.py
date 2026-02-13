import os
import re
import json
import base64
import logging
from datetime import datetime, date
from typing import Optional, Dict, Any, List

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update, MessageEntity
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

# =========================
# CONFIG
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))

DEFAULT_CURRENCY = (os.getenv("DEFAULT_CURRENCY", "UZS") or "UZS").strip().upper()

# Token saving: bot processes only when mentioned or when user replies to bot
MENTION_ONLY = (os.getenv("MENTION_ONLY", "1").strip() != "0")

# Optional: restrict to one topic (forum thread). 0 -> all topics
ALLOWED_THREAD_ID = os.getenv("ALLOWED_THREAD_ID", "").strip()
ALLOWED_THREAD_ID = int(ALLOWED_THREAD_ID) if ALLOWED_THREAD_ID.isdigit() else 0

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1-mini").strip()
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini").strip()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("budget-bot")

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
    """Create tables and migrate legacy schema safely (idempotent)."""
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

        # user state for clarifications (so the bot can ask follow-up questions)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS user_states_unique
            ON user_states (chat_id, tg_user_id);
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


def spent_since(chat_id: int, user_id: int, since_ts: datetime, category: Optional[str] = None, currency: Optional[str] = None) -> float:
    with db() as conn, conn.cursor() as cur:
        if category and currency:
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0) AS s
                FROM expenses
                WHERE chat_id=%s AND tg_user_id=%s AND spent_at >= %s AND category=%s AND currency=%s;
            """, (chat_id, user_id, since_ts, category, currency))
        else:
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0) AS s
                FROM expenses
                WHERE chat_id=%s AND tg_user_id=%s AND spent_at >= %s;
            """, (chat_id, user_id, since_ts))
        return float(cur.fetchone()["s"])


def breakdown_since(chat_id: int, user_id: int, since_ts: datetime) -> List[Dict[str, Any]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT category, currency, COALESCE(SUM(amount), 0) AS spent
            FROM expenses
            WHERE chat_id=%s AND tg_user_id=%s AND spent_at >= %s
            GROUP BY category, currency
            ORDER BY spent DESC;
        """, (chat_id, user_id, since_ts))
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


def get_user_state(chat_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT state_json FROM user_states WHERE chat_id=%s AND tg_user_id=%s;", (chat_id, user_id))
        row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["state_json"])
        except Exception:
            return None


def set_user_state(chat_id: int, user_id: int, state: Dict[str, Any]):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO user_states (chat_id, tg_user_id, state_json, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (chat_id, tg_user_id)
            DO UPDATE SET state_json = EXCLUDED.state_json, updated_at = NOW();
        """, (chat_id, user_id, json.dumps(state, ensure_ascii=False)))
        conn.commit()


def clear_user_state(chat_id: int, user_id: int):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM user_states WHERE chat_id=%s AND tg_user_id=%s;", (chat_id, user_id))
        conn.commit()


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

def _extract_bot_mention(text: str, entities: Optional[List[MessageEntity]], bot_username: str) -> bool:
    if not text or not entities or not bot_username:
        return False
    target = f"@{bot_username.lower()}"
    for e in entities:
        if e.type == "mention":
            frag = text[e.offset : e.offset + e.length]
            if frag.lower() == target:
                return True
    return False

def _strip_bot_mention(text: str, bot_username: str) -> str:
    if not text or not bot_username:
        return text
    t = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s+", " ", t)
    return t

def should_process(update: Update, bot_username: str) -> bool:
    if not is_group(update) or not allowed_topic(update):
        return False
    if not MENTION_ONLY:
        return True

    msg = update.effective_message
    if not msg:
        return False

    if _extract_bot_mention(msg.text or "", msg.entities, bot_username):
        return True

    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot:
        if (msg.reply_to_message.from_user.username or "").lower() == bot_username.lower():
            return True

    # If user is answering a clarification (stored state), allow without mention only if it is a reply to bot
    return False


# =========================
# OpenAI: Planner / Interpreter
# =========================

PLANNER_SYSTEM = f"""
You are an interpreter for a Telegram group personal expense tracker (per-user in a group).
You MUST convert the user's free-form Russian text into an executable "plan" for the backend.
Return ONLY JSON. No markdown, no extra text.

Important:
- You DO NOT write SQL.
- You MUST choose from allowed actions and parameters.
- If information is missing, ask a clarifying question instead of guessing.

Allowed plan schemas:

1) Add expense:
{{
  "type": "expense",
  "amount": 12345,
  "currency": "{DEFAULT_CURRENCY}",
  "category": "еда",
  "note": "optional short note"
}}

2) Set budget:
{{
  "type": "budget",
  "period": "daily"|"monthly",
  "category": "еда",
  "limit_amount": 3000000,
  "currency": "{DEFAULT_CURRENCY}"
}}

3) Get report (totals/breakdowns):
{{
  "type": "report",
  "period": "today"|"month",
  "format": "total"|"breakdown"
}}

4) Show my budgets + remaining:
{{ "type": "my_budgets" }}

5) Clarification:
{{
  "type": "clarify",
  "question": "Short question in Russian asking only what is needed",
  "expected": "one_of: amount|category|period|format|budget_period|budget_amount"
}}

Interpretation guidance:
- "сколько я потратил сегодня" => report period=today format=total
- "на что я тратил сегодня" => report period=today format=breakdown
- "сколько за месяц" / "в этом месяце" => period=month
- For expenses: accept phrases like "потратил 12000 на такси", "такси 12000", "минус 50000 кафе"
- For budgets: accept "бюджет на день кофе 50000", "лимит на месяц еда 3000000"
If the user asks something unrelated, return clarify with a question OR a safe response plan:
{{"type":"clarify","question":"Уточните, что вы хотите: записать расход, поставить бюджет или показать статистику?","expected":"period"}}
""".strip()


async def openai_plan(user_text: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Takes free-form user text and (optionally) prior state, returns a JSON plan:
    expense|budget|report|my_budgets|clarify
    """
    if not OPENAI_API_KEY:
        return {"type": "clarify", "question": "Не задан OPENAI_API_KEY. Уточните, что вы хотите сделать?", "expected": "period"}

    # If we have a pending clarification, provide it as context and ask the model to finalize.
    state_text = ""
    if state and state.get("pending") and state.get("question"):
        state_text = (
            "Previous clarification asked by the bot:\n"
            f"Question: {state.get('question')}\n"
            f"User answer now: {user_text}\n"
            "Now produce the final plan. If still missing, ask another clarification.\n"
        )

    payload = {
        "model": OPENAI_TEXT_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": PLANNER_SYSTEM}]},
            {"role": "user", "content": [{"type": "input_text", "text": state_text + user_text}]},
        ],
        "text": {"format": {"type": "json_object"}},
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if r.status_code >= 400:
            logger.error("OpenAI planner error %s: %s", r.status_code, r.text[:300])
            return {"type": "clarify", "question": "Не получилось обработать запрос. Повторите, пожалуйста, короче.", "expected": "period"}
        data = r.json()

    out = ""
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                out += c.get("text", "")

    try:
        return json.loads(out) if out else {"type": "clarify", "question": "Уточните запрос.", "expected": "period"}
    except Exception:
        return {"type": "clarify", "question": "Уточните запрос. Например: «сколько я потратил сегодня?»", "expected": "period"}


# =========================
# OpenAI: Receipt photo (optional)
# =========================

RECEIPT_PROMPT = f"""
Extract expense data from this receipt image for a personal expense tracker.
Return JSON only.

Format:
{{"type":"expense","amount":12345,"currency":"{DEFAULT_CURRENCY}","category":"продукты","note":"STORE"}}

If you cannot confidently detect the total amount:
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
                    {"type": "input_text", "text": "Extract the total paid amount and category. Return JSON only."},
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
# Formatting helpers
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
        return (
            "Бюджеты не заданы.\n"
            "Пример:\n"
            "• «бюджет на день кофе 50000»\n"
            "• «бюджет на месяц еда 3000000»"
        )

    # calculate remaining for each budget
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = datetime(date.today().year, date.today().month, 1)

    lines = ["Ваши бюджеты и остатки:"]
    for r in rows:
        cat = r["category"]
        period = r["period"]
        cur = r["currency"]
        limit_amt = float(r["limit_amount"])

        if period == "daily":
            spent = spent_since(chat_id, user_id, today_start, category=cat, currency=cur)
            label = "день"
        else:
            spent = spent_since(chat_id, user_id, month_start, category=cat, currency=cur)
            label = "месяц"

        left = limit_amt - spent
        lines.append(f"• {cat} ({label}): лимит {limit_amt:.0f} {cur}, потрачено {spent:.0f} {cur}, осталось {left:.0f} {cur}")

    return "\n".join(lines)


def fmt_after_expense(chat_id: int, user_id: int, category: str, currency: str, amount: float) -> str:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = datetime(date.today().year, date.today().month, 1)

    d_limit = get_budget(chat_id, user_id, category, "daily", currency)
    m_limit = get_budget(chat_id, user_id, category, "monthly", currency)

    d_spent = spent_since(chat_id, user_id, today_start, category=category, currency=currency)
    m_spent = spent_since(chat_id, user_id, month_start, category=category, currency=currency)

    lines = [f"✅ Записано: {category} — {amount:.0f} {currency}"]

    if d_limit is not None:
        lines.append(f"День: потрачено {d_spent:.0f} {currency}, лимит {d_limit:.0f}, осталось {d_limit - d_spent:.0f}")
    else:
        lines.append("День: бюджет не задан")

    if m_limit is not None:
        lines.append(f"Месяц: потрачено {m_spent:.0f} {currency}, лимит {m_limit:.0f}, осталось {m_limit - m_spent:.0f}")
    else:
        lines.append("Месяц: бюджет не задан")

    return "\n".join(lines)


# =========================
# Handlers
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME_CACHE
    if not BOT_USERNAME_CACHE:
        BOT_USERNAME_CACHE = (context.bot.username or "").strip()

    await update.effective_message.reply_text(
        "Я работаю в группе и понимаю свободный текст.\n"
        "Чтобы экономить токены, я отвечаю, когда вы меня упоминаете.\n\n"
        "Примеры:\n"
        f"• @{BOT_USERNAME_CACHE} сколько я потратил сегодня?\n"
        f"• @{BOT_USERNAME_CACHE} расскажи на что я тратил сегодня\n"
        f"• @{BOT_USERNAME_CACHE} бюджет на день кофе 50000\n"
        f"• @{BOT_USERNAME_CACHE} потратил 12000 на такси\n"
        f"• @{BOT_USERNAME_CACHE} мои бюджеты\n\n"
        "Если запрос неоднозначный — я задам уточняющий вопрос."
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME_CACHE
    msg = update.effective_message
    if not msg:
        return

    if not BOT_USERNAME_CACHE:
        BOT_USERNAME_CACHE = (context.bot.username or "").strip()
    bot_username = BOT_USERNAME_CACHE or ""
    if not bot_username:
        return

    if not is_group(update) or not allowed_topic(update):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Check if user is replying to a pending clarification
    pending = get_user_state(chat_id, user_id)

    # Mention-only processing (unless user replies to bot)
    if pending and pending.get("pending") and msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot:
        pass
    else:
        if not should_process(update, bot_username):
            return

    raw_text = (msg.text or "").strip()
    if not raw_text:
        return

    # If it was mentioned, remove mention for cleaner planning
    clean_text = _strip_bot_mention(raw_text, bot_username).strip()

    plan = await openai_plan(clean_text, state=pending)

    ptype = str(plan.get("type") or "").lower().strip()

    # If we were in clarification mode and got a non-clarify plan, clear state
    if pending and pending.get("pending") and ptype != "clarify":
        clear_user_state(chat_id, user_id)

    if ptype == "clarify":
        q = str(plan.get("question") or "").strip()
        if not q:
            q = "Уточните, пожалуйста, что именно вы хотите узнать или записать?"
        # save pending clarification
        set_user_state(chat_id, user_id, {"pending": True, "question": q})
        await msg.reply_text(q)
        return

    if ptype == "my_budgets":
        await msg.reply_text(fmt_my_budgets(chat_id, user_id))
        return

    if ptype == "report":
        period = str(plan.get("period") or "").lower().strip()
        fmt = str(plan.get("format") or "").lower().strip()  # total|breakdown

        if period not in ("today", "month"):
            set_user_state(chat_id, user_id, {"pending": True, "question": "За какой период: сегодня или месяц?"})
            await msg.reply_text("За какой период: сегодня или месяц?")
            return

        if fmt not in ("total", "breakdown"):
            set_user_state(chat_id, user_id, {"pending": True, "question": "Нужна сумма или разбивка по категориям?"})
            await msg.reply_text("Нужна сумма или разбивка по категориям?")
            return

        if period == "today":
            since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            if fmt == "total":
                total = spent_since(chat_id, user_id, since)
                await msg.reply_text(f"Сегодня вы потратили: {total:.0f} {DEFAULT_CURRENCY}")
            else:
                rows = breakdown_since(chat_id, user_id, since)
                await msg.reply_text(fmt_breakdown("Ваши расходы за сегодня:", rows))
            return

        if period == "month":
            since = datetime(date.today().year, date.today().month, 1)
            if fmt == "total":
                total = spent_since(chat_id, user_id, since)
                await msg.reply_text(f"В этом месяце вы потратили: {total:.0f} {DEFAULT_CURRENCY}")
            else:
                rows = breakdown_since(chat_id, user_id, since)
                await msg.reply_text(fmt_breakdown("Ваши расходы за месяц:", rows))
            return

    if ptype == "budget":
        try:
            period = str(plan.get("period") or "").lower().strip()
            category = str(plan.get("category") or "другое").lower().strip()
            limit_amount = float(plan.get("limit_amount"))
            currency = str(plan.get("currency") or DEFAULT_CURRENCY).upper().strip()
        except Exception:
            set_user_state(chat_id, user_id, {"pending": True, "question": "Уточните бюджет: период (день/месяц), категория и сумма."})
            await msg.reply_text("Уточните бюджет: период (день/месяц), категория и сумма.")
            return

        if period not in ("daily", "monthly"):
            set_user_state(chat_id, user_id, {"pending": True, "question": "Это бюджет на день или на месяц?"})
            await msg.reply_text("Это бюджет на день или на месяц?")
            return

        set_budget(chat_id, user_id, category, period, limit_amount, currency)
        label = "день" if period == "daily" else "месяц"
        await msg.reply_text(f"Бюджет установлен ({label}): {category} — {limit_amount:.0f} {currency}")
        return

    if ptype == "expense":
        try:
            amount = float(plan.get("amount"))
            currency = str(plan.get("currency") or DEFAULT_CURRENCY).upper().strip()
            category = str(plan.get("category") or "другое").lower().strip()
            note = str(plan.get("note") or "").strip()
        except Exception:
            set_user_state(chat_id, user_id, {"pending": True, "question": "Уточните расход: сумма и категория."})
            await msg.reply_text("Уточните расход: сумма и категория.")
            return

        if amount <= 0:
            await msg.reply_text("Сумма должна быть больше нуля.")
            return

        add_expense(chat_id, user_id, amount, currency, category, note)
        await msg.reply_text(fmt_after_expense(chat_id, user_id, category, currency, amount))
        return

    # fallback
    set_user_state(chat_id, user_id, {"pending": True, "question": "Уточните, вы хотите: записать расход, поставить бюджет или показать статистику?"})
    await msg.reply_text("Уточните, вы хотите: записать расход, поставить бюджет или показать статистику?")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME_CACHE
    msg = update.effective_message
    if not msg or not msg.photo:
        return

    if not BOT_USERNAME_CACHE:
        BOT_USERNAME_CACHE = (context.bot.username or "").strip()
    bot_username = BOT_USERNAME_CACHE or ""
    if not bot_username:
        return

    if not is_group(update) or not allowed_topic(update):
        return

    # Process photo only when mentioned in caption (or reply to bot) to save tokens
    caption = (msg.caption or "").strip()
    mentioned = _extract_bot_mention(caption, msg.caption_entities, bot_username) if caption else False
    replied = bool(
        msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot
        and (msg.reply_to_message.from_user.username or "").lower() == bot_username.lower()
    )

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
        await msg.reply_text("Не удалось надёжно распознать чек. Напишите расход текстом, упомянув бота.")
        return

    try:
        amount = float(parsed.get("amount"))
        currency = str(parsed.get("currency") or DEFAULT_CURRENCY).upper().strip()
        category = str(parsed.get("category") or "другое").lower().strip()
        note = str(parsed.get("note") or "").strip()
    except Exception:
        await msg.reply_text("Не удалось корректно распознать данные чека. Напишите расход текстом, упомянув бота.")
        return

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

    app.add_handler(CommandHandler("start", start_cmd))
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