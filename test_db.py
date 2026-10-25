import os
from pymongo import MongoClient

# Grab connection string from environment variable (or paste string here for a local test)
MONGO_URI = os.environ.get("MONGO_URI", mongodb+srv://bot_martin:AllenEdward77@martin0.legrdsu.mongodb.net/)

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client["telegram_media"]      # Database name
files_col = db["movies"]           # Collection (table) name

# 1. Test Insert
test_movie = {
    "slug": "test123",
    "title": "Sample Movie Name",
    "file_id": "BAACAgUAAxkBAAICvWb...",
    "category": "Hindi"
}

files_col.insert_one(test_movie)
print("✅ Inserted test document successfully!")

# 2. Test Read
found_movie = files_col.find_one({"slug": "test123"})
print(f"✅ Found movie in database: {found_movie['title']} (File ID: {found_movie['file_id']})")
