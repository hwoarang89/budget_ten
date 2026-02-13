{\rtf1\ansi\ansicpg1251\cocoartf2822
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\paperw11900\paperh16840\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx720\tx1440\tx2160\tx2880\tx3600\tx4320\tx5040\tx5760\tx6480\tx7200\tx7920\tx8640\pardirnatural\partightenfactor0

\f0\fs24 \cf0 import os\
import json\
import re\
import base64\
import httpx\
import psycopg\
from datetime import datetime\
from telegram import Update\
from telegram.ext import Application, MessageHandler, ContextTypes, filters\
\
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")\
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")\
DATABASE_URL = os.getenv("DATABASE_URL")\
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "UZS")\
\
SYSTEM_PROMPT = """You are an expense-tracking assistant.\
Return ONLY valid JSON for each user message.\
If info is missing, set needs_clarification=true and ask one short question.\
Schema:\
\{\
  "intent": "add_expense|set_budget|report|unknown",\
  "amount": number|null,\
  "currency": "UZS|USD|EUR"|null,\
  "category": string|null,\
  "date": "YYYY-MM-DD"|null,\
  "note": string|null,\
  "confidence": number,\
  "needs_clarification": boolean,\
  "clarification_question": string|null\
\}\
Rules:\
- Prefer DEFAULT_CURRENCY when currency not specified.\
- If message is clearly a report request, set intent=report.\
- If message is setting a budget, intent=set_budget and amount is budget limit.\
"""\
\
def db():\
    return psycopg.connect(DATABASE_URL)\
\
def init_db():\
    with db() as conn, conn.cursor() as cur:\
        cur.execute("""\
        CREATE TABLE IF NOT EXISTS expenses (\
            id bigserial PRIMARY KEY,\
            tg_user_id bigint NOT NULL,\
            amount numeric NOT NULL,\
            currency text NOT NULL,\
            category text NOT NULL,\
            note text,\
            spent_date date NOT NULL,\
            created_at timestamptz NOT NULL DEFAULT now()\
        );\
        """)\
        cur.execute("""\
        CREATE TABLE IF NOT EXISTS budgets (\
            id bigserial PRIMARY KEY,\
            tg_user_id bigint NOT NULL,\
            category text NOT NULL,\
            period text NOT NULL DEFAULT 'monthly',\
            limit_amount numeric NOT NULL,\
            currency text NOT NULL,\
            created_at timestamptz NOT NULL DEFAULT now(),\
            UNIQUE(tg_user_id, category, period)\
        );\
        """)\
        conn.commit()\
\
async def openai_parse_text(user_text: str) -> dict:\
    # Responses API: text -> structured JSON (you can strengthen with json_schema if needed)\
    headers = \{"Authorization": f"Bearer \{OPENAI_API_KEY\}", "Content-Type": "application/json"\}\
    payload = \{\
        "model": "gpt-5.2-chat-latest",\
        "input": [\
            \{"role": "system", "content": [\{"type": "text", "text": SYSTEM_PROMPT.replace("DEFAULT_CURRENCY", DEFAULT_CURRENCY)\}]\},\
            \{"role": "user", "content": [\{"type": "text", "text": user_text\}]\}\
        ],\
        "text": \{"format": \{"type": "json_object"\}\}\
    \}\
    async with httpx.AsyncClient(timeout=30) as client:\
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)\
        r.raise_for_status()\
        data = r.json()\
        # Most SDKs expose parsed output; here we safely extract text output:\
        out_text = ""\
        for item in data.get("output", []):\
            for c in item.get("content", []):\
                if c.get("type") == "output_text":\
                    out_text += c.get("text", "")\
        return json.loads(out_text)\
