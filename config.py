import os

# ---- Telegram API Credentials ----
API_ID    = int(os.getenv("API_ID",   "0"))
API_HASH  = os.getenv("API_HASH",     "")
BOT_TOKEN = os.getenv("BOT_TOKEN",    "")
OWNER_ID  = int(os.getenv("OWNER_ID", "0"))

# ---- Database ----
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ---- Bot Settings ----
MAX_GROUPS = 5
