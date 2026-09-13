"""
🚫 BLACKLIST FEATURE (self-contained module)
=============================================
Admin koi word/link pattern blacklist mein add karta hai (wildcard '*' support
ke saath, jaise https://www.instagram.com/*) + ek auto-delete time.
Uske baad **groups** mein koi bhi admin wahi pattern bheje, message set kiye
gaye time par khud-ba-khud (silently) delete ho jaata hai. (PM mein ye check
nahi hota — sirf groups ke liye hai.)

Restart-safe: jab bhi koi message match hokar delete ke liye schedule hota
hai, uska record ek "PendingDeletions" collection mein bhi save hota hai.
Bot restart hone par ye records RAM mein wapas load ho jaate hain aur bache
hue time ke liye dobara schedule ho jaate hain (agar time already nikal chuka
hai to turant delete kar diya jaata hai). Message delete hote hi uska record
RAM aur DB dono se clean ho jaata hai.

Is file ko project ke usi folder mein rakhein jahan handlers.py hai
(plugins/ folder, agar aapka project usse use karta hai), taaki hydrogram
ke @Client.on_message handlers auto-register ho jaayein.

Public functions jo bahar (bot.py) se use hote hain:
    - load_blacklist_cache()         -> bot startup par ek baar call karein
    - load_pending_deletions(client) -> bot startup par (client ready hone ke baad) call karein
"""

import asyncio
import html
import re
import time

from hydrogram import Client, filters

from config import ADMINS
from database import _db
from utils import get_readable_time, get_seconds

# ==========================================
# 🗄️ DATABASE (apne alag collections)
# ==========================================

blacklist_col = _db["Blacklist"]           # Blacklisted patterns
pending_col   = _db["BlacklistPending"]    # Abhi jo messages delete hone ke liye schedule hain

# In-memory cache: [(pattern_str, compiled_regex, delay_seconds), ...]
# Har message par DB call se bachne ke liye.
_CACHE = []
_PENDING_TASKS = {}
_PENDING_LOCK = asyncio.Lock()


def _wildcard_to_regex(pattern):
    """Wildcard pattern (e.g. https://www.instagram.com/*) ko compiled regex mein convert karo.
    '*' ka matlab hai 'kuch bhi (0 ya usse zyada characters)'."""
    escaped = re.escape(pattern)
    escaped = escaped.replace(r'\*', '.*')
    return re.compile(escaped, re.IGNORECASE)


async def load_blacklist_cache():
    """DB se saare patterns padho aur memory cache refresh karo.
    Bot startup par aur har add/remove ke baad call hota hai."""
    global _CACHE
    docs = await blacklist_col.find({}).to_list(length=None)
    _CACHE = [
        (doc['_id'], _wildcard_to_regex(doc['_id']), doc.get('delay', 60))
        for doc in docs
    ]


async def _add_word(pattern, delay_seconds, admin_id):
    await blacklist_col.update_one(
        {'_id': pattern},
        {'$set': {'delay': int(delay_seconds), 'added_by': int(admin_id)}},
        upsert=True
    )
    await load_blacklist_cache()


async def _remove_word(pattern):
    result = await blacklist_col.delete_one({'_id': pattern})
    await load_blacklist_cache()
    return result.deleted_count > 0


async def _get_all_words():
    return await blacklist_col.find({}).to_list(length=None)


# ==========================================
# ⏳ PENDING DELETIONS (restart-safe scheduling)
# ==========================================

def _pending_key(chat_id, message_id):
    return f"{chat_id}:{message_id}"


async def _schedule_delete(client, chat_id, message_id, delay_seconds, pattern_str):
    """Persist a pending delete and schedule it once in the current process."""
    key = _pending_key(chat_id, message_id)
    async with _PENDING_LOCK:
        if key in _PENDING_TASKS and not _PENDING_TASKS[key].done():
            return

        delay_seconds = max(0, int(delay_seconds))
        delete_at = time.time() + delay_seconds
        await pending_col.update_one(
            {"_id": key},
            {"$set": {
                "chat_id": chat_id,
                "message_id": message_id,
                "delete_at": delete_at,
                "pattern": pattern_str,
            }},
            upsert=True,
        )
        task = asyncio.create_task(
            _wait_and_delete(client, chat_id, message_id, delay_seconds)
        )
        _PENDING_TASKS[key] = task


async def _wait_and_delete(client, chat_id, message_id, delay_seconds):
    key = _pending_key(chat_id, message_id)
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await client.delete_messages(chat_id, message_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"Blacklist auto-delete error ({chat_id}/{message_id}): {error}")
    finally:
        _PENDING_TASKS.pop(key, None)
        # Whether deletion succeeded or the message was already gone, remove
        # the durable queue entry so it cannot be retried forever.
        try:
            await pending_col.delete_one({"_id": key})
        except Exception as error:
            print(f"Blacklist queue cleanup error ({key}): {error}")


