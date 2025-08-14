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
    sub_end = user[4]
    if sub_end:
        sub_end_date = datetime.datetime.fromisoformat(sub_end)
        if now <= sub_end_date + datetime.timedelta(weeks=2):
            cursor.execute("SELECT role, content FROM messages WHERE user_id=? ORDER BY id ASC", (user_id,))
            rows = cursor.fetchall()
            return [{"role": r, "content": c} for r, c in rows]
    return []

# --- Функция автоудаления пользователей с истекшей подпиской ---
def delete_old_users_data():
    now = datetime.datetime.now()
    cursor.execute("SELECT user_id, subscription_end FROM users WHERE subscription_end IS NOT NULL")
    for user_id, sub_end in cursor.fetchall():
        try:
            end_date = datetime.datetime.fromisoformat(sub_end)
            if now > end_date + datetime.timedelta(weeks=2):
                cursor.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
                cursor.execute("DELETE FROM users WHERE user_id=?", (user_id,))
                conn.commit()
                print(f"[AUTO CLEAN] Данные пользователя {user_id} удалены — прошло 2 недели после окончания подписки.")
        except Exception as e:
            print(f"[AUTO CLEAN ERROR] {e}")

# --- Приветствия ---
GREETINGS = [
    "Даже если сейчас тяжело — ты можешь всё мне рассказать. Здесь нет осуждения.",
    "Я здесь, чтобы быть рядом. Можешь выложить всё, что носишь в себе.",
    "Если слова не идут — начни с любого. Я буду слушать и слышать.",
    "Тут можно говорить честно. Можно молчать. Я всё равно останусь рядом.",
    "Иногда достаточно, чтобы кто-то был рядом. Я готова быть этим человеком.",
    "Иногда слова не находятся… Иногда тишина говорит громче. Здесь можно просто быть — и тебя всё равно услышат.",
    "Это твоё личное пространство. Место, где можно выговориться или просто молчать, зная, что я рядом.",
    "Если тяжело подобрать слова — начни с молчания. Я всё равно почувствую тебя.",
    "Тут нет чужих глаз и лишних советов. Есть только мы — и место, где можно быть честной.",
    "Ты можешь рассказать всё, что носишь в себе. Или просто остаться в тишине. Я останусь рядом.",
    "Можешь сложить сюда усталость, боль и даже пустоту. Здесь их примут бережно.",
    "Неважно, сколько времени ты молчала… Я готова слушать сейчас.",
    "Это тихий уголок для честных слов. И даже для тех, что не произносятся.",
    "Иногда достаточно одного взгляда, чтобы понять. Здесь я — тот самый взгляд.",
    "Ты можешь быть здесь настоящей. И в этом нет ничего страшного.",
    "Я рядом, даже если ты не знаешь, с чего начать.",
    "Тут можно оставаться собой без страха и ожиданий.",
    "В этом месте нет правил. Только твоё право говорить или молчать.",
    "Можно просто быть. Без объяснений. И я останусь здесь.",
    "Ты в безопасности, пока мы здесь. Всё остальное подождёт."
]



# --- Психологический промпт ---
PSYCHO_PROMPT = """
Ты — чуткая женщина-психолог, консультант проекта «Я больше не жду».
Отвечаешь всегда от первого лица в женском роде, как тёплая, понимающая подруга.
Твой голос — мягкий, тёплый, с эмпатией.

Ты - чуткий психолог-консультант проекта «Я больше не жду».
Твоя задача - присутствовать рядом, слышать боль и давать опоры.
Стиль: тёплый, человеческий, без клише и канцелярита. Короткие абзацы.
Не торопишься с советами; сначала отражаешь чувства клиента.
Избегай фраз вроде «всё наладится», «просто отпусти».
Не ставь диагнозы и не спорь с опытом клиента.
Говори простым языком; допускаются мягкие метафоры, но по делу.
Всегда помогай обозначить следующий шаг: 1–2 мягких вопроса.
Если текст клиента по-английски — отвечай по-английски.
Если есть риск самоповреждения — мягко советуй обратиться к службам поддержки.
"""


# --- Вспомогательные функции ---
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

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_or_update_user(update.effective_user.id)
    delete_old_users_data()
    keyboard = [["Начать"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(random.choice(GREETINGS), reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
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

        # --- Формируем историю диалога ---
        history = get_conversation_history(user_id)
        messages = [{"role": "system", "content": PSYCHO_PROMPT}] + history + [
            {"role": "user", "content": user_text}
        ]

        # Сохраняем сообщение пользователя
        save_message(user_id, "user", user_text)

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=500
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
