import telebot
import os
import schedule
import time
import threading
import secrets
import requests
from pymongo import MongoClient
from keep_alive import keep_alive

# --- Environment Variables ---
ADMIN_USER_ID_STR = os.environ.get('ADMIN_USER_ID')
MONGO_URI = os.environ.get('MONGO_URI')
VPLINK_API_KEY = os.environ.get('VPLINK_API_KEY')

BOT_TOKENS = [
    os.environ.get('BOT_TOKEN_1'),
    os.environ.get('BOT_TOKEN_2'),
    os.environ.get('BOT_TOKEN_3')
]

# Ensure critical secrets are set
valid_tokens = [tok for tok in BOT_TOKENS if tok]
if not all([ADMIN_USER_ID_STR, MONGO_URI, valid_tokens]):
    raise ValueError("FATAL ERROR: ADMIN_USER_ID, MONGO_URI, and at least one BOT_TOKEN must be set.")

try:
    ADMIN_USER_ID = int(ADMIN_USER_ID_STR)
except ValueError:
    raise ValueError("FATAL ERROR: ADMIN_USER_ID must be a valid integer.")

# --- Database Setup ---
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["movie_delivery"]
files_col = db["files"]

# Track daily user claims and ongoing admin batch sessions
user_usage = {}
admin_batches = {}  # { admin_id: { "title": "...", "file_ids": [] } }


# --- Helper: URL Shortener Function ---

def get_short_url(long_url):
    """Calls VPLINK API to shorten link."""
    if not VPLINK_API_KEY:
        return long_url

    endpoint = "https://vplink.in/api"
    params = {"api": VPLINK_API_KEY, "url": long_url}
    try:
        response = requests.get(endpoint, params=params, timeout=10)
        data = response.json()
        if data.get("status") == "success" and "shortenedUrl" in data:
            return data["shortenedUrl"]
        elif "shortenedUrl" in data:
            return data["shortenedUrl"]
    except Exception as e:
        print(f"Error calling VPLINK API: {e}")
    return long_url


# --- Schedulers & Auto-Deletion ---

def schedule_message_deletion(bot_instance, chat_id, message_id):
    """Waits 10 minutes and deletes the media file."""
    time.sleep(600)  # 10 minutes
    try:
        bot_instance.delete_message(chat_id, message_id)
        print(f"Deleted message {message_id} from {chat_id}")
    except Exception as e:
        print(f"Could not delete message {message_id}: {e}")

def reset_user_sessions():
    """Resets user claim limits daily."""
    global user_usage
    user_usage.clear()
    print("--- User sessions cleared daily ---")

schedule.every().day.at("00:00", "UTC").do(reset_user_sessions)

def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(1)


# --- Attach Logic to Any Bot Instance ---

