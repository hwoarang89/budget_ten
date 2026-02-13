import os
import re
import json
import base64
import logging
from decimal import Decimal
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List, Tuple

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update, MessageEntity
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("budget-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini").strip()

DEFAULT_CURRENCY = (os.getenv("DEFAULT_CURRENCY", "UZS") or "UZS").strip().upper()

# Реагировать только на упоминание или reply (экономия токенов)
MENTION_ONLY = (os.getenv("MENTION_ONLY", "1").strip() != "0")

# Если группа с топиками: отвечать только в конкретный topic (forum thread)
ALLOWED_THREAD_ID = os.getenv("ALLOWED_THREAD_ID", "").strip()
ALLOWED_THREAD_ID = int(ALLOWED_THREAD_ID) if ALLOWED_THREAD_ID.isdigit() else 0

# Версия (для рассылки после деплоя)
BOT_VERSION = (
    os.getenv("RAILWAY_GIT_COMMIT_SHA", "").strip()
    or os.getenv("GIT_SHA", "").strip()
    or os.getenv("BOT_VERSION", "").strip()
    or "local"
)

RELEASE_NOTES = (os.getenv("RELEASE_NOTES", "") or "").strip()

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "12"))
MAX_SUMMARY_CHARS = int(os.getenv("MAX_SUMMARY_CHARS", "900"))

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
    """
    Создаёт таблицы если их нет.
    Если таблицы уже есть со старой схемой — делает безопасные ALTER (без потери данных).
    """
    with db() as conn, conn.cursor() as cur:
        # --- служебные ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS known_chats (
                chat_id BIGINT PRIMARY KEY,
                first_seen TIMESTAMP NOT NULL DEFAULT NOW(),
                last_seen TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_meta (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)

        # --- expenses ---
        # Создаём базовую таблицу "как новая" (если её нет)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                amount NUMERIC NOT NULL,
                currency TEXT NOT NULL DEFAULT 'UZS',
                category TEXT,
                main_category TEXT,
                sub_category TEXT,
                note TEXT,
                spent_at TIMESTAMP NOT NULL DEFAULT NOW(),
                spent_date DATE
            );
        """)

        # MIGRATION: если таблица была старой — гарантируем наличие нужных колонок
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS amount NUMERIC;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS currency TEXT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS category TEXT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS main_category TEXT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS sub_category TEXT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS note TEXT;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS spent_at TIMESTAMP;")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS spent_date DATE;")

        # заполняем spent_at если NULL
        cur.execute("""
            UPDATE expenses
            SET spent_at = NOW()
            WHERE spent_at IS NULL;
        """)

        # заполняем spent_date если NULL (из spent_at или today)
        cur.execute("""
            UPDATE expenses
            SET spent_date = COALESCE(spent_date, (spent_at::date), CURRENT_DATE)
            WHERE spent_date IS NULL;
        """)

        # переносим старое category -> main_category
        cur.execute("""
            UPDATE expenses
            SET main_category = COALESCE(main_category, category)
            WHERE main_category IS NULL;
        """)

        # подкатегорию по умолчанию: = main_category
        cur.execute("""
            UPDATE expenses
            SET sub_category = COALESCE(sub_category, main_category)
            WHERE sub_category IS NULL;
        """)

        # если всё пусто — 'other'
        cur.execute("""
            UPDATE expenses
            SET main_category = 'other'
            WHERE main_category IS NULL OR TRIM(main_category) = '';
        """)
        cur.execute("""
            UPDATE expenses
            SET sub_category = 'other'
            WHERE sub_category IS NULL OR TRIM(sub_category) = '';
        """)

        # Приводим обязательность (с осторожностью): только если данные уже заполнены
        # chat_id / tg_user_id / amount / currency / spent_at / spent_date обязательны для новой логики
        # Если у вас ранее могли быть NULL — эти UPDATE выше уже закрывают часть.
        cur.execute("ALTER TABLE expenses ALTER COLUMN currency SET DEFAULT 'UZS';")
        cur.execute("ALTER TABLE expenses ALTER COLUMN spent_at SET DEFAULT NOW();")
        cur.execute("ALTER TABLE expenses ALTER COLUMN spent_date SET DEFAULT CURRENT_DATE;")

        # Индексы
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_expenses_chat_user_date
            ON expenses (chat_id, tg_user_id, spent_date);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_expenses_chat_user_time
            ON expenses (chat_id, tg_user_id, spent_at);
        """)

        # --- budgets ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                category TEXT,
                main_category TEXT,
                currency TEXT NOT NULL DEFAULT 'UZS',
                daily_limit NUMERIC,
                monthly_limit NUMERIC,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)

        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS category TEXT;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS main_category TEXT;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS currency TEXT;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS daily_limit NUMERIC;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS monthly_limit NUMERIC;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;")

        cur.execute("""
            UPDATE budgets
            SET main_category = COALESCE(main_category, category)
            WHERE main_category IS NULL;
        """)
        cur.execute("""
            UPDATE budgets
            SET main_category = 'other'
            WHERE main_category IS NULL OR TRIM(main_category) = '';
        """)
        cur.execute("""
            UPDATE budgets
            SET currency = COALESCE(currency, 'UZS')
            WHERE currency IS NULL OR TRIM(currency) = '';
        """)

        # уникальность бюджета на (chat,user,main_category,currency)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS budgets_unique
            ON budgets (chat_id, tg_user_id, main_category, currency);
        """)

        # --- daily_overrides (для переносов) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_overrides (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                main_category TEXT NOT NULL,
                currency TEXT NOT NULL,
                day DATE NOT NULL,
                effective_limit NUMERIC NOT NULL,
                reason TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS daily_overrides_unique
            ON daily_overrides (chat_id, tg_user_id, main_category, currency, day);
        """)

        # --- память диалога (контекст) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS convo_memory (
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                PRIMARY KEY(chat_id, tg_user_id)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS convo_messages (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS convo_messages_idx
            ON convo_messages (chat_id, tg_user_id, created_at DESC);
        """)

        # --- состояния (например подтверждение удаления) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                chat_id BIGINT NOT NULL,
                tg_user_id BIGINT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                PRIMARY KEY(chat_id, tg_user_id)
            );
        """)

        conn.commit()