async def load_pending_deletions(client):
    """Bot restart hone par DB se pending deletions padho aur dobara schedule/delete karo.
    Bot startup par (client fully ready hone ke baad) ek baar call karein."""
    docs = await pending_col.find({}).to_list(length=None)
    now = time.time()
    for doc in docs:
        try:
            remaining = max(0, float(doc.get("delete_at", now)) - now)
            await _schedule_delete(
                client,
                doc["chat_id"],
                doc["message_id"],
                int(remaining),
                doc.get("pattern", ""),
            )
        except (KeyError, TypeError, ValueError):
            await pending_col.delete_one({"_id": doc.get("_id")})


# ==========================================
# 🚫 /blacklist  COMMAND — add / remove / list
# ==========================================

@Client.on_message(filters.command('blacklist') & filters.incoming)
async def blacklist_cmd(client, message):
    if message.from_user.id not in ADMINS:
        return

    args = message.text.split(None, 3)  # ['/blacklist', 'add'/'remove'/'list', <time>, <pattern>]
    if len(args) < 2:
        return await message.reply_text(
            "<b>Usage:</b>\n"
            "🔹 <code>/blacklist add [time] [word/pattern]</code>\n"
            "   Example: <code>/blacklist add 1m https://www.instagram.com/*</code>\n"
            "🔹 <code>/blacklist remove [word/pattern]</code>\n"
            "🔹 <code>/blacklist list</code>\n\n"
            "<i>Note: Ye sirf groups mein kaam karta hai, PM mein nahi.</i>"
        )

    action = args[1].lower()

    # --- ADD ---
    if action == "add":
        if len(args) < 4:
            return await message.reply_text(
                "<b>Usage:</b> <code>/blacklist add [time] [word/pattern]</code>\n"
                "Example: <code>/blacklist add 1m https://www.instagram.com/*</code>"
            )
        time_str = args[2]
        pattern  = args[3].strip()
        delay_seconds = await get_seconds(time_str)
        if delay_seconds <= 0:
            return await message.reply_text(
                "<b>Invalid time! ❌</b>\nExample valid times: <code>30s</code>, <code>1m</code>, <code>2h</code>"
            )

        await _add_word(pattern, delay_seconds, message.from_user.id)
        await message.reply_text(
            f"<b>✅ Blacklisted!</b>\n\n"
            f"🔹 Pattern: <code>{html.escape(pattern)}</code>\n"
            f"🔹 Auto-delete after: <b>{get_readable_time(delay_seconds)}</b>\n\n"
            f"Ab groups mein koi bhi admin ye bhejega to message {get_readable_time(delay_seconds)} mein auto-delete ho jaayega."
        )

    # --- REMOVE ---
    elif action == "remove":
        if len(args) < 3:
            return await message.reply_text("<b>Usage:</b> <code>/blacklist remove [word/pattern]</code>")
        pattern = message.text.split(None, 2)[2].strip()
        removed = await _remove_word(pattern)
        if removed:
            await message.reply_text(
                f"<b>✅ Removed from blacklist:</b> <code>{html.escape(pattern)}</code>"
            )
        else:
            await message.reply_text(
                f"<b>❌ Pattern not found in blacklist:</b> <code>{html.escape(pattern)}</code>"
            )

    # --- LIST ---
    elif action == "list":
        words = await _get_all_words()
        if not words:
            return await message.reply_text("<b>Blacklist khaali hai! 🗒️</b>")
        text = "<b>🚫 Blacklisted Patterns:</b>\n\n"
        for w in words:
            text += (
                f"🔹 <code>{html.escape(str(w['_id']))}</code> — "
                f"auto-delete in {get_readable_time(w.get('delay', 60))}\n"
            )
        await message.reply_text(text)

    else:
        await message.reply_text(
            "<b>Usage:</b>\n"
            "🔹 <code>/blacklist add [time] [word/pattern]</code>\n"
            "🔹 <code>/blacklist remove [word/pattern]</code>\n"
            "🔹 <code>/blacklist list</code>"
        )


# ==========================================
# 🕵️ AUTO-DELETE CHECKER  (GROUPS ONLY)
# Group ka koi bhi member (admin ho ya na ho) blacklisted word/pattern
# bheje (text ya caption mein), to woh message set kiye gaye time ke baad
# khud-ba-khud (restart-safe) delete ho jaayega.
# group=-1 -> baaki sab handlers (jaise search) se pehle check ho jaata hai.
# ==========================================

@Client.on_message((filters.text | filters.caption) & filters.incoming & filters.group, group=-1)
async def blacklist_checker(client, message):
    # Note: Ye check group ke HAR member ke messages pe lagta hai — sirf bot
    # admins ke nahi. (ADMINS check sirf /blacklist add|remove|list command
    # tak simit hai, kyunki wahi decide karta hai kaunse words block karne hain.)
    content = message.text or message.caption or ""
    if not content or content.startswith("/"):
        return

    for pattern_str, regex, delay_seconds in _CACHE:
        if regex.search(content):
            await _schedule_delete(client, message.chat.id, message.id, delay_seconds, pattern_str)
            break