\
async def openai_extract_from_image(image_bytes: bytes, hint: str = "") -> dict:\
    # Vision: ask model to extract amount/currency/merchant-like note.\
    # See image inputs in Responses API docs.  [oai_citation:2\'87developers.openai.com](https://developers.openai.com/api/reference/resources/responses/?utm_source=chatgpt.com)\
    b64 = base64.b64encode(image_bytes).decode("utf-8")\
    headers = \{"Authorization": f"Bearer \{OPENAI_API_KEY\}", "Content-Type": "application/json"\}\
    prompt = f"""Extract expense info from the screenshot. Return ONLY JSON with:\
\{\{"amount": number|null, "currency": string|null, "note": string|null, "confidence": number\}\}\
Prioritize total/\uc0\u1080 \u1090 \u1086 \u1075 \u1086 /\u1082  \u1086 \u1087 \u1083 \u1072 \u1090 \u1077 .\
Hint: \{hint\}"""\
    payload = \{\
        "model": "gpt-5.2-chat-latest",\
        "input": [\{\
            "role": "user",\
            "content": [\
                \{"type": "input_text", "text": prompt\},\
                \{"type": "input_image", "image_base64": b64\}\
            ]\
        \}],\
        "text": \{"format": \{"type": "json_object"\}\}\
    \}\
    async with httpx.AsyncClient(timeout=60) as client:\
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)\
        r.raise_for_status()\
        data = r.json()\
        out_text = ""\
        for item in data.get("output", []):\
            for c in item.get("content", []):\
                if c.get("type") == "output_text":\
                    out_text += c.get("text", "")\
        return json.loads(out_text)\
\
def add_expense(tg_user_id: int, amount: float, currency: str, category: str, note: str, spent_date: str):\
    with db() as conn, conn.cursor() as cur:\
        cur.execute(\
            "INSERT INTO expenses (tg_user_id, amount, currency, category, note, spent_date) VALUES (%s,%s,%s,%s,%s,%s)",\
            (tg_user_id, amount, currency, category, note, spent_date)\
        )\
        conn.commit()\