# =========================
# DB helpers
# =========================

def touch_chat(chat_id: int):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO known_chats(chat_id, first_seen, last_seen)
            VALUES (%s, NOW(), NOW())
            ON CONFLICT(chat_id) DO UPDATE SET last_seen = NOW();
        """, (chat_id,))
        conn.commit()


def get_meta(k: str) -> Optional[str]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT v FROM bot_meta WHERE k=%s;", (k,))
        row = cur.fetchone()
        return row["v"] if row else None


def set_meta(k: str, v: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO bot_meta(k, v, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT(k) DO UPDATE SET v=EXCLUDED.v, updated_at=NOW();
        """, (k, v))
        conn.commit()


def get_summary(chat_id: int, user_id: int) -> str:
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT summary FROM convo_memory WHERE chat_id=%s AND tg_user_id=%s;", (chat_id, user_id))
        row = cur.fetchone()
        return (row["summary"] if row else "") or ""


def set_summary(chat_id: int, user_id: int, summary: str):
    summary = (summary or "")[:MAX_SUMMARY_CHARS]
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO convo_memory(chat_id, tg_user_id, summary, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT(chat_id, tg_user_id) DO UPDATE SET summary=EXCLUDED.summary, updated_at=NOW();
        """, (chat_id, user_id, summary))
        conn.commit()


def add_history(chat_id: int, user_id: int, role: str, content: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO convo_messages(chat_id, tg_user_id, role, content)
            VALUES (%s, %s, %s, %s);
        """, (chat_id, user_id, role, content))
        conn.commit()


def get_history(chat_id: int, user_id: int, limit: int = HISTORY_LIMIT) -> List[Dict[str, str]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT role, content
            FROM convo_messages
            WHERE chat_id=%s AND tg_user_id=%s
            ORDER BY created_at DESC
            LIMIT %s;
        """, (chat_id, user_id, limit))
        rows = cur.fetchall()
    rows = list(reversed(rows))
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def get_state(chat_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT state_json FROM user_states WHERE chat_id=%s AND tg_user_id=%s;", (chat_id, user_id))
        row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["state_json"])
        except Exception:
            return None


def set_state(chat_id: int, user_id: int, state: Dict[str, Any]):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO user_states(chat_id, tg_user_id, state_json, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT(chat_id, tg_user_id) DO UPDATE SET state_json=EXCLUDED.state_json, updated_at=NOW();
        """, (chat_id, user_id, json.dumps(state, ensure_ascii=False)))
        conn.commit()


def clear_state(chat_id: int, user_id: int):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM user_states WHERE chat_id=%s AND tg_user_id=%s;", (chat_id, user_id))
        conn.commit()


def set_budget_base(chat_id: int, user_id: int, main_category: str, currency: str,
                    daily_limit: Optional[Decimal], monthly_limit: Optional[Decimal]):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO budgets(chat_id, tg_user_id, main_category, currency, daily_limit, monthly_limit, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW(),NOW())
            ON CONFLICT(chat_id, tg_user_id, main_category, currency)
            DO UPDATE SET daily_limit=EXCLUDED.daily_limit, monthly_limit=EXCLUDED.monthly_limit, updated_at=NOW();
        """, (chat_id, user_id, main_category, currency, daily_limit, monthly_limit))
        conn.commit()


def get_budget_base(chat_id: int, user_id: int, main_category: str, currency: str) -> Optional[Dict[str, Any]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT daily_limit, monthly_limit
            FROM budgets
            WHERE chat_id=%s AND tg_user_id=%s AND main_category=%s AND currency=%s;
        """, (chat_id, user_id, main_category, currency))
        row = cur.fetchone()
        return row if row else None


