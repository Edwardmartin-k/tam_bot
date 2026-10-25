import telebot
import os
import schedule
import time
import threading
import secrets
import requests
from pymongo import MongoClient
from keep_alive import keep_alive

# --- Configuration ---
API_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_USER_ID_STR = os.environ.get('ADMIN_USER_ID')
MONGO_URI = os.environ.get('MONGO_URI')
VPLINK_API_KEY = os.environ.get('VPLINK_API_KEY')

# Check secrets
if not all([API_TOKEN, ADMIN_USER_ID_STR, MONGO_URI]):
    raise ValueError("FATAL ERROR: TELEGRAM_BOT_TOKEN, ADMIN_USER_ID, and MONGO_URI must be set in Render's Environment Variables.")

try:
    ADMIN_USER_ID = int(ADMIN_USER_ID_STR)
except ValueError:
    raise ValueError("FATAL ERROR: ADMIN_USER_ID must be a valid integer.")

# --- Database Setup ---
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["movie_delivery"]
files_col = db["files"]

bot = telebot.TeleBot(API_TOKEN)
BOT_INFO = bot.get_me()
BOT_USERNAME = BOT_INFO.username

# Track users who received a file
user_usage = {}


# --- Helper: URL Shortener Function ---

def get_short_url(long_url):
    """Calls VPLINK API to shorten the link."""
    if not VPLINK_API_KEY:
        return long_url

    endpoint = "https://vplink.in/api"
    params = {
        "api": VPLINK_API_KEY,
        "url": long_url
    }
    try:
        response = requests.get(endpoint, params=params, timeout=10)
        data = response.json()
        # VPLINK returns {"status": "success", "shortenedUrl": "https://vplink.in/..."}
        if data.get("status") == "success" and "shortenedUrl" in data:
            return data["shortenedUrl"]
        elif "shortenedUrl" in data:
            return data["shortenedUrl"]
    except Exception as e:
        print(f"Error while calling VPLINK API: {e}")
    
    return long_url


# --- Auto-Delete and Daily Reset Schedulers ---

def schedule_message_deletion(chat_id, message_id):
    """Waits 10 minutes and then deletes the specified message."""
    time.sleep(600)  # 10 minutes = 600 seconds
    try:
        bot.delete_message(chat_id, message_id)
        print(f"Successfully deleted message {message_id} from chat {chat_id}.")
    except Exception as e:
        print(f"Could not delete message {message_id} from chat {chat_id}: {e}")

def reset_user_sessions():
    """Resets the user usage dictionary daily."""
    global user_usage
    print("--- SCHEDULER: Resetting all user sessions... ---")
    user_usage = {}
    print("--- SESSIONS CLEARED ---")

schedule.every().day.at("00:00", "UTC").do(reset_user_sessions)

def run_scheduler():
    """Runs the daily reset scheduler in a background thread."""
    while True:
        schedule.run_pending()
        time.sleep(1)


# --- Bot Command Handlers ---

@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    args = message.text.split()

    if len(args) == 1:
        bot.reply_to(message, "👋 Welcome! Please use a special link from one of our channels to get a file.")
        return

    file_key = args[1]

    # Search directly from MongoDB
    file_record = files_col.find_one({"file_key": file_key})

    if not file_record:
        bot.reply_to(message, "❌ Invalid or expired file link.")
        return

    if user_id in user_usage:
        bot.reply_to(message, "❌ You have already claimed your one file for this session.")
        return

    send_files_and_finalize(message, file_record)


def send_files_and_finalize(message, file_record):
    """Sends one or more files, adds warnings, and schedules deletion for each."""
    user_id = message.from_user.id
    chat_id = message.chat.id

    if user_id in user_usage:
        return

    try:
        file_id_list = file_record.get('file_ids', [])
        if not file_id_list:
            raise ValueError("No file_ids found for this record.")

        bot.send_message(chat_id, f"📂 Preparing your request... You will receive {len(file_id_list)} file(s).")
        time.sleep(2)

        for i, file_id in enumerate(file_id_list):
            caption_text = (
                f"📎 File {i+1} of {len(file_id_list)}\n\n"
                f"⚠️ **Note:** This File/Video will be deleted in 10 mins ❌ (Due to Copyright Issues).\n\n"
                f"Please forward this to your **Saved Messages** and start your download there."
            )

            sent_message = bot.send_document(chat_id, file_id, caption=caption_text, parse_mode="Markdown")

            # Schedule Deletion
            deletion_thread = threading.Thread(target=schedule_message_deletion, args=(chat_id, sent_message.message_id))
            deletion_thread.start()
            time.sleep(1)

        user_usage[user_id] = True
        bot.send_message(chat_id, "✅ All files have been sent. Access is now complete.")

    except Exception as e:
        print(f"Error in send_files_and_finalize: {e}")
        bot.send_message(chat_id, "❌ An error occurred while sending the files.")


@bot.message_handler(content_types=['document', 'video', 'audio'])
def handle_admin_upload(message):
    """Admin-only: Stores file in MongoDB, calls VPLINK API, and returns ready post."""
    if message.from_user.id != ADMIN_USER_ID or message.chat.type != 'private':
        return

    file_id = ""
    if message.document:
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.audio:
        file_id = message.audio.file_id

    if not file_id:
        return

    # Generate random key
    file_key = secrets.token_urlsafe(5)

    # Title from caption or fallback
    title = message.caption if message.caption else "Movie / Series Download"

    # Save to MongoDB
    files_col.insert_one({
        "file_key": file_key,
        "title": title,
        "file_ids": [file_id],
        "created_at": time.time()
    })

    # Generate Telegram deep link
    deeplink = f"https://t.me/{BOT_USERNAME}?start={file_key}"

    # Auto-shorten with VPLINK
    short_url = get_short_url(deeplink)

    # Formatted post template ready to copy or forward to channels
    post_template = (
        f"🎬 **{title}**\n\n"
        f"⚡ **Direct Download Link:**\n"
        f"{short_url}\n\n"
        f"🍿 _Files auto-delete after 10 mins. Forward to Saved Messages!_"
    )

    reply_text = (
        f"✅ **Saved & Shortened!**\n\n"
        f"🔑 **Key:** `{file_key}`\n"
        f"🔗 **Short URL:** `{short_url}`\n\n"
        f"📋 **Ready Channel Post (Forward / Copy):**\n"
        f"───────────────\n"
        f"{post_template}"
    )
    bot.reply_to(message, reply_text, parse_mode="Markdown")


# --- Main Bot Execution ---
if __name__ == "__main__":
    print("🤖 Bot is starting...")
    keep_alive()
    scheduler_thread = threading.Thread(target=run_scheduler)
    scheduler_thread.daemon = True
    scheduler_thread.start()
    print("✔️ Daily reset scheduler started.")
    print("✔️ Bot is now polling for messages.")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
