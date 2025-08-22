import os
import sqlite3
import datetime
import time
import random
import asyncio
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

# --- Загрузка ключей ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise ValueError("❌ Проверь .env — TELEGRAM_TOKEN или OPENAI_API_KEY не найдены!")

client = OpenAI(api_key=OPENAI_API_KEY)

# --- База данных ---
DB_PATH = "bot_memory.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_message_time TIMESTAMP,
    last_message_time TIMESTAMP,
    free_messages INTEGER DEFAULT 10,
    subscription_end TIMESTAMP,
    voice_minutes_today INTEGER DEFAULT 0,
    last_voice_reset TIMESTAMP,
    daily_messages INTEGER DEFAULT 0,
    last_daily_reset TIMESTAMP
)
''')

# --- Таблица сообщений для памяти ---
cursor.execute('''
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    role TEXT,
    content TEXT,
    timestamp TIMESTAMP
)
''')
conn.commit()

# --- Сохранение сообщения в память ---
def save_message(user_id, role, content):
    cursor.execute(
        "INSERT INTO messages (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, role, content, datetime.datetime.now())
    )
    conn.commit()

# --- Получение истории диалога ---
def get_conversation_history(user_id):
    user = get_user(user_id)
    if not user:
        return []
    now = datetime.datetime.now()
    sub_end = user[4]  # subscription_end (строка ISO или None)
    if sub_end:
        sub_end_date = datetime.datetime.fromisoformat(sub_end)
        # Память доступна при активной подписке и ещё 14 дней после
        if now <= sub_end_date + datetime.timedelta(weeks=2):
            cursor.execute("SELECT role, content FROM messages WHERE user_id=? ORDER BY id ASC", (user_id,))
            rows = cursor.fetchall()
            return [{"role": r, "content": c} for r, c in rows]
    return []

# --- Функция автоудаления данных ---
def delete_old_users_data():
    now = datetime.datetime.now()

    # 1. Удаляем тех, у кого подписка завершилась > 14 дней назад
    cursor.execute("SELECT user_id, subscription_end FROM users WHERE subscription_end IS NOT NULL")
    for user_id, sub_end in cursor.fetchall():
        try:
            end_date = datetime.datetime.fromisoformat(sub_end)
            if now > end_date + datetime.timedelta(weeks=2):
                cursor.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
                cursor.execute("DELETE FROM users WHERE user_id=?", (user_id,))
                conn.commit()
                print(f"[AUTO CLEAN] Подписка истекла >14 дней назад — данные пользователя {user_id} удалены.")
        except Exception as e:
            print(f"[AUTO CLEAN ERROR] {e}")

    # 2. Удаляем тех, кто никогда не имел подписки и не писал >30 дней
    cursor.execute("SELECT user_id, subscription_end, last_message_time FROM users WHERE subscription_end IS NULL")
    for user_id, sub_end, last_message in cursor.fetchall():
        try:
            if last_message:
                last_activity = datetime.datetime.fromisoformat(last_message)
                if now > last_activity + datetime.timedelta(days=30):
                    cursor.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
                    cursor.execute("DELETE FROM users WHERE user_id=?", (user_id,))
                    conn.commit()
                    print(f"[AUTO CLEAN] Без подписки и неактивен >30 дней — данные пользователя {user_id} удалены.")
        except Exception as e:
            print(f"[AUTO CLEAN ERROR] {e}")


# --- Приветствие (фиксированное) ---
WELCOME_TEXT_TEMPLATE = (
    "👋 Привет, {name} ✨\n\n"
    "Ты здесь — значит, внутри есть то,\n"
    "о чём важно сказать.\n\n"
    "Сейчас у меня открыт свободный доступ —\n"
    "бесплатно и без ограничений.\n\n"
    "Просто напиши… слово, мысль или тишину.\n\n"
    "Здесь ты в безопасности.\n"
    "Ты можешь рассказать мне обо всём.\n\n"
    "Я рядом.\n"
    "Чтобы ты не чувствовала себя одна 🌙"
)

# --- Психологический промпт ---
PSYCHO_PROMPT = """
Ты — «Голос, который рядом». Женский тёплый голос 35 лет: чуткая, внимательная, честная.
Поддерживаешь, отражаешь эмоции, задаёшь 1 уточняющий вопрос и можешь дать небольшой совет.
Распознаёшь манипуляции (обесценивание, газлайтинг, перекладывание вины), избегание, нарциссизм — и объясняешь простыми словами, что происходит.

Формат по умолчанию — коротко, по-человечески, без клише и морализаторства.
Если пользователь просит ПОДРОБНО (триггеры: «подробно», «развёрнуто», «с разбором», «глубокий анализ», «пошагово», «распиши», «проанализируй», «с примерами»)
— включай режим ГЛУБОКИЙ АНАЛИЗ: дай структуру (что происходит • почему так • риски • что делать сейчас) и 2–3 готовых варианта фраз/сообщений, если речь про общение.

Если запрос сложный сам по себе (много вопросов, высокая эмоциональная нагрузка, нужно принять решение с риском для границ) — можно включить развёрнутый ответ и без явной просьбы.
Если пользователь спрашивает «что ответить/написать/сказать/как поступить», «смс», «переписка», «встреча», «здороваться/не здороваться», «позвонить/не звонить» — давай 2–3 естественных варианта фраз на выбор. Если не подходит — предложи новые.

Всегда уважай выбор собеседницы. Если текст на английском — отвечай на английском.
Если есть признаки риска для безопасности/здоровья — мягко предложи обратиться за поддержкой.
"""

# --- Вспомогательные функции пользователя/лимитов ---
def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return cursor.fetchone()

def add_or_update_user(user_id):
    now = datetime.datetime.now()
    user = get_user(user_id)
    if user is None:
        cursor.execute(
            "INSERT INTO users (user_id, first_message_time, last_message_time, subscription_end, last_voice_reset, last_daily_reset) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, now, now, None, now, now)
        )
    else:
        cursor.execute(
            "UPDATE users SET last_message_time=? WHERE user_id=?",
            (now, user_id)
        )
    conn.commit()

def can_send_free_message(user):
    return user is None or (user[3] is not None and user[3] > 0)

def decrement_free_message(user_id):
    cursor.execute("UPDATE users SET free_messages = free_messages - 1 WHERE user_id=?", (user_id,))
    conn.commit()

def has_active_subscription(user):
    if user is None or user[4] is None:
        return False
    now = datetime.datetime.now()
    end_date = datetime.datetime.fromisoformat(user[4])
    return now <= end_date + datetime.timedelta(weeks=2)

def reset_daily_limit_if_needed(user_id, user):
    now = datetime.datetime.now()
    last_reset = datetime.datetime.fromisoformat(user[8]) if user[8] else None
    if last_reset is None or (now - last_reset).days >= 1:
        cursor.execute("UPDATE users SET daily_messages=0, last_daily_reset=? WHERE user_id=?", (now, user_id))
        conn.commit()
        return True
    return False

def increment_daily_messages(user_id):
    cursor.execute("UPDATE users SET daily_messages = daily_messages + 1 WHERE user_id=?", (user_id,))
    conn.commit()

def check_voice_limit(user):
    now = datetime.datetime.now()
    last_reset = user[6]
    if last_reset is None or (now - datetime.datetime.fromisoformat(last_reset)).days >= 1:
        cursor.execute("UPDATE users SET voice_minutes_today=0, last_voice_reset=? WHERE user_id=?", (now, user[0]))
        conn.commit()
    return user[5] < 20

def increment_voice_minutes(user_id, minutes):
    cursor.execute("UPDATE users SET voice_minutes_today = voice_minutes_today + ? WHERE user_id=?", (minutes, user_id))
    conn.commit()

# --- Детекторы режимов ответа ---
def wants_detailed_explicit(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    keys = [
        "подробно", "развёрнуто", "развернуто", "с разбором",
        "глубокий анализ", "проанализируй", "проанализировать",
        "распиши", "пошагово", "с примерами", "подробный разбор"
    ]
    return any(k in t for k in keys)

def wants_detailed_auto(text: str, history: list) -> bool:
    if not text:
        return False
    t = text.lower()
    complex_triggers = [
        "почему", "объясни", "объяснишь", "анал", "разбор",
        "что делать", "как поступить", "как быть",
        "стоит ли", "нужно ли", "правильно ли", "это манипуляция",
        "газлайт", "абьюз", "нарцис"
    ]
    long_enough = len(t) > 600
    many_questions = t.count("?") >= 2
    has_complex = any(k in t for k in complex_triggers)
    recent_user_msgs = [m["content"].lower() for m in history[-6:] if m["role"] == "user"]
    reformulate_signals = any(
        any(k in msg for k in ["не подходит", "иначе", "по-другому", "не то", "распиши"])
        for msg in recent_user_msgs
    )
    return long_enough or many_questions or has_complex or reformulate_signals

def needs_variants(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    keys = [
        "что ответить", "как ответить", "смс", "сообщение",
        "написал", "написала", "переписка", "что сказать",
        "здороваться", "не здороваться", "встреча",
        "позвонить", "не звонить"
    ]
    return any(k in t for k in keys)

# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_or_update_user(update.effective_user.id)
    delete_old_users_data()

    keyboard = [["Начать"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    first_name = update.effective_user.first_name or "друг"
    welcome_text = WELCOME_TEXT_TEMPLATE.format(name=first_name)

    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

# --- Обработка сообщений ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id

        # --- VIP-доступ для себя ---
        if user_id == 1195425593:
            cursor.execute("""
                UPDATE users
                SET subscription_end = ?,
                    free_messages = 999999
                WHERE user_id = ?
            """, (
                (datetime.datetime.now() + datetime.timedelta(days=365)).isoformat(),
                user_id
            ))
            conn.commit()

        add_or_update_user(user_id)
        delete_old_users_data()
        user = get_user(user_id)

        reset_daily_limit_if_needed(user_id, user)

        if not has_active_subscription(user) and not can_send_free_message(user):
            await update.message.reply_text("🔒 Лимит бесплатных сообщений исчерпан. Оформи подписку, чтобы продолжить.")
            return
        elif user[3] > 0 and not has_active_subscription(user):
            decrement_free_message(user_id)

        increment_daily_messages(user_id)

        # Лимит по моделям
        if user[7] >= 100:
            await update.message.reply_text("⏳ Лимит 100 сообщений в день. Пожалуйста, подожди немного.")
            await asyncio.sleep(random.randint(5, 10))
            return
        elif user[7] >= 50:
            model = "gpt-3.5-turbo"
            await asyncio.sleep(random.randint(3, 5))
        else:
            model = "gpt-4o-mini"

        user_text = update.message.text or ""

        # --- Обработка голосовых ---
        if update.message.voice:
            if not check_voice_limit(user):
                await update.message.reply_text("🎙 Лимит голосовых сообщений на сегодня исчерпан. Пиши текстом.")
                return
            increment_voice_minutes(user_id, update.message.voice.duration / 60)

            file = await context.bot.get_file(update.message.voice.file_id)
            file_path = "voice.ogg"
            await file.download_to_drive(file_path)

            with open(file_path, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="gpt-4o-mini-transcribe",
                    file=audio_file
                )
            user_text = transcript.text.strip()

            if not user_text:
                await update.message.reply_text("Кажется, я не расслышал тебя. Попробуй ещё раз сказать или напиши словами.")
                return

        if not user_text.strip():
            await update.message.reply_text("Отправь мне текст или голосовое сообщение.")
            return

        # --- Формируем историю диалога (память: срок подписки + 14 дней) ---
        history = get_conversation_history(user_id)
        messages = [{"role": "system", "content": PSYCHO_PROMPT}] + history + [
            {"role": "user", "content": user_text}
        ]

        # Сохраняем сообщение пользователя
        save_message(user_id, "user", user_text)

        # --- Определяем режим ответа ---
        explicit_detail = wants_detailed_explicit(user_text)
        auto_detail = wants_detailed_auto(user_text, history)
        is_detailed = explicit_detail or auto_detail

        # если явно/авто детально — форсируем умнее модель
        if is_detailed and model == "gpt-3.5-turbo":
            model = "gpt-4o-mini"

        need_variants = needs_variants(user_text)
        max_tokens_for_reply = 1500 if is_detailed else 500

        # --- Локальные системные подсказки поколению ---
        if is_detailed:
            messages.insert(1, {
                "role": "system",
                "content": "Сделай развёрнутый структурированный ответ: что происходит • почему так • риски • что делать сейчас. Дай спокойный тон."
            })
        if need_variants:
            messages.insert(1, {
                "role": "system",
                "content": "В конце ответа предложи 2–3 готовых естественных варианта фраз/сообщений на выбор (без пафоса)."
            })

        # --- Генерация ответа ---
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens_for_reply,
            temperature=0.7 if is_detailed else 0.6
        )

        reply_text = response.choices[0].message.content

        # Сохраняем ответ бота
        save_message(user_id, "assistant", reply_text)

        await update.message.reply_text(reply_text)

        if user_id == ADMIN_ID:
            print(f"[ADMIN LOG] Пользователь {user_id}: {user_text}")

    except Exception as e:
        await update.message.reply_text(f"⚠ Произошла ошибка: {e}")
        if user_id == ADMIN_ID:
            print(f"[ADMIN ERROR] {e}")

# --- Запуск ---
if __name__ == "__main__":
    try:
        print("🚀 Запуск бота...")
        delete_old_users_data()
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
        print("✅ Бот запущен и слушает сообщения...")
        app.run_polling()
    except Exception as e:
        print(f"❌ Ошибка запуска бота: {e}")
