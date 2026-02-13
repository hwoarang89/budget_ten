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
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

# =========================
# CONFIG
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))

DEFAULT_CURRENCY = (os.getenv("DEFAULT_CURRENCY", "UZS") or "UZS").strip().upper()

# Bot processes only when mentioned or user replies to bot (token saving)
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

# Context sizes (token economy)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "10"))  # last N messages (user+assistant)
MAX_SUMMARY_CHARS = int(os.getenv("MAX_SUMMARY_CHARS", "900"))


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
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;")
        cur.execute("UPDATE budgets SET created_at = NOW() WHERE created_at IS NULL;")
        cur.execute("ALTER TABLE budgets ALTER COLUMN created_at SET NOT NULL;")
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS budgets_unique_personal
            ON budgets (chat_id, tg_user_id, category, period, currency);
        """)

        # ---- conversational memory (summary + last messages) ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS convo_memory (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS convo_memory_unique
            ON convo_memory (chat_id, tg_user_id);
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS convo_messages (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                role TEXT NOT NULL,            -- 'user' | 'assistant'
                content TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS convo_messages_idx
            ON convo_messages (chat_id, tg_user_id, created_at DESC);
        """)

        # ---- pending clarification state ----
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


# =========================
# Conversation memory (DB)
# =========================

def get_memory_summary(chat_id: int, user_id: int) -> str:
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT summary FROM convo_memory WHERE chat_id=%s AND tg_user_id=%s;", (chat_id, user_id))
        row = cur.fetchone()
        return (row["summary"] if row else "") or ""


def set_memory_summary(chat_id: int, user_id: int, summary: str):
    summary = (summary or "")[:MAX_SUMMARY_CHARS]
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO convo_memory (chat_id, tg_user_id, summary, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (chat_id, tg_user_id)
            DO UPDATE SET summary = EXCLUDED.summary, updated_at = NOW();
        """, (chat_id, user_id, summary))
        conn.commit()


def add_convo_message(chat_id: int, user_id: int, role: str, content: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO convo_messages (chat_id, tg_user_id, role, content)
            VALUES (%s, %s, %s, %s);
        """, (chat_id, user_id, role, content))
        conn.commit()


def get_recent_history(chat_id: int, user_id: int, limit: int = HISTORY_LIMIT) -> List[Dict[str, str]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT role, content
            FROM convo_messages
            WHERE chat_id=%s AND tg_user_id=%s
            ORDER BY created_at DESC
            LIMIT %s;
        """, (chat_id, user_id, limit))
        rows = cur.fetchall()
    # reverse to chronological
    rows = list(reversed(rows))
    return [{"role": r["role"], "content": r["content"]} for r in rows]


# =========================
# Pending state (clarifications)
# =========================

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

    # reply-to-bot
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot:
        if (msg.reply_to_message.from_user.username or "").lower() == bot_username.lower():
            return True

    return False


# =========================
# OpenAI: conversational planner + optional followups
# =========================

SYSTEM_CORE = f"""
Ты — ассистент для учёта личных расходов в Telegram-группе.
У тебя есть доступ к базе расходов и бюджетов конкретного пользователя (по tg_user_id) внутри конкретного чата (chat_id).

Тебе нужно:
- понимать свободный русский текст
- сохранять контекст диалога (на основе краткого summary + последних сообщений)
- формировать ПЛАН действий к базе
- если данных не хватает — задавать ОДИН уточняющий вопрос
- после получения данных из базы — отвечать кратко и по делу

Валюта по умолчанию: {DEFAULT_CURRENCY}.
Категории — короткие слова/фразы (1–2 слова), нижний регистр.

Ты НЕ пишешь SQL. Ты выбираешь только разрешённые действия ниже.
""".strip()

PLANNER_INSTRUCTIONS = """
Верни ТОЛЬКО JSON с одним из типов:

1) plan (нужно сходить в БД или записать данные):
{
  "type": "plan",
  "actions": [
    { "action": "add_expense", "amount": 12000, "currency": "UZS", "category": "такси", "note": "optional" },
    { "action": "set_budget", "period": "daily|monthly", "category": "кофе", "limit_amount": 50000, "currency": "UZS" },
    { "action": "get_report_total", "period": "today|month" },
    { "action": "get_report_breakdown", "period": "today|month" },
    { "action": "get_my_budgets" }
  ],
  "assistant_message": "короткий текст пользователю (если можно ответить сразу, но чаще пусто)"
}