def upsert_override(chat_id: int, user_id: int, main_category: str, currency: str, day: date,
                    effective_limit: Decimal, reason: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO daily_overrides(chat_id, tg_user_id, main_category, currency, day, effective_limit, reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(chat_id, tg_user_id, main_category, currency, day)
            DO UPDATE SET effective_limit=EXCLUDED.effective_limit, reason=EXCLUDED.reason, created_at=NOW();
        """, (chat_id, user_id, main_category, currency, day, effective_limit, reason))
        conn.commit()


def get_override(chat_id: int, user_id: int, main_category: str, currency: str, day: date) -> Optional[Dict[str, Any]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT effective_limit, reason
            FROM daily_overrides
            WHERE chat_id=%s AND tg_user_id=%s AND main_category=%s AND currency=%s AND day=%s;
        """, (chat_id, user_id, main_category, currency, day))
        row = cur.fetchone()
        if not row:
            return None
        return {"limit": Decimal(row["effective_limit"]), "reason": row["reason"], "source": "override"}


def sum_expenses(chat_id: int, user_id: int, start: date, end: date,
                 main_category: Optional[str] = None,
                 currency: Optional[str] = None) -> Decimal:
    with db() as conn, conn.cursor() as cur:
        if main_category and currency:
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0) AS s
                FROM expenses
                WHERE chat_id=%s AND tg_user_id=%s
                  AND spent_date BETWEEN %s AND %s
                  AND main_category=%s AND currency=%s;
            """, (chat_id, user_id, start, end, main_category, currency))
        else:
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0) AS s
                FROM expenses
                WHERE chat_id=%s AND tg_user_id=%s
                  AND spent_date BETWEEN %s AND %s;
            """, (chat_id, user_id, start, end))
        return Decimal(cur.fetchone()["s"])


def add_expense(chat_id: int, user_id: int, amount: Decimal, currency: str,
                main_category: str, sub_category: str, note: str = "", when: Optional[datetime] = None):
    when = when or datetime.utcnow()
    spent_date = when.date()
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO expenses(chat_id, tg_user_id, amount, currency, main_category, sub_category, note, spent_at, spent_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id;
        """, (chat_id, user_id, amount, currency, main_category, sub_category, note, when, spent_date))
        rid = cur.fetchone()["id"]
        conn.commit()
        return rid


def delete_expenses_by_ids(chat_id: int, user_id: int, ids: List[int]) -> int:
    if not ids:
        return 0
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            DELETE FROM expenses
            WHERE chat_id=%s AND tg_user_id=%s AND id = ANY(%s)
        """, (chat_id, user_id, ids))
        deleted = cur.rowcount
        conn.commit()
        return deleted


def find_expenses(chat_id: int, user_id: int, start: date, end: date,
                  main_category: Optional[str] = None,
                  sub_category: Optional[str] = None) -> List[Dict[str, Any]]:
    with db() as conn, conn.cursor() as cur:
        if main_category and sub_category:
            cur.execute("""
                SELECT id, amount, currency, main_category, sub_category, note, spent_at, spent_date
                FROM expenses
                WHERE chat_id=%s AND tg_user_id=%s
                  AND spent_date BETWEEN %s AND %s
                  AND main_category=%s AND sub_category=%s
                ORDER BY spent_at DESC;
            """, (chat_id, user_id, start, end, main_category, sub_category))
        elif main_category:
            cur.execute("""
                SELECT id, amount, currency, main_category, sub_category, note, spent_at, spent_date
                FROM expenses
                WHERE chat_id=%s AND tg_user_id=%s
                  AND spent_date BETWEEN %s AND %s
                  AND main_category=%s
                ORDER BY spent_at DESC;
            """, (chat_id, user_id, start, end, main_category))
        else:
            cur.execute("""
                SELECT id, amount, currency, main_category, sub_category, note, spent_at, spent_date
                FROM expenses
                WHERE chat_id=%s AND tg_user_id=%s
                  AND spent_date BETWEEN %s AND %s
                ORDER BY spent_at DESC;
            """, (chat_id, user_id, start, end))
        return cur.fetchall()


