import os
import sqlite3
import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import openai

# --- Загрузка ключей из .env ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID'))

openai.api_key = OPENAI_API_KEY

# --- Настройка базы данных ---
DB_PATH = "bot_memory.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_message_time TIMESTAMP,
    last_message_time TIMESTAMP,
    free_messages INTEGER DEFAULT 10,
    subscription_end TIMESTAMP,
    voice_minutes_today INTEGER DEFAULT 0,
    last_voice_reset TIMESTAMP
)
''')
conn.commit()

# --- Психологический промпт ---
PSYCHO_PROMPT = """
Ты - чуткий психолог-консультант проекта «Я больше не жду».
Твоя задача - присутствовать рядом, слышать боль и давать опоры.
Стиль: тёплый, человеческий, без клише и канцелярита. Короткие абзацы.
Не торопишься с советами; сначала отсвечиваешь чувства клиента, называешь их.
Избегай шаблонов вроде «всё наладится», «просто отпусти».
Не ставь диагнозы и не спорь с опытом клиента.
Говори простым языком; допускаются тихие метафоры, но по делу.
Всегда помогай сузить следующий шаг: 1-2 мягких вопроса или мини-пр.
Если текст клиента по-английски - отвечай по-английски в таком же тоне.
Если есть риски самоповреждения/суицида - мягко обозначь важность обращения к местным службам поддержки.
Формат ответа: 
1) короткое отражение чувств (1-2 предложения)
2) бережная мысль/переспрашивание
3) один небольшой следующий шаг
"""

# --- Вспомогательные функции ---
def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return cursor.fetchone()

def add_or_update_user(user_id):
    user = get_user(user_id)
    now = datetime.datetime.now()
    if user is None:
        cursor.execute(
            "INSERT INTO users (user_id, first_message_time, last_message_time, subscription_end, last_voice_reset) VALUES (?, ?, ?, ?, ?)",
            (user_id, now, now, None, now)
        )
    else:
        cursor.execute(
            "UPDATE users SET last_message_time=? WHERE user_id=?",
            (now, user_id)
        )
    conn.commit()

def can_send_free_message(user):
    if user is None:
        return True
    return user[3] > 0  # free_messages

def decrement_free_message(user_id):
    cursor.execute("UPDATE users SET free_messages = free_messages - 1 WHERE user_id=?", (user_id,))
    conn.commit()

def check_voice_limit(user):
    if user is None:
        return True
    now = datetime.datetime.now()
    last_reset = user[6]
    if last_reset is None or (now - datetime.datetime.fromisoformat(last_reset)).days >= 1:
        cursor.execute("UPDATE users SET voice_minutes_today=0, last_voice_reset=? WHERE user_id=?", (now, user[0]))
        conn.commit()
    return user[5] < 20  # минуты в день

def increment_voice_minutes(user_id, minutes):
    cursor.execute("UPDATE users SET voice_minutes_today = voice_minutes_today + ? WHERE user_id=?", (minutes, user_id))
    conn.commit()

def has_active_subscription(user):
    if user is None or user[4] is None:
        return False
    return datetime.datetime.now() <= datetime.datetime.fromisoformat(user[4]) + datetime.timedelta(weeks=2)

# --- Обработка сообщений ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_or_update_user(update.effective_user.id)
    await update.message.reply_text("Привет! Я рядом, чтобы выслушать тебя. Начни писать свои мысли, и я отвечу.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_or_update_user(user_id)
    user = get_user(user_id)

    # Проверка лимита текстовых сообщений
    if not has_active_subscription(user) and not can_send_free_message(user):
        await update.message.reply_text("Для продолжения общения оформи подписку, пожалуйста.")
        return
    elif user[3] > 0:
        decrement_free_message(user_id)

    # Проверка голосового сообщения
    if update.message.voice:
        if not check_voice_limit(user):
            await update.message.reply_text("Сейчас я не могу распознать твои голосовые сообщения. Пиши текстом, пожалуйста.")
            return
        increment_voice_minutes(user_id, update.message.voice.duration / 60)
        await update.message.reply_text("Я получил голосовое сообщение и прочитал его, отвечаю текстом.")

    # Отправка запроса в OpenAI
    prompt = PSYCHO_PROMPT + "\nКлиент: " + (update.message.text or "")
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": prompt}],
        max_tokens=500
    )
    reply_text = response['choices'][0]['message']['content']
    await update.message.reply_text(reply_text)

# --- Запуск бота ---
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))

print("Бот запущен...")
app.run_polling()