2) clarify (нужен уточняющий вопрос):
{
  "type": "clarify",
  "question": "один короткий вопрос",
  "expected": "amount|category|period|format|budget_period|budget_amount"
}

Правила:
- Если запрос про "сколько потратил" => get_report_total
- Если "на что тратил" / "разбивка" => get_report_breakdown
- "мои бюджеты" / "остаток по бюджетам" => get_my_budgets
- Для записи расхода — add_expense
- Для бюджета — set_budget
- Если пользователь просит "за неделю" — спроси уточнение (пока поддерживаем today/month)
""".strip()

RESPONDER_INSTRUCTIONS = f"""
Тебе дадут:
- исходный запрос пользователя
- результаты действий из БД (data)
- контекст (summary + история)

Верни ТОЛЬКО JSON:
{{
  "type":"final",
  "reply":"готовый ответ пользователю на русском",
  "new_summary":"обновлённое краткое резюме диалога (до {MAX_SUMMARY_CHARS} символов)"
}}

Требования к ответу:
- коротко, без лишних деталей
- если это запись расхода — покажи дневной и месячный остаток по категории (если бюджеты есть), иначе скажи что бюджеты не заданы
- если это отчёт — выдай сумму или разбивку по категориям
- если это бюджеты — покажи лимит/потрачено/осталось
""".strip()


async def openai_json(model: str, messages: List[Dict[str, Any]], timeout_s: int = 30) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        return {"type": "clarify", "question": "Не задан OPENAI_API_KEY. Добавьте ключ и повторите.", "expected": "period"}

    payload = {
        "model": model,
        "input": messages,
        "text": {"format": {"type": "json_object"}},
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if r.status_code >= 400:
            logger.error("OpenAI error %s: %s", r.status_code, r.text[:300])
            return {"type": "clarify", "question": "Не получилось обработать запрос. Повторите короче.", "expected": "period"}
        data = r.json()

    out = ""
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                out += c.get("text", "")

    try:
        return json.loads(out) if out else {"type": "clarify", "question": "Уточните запрос.", "expected": "period"}
    except Exception:
        return {"type": "clarify", "question": "Уточните запрос.", "expected": "period"}


def build_context_messages(summary: str, history: List[Dict[str, str]], user_text: str) -> List[Dict[str, Any]]:
    # We pass summary + last turns as user/assistant messages to the model
    content = []
    content.append({"role": "system", "content": [{"type": "input_text", "text": SYSTEM_CORE}]})
    if summary:
        content.append({"role": "system", "content": [{"type": "input_text", "text": f"Краткое резюме контекста:\n{summary}"}]})
    if history:
        # replay last messages in natural form
        for h in history:
            role = "user" if h["role"] == "user" else "assistant"
            content.append({"role": role, "content": [{"type": "input_text", "text": h["content"]}]})
    content.append({"role": "user", "content": [{"type": "input_text", "text": user_text}]})
    return content


async def plan_from_openai(summary: str, history: List[Dict[str, str]], user_text: str) -> Dict[str, Any]:
    msgs = build_context_messages(summary, history, user_text)
    # Add planner instruction as last system message (strong)
    msgs.insert(1, {"role": "system", "content": [{"type": "input_text", "text": PLANNER_INSTRUCTIONS}]})
    return await openai_json(OPENAI_TEXT_MODEL, msgs, timeout_s=30)


async def respond_from_openai(summary: str, history: List[Dict[str, str]], user_text: str, data: Dict[str, Any]) -> Dict[str, Any]:
    msgs = build_context_messages(summary, history, user_text)
    msgs.insert(1, {"role": "system", "content": [{"type": "input_text", "text": RESPONDER_INSTRUCTIONS}]})
    msgs.append({"role": "system", "content": [{"type": "input_text", "text": f"Результаты из БД (data):\n{json.dumps(data, ensure_ascii=False)}"}]})
    return await openai_json(OPENAI_TEXT_MODEL, msgs, timeout_s=30)


# =========================
# Receipt photo (optional)
# =========================

RECEIPT_PROMPT = f"""
Extract expense data from this receipt image for a personal expense tracker.
Return JSON only.

