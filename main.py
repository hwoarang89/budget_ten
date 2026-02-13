import os
import json
import re
import base64
import httpx
import psycopg
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "UZS")

SYSTEM_PROMPT = """You are an expense-tracking assistant.
Return ONLY valid JSON for each user message.
If info is missing, set needs_clarification=true and ask one short question.
Schema:
{
  "intent": "add_expense|set_budget|report|unknown",
  "amount": number|null,
  "currency": "UZS|USD|EUR"|null,
  "category": string|null,
  "date": "YYYY-MM-DD"|null,
  "note": string|null,
  "confidence": number,
  "needs_clarification": boolean,
  "clarification_question": string|null
}
Rules:
- Prefer DEFAULT_CURRENCY when currency not specified.
- If message is clearly a report request, set intent=report.
- If message is setting a budget, intent=set_budget and amount is budget limit.
"""

def db():
    return psycopg.connect(DATABASE_URL)

def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id bigserial PRIMARY KEY,
            tg_user_id bigint NOT NULL,
            amount numeric NOT NULL,
            currency text NOT NULL,
            category text NOT NULL,
            note text,
            spent_date date NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            id bigserial PRIMARY KEY,
            tg_user_id bigint NOT NULL,
            category text NOT NULL,
            period text NOT NULL DEFAULT 'monthly',
            limit_amount numeric NOT NULL,
            currency text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(tg_user_id, category, period)
        );
        """)
        conn.commit()

async def openai_parse_text(user_text: str) -> dict:
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "gpt-5.2-chat-latest",
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_text}]
            }
        ],
        "text": {"format": {"type": "json_object"}}
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI error {r.status_code}: {r.text}")
        data = r.json()

    out_text = ""
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                out_text += c.get("text", "")

    return json.loads(out_text)

async def openai_extract_from_image(image_bytes: bytes, hint: str = "") -> dict:
    # Vision: ask model to extract amount/currency/merchant-like note.
    # See image inputs in Responses API docs.  [oai_citation:2‡developers.openai.com](https://developers.openai.com/api/reference/resources/responses/?utm_source=chatgpt.com)
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""Extract expense info from the screenshot. Return ONLY JSON with:
{{"amount": number|null, "currency": string|null, "note": string|null, "confidence": number}}
Prioritize total/итого/к оплате.
Hint: {hint}"""
    payload = {
        "model": "gpt-5.2-chat-latest",
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_base64": b64}
            ]
        }],
        "text": {"format": {"type": "json_object"}}
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        out_text = ""
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    out_text += c.get("text", "")
        return json.loads(out_text)

def add_expense(tg_user_id: int, amount: float, currency: str, category: str, note: str, spent_date: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO expenses (tg_user_id, amount, currency, category, note, spent_date) VALUES (%s,%s,%s,%s,%s,%s)",
            (tg_user_id, amount, currency, category, note, spent_date)
        )
        conn.commit()

def month_spent_and_limit(tg_user_id: int, category: str, currency: str):
    today = datetime.utcnow().date()
    month_start = today.replace(day=1)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE tg_user_id=%s AND category=%s AND currency=%s AND spent_date >= %s",
            (tg_user_id, category, currency, month_start)
        )
        spent = float(cur.fetchone()[0])

        cur.execute(
            "SELECT limit_amount FROM budgets WHERE tg_user_id=%s AND category=%s AND period='monthly' AND currency=%s",
            (tg_user_id, category, currency)
        )
        row = cur.fetchone()
        limit_amt = float(row[0]) if row else None
    return spent, limit_amt

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user_id = update.effective_user.id

    # 1) If photo: extract from image then ask/record
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        image_bytes = await file.download_as_bytearray()
        extracted = await openai_extract_from_image(bytes(image_bytes))
        if not extracted.get("amount"):
            await update.message.reply_text("Не смогла уверенно распознать сумму. Напишите сумму текстом, например: “такси 35000”.")
            return

        # We still need category -> ask model using note + amount
        text = f"{extracted.get('note','')} {extracted['amount']} {extracted.get('currency') or DEFAULT_CURRENCY}"
        parsed = await openai_parse_text(text)
    else:
        parsed = await openai_parse_text(update.message.text or "")

    # 2) Clarification flow (MVP: просто задаём вопрос и не ведём state-machine)
    if parsed.get("needs_clarification"):
        await update.message.reply_text(parsed.get("clarification_question") or "Уточните, пожалуйста.")
        return

    intent = parsed.get("intent")
    if intent != "add_expense":
        await update.message.reply_text("Пока поддерживаю только добавление расходов в MVP. Напишите расход: “еда 120000”.")
        return

    amount = parsed.get("amount")
    currency = parsed.get("currency") or DEFAULT_CURRENCY
    category = (parsed.get("category") or "Прочее").strip()
    note = (parsed.get("note") or "").strip()
    spent_date = parsed.get("date") or str(datetime.utcnow().date())

    if amount is None:
        await update.message.reply_text("Не вижу сумму. Напишите, например: “кофе 28000”.")
        return

    add_expense(tg_user_id, float(amount), currency, category, note, spent_date)

    spent, limit_amt = month_spent_and_limit(tg_user_id, category, currency)
    if limit_amt is None:
        await update.message.reply_text(
            f"✅ Записано: {category} — {amount} {currency}\n"
            f"Потрачено в этом месяце по категории: {spent:.0f} {currency}\n"
            f"Бюджет на категорию не задан. Напишите: “бюджет на {category} 3000000 в месяц”."
        )
    else:
        left = limit_amt - spent
        await update.message.reply_text(
            f"✅ Записано: {category} — {amount} {currency}\n"
            f"Осталось по бюджету в этом месяце: {left:.0f} {currency} (лимит {limit_amt:.0f})"
        )

def migrate_personal_budgets():
    with db() as conn, conn.cursor() as cur:
        # add chat_id to expenses
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS chat_id bigint;")
        cur.execute("UPDATE expenses SET chat_id = tg_user_id WHERE chat_id IS NULL;")
        cur.execute("ALTER TABLE expenses ALTER COLUMN chat_id SET NOT NULL;")

        # add chat_id to budgets
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS chat_id bigint;")
        cur.execute("UPDATE budgets SET chat_id = tg_user_id WHERE chat_id IS NULL;")
        cur.execute("ALTER TABLE budgets ALTER COLUMN chat_id SET NOT NULL;")

        # unique index for personal budgets
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS budgets_unique_personal
            ON budgets (chat_id, tg_user_id, category, period, currency);
        """)

        conn.commit()

def main():
    if not TELEGRAM_BOT_TOKEN or not OPENAI_API_KEY or not DATABASE_URL:
        raise RuntimeError("Missing env vars: TELEGRAM_BOT_TOKEN / OPENAI_API_KEY / DATABASE_URL")

    init_db()
    migrate_personal_budgets()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, on_message))
    public_url = os.getenv("PUBLIC_URL")
    if not public_url:
        raise RuntimeError("Missing env var: PUBLIC_URL")

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        url_path="telegram",
        webhook_url=f"worker-production-e43d.up.railway.app/telegram",
)

if __name__ == "__main__":
    main()