def setup_bot_handlers(bot):
    bot_info = bot.get_me()
    bot_username = bot_info.username

    # 1. Delivery Handler: /start <file_key>
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        user_id = message.from_user.id
        args = message.text.split()

        if len(args) == 1:
            bot.reply_to(message, "👋 Welcome! Use a download link from our channel to get files.")
            return

        file_key = args[1]
        file_record = files_col.find_one({"file_key": file_key})

        if not file_record:
            bot.reply_to(message, "❌ Invalid or expired file link.")
            return

        if user_id in user_usage:
            bot.reply_to(message, "❌ You have already claimed your file for this session.")
            return

        # Deliver file pack
        file_id_list = file_record.get('file_ids', [])
        if not file_id_list:
            bot.reply_to(message, "❌ File records not found.")
            return

        bot.send_message(message.chat.id, f"📂 Preparing your request... Sending {len(file_id_list)} file(s).")
        time.sleep(2)

        for i, file_id in enumerate(file_id_list):
            caption_text = (
                f"📎 File {i+1} of {len(file_id_list)}\n\n"
                f"⚠️ **Note:** This File/Video will be deleted in 10 mins ❌ (Copyright Protection).\n\n"
                f"Please forward this to your **Saved Messages** to save it permanently!"
            )
            sent_msg = bot.send_document(message.chat.id, file_id, caption=caption_text, parse_mode="Markdown")

            # Start 10-min deletion timer
            del_thread = threading.Thread(target=schedule_message_deletion, args=(bot, message.chat.id, sent_msg.message_id))
            del_thread.start()
            time.sleep(1)

        user_usage[user_id] = True
        bot.send_message(message.chat.id, "✅ All files sent. Access complete.")

    # 2. Batching: Start a series pack
    @bot.message_handler(commands=['newbatch'])
    def start_batch(message):
        if message.from_user.id != ADMIN_USER_ID or message.chat.type != 'private':
            return
        args = message.text.split(maxsplit=1)
        batch_title = args[1] if len(args) > 1 else "Series Pack"
        
        admin_batches[message.from_user.id] = {
            "title": batch_title,
            "file_ids": []
        }
        bot.reply_to(
            message,
            f"📦 **Started Batch:** `{batch_title}`\n\n"
            "Now upload or forward all episodes for this part (e.g., Ep 1, Ep 2, Ep 3).\n"
            "When done, send `/done` to generate the pack link.",
            parse_mode="Markdown"
        )

    # 3. Batching: Finalize pack
    @bot.message_handler(commands=['done'])
    def finalize_batch(message):
        if message.from_user.id != ADMIN_USER_ID or message.chat.type != 'private':
            return
        
        session = admin_batches.get(message.from_user.id)
        if not session or not session["file_ids"]:
            bot.reply_to(message, "❌ No files collected. Upload episodes first or send `/newbatch <title>`.")
            return

        file_key = secrets.token_urlsafe(5)
        title = session["title"]
        file_ids = session["file_ids"]

        # Save multi-file pack into MongoDB
        files_col.insert_one({
            "file_key": file_key,
            "title": title,
            "file_ids": file_ids,
            "created_at": time.time()
        })

        del admin_batches[message.from_user.id]

        deeplink = f"https://t.me/{bot_username}?start={file_key}"
        short_url = get_short_url(deeplink)

        post_template = (
            f"🎬 **{title}**\n\n"
            f"📦 **Bundle:** {len(file_ids)} Episodes\n"
            f"⚡ **Direct Download Link:**\n"
            f"{short_url}\n\n"
            f"🍿 _Files auto-delete after 10 mins. Forward to Saved Messages!_"
        )

        bot.reply_to(
            message,
            f"✅ **Batch Completed ({len(file_ids)} files linked)!**\n\n"
            f"🔑 **Key:** `{file_key}`\n"
            f"🔗 **Short URL:** `{short_url}`\n\n"
            f"📋 **Channel Post:**\n"
            f"───────────────\n"
            f"{post_template}",
            parse_mode="Markdown"
        )

    # 4. Single-Upload Handler (Single Movie or Ongoing Batch item)
    @bot.message_handler(content_types=['document', 'video', 'audio'])
    def handle_upload(message):
        if message.from_user.id != ADMIN_USER_ID or message.chat.type != 'private':
            return

        file_id = ""
        if message.document: file_id = message.document.file_id
        elif message.video: file_id = message.video.file_id
        elif message.audio: file_id = message.audio.file_id

        if not file_id:
            return

        # Case A: If admin is actively uploading to an episode batch
        if message.from_user.id in admin_batches:
            admin_batches[message.from_user.id]["file_ids"].append(file_id)
            count = len(admin_batches[message.from_user.id]["file_ids"])
            bot.reply_to(message, f"➕ Added Episode {count} to batch. Upload next or send `/done`.")
            return

        # Case B: Standard Single Movie Upload
        file_key = secrets.token_urlsafe(5)
        title = message.caption if message.caption else "Movie Download"

        files_col.insert_one({
            "file_key": file_key,
            "title": title,
            "file_ids": [file_id],
            "created_at": time.time()
        })

        deeplink = f"https://t.me/{bot_username}?start={file_key}"
        short_url = get_short_url(deeplink)

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
            f"📋 **Channel Post:**\n"
            f"───────────────\n"
            f"{post_template}"
        )
        bot.reply_to(message, reply_text, parse_mode="Markdown")


# --- Multi-Bot Worker Runner ---

def start_bot_thread(token):
    bot = telebot.TeleBot(token)
    setup_bot_handlers(bot)
    print(f"🤖 Bot @{bot.get_me().username} started polling...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)


if __name__ == "__main__":
    print("🚀 Initializing Master Multi-Bot Service...")
    keep_alive()

    # Background scheduler thread
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Launch each bot concurrently in its own daemon thread
    bot_threads = []
    for token in valid_tokens:
        t = threading.Thread(target=start_bot_thread, args=(token,), daemon=True)
        t.start()
        bot_threads.append(t)

    print(f"✔️ Active bots running: {len(valid_tokens)}")

    # Keep the main process alive
    while True:
        time.sleep(10)