def breakdown_main_sub(chat_id: int, user_id: int, start: date, end: date) -> List[Dict[str, Any]]:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT main_category, sub_category, currency, COALESCE(SUM(amount), 0) AS spent
            FROM expenses
            WHERE chat_id=%s AND tg_user_id=%s AND spent_date BETWEEN %s AND %s
            GROUP BY main_category, sub_category, currency
            ORDER BY spent DESC;
        """, (chat_id, user_id, start, end))
        return cur.fetchall()


# =========================
# Dates
# =========================

def today() -> date:
    return date.today()


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def prev_month_range(d: date) -> Tuple[date, date]:
    first_this = month_start(d)
    last_prev = first_this - timedelta(days=1)
    first_prev = month_start(last_prev)
    return first_prev, last_prev


def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# =========================
# Carryover logic (остаток переносится / перерасход уменьшает)
# =========================

def get_effective_daily_limit(chat_id: int, user_id: int, main_category: str, currency: str, day: date) -> Optional[Dict[str, Any]]:
    ovr = get_override(chat_id, user_id, main_category, currency, day)
    if ovr:
        return ovr
    base = get_budget_base(chat_id, user_id, main_category, currency)
    if base and base.get("daily_limit") is not None:
        return {"limit": Decimal(base["daily_limit"]), "reason": "базовый дневной бюджет", "source": "base"}
    return None


def ensure_daily_rollover_for_today(chat_id: int, user_id: int, main_category: str, currency: str, day: date) -> Optional[Dict[str, Any]]:
    """
    Если на day (сегодня) override ещё не создан — создаём его, учитывая вчерашний результат.
    Это обеспечивает перенос остатка: если вчера был лимит 350к и потратил 100к — +250к к сегодняшнему лимиту.
    """
    base = get_budget_base(chat_id, user_id, main_category, currency)
    if not base or base.get("daily_limit") is None:
        return None

    # если уже есть override на today — ничего не делаем
    if get_override(chat_id, user_id, main_category, currency, day):
        return None

    base_daily = Decimal(base["daily_limit"])
    yesterday = day - timedelta(days=1)

    eff_y = get_effective_daily_limit(chat_id, user_id, main_category, currency, yesterday)
    yesterday_limit = Decimal(eff_y["limit"]) if eff_y else base_daily

    yesterday_spent = sum_expenses(chat_id, user_id, yesterday, yesterday, main_category=main_category, currency=currency)

    delta = yesterday_limit - yesterday_spent  # + остаток, - перерасход
    today_limit = base_daily + delta
    if today_limit < 0:
        today_limit = Decimal("0")

    if delta == 0:
        reason = "Переноса нет: вчера потрачено ровно по дневному бюджету."
    elif delta > 0:
        reason = f"Перенос остатка {delta} {currency} со вчера."
    else:
        reason = f"Перенос перерасхода {abs(delta)} {currency} со вчера."

    upsert_override(chat_id, user_id, main_category, currency, day, today_limit, reason)

    return {
        "yesterday_limit": str(yesterday_limit),
        "yesterday_spent": str(yesterday_spent),
        "delta": str(delta),
        "today_limit": str(today_limit),
        "reason": reason
    }


def calc_left_and_warn(chat_id: int, user_id: int, main_category: str, currency: str, day: date) -> Dict[str, Any]:
    eff = get_effective_daily_limit(chat_id, user_id, main_category, currency, day)
    d_limit = eff["limit"] if eff else None
    d_reason = eff["reason"] if eff else None

    d_spent = sum_expenses(chat_id, user_id, day, day, main_category=main_category, currency=currency)
    d_left = (d_limit - d_spent) if d_limit is not None else None
    d_warn = bool(d_limit is not None and d_limit > 0 and d_left is not None and (d_left / d_limit) < Decimal("0.10"))

    base = get_budget_base(chat_id, user_id, main_category, currency) or {}
    m_limit = Decimal(base["monthly_limit"]) if base.get("monthly_limit") is not None else None
    ms = month_start(day)
    m_spent = sum_expenses(chat_id, user_id, ms, day, main_category=main_category, currency=currency)
    m_left = (m_limit - m_spent) if m_limit is not None else None
    m_warn = bool(m_limit is not None and m_limit > 0 and m_left is not None and (m_left / m_limit) < Decimal("0.10"))

    return {
        "daily": {
            "limit": str(d_limit) if d_limit is not None else None,
            "spent": str(d_spent),
            "left": str(d_left) if d_left is not None else None,
            "warn": d_warn,
            "reason": d_reason
        },
        "monthly": {
            "limit": str(m_limit) if m_limit is not None else None,
            "spent": str(m_spent),
            "left": str(m_left) if m_left is not None else None,
            "warn": m_warn
        }
    }


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


def extract_bot_mention(text: str, entities: Optional[List[MessageEntity]], bot_username: str) -> bool:
    if not text or not entities or not bot_username:
        return False
    target = f"@{bot_username.lower()}"
    for e in entities:
        if e.type == "mention":
            frag = text[e.offset : e.offset + e.length]
            if frag.lower() == target:
                return True
    return False


def strip_bot_mention(text: str, bot_username: str) -> str:
    if not text:
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

    # упоминание в тексте
    if extract_bot_mention(msg.text or "", msg.entities, bot_username):
        return True

    # или reply на сообщение бота
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot:
        if (msg.reply_to_message.from_user.username or "").lower() == bot_username.lower():
            return True

    return False


# =========================
# OpenAI prompts
# =========================

SYSTEM = f"""
Ты — ассистент в Telegram-группе для учёта личных расходов и бюджетов.
Тебе доступна база расходов и бюджетов конкретного пользователя (tg_user_id) внутри чата (chat_id).

Требования:
- понимать свободный русский текст
- поддерживать контекст диалога (summary + последние сообщения)
- уметь: записывать расходы, ставить бюджеты (дневной+месячный), выдавать историю за любой период,
  выдавать категории/подкатегории за период, анализировать и давать советы, удалять ошибочные записи (с подтверждением).
