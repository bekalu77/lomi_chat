import os
import telebot
from flask import Flask, request
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from dotenv import load_dotenv
import sqlite3
import logging

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
RENDER_URL = os.getenv("RENDER_URL")  # Your Render service URL (e.g., https://your-service-name.onrender.com)

# Initialize bot and Flask app
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Enable logging
logging.basicConfig(level=logging.DEBUG)

# Database setup
class DatabaseConnection:
    def __enter__(self):
        self.conn = sqlite3.connect("bot_data.db", check_same_thread=False)
        logging.debug("Connected to database.")
        return self.conn.cursor()
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.conn.commit()
        self.conn.close()
        logging.debug("Database connection closed.")

# Initialize database tables
with DatabaseConnection() as cursor:
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        category TEXT,
        last_activity REAL DEFAULT (strftime('%s', 'now'))
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        post_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        category TEXT,
        status TEXT DEFAULT 'pending')
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS media (
        media_id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER,
        file_id TEXT,
        type TEXT)
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS text_content (
        post_id INTEGER PRIMARY KEY,
        content TEXT)
    """)

# Helper functions
def register_user(user_id):
    with DatabaseConnection() as cursor:
        cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    logging.debug(f"Registered user: {user_id}")

# Handlers
@bot.message_handler(commands=['start'])
def start(message):
    logging.debug(f"Received /start command from user: {message.from_user.username}")
    register_user(message.chat.id)
    markup = InlineKeyboardMarkup()
    [markup.add(InlineKeyboardButton(v, callback_data=k)) for k, v in CATEGORIES.items()]
    bot.send_message(message.chat.id, TEXTS["welcome"], reply_markup=markup)
    logging.debug("Sent welcome message to user.")

# Webhook endpoint
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        return "This is a Telegram bot webhook. Send a POST request here.", 200
    elif request.method == 'POST':
        if request.headers.get('content-type') == 'application/json':
            json_string = request.get_data().decode('utf-8')
            logging.debug("Received update: %s", json_string)  # Log incoming updates
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return 'ok', 200
        return 'Unsupported content type', 400

# Set webhook when the app starts
def set_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=f"{RENDER_URL}/webhook")

# Initialize webhook when the app starts
set_webhook()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))  # Use PORT if provided, otherwise default to 10000
    app.run(host='0.0.0.0', port=port)
