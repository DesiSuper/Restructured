import re
from os import environ


# ==========================================
# ⚙️ HELPER FUNCTIONS
# ==========================================

def is_enabled(type, value):
    data = environ.get(type, str(value))
    if data.lower() in ["true", "yes", "1", "enable", "y"]:
        return True
    elif data.lower() in ["false", "no", "0", "disable", "n"]:
        return False
    else:
        print(f'Error - {type} is invalid, exiting now')
        exit()

def is_valid_ip(ip):
    ip_pattern = r'\b(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b'
    return re.match(ip_pattern, ip) is not None


# ==========================================
# 🤖 BOT CREDENTIALS
# ==========================================

API_ID = environ.get('API_ID', '')
if len(API_ID) == 0:
    print('Error - API_ID is missing, exiting now')
    exit()
else:
    API_ID = int(API_ID)

API_HASH = environ.get('API_HASH', '')
if len(API_HASH) == 0:
    print('Error - API_HASH is missing, exiting now')
    exit()

BOT_TOKEN = environ.get('BOT_TOKEN', '')
if len(BOT_TOKEN) == 0:
    print('Error - BOT_TOKEN is missing, exiting now')
    exit()

try:
    PORT = int(environ.get("PORT", "80"))
    if not 1 <= PORT <= 65535:
        raise ValueError
except ValueError:
    print("Error - PORT must be between 1 and 65535, exiting now")
    exit()

PICS = (environ.get("PICS") or "https://telegra.ph/file/58fef5cb458d5b29b0186.jpg").split()


# ==========================================
# 👮 AUTHORIZED ADMINS
# ==========================================

ADMINS = environ.get('ADMINS', '')
if len(ADMINS) == 0:
    print('Error - ADMINS is missing, exiting now')
    exit()
else:
    ADMINS = [int(x) for x in ADMINS.split()]


# ==========================================
# 📂 INDEX CHANNELS & LOG CHANNEL
# ==========================================

INDEX_CHANNELS = [
    int(value) if re.fullmatch(r"-?\d+", value) else value
    for value in environ.get("INDEX_CHANNELS", "").split()
]

LOG_CHANNEL = environ.get('LOG_CHANNEL', '')
if len(LOG_CHANNEL) == 0:
    print('Error - LOG_CHANNEL is missing, exiting now')
    exit()
else:
    LOG_CHANNEL = int(LOG_CHANNEL)


# ==========================================
# 🗄️ MONGODB CONFIGURATION
# ==========================================

DATABASE_URL = environ.get('DATABASE_URL', "")
if len(DATABASE_URL) == 0:
    print('Error - DATABASE_URL is missing, exiting now')
    exit()

DATABASE_NAME = environ.get('DATABASE_NAME', "Cluster0")
COLLECTION_NAME = environ.get('COLLECTION_NAME', 'Files')


# ==========================================
# 🌐 STREAMING ENGINE SETTINGS
# ==========================================

IS_STREAM = is_enabled('IS_STREAM', True)

BIN_CHANNEL = environ.get("BIN_CHANNEL", "")
if len(BIN_CHANNEL) == 0:
    print('Error - BIN_CHANNEL is missing, exiting now')
    exit()
else:
    BIN_CHANNEL = int(BIN_CHANNEL)

URL = environ.get("URL", "")
if len(URL) == 0:
    print('Error - URL is missing, exiting now')
    exit()
else:
    if URL.startswith(('https://', 'http://')):
        if not URL.endswith("/"):
            URL += '/'
    elif is_valid_ip(URL):
        URL = f'http://{URL}/'
    else:
        print('Error - URL is not valid, exiting now')
        exit()


# ==========================================
# 🔧 MISC SETTINGS
# ==========================================

FILE_CAPTION = environ.get("FILE_CAPTION", "<b>{file_name}</b>")
try:
    MAX_BTN = max(1, min(50, int(environ.get("MAX_BTN", "12"))))
    CACHE_TIME = max(0, int(environ.get("CACHE_TIME", "300")))
except ValueError:
    print("Error - MAX_BTN and CACHE_TIME must be valid integers, exiting now")
    exit()
IS_PM_SEARCH = is_enabled('IS_PM_SEARCH', True)
PROTECT_CONTENT = is_enabled('PROTECT_CONTENT', False)
REACTIONS = ["🤝", "😇", "🤗", "😍", "👍", "⚡️", "😎", "🔥"]


# ==========================================
# 🔐 WEB PANEL LOGIN (Admin Only)
# ==========================================

WEB_USERNAME = environ.get("WEB_USERNAME", "")
if len(WEB_USERNAME) == 0:
    print('Error - WEB_USERNAME is missing, exiting now')
    exit()

WEB_PASSWORD = environ.get("WEB_PASSWORD", "")
if len(WEB_PASSWORD) == 0:
    print('Error - WEB_PASSWORD is missing, exiting now')
    exit()

SECRET_KEY = environ.get("SECRET_KEY", "")
if len(SECRET_KEY) == 0:
    print('Error - SECRET_KEY is missing, exiting now')
    exit()


# ==========================================
# 📝 MESSAGE TEMPLATES  (was Script.py)
# ==========================================

class script:

    START_TXT = """<b>Hey {}, <i>{}</i>

I am your personal auto filter bot. Send me the movie or file name to get direct links instantly... ⚡️</b>"""

    STATUS_TXT = """🗃️ <b>Database Status (Bot Stats):</b>

📂 Total Files: <code>{}</code>
🦹 Total Admins: <code>{}</code>
🚀 Used Storage: <code>{}</code>
🗂️ Free Storage: <code>{}</code>
⏰ Uptime: <code>{}</code>"""

    NOT_FILE_TXT = """👋 <b>Hey {},

No file found in the database with the keyword <code>{}</code>! 🥲

👉 Please check the spelling or search again with the correct name.</b>"""

    FILE_CAPTION = FILE_CAPTION

    HELP_TXT = """<b>Note - Click the button below for correct information about the commands. ⚙️</b>"""

    ADMIN_COMMAND_TXT = """<b>🤖 Admin Commands List:</b>

🔹 /start - Check live status of the bot
🔹 /link - Reply to a file/video/audio to get watch & download links
🔹 /search on - Turn ON search (in the current chat — PM or group)
🔹 /search off - Turn OFF search (in the current chat — PM or group)
🔹 /index_channels - Check indexed channels
🔹 /stats - View live status of bot and database
🔹 /delete - Delete files using a specific query
🔹 /delete_all - Delete all indexed files from database
🔹 /ping - Check the bot's response speed
🔹 /id - View your user ID or the ID of a replied message
🔹 /blacklist add [time] [word/pattern] - Blacklist a word/link (auto-delete after set time)
🔹 /blacklist remove [word/pattern] - Remove a word/link from blacklist
🔹 /blacklist list - View all blacklisted words/patterns"""