- если не хватает данных — задай 1 уточняющий вопрос.

ВАЖНО: перенос дневного бюджета:
- если недотрата: остаток переносится на следующий день (увеличивает дневной лимит)
- если перерасход: уменьшает дневной лимит следующего дня

Категоризация:
- main_category (например: "еда", "транспорт", "дом", "здоровье", "развлечения", "подписки", "другое")
- sub_category (например: "кофе", "рестораны", "такси", "продукты")

Валюта по умолчанию: {DEFAULT_CURRENCY}.
""".strip()

PLANNER = """
Верни ТОЛЬКО JSON (json_object) одного типа:

1) plan:
{
  "type": "plan",
  "actions": [
    {
      "action": "add_expense",
      "amount": 12000,
      "currency": "UZS",
      "main_category": "еда",
      "sub_category": "кофе",
      "note": "опционально",
      "spent_date": "YYYY-MM-DD (опционально, иначе сегодня)"
    },
    {
      "action": "set_budget",
      "currency": "UZS",
      "main_category": "еда",
      "daily_limit": 50000,
      "monthly_limit": 1200000
    },
    {
      "action": "get_history",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "group_by": "none|day|main_sub"
    },
    {
      "action": "get_categories",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD"
    },
    {
      "action": "get_stats",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD"
    },
    {
      "action": "suggest_savings",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD"
    },
    {
      "action": "delete_expense",
      "mode": "last|by_id|filter",
      "id": 123,
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "main_category": "еда (опционально)",
      "sub_category": "кофе (опционально)"
    }
  ],
  "assistant_message": "если уместно — короткое пояснение, иначе пусто"
}

2) clarify:
{
  "type": "clarify",
  "question": "один короткий уточняющий вопрос",
  "expected": "category|period|amount|confirm_delete|budget"
}
""".strip()

RESPONDER = f"""
Верни ТОЛЬКО JSON:
{{
  "type":"final",
  "reply":"готовый ответ пользователю на русском",
  "new_summary":"обновлённое краткое резюме диалога (до {MAX_SUMMARY_CHARS} символов)"
}}

Требования к ответу:
- при add_expense: покажи остаток дневного и месячного бюджета по main_category (если задан)
- если осталось <10% по дневному или месячному бюджету — предупреди
- учитывай перенос: если вчера была недотрата — сегодня дневной лимит больше; если перерасход — меньше
""".strip()


async def openai_json(messages: List[Dict[str, Any]], model: str = OPENAI_MODEL, timeout_s: int = 35) -> Dict[str, Any]:
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
            logger.error("OpenAI error %s: %s", r.status_code, r.text[:400])
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


def build_context(summary: str, history: List[Dict[str, str]], user_text: str, phase: str) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    msgs.append({"role": "system", "content": [{"type": "input_text", "text": SYSTEM}]})
    msgs.append({"role": "system", "content": [{"type": "input_text", "text": PLANNER if phase == "plan" else RESPONDER}]})

    if summary:
        msgs.append({"role": "system", "content": [{"type": "input_text", "text": f"Краткий контекст:\n{summary}"}]})

    for h in history:
        role = "user" if h["role"] == "user" else "assistant"
        msgs.append({"role": role, "content": [{"type": "input_text", "text": h["content"]}]})

    msgs.append({"role": "user", "content": [{"type": "input_text", "text": user_text}]})
    return msgs


# =========================
# Receipt parsing (photo)
# =========================

RECEIPT_PROMPT = f"""
Extract expense data from this receipt image for a personal expense tracker.
Return JSON only.

Format:
{{"type":"expense","amount":12345,"currency":"{DEFAULT_CURRENCY}","main_category":"еда","sub_category":"рестораны","note":"merchant or hint"}}

If not confident:
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
                    {"type": "input_text", "text": "Extract the total paid amount and categorize it. Return JSON only."},
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
# Action executor
# =========================

