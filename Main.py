import logging
import os
import sqlite3
import hashlib
import secrets
from uuid import uuid4
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)
from dotenv import load_dotenv

# --- Load Environment Variables ---
load_dotenv()

# --- Database Setup ---
conn = sqlite3.connect('users.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id_hash TEXT PRIMARY KEY,
    username TEXT UNIQUE,
    credits INTEGER,
    is_anonymous BOOLEAN
)
''')
conn.commit()

# --- Configuration ---
TOKEN = os.getenv("BOT_TOKEN", "your-bot-token")
CHANNEL_ID = os.getenv("CHANNEL_ID", "your-channel-id")
COST_TEXT = int(os.getenv("COST_TEXT", 3))
COST_MEDIA = int(os.getenv("COST_MEDIA", 5))
INITIAL_CREDITS = int(os.getenv("INITIAL_CREDITS", 100))

# --- Temporary Storage ---
pending_posts = {}
message_collections = {}

# --- Helper Functions ---
def hash_user_id(user_id: int) -> str:
    return hashlib.sha256(str(user_id).encode()).hexdigest()

def generate_unique_username() -> str:
    while True:
        username = f"User{secrets.randbelow(9000) + 1000}"
        cursor.execute('SELECT username FROM users WHERE username = ?', (username,))
        if not cursor.fetchone():
            return username

def get_message(key: str, **kwargs) -> str:
    raw_message = os.getenv(key, key)
    return raw_message.replace(r'\n', '\n').format(**kwargs)

# --- Start Command ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id_hash = hash_user_id(user.id)
    
    cursor.execute('SELECT * FROM users WHERE user_id_hash = ?', (user_id_hash,))
    if not cursor.fetchone():
        username = generate_unique_username()
        cursor.execute('''
            INSERT INTO users VALUES (?, ?, ?, ?)
        ''', (user_id_hash, username, INITIAL_CREDITS, True))
        conn.commit()
        
        await update.message.reply_text(
            get_message("WELCOME_MESSAGE",
                username=username,
                credits=INITIAL_CREDITS
            )
        )
    else:
        await update.message.reply_text(get_message("ALREADY_REGISTERED_MSG"))

# --- Balance Command ---
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_hash = hash_user_id(update.effective_user.id)
    cursor.execute('SELECT credits FROM users WHERE user_id_hash = ?', (user_id_hash,))
    result = cursor.fetchone()
    
    if result:
        await update.message.reply_text(
            get_message("BALANCE_MSG", credits=result[0])
        )
    else:
        await update.message.reply_text(get_message("USER_NOT_REGISTERED_MSG"))

# --- Write Command ---
async def write_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_hash = hash_user_id(update.effective_user.id)
    message_collections[user_id_hash] = []
    await update.message.reply_text(get_message("WRITE_INSTRUCTIONS"))

# --- Handle Single Post Content ---
async def handle_single_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_hash = hash_user_id(update.effective_user.id)
    
    if user_id_hash not in message_collections:
        return
    
    message = update.message
    content_type = 'text'
    file_id = None
    cost = COST_TEXT
    
    if message.photo:
        cost = COST_MEDIA
        content_type = 'photo'
        file_id = message.photo[-1].file_id
    elif message.document:
        cost = COST_MEDIA
        content_type = 'document'
        file_id = message.document.file_id

    message_collections[user_id_hash].append({
        'content': message.text or message.caption,
        'type': content_type,
        'file_id': file_id,
        'cost': cost
    })

    await update.message.reply_text(
        get_message("MESSAGE_ADDED_MSG",
            count=len(message_collections[user_id_hash]),
            current_cost=sum(msg['cost'] for msg in message_collections[user_id_hash])
        )
    )

# --- Done Command ---
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_hash = hash_user_id(update.effective_user.id)
    messages = message_collections.get(user_id_hash, [])
    
    if not messages:
        await update.message.reply_text(get_message("NO_MESSAGES_MSG"))
        return
    
    total_cost = sum(msg['cost'] for msg in messages)
    
    cursor.execute('SELECT credits FROM users WHERE user_id_hash = ?', (user_id_hash,))
    credits = cursor.fetchone()[0]
    
    if credits < total_cost:
        await update.message.reply_text(
            get_message("INSUFFICIENT_CREDITS_MSG",
                cost=total_cost,
                credits=credits
            )
        )
        del message_collections[user_id_hash]
        return
    
    confirmation_id = str(uuid4())
    pending_posts[confirmation_id] = {
        'user_id_hash': user_id_hash,
        'messages': messages,
        'total_cost': total_cost,
        'credits': credits
    }
    
    keyboard = [[
        InlineKeyboardButton(get_message("CONFIRM_BTN"), callback_data=f"confirm_{confirmation_id}"),
        InlineKeyboardButton(get_message("CANCEL_BTN"), callback_data=f"cancel_{confirmation_id}")
    ]]
    
    await update.message.reply_text(
        get_message("BATCH_PREVIEW_TEXT",
            count=len(messages),
            total_cost=total_cost
        ),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# --- Confirmation Handler ---
async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action, confirmation_id = query.data.split('_')
    post_data = pending_posts.get(confirmation_id)
    
    if not post_data:
        await query.edit_message_text(get_message("POST_EXPIRED_MSG"))
        return

    try:
        if action == "confirm":
            conn.execute("BEGIN TRANSACTION")
            
            # Deduct credits
            cursor.execute('''
                UPDATE users 
                SET credits = credits - ? 
                WHERE user_id_hash = ?
            ''', (post_data['total_cost'], post_data['user_id_hash']))
            
            # Get user info
            cursor.execute('''
                SELECT username, is_anonymous 
                FROM users 
                WHERE user_id_hash = ?
            ''', (post_data['user_id_hash'],))
            username, is_anonymous = cursor.fetchone()
            
            # Post messages
            sender = "Anonymous" if is_anonymous else username
            for msg in post_data['messages']:
                content = f"{sender}:\n{msg['content']}"
                if msg['type'] == 'photo':
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=msg['file_id'],
                        caption=content
                    )
                elif msg['type'] == 'document':
                    await context.bot.send_document(
                        chat_id=CHANNEL_ID,
                        document=msg['file_id'],
                        caption=content
                    )
                else:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=content
                    )
            
            conn.commit()
            await query.edit_message_text(
                get_message("BATCH_POST_SUCCESS_MSG",
                    count=len(post_data['messages']),
                    total_cost=post_data['total_cost']
                )
            )
        else:
            await query.edit_message_text(get_message("POST_CANCELLED_MSG"))
    
    except Exception as e:
        conn.rollback()
        logging.error(f"Error: {str(e)}")
        await query.edit_message_text(get_message("POST_ERROR_MSG"))
    
    finally:
        if confirmation_id in pending_posts:
            del pending_posts[confirmation_id]

# --- Help Command ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_message("HELP_MESSAGE"))

# --- Main Application ---
def main():
    application = Application.builder().token(TOKEN).build()
    
    handlers = [
        CommandHandler("start", start),
        CommandHandler("balance", balance),
        CommandHandler("write", write_command),
        CommandHandler("done", done_command),
        CommandHandler("help", help_command),
        MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL, handle_single_content),
        CallbackQueryHandler(handle_confirmation)
    ]
    
    for handler in handlers:
        application.add_handler(handler)

    application.run_polling()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