\
def month_spent_and_limit(tg_user_id: int, category: str, currency: str):\
    today = datetime.utcnow().date()\
    month_start = today.replace(day=1)\
    with db() as conn, conn.cursor() as cur:\
        cur.execute(\
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE tg_user_id=%s AND category=%s AND currency=%s AND spent_date >= %s",\
            (tg_user_id, category, currency, month_start)\
        )\
        spent = float(cur.fetchone()[0])\
\
        cur.execute(\
            "SELECT limit_amount FROM budgets WHERE tg_user_id=%s AND category=%s AND period='monthly' AND currency=%s",\
            (tg_user_id, category, currency)\
        )\
        row = cur.fetchone()\
        limit_amt = float(row[0]) if row else None\
    return spent, limit_amt\
\
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):\
    tg_user_id = update.effective_user.id\
\
    # 1) If photo: extract from image then ask/record\
    if update.message.photo:\
        file = await update.message.photo[-1].get_file()\
        image_bytes = await file.download_as_bytearray()\
        extracted = await openai_extract_from_image(bytes(image_bytes))\
        if not extracted.get("amount"):\
            await update.message.reply_text("\uc0\u1053 \u1077  \u1089 \u1084 \u1086 \u1075 \u1083 \u1072  \u1091 \u1074 \u1077 \u1088 \u1077 \u1085 \u1085 \u1086  \u1088 \u1072 \u1089 \u1087 \u1086 \u1079 \u1085 \u1072 \u1090 \u1100  \u1089 \u1091 \u1084 \u1084 \u1091 . \u1053 \u1072 \u1087 \u1080 \u1096 \u1080 \u1090 \u1077  \u1089 \u1091 \u1084 \u1084 \u1091  \u1090 \u1077 \u1082 \u1089 \u1090 \u1086 \u1084 , \u1085 \u1072 \u1087 \u1088 \u1080 \u1084 \u1077 \u1088 : \'93\u1090 \u1072 \u1082 \u1089 \u1080  35000\'94.")\
            return\
\
        # We still need category -> ask model using note + amount\
        text = f"\{extracted.get('note','')\} \{extracted['amount']\} \{extracted.get('currency') or DEFAULT_CURRENCY\}"\
        parsed = await openai_parse_text(text)\
    else:\
        parsed = await openai_parse_text(update.message.text or "")\
\
    # 2) Clarification flow (MVP: \uc0\u1087 \u1088 \u1086 \u1089 \u1090 \u1086  \u1079 \u1072 \u1076 \u1072 \u1105 \u1084  \u1074 \u1086 \u1087 \u1088 \u1086 \u1089  \u1080  \u1085 \u1077  \u1074 \u1077 \u1076 \u1105 \u1084  state-machine)\
    if parsed.get("needs_clarification"):\
        await update.message.reply_text(parsed.get("clarification_question") or "\uc0\u1059 \u1090 \u1086 \u1095 \u1085 \u1080 \u1090 \u1077 , \u1087 \u1086 \u1078 \u1072 \u1083 \u1091 \u1081 \u1089 \u1090 \u1072 .")\
        return\
\
    intent = parsed.get("intent")\
    if intent != "add_expense":\
        await update.message.reply_text("\uc0\u1055 \u1086 \u1082 \u1072  \u1087 \u1086 \u1076 \u1076 \u1077 \u1088 \u1078 \u1080 \u1074 \u1072 \u1102  \u1090 \u1086 \u1083 \u1100 \u1082 \u1086  \u1076 \u1086 \u1073 \u1072 \u1074 \u1083 \u1077 \u1085 \u1080 \u1077  \u1088 \u1072 \u1089 \u1093 \u1086 \u1076 \u1086 \u1074  \u1074  MVP. \u1053 \u1072 \u1087 \u1080 \u1096 \u1080 \u1090 \u1077  \u1088 \u1072 \u1089 \u1093 \u1086 \u1076 : \'93\u1077 \u1076 \u1072  120000\'94.")\
        return\
\
    amount = parsed.get("amount")\
    currency = parsed.get("currency") or DEFAULT_CURRENCY\
    category = (parsed.get("category") or "\uc0\u1055 \u1088 \u1086 \u1095 \u1077 \u1077 ").strip()\
    note = (parsed.get("note") or "").strip()\
    spent_date = parsed.get("date") or str(datetime.utcnow().date())\
\
    if amount is None:\
        await update.message.reply_text("\uc0\u1053 \u1077  \u1074 \u1080 \u1078 \u1091  \u1089 \u1091 \u1084 \u1084 \u1091 . \u1053 \u1072 \u1087 \u1080 \u1096 \u1080 \u1090 \u1077 , \u1085 \u1072 \u1087 \u1088 \u1080 \u1084 \u1077 \u1088 : \'93\u1082 \u1086 \u1092 \u1077  28000\'94.")\
        return\
\
    add_expense(tg_user_id, float(amount), currency, category, note, spent_date)\
\
    spent, limit_amt = month_spent_and_limit(tg_user_id, category, currency)\
    if limit_amt is None:\
        await update.message.reply_text(\
            f"\uc0\u9989  \u1047 \u1072 \u1087 \u1080 \u1089 \u1072 \u1085 \u1086 : \{category\} \'97 \{amount\} \{currency\}\\n"\
            f"\uc0\u1055 \u1086 \u1090 \u1088 \u1072 \u1095 \u1077 \u1085 \u1086  \u1074  \u1101 \u1090 \u1086 \u1084  \u1084 \u1077 \u1089 \u1103 \u1094 \u1077  \u1087 \u1086  \u1082 \u1072 \u1090 \u1077 \u1075 \u1086 \u1088 \u1080 \u1080 : \{spent:.0f\} \{currency\}\\n"\
            f"\uc0\u1041 \u1102 \u1076 \u1078 \u1077 \u1090  \u1085 \u1072  \u1082 \u1072 \u1090 \u1077 \u1075 \u1086 \u1088 \u1080 \u1102  \u1085 \u1077  \u1079 \u1072 \u1076 \u1072 \u1085 . \u1053 \u1072 \u1087 \u1080 \u1096 \u1080 \u1090 \u1077 : \'93\u1073 \u1102 \u1076 \u1078 \u1077 \u1090  \u1085 \u1072  \{category\} 3000000 \u1074  \u1084 \u1077 \u1089 \u1103 \u1094 \'94."\
        )\
    else:\
        left = limit_amt - spent\
        await update.message.reply_text(\
            f"\uc0\u9989  \u1047 \u1072 \u1087 \u1080 \u1089 \u1072 \u1085 \u1086 : \{category\} \'97 \{amount\} \{currency\}\\n"\
            f"\uc0\u1054 \u1089 \u1090 \u1072 \u1083 \u1086 \u1089 \u1100  \u1087 \u1086  \u1073 \u1102 \u1076 \u1078 \u1077 \u1090 \u1091  \u1074  \u1101 \u1090 \u1086 \u1084  \u1084 \u1077 \u1089 \u1103 \u1094 \u1077 : \{left:.0f\} \{currency\} (\u1083 \u1080 \u1084 \u1080 \u1090  \{limit_amt:.0f\})"\
        )\
\
def main():\
    if not TELEGRAM_BOT_TOKEN or not OPENAI_API_KEY or not DATABASE_URL:\
        raise RuntimeError("Missing env vars: TELEGRAM_BOT_TOKEN / OPENAI_API_KEY / DATABASE_URL")\
\
    init_db()\
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()\
    app.add_handler(MessageHandler(filters.ALL, on_message))\
    app.run_polling()\
\
if __name__ == "__main__":\
    main()}