def execute_plan(chat_id: int, user_id: int, plan: Dict[str, Any]) -> Dict[str, Any]:
    actions = plan.get("actions") or []
    if not isinstance(actions, list):
        actions = []

    data: Dict[str, Any] = {"results": []}

    for a in actions:
        act = str(a.get("action") or "").strip()

        if act == "set_budget":
            main_category = str(a.get("main_category") or "other").lower().strip()
            currency = str(a.get("currency") or DEFAULT_CURRENCY).upper().strip()

            daily_limit = a.get("daily_limit", None)
            monthly_limit = a.get("monthly_limit", None)

            dl = Decimal(str(daily_limit)) if daily_limit is not None else None
            ml = Decimal(str(monthly_limit)) if monthly_limit is not None else None

            set_budget_base(chat_id, user_id, main_category, currency, dl, ml)

            # важное: сразу применим rollover на сегодня (создаст override если надо)
            if dl is not None:
                ensure_daily_rollover_for_today(chat_id, user_id, main_category, currency, today())

            data["results"].append({
                "action": "set_budget",
                "main_category": main_category,
                "currency": currency,
                "daily_limit": str(dl) if dl is not None else None,
                "monthly_limit": str(ml) if ml is not None else None
            })

        elif act == "add_expense":
            amount = Decimal(str(a.get("amount")))
            currency = str(a.get("currency") or DEFAULT_CURRENCY).upper().strip()
            main_category = str(a.get("main_category") or "other").lower().strip()
            sub_category = str(a.get("sub_category") or main_category).lower().strip()
            note = str(a.get("note") or "").strip()

            spent_date_str = (a.get("spent_date") or "").strip()
            when = None
            if spent_date_str:
                d = parse_ymd(spent_date_str)
                when = datetime(d.year, d.month, d.day, 12, 0, 0)

            d = when.date() if when else today()

            # ВАЖНО: rollover применяем ДО записи расхода (чтобы лимит дня был корректным)
            roll = ensure_daily_rollover_for_today(chat_id, user_id, main_category, currency, d)

            rid = add_expense(chat_id, user_id, amount, currency, main_category, sub_category, note, when=when)

            budget_info = calc_left_and_warn(chat_id, user_id, main_category, currency, d)

            data["results"].append({
                "action": "add_expense",
                "id": rid,
                "amount": str(amount),
                "currency": currency,
                "main_category": main_category,
                "sub_category": sub_category,
                "note": note,
                "rollover": roll,
                "budget_info": budget_info,
            })

        elif act == "get_history":
            start = parse_ymd(a["start_date"])
            end = parse_ymd(a["end_date"])
            group_by = str(a.get("group_by") or "none").strip()

            rows = find_expenses(chat_id, user_id, start, end)
            total = sum_expenses(chat_id, user_id, start, end)

            cats = breakdown_main_sub(chat_id, user_id, start, end) if group_by == "main_sub" else None

            data["results"].append({
                "action": "get_history",
                "start_date": str(start),
                "end_date": str(end),
                "group_by": group_by,
                "total": str(total),
                "rows_count": len(rows),
                "rows_preview": rows[:25],
                "by_main_sub": cats[:60] if cats else None
            })

        elif act == "get_categories":
            start = parse_ymd(a["start_date"])
            end = parse_ymd(a["end_date"])
            cats = breakdown_main_sub(chat_id, user_id, start, end)
            data["results"].append({
                "action": "get_categories",
                "start_date": str(start),
                "end_date": str(end),
                "rows": cats
            })

        elif act == "get_stats":
            start = parse_ymd(a["start_date"])
            end = parse_ymd(a["end_date"])
            cats = breakdown_main_sub(chat_id, user_id, start, end)
            total = sum_expenses(chat_id, user_id, start, end)
            data["results"].append({
                "action": "get_stats",
                "start_date": str(start),
                "end_date": str(end),
                "total": str(total),
                "cats": cats
            })

        elif act == "suggest_savings":
            start = parse_ymd(a["start_date"])
            end = parse_ymd(a["end_date"])
            cats = breakdown_main_sub(chat_id, user_id, start, end)
            total = sum_expenses(chat_id, user_id, start, end)
            data["results"].append({
                "action": "suggest_savings",
                "start_date": str(start),
                "end_date": str(end),
                "total": str(total),
                "cats": cats
            })

        elif act == "delete_expense":
            mode = str(a.get("mode") or "last").strip()
            if mode == "by_id":
                rid = int(a["id"])
                deleted = delete_expenses_by_ids(chat_id, user_id, [rid])
                data["results"].append({"action": "delete_expense", "mode": mode, "deleted": deleted, "ids": [rid]})

            elif mode == "last":
                rows = find_expenses(chat_id, user_id, today() - timedelta(days=3650), today())
                if rows:
                    rid = int(rows[0]["id"])
                    deleted = delete_expenses_by_ids(chat_id, user_id, [rid])
                    data["results"].append({"action": "delete_expense", "mode": mode, "deleted": deleted, "ids": [rid]})
                else:
                    data["results"].append({"action": "delete_expense", "mode": mode, "deleted": 0, "ids": []})

            elif mode == "filter":
                start = parse_ymd(a["start_date"])
                end = parse_ymd(a["end_date"])
                mc = (a.get("main_category") or None)
                sc = (a.get("sub_category") or None)
                mc = mc.lower().strip() if isinstance(mc, str) and mc.strip() else None
                sc = sc.lower().strip() if isinstance(sc, str) and sc.strip() else None
                rows = find_expenses(chat_id, user_id, start, end, main_category=mc, sub_category=sc)
                ids = [int(r["id"]) for r in rows]
                deleted = delete_expenses_by_ids(chat_id, user_id, ids)
                data["results"].append({
                    "action": "delete_expense",
                    "mode": mode,
                    "deleted": deleted,
                    "ids": ids[:200],
                    "matched": len(ids)
                })
            else:
                data["results"].append({"action": "delete_expense", "error": "unknown_mode"})

        else:
            data["results"].append({"action": act, "error": "unknown_action"})

    return data


# =========================
# Monthly report (1st day)
# =========================

