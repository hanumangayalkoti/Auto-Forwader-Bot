import os

# ---- Telegram API Credentials ----
API_ID    = int(os.getenv("API_ID",   "0"))
API_HASH  = os.getenv("API_HASH",     "")
BOT_TOKEN = os.getenv("BOT_TOKEN",    "")
OWNER_ID  = int(os.getenv("OWNER_ID", "0"))

# ---- Bot Settings ----
MAX_GROUPS    = 5
AFFILIATE_TAG = os.getenv("AFFILIATE_TAG", "dealskoti-21")

# ---- Storage ----
# Telegram Saved Messages mein is tag se data save hoga
STORAGE_TAG   = "#DEALSKOTI_GROUPS_DATA"
# Backup ke liye local JSON file (Heroku pe temp hai, Telegram primary hai)
LOCAL_BACKUP  = "groups_data.json"