Format:
{{"type":"expense","amount":12345,"currency":"{DEFAULT_CURRENCY}","category":"продукты","note":"STORE"}}

If you cannot confidently detect total:
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
# DB-backed executors for planned actions
# =========================

def _today_start_utc() -> datetime:
    return datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

def _month_start_utc() -> datetime:
    today = date.today()
    return datetime(today.year, today.month, 1)

def get_budget_remaining(chat_id: int, user_id: int, category: str, currency: str) -> Dict[str, Any]:
    today_start = _today_start_utc()
    month_start = _month_start_utc()

    d_limit = get_budget(chat_id, user_id, category, "daily", currency)
    m_limit = get_budget(chat_id, user_id, category, "monthly", currency)

    d_spent = spent_since(chat_id, user_id, today_start, category=category, currency=currency)
    m_spent = spent_since(chat_id, user_id, month_start, category=category, currency=currency)

    return {
        "daily": {
            "limit": d_limit,
            "spent": d_spent,
            "left": (d_limit - d_spent) if d_limit is not None else None
        },
        "monthly": {
            "limit": m_limit,
            "spent": m_spent,
            "left": (m_limit - m_spent) if m_limit is not None else None
        }
    }


def execute_actions(chat_id: int, user_id: int, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Executes allowed actions and returns structured data for the responder.
    """
    data: Dict[str, Any] = {"results": []}

    for a in actions:
        action = str(a.get("action") or "").strip()

        if action == "add_expense":
            amount = float(a.get("amount"))
            currency = str(a.get("currency") or DEFAULT_CURRENCY).upper()
            category = str(a.get("category") or "другое").lower().strip()
            note = str(a.get("note") or "").strip()

            add_expense(chat_id, user_id, amount, currency, category, note)
            rem = get_budget_remaining(chat_id, user_id, category, currency)

            data["results"].append({
                "action": "add_expense",
                "amount": amount,
                "currency": currency,
                "category": category,
                "note": note,
                "remaining": rem
            })

        elif action == "set_budget":
            period = str(a.get("period") or "").lower().strip()
            category = str(a.get("category") or "другое").lower().strip()
            limit_amount = float(a.get("limit_amount"))
            currency = str(a.get("currency") or DEFAULT_CURRENCY).upper()

            set_budget(chat_id, user_id, category, period, limit_amount, currency)
            rem = get_budget_remaining(chat_id, user_id, category, currency)

            data["results"].append({
                "action": "set_budget",
                "period": period,
                "category": category,
                "limit_amount": limit_amount,
                "currency": currency,
                "remaining": rem
            })

        elif action == "get_report_total":
            period = str(a.get("period") or "").lower().strip()
            if period == "today":
                since = _today_start_utc()
            else:
                since = _month_start_utc()
            total = spent_since(chat_id, user_id, since)
            data["results"].append({
                "action": "get_report_total",
                "period": period,
                "total": total,
                "currency": DEFAULT_CURRENCY
            })

        elif action == "get_report_breakdown":
            period = str(a.get("period") or "").lower().strip()
            if period == "today":
                since = _today_start_utc()
            else:
                since = _month_start_utc()
            rows = breakdown_since(chat_id, user_id, since)
            data["results"].append({
                "action": "get_report_breakdown",
                "period": period,
                "rows": rows
            })

        elif action == "get_my_budgets":
            budgets = list_budgets(chat_id, user_id)
            # add remaining computed per budget line
            enriched = []
            for b in budgets:
                cat = b["category"]
                per = b["period"]
                cur = b["currency"]
                lim = float(b["limit_amount"])
                since = _today_start_utc() if per == "daily" else _month_start_utc()
                sp = spent_since(chat_id, user_id, since, category=cat, currency=cur)
                enriched.append({
                    "category": cat,
                    "period": per,
                    "currency": cur,
                    "limit": lim,
                    "spent": sp,
                    "left": lim - sp
                })
            data["results"].append({
                "action": "get_my_budgets",
                "budgets": enriched
            })

        else:
            data["results"].append({"action": action, "error": "unknown_action"})

    return data


# =========================
# Handlers
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME_CACHE
    if not BOT_USERNAME_CACHE:
        BOT_USERNAME_CACHE = (context.bot.username or "").strip()

    await update.effective_message.reply_text(
        "Я веду диалоговый контекст и понимаю свободный текст.\n"
        "Чтобы экономить токены, я отвечаю по упоминанию или ответом на моё сообщение.\n\n"
        f"Примеры:\n"
        f"• @{BOT_USERNAME_CACHE} сколько я потратил сегодня?\n"
        f"• @{BOT_USERNAME_CACHE} на что я тратил сегодня?\n"
        f"• @{BOT_USERNAME_CACHE} потратил 12000 на такси\n"
        f"• @{BOT_USERNAME_CACHE} бюджет на день кофе 50000\n"
        f"• @{BOT_USERNAME_CACHE} мои бюджеты\n"
        "\nЕсли не хватает данных — задам один уточняющий вопрос."
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

    # Allow processing if mentioned/replied-to-bot; OR if user is replying to a pending clarification and reply-to-bot.
    pending = get_user_state(chat_id, user_id)

    if pending and pending.get("pending"):
        # only accept clarification answers as a reply to the bot message (prevents unintended triggers)
        if not (msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot):
            if not should_process(update, bot_username):
                return
    else:
        if not should_process(update, bot_username):
            return

    raw_text = (msg.text or "").strip()
    if not raw_text:
        return

    # If mentioned, remove mention for cleaner model input
    user_text = _strip_bot_mention(raw_text, bot_username).strip()

    # Load memory + last messages
    summary = get_memory_summary(chat_id, user_id)
    history = get_recent_history(chat_id, user_id, HISTORY_LIMIT)

    # If we were waiting for clarification, prepend that context explicitly (state is already stored)
    if pending and pending.get("pending") and pending.get("question"):
        user_text = f"Я ранее спросил уточнение: {pending.get('question')}\nОтвет пользователя: {user_text}"

    # Store user message to history first (so the model can see it next turn too)
    add_convo_message(chat_id, user_id, "user", user_text)

    plan = await plan_from_openai(summary, history, user_text)

    ptype = str(plan.get("type") or "").lower().strip()

    if ptype == "clarify":
        q = str(plan.get("question") or "").strip() or "Уточните, пожалуйста, что именно вы хотите?"
        set_user_state(chat_id, user_id, {"pending": True, "question": q})
        add_convo_message(chat_id, user_id, "assistant", q)
        await msg.reply_text(q)
        return

    # Plan execution
    actions = plan.get("actions") or []
    if not isinstance(actions, list):
        actions = []

    # If any pending state existed and we got a plan, clear it
    if pending and pending.get("pending"):
        clear_user_state(chat_id, user_id)

    data = execute_actions(chat_id, user_id, actions)

    # Generate final response with context + DB data
    final = await respond_from_openai(summary, history, user_text, data)
    reply = str(final.get("reply") or "").strip() or "Готово."
    new_summary = str(final.get("new_summary") or summary).strip()

    # Persist assistant reply + new summary
    add_convo_message(chat_id, user_id, "assistant", reply)
    set_memory_summary(chat_id, user_id, new_summary)

    await msg.reply_text(reply)


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

    # Process photo only when mentioned in caption or reply-to-bot (token saving)
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
    rem = get_budget_remaining(chat_id, user_id, category, currency)

    # Update conversational memory quickly (no second model call here to save tokens)
    add_convo_message(chat_id, user_id, "user", "[фото чека]")
    reply = f"🧾 Чек записан: {category} — {amount:.0f} {currency}"
    if rem["daily"]["limit"] is not None:
        reply += f"\nДень осталось: {rem['daily']['left']:.0f} {currency}"
    if rem["monthly"]["limit"] is not None:
        reply += f"\nМесяц осталось: {rem['monthly']['left']:.0f} {currency}"
    add_convo_message(chat_id, user_id, "assistant", reply)

    await msg.reply_text(reply)


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