def month_report_text_for_user(chat_id: int, user_id: int) -> Optional[str]:
    start, end = prev_month_range(today())
    total = sum_expenses(chat_id, user_id, start, end)
    if total <= 0:
        return None
    cats = breakdown_main_sub(chat_id, user_id, start, end)
    top = cats[:8]
    lines = [
        f"📊 Отчёт за прошлый месяц ({start} — {end})",
        f"Итого: {total} {DEFAULT_CURRENCY}",
    ]
    if top:
        lines.append("Топ категорий:")
        for r in top:
            lines.append(f"• {r['main_category']} / {r['sub_category']}: {r['spent']} {r['currency']}")
    return "\n".join(lines)


async def monthly_job(context: ContextTypes.DEFAULT_TYPE):
    if today().day != 1:
        return

    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT chat_id FROM known_chats;")
        chats = [int(r["chat_id"]) for r in cur.fetchall()]

    for chat_id in chats:
        start, end = prev_month_range(today())
        with db() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT tg_user_id
                FROM expenses
                WHERE chat_id=%s AND spent_date BETWEEN %s AND %s;
            """, (chat_id, start, end))
            users = [int(r["tg_user_id"]) for r in cur.fetchall()]

        for uid in users:
            txt = month_report_text_for_user(chat_id, uid)
            if not txt:
                continue
            try:
                await context.bot.send_message(chat_id=chat_id, text=txt)
            except Exception as e:
                logger.error("monthly send failed chat=%s: %s", chat_id, e)


# =========================
# Update broadcast
# =========================

async def broadcast_update(app: Application):
    prev = get_meta("version")
    if prev == BOT_VERSION:
        return

    msg = RELEASE_NOTES.strip()
    if not msg:
        msg = (
            "🆕 Обновление бота\n"
            "— перенос остатка/перерасхода дневного бюджета на следующий день\n"
            "— дневной и месячный бюджеты, история, категории\n"
            "— подтверждение удаления записей\n"
        )

    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT chat_id FROM known_chats;")
        chats = [int(r["chat_id"]) for r in cur.fetchall()]

    for chat_id in chats:
        try:
            await app.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.error("broadcast failed chat=%s: %s", chat_id, e)

    set_meta("version", BOT_VERSION)


# =========================
# Handlers
# =========================

WELCOME_TEXT = (
    "Привет. Я веду учёт расходов и бюджетов в этой группе.\n\n"
    "Как пользоваться:\n"
    "— Упомяните меня и напишите текстом: расход, бюджет, запрос истории.\n"
    "— Я пойму свободную формулировку и выполню действие.\n"
    "— Если данных мало — задам один уточняющий вопрос.\n\n"
    "Перенос дневного бюджета:\n"
    "— если вчера недотратили, остаток переносится и увеличивает лимит сегодня\n"
    "— если вчера перерасход, уменьшает лимит сегодня\n"
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME_CACHE
    if not BOT_USERNAME_CACHE:
        BOT_USERNAME_CACHE = (context.bot.username or "").strip()

    if update.effective_chat:
        touch_chat(update.effective_chat.id)

    await update.effective_message.reply_text(WELCOME_TEXT)


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
    touch_chat(chat_id)

    raw = (msg.text or "").strip()
    if not raw:
        return

    # подтверждение удаления
    st = get_state(chat_id, user_id)
    if st and st.get("pending") and st.get("kind") == "confirm_delete":
        low = raw.lower().strip()
        if low in ("да", "да.", "yes", "y"):
            ids = st.get("ids", [])
            deleted = delete_expenses_by_ids(chat_id, user_id, [int(x) for x in ids])
            clear_state(chat_id, user_id)
            reply = f"Готово. Удалено записей: {deleted}."
            add_history(chat_id, user_id, "assistant", reply)
            await msg.reply_text(reply)
            return
        if low in ("нет", "нет.", "no", "n"):
            clear_state(chat_id, user_id)
            reply = "Отменено."
            add_history(chat_id, user_id, "assistant", reply)
            await msg.reply_text(reply)
            return
        await msg.reply_text("Ответьте, пожалуйста: Да или Нет.")
        return

    if not should_process(update, bot_username):
        return

    user_text = strip_bot_mention(raw, bot_username).strip()
    add_history(chat_id, user_id, "user", user_text)

    summary = get_summary(chat_id, user_id)
    history = get_history(chat_id, user_id, HISTORY_LIMIT)

    plan = await openai_json(build_context(summary, history, user_text, phase="plan"))

    if str(plan.get("type") or "").lower() == "clarify":
        q = str(plan.get("question") or "Уточните, пожалуйста.").strip()
        add_history(chat_id, user_id, "assistant", q)
        await msg.reply_text(q)
        return

    # удаление: если нужно подтверждение — спросим
    actions = plan.get("actions") or []
    if isinstance(actions, list):
        for a in actions:
            if str(a.get("action") or "") == "delete_expense":
                mode = str(a.get("mode") or "last")
                if mode == "filter":
                    start = parse_ymd(a["start_date"])
                    end = parse_ymd(a["end_date"])
                    mc = (a.get("main_category") or None)
                    sc = (a.get("sub_category") or None)
                    mc = mc.lower().strip() if isinstance(mc, str) and mc.strip() else None
                    sc = sc.lower().strip() if isinstance(sc, str) and sc.strip() else None
                    rows = find_expenses(chat_id, user_id, start, end, main_category=mc, sub_category=sc)
                    ids = [int(r["id"]) for r in rows]
                    if len(ids) == 0:
                        reply = "Не нашла подходящих записей для удаления."
                        add_history(chat_id, user_id, "assistant", reply)
                        await msg.reply_text(reply)
                        return
                    set_state(chat_id, user_id, {"pending": True, "kind": "confirm_delete", "ids": ids[:200]})
                    q = f"Найдено {len(ids)} записей. Удалить все? Напишите: Да / Нет"
                    add_history(chat_id, user_id, "assistant", q)
                    await msg.reply_text(q)
                    return

                if mode == "last":
                    rows = find_expenses(chat_id, user_id, today() - timedelta(days=3650), today())
                    if not rows:
                        reply = "Нет записей для удаления."
                        add_history(chat_id, user_id, "assistant", reply)
                        await msg.reply_text(reply)
                        return
                    rid = int(rows[0]["id"])
                    set_state(chat_id, user_id, {"pending": True, "kind": "confirm_delete", "ids": [rid]})
                    q = f"Удалить последнюю запись (id={rid}, {rows[0]['amount']} {rows[0]['currency']} — {rows[0]['main_category']}/{rows[0]['sub_category']})? Да / Нет"
                    add_history(chat_id, user_id, "assistant", q)
                    await msg.reply_text(q)
                    return

    data = execute_plan(chat_id, user_id, plan)

    final = await openai_json(build_context(
        summary,
        history,
        user_text + "\n\nDATA:\n" + json.dumps(data, ensure_ascii=False),
        phase="final"
    ))
    reply = str(final.get("reply") or "Готово.").strip()
    new_summary = str(final.get("new_summary") or summary).strip()

    add_history(chat_id, user_id, "assistant", reply)
    set_summary(chat_id, user_id, new_summary)

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

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    touch_chat(chat_id)

    caption = (msg.caption or "").strip()
    mentioned = extract_bot_mention(caption, msg.caption_entities, bot_username) if caption else False
    replied = bool(
        msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot
        and (msg.reply_to_message.from_user.username or "").lower() == bot_username.lower()
    )

    if MENTION_ONLY and not (mentioned or replied):
        return

    photo = msg.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    parsed = await parse_receipt(bytes(image_bytes))
    if parsed.get("type") != "expense":
        await msg.reply_text("Не удалось надёжно распознать чек. Напишите расход текстом, упомянув меня.")
        return

    try:
        amount = Decimal(str(parsed.get("amount")))
        currency = str(parsed.get("currency") or DEFAULT_CURRENCY).upper().strip()
        mc = str(parsed.get("main_category") or "other").lower().strip()
        sc = str(parsed.get("sub_category") or mc).lower().strip()
        note = str(parsed.get("note") or "").strip()
    except Exception:
        await msg.reply_text("Не удалось корректно распознать чек. Напишите расход текстом.")
        return

    roll = ensure_daily_rollover_for_today(chat_id, user_id, mc, currency, today())

    rid = add_expense(chat_id, user_id, amount, currency, mc, sc, note)
    info = calc_left_and_warn(chat_id, user_id, mc, currency, today())

    text = f"🧾 Записано: {mc}/{sc} — {amount} {currency} (id={rid})"
    if roll:
        text += f"\n📌 {roll['reason']} Сегодня лимит: {roll['today_limit']} {currency}"

    if info["daily"]["limit"]:
        text += f"\nДень: осталось {info['daily']['left']} из {info['daily']['limit']} {currency}"
        if info["daily"]["warn"]:
            text += "\n⚠️ В дневном бюджете осталось меньше 10%."

    if info["monthly"]["limit"]:
        text += f"\nМесяц: осталось {info['monthly']['left']} из {info['monthly']['limit']} {currency}"
        if info["monthly"]["warn"]:
            text += "\n⚠️ В месячном бюджете осталось меньше 10%."

    add_history(chat_id, user_id, "user", "[фото]")
    add_history(chat_id, user_id, "assistant", text)
    await msg.reply_text(text)


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("OK")


def normalize_url(u: str) -> str:
    u = (u or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith("https://"):
        u = "https://" + u
    return u


def main():
    if not TELEGRAM_BOT_TOKEN or not DATABASE_URL or not PUBLIC_URL:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / DATABASE_URL / PUBLIC_URL")

    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue:
        app.job_queue.run_repeating(monthly_job, interval=24 * 60 * 60, first=30)

    app.post_init = broadcast_update

    public_url = normalize_url(PUBLIC_URL)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{public_url}/telegram",
    )


if __name__ == "__main__":
    main()