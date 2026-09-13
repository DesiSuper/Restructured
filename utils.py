import asyncio
import math
from collections import OrderedDict
from datetime import datetime

import pytz


class temp:
    """State manager for the bot's live session."""

    START_TIME = 0
    ME = None
    CANCEL = False
    U_NAME = None
    B_NAME = None
    FILES = {}
    BOT = None
    INDEX_CANCEL = set()


# Sending the same Telegram file to BIN_CHANNEL for every click is slow and
# creates a large number of unnecessary messages.  Keep a small process-local
# cache of the last generated BIN messages.  The message is still validated by
# get_bin_message before it is reused, so deleting a cached message is safe.
_BIN_MESSAGE_CACHE = OrderedDict()
_BIN_CACHE_LIMIT = 2048
_BIN_MESSAGE_LOCK = asyncio.Lock()


async def get_bin_message(client, bin_channel, file_id):
    """Return a reusable BIN_CHANNEL message for *file_id*.

    This also serializes cache misses.  Without the lock, two simultaneous
    clicks for a new file both upload a copy to Telegram.
    """
    cache_key = str(file_id)
    async with _BIN_MESSAGE_LOCK:
        message_id = _BIN_MESSAGE_CACHE.get(cache_key)
        if message_id is not None:
            try:
                message = await client.get_messages(bin_channel, message_id)
            except Exception:
                message = None
            if message and not message.empty:
                _BIN_MESSAGE_CACHE.move_to_end(cache_key)
                return message
            _BIN_MESSAGE_CACHE.pop(cache_key, None)

        message = await client.send_cached_media(chat_id=bin_channel, file_id=file_id)
        _BIN_MESSAGE_CACHE[cache_key] = message.id
        _BIN_MESSAGE_CACHE.move_to_end(cache_key)
        while len(_BIN_MESSAGE_CACHE) > _BIN_CACHE_LIMIT:
            _BIN_MESSAGE_CACHE.popitem(last=False)
        return message


def get_size(size):
    """Convert a byte count into a compact, human-readable size."""
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        value = 0.0

    if not math.isfinite(value) or value < 0:
        value = 0.0

    units = ("Bytes", "KB", "MB", "GB", "TB")
    unit = 0
    while value >= 1024.0 and unit < len(units) - 1:
        value /= 1024.0
        unit += 1
    return f"{value:.2f} {units[unit]}"


def get_readable_time(seconds):
    """Make the bot's live uptime human-readable."""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        seconds = 0

    periods = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
    result = []
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result.append(f"{period_value}{period_name}")
    return "".join(result) or "0s"


def get_wish():
    """Greet the admin based on the time of day in India."""
    tz = pytz.timezone("Asia/Kolkata")
    hour = datetime.now(tz).hour
    if hour < 12:
        return "ɢᴏᴏᴅ ᴍᴏʀɴɪɴɢ 🌞"
    if hour < 18:
        return "ɢᴏᴏᴅ ᴀꜰᴛᴇʀɴᴏᴏɴ 🌗"
    return "ɢᴏᴏᴅ ᴇᴠᴇɴɪɴɢ 🌘"


async def get_seconds(time_string):
    """Convert a time string such as ``5m`` or ``1h`` into seconds."""
    if not isinstance(time_string, str):
        return 0

    value = ""
    index = 0
    text = time_string.strip().lower()
    while index < len(text) and text[index].isdigit():
        value += text[index]
        index += 1

    if not value:
        return 0
    try:
        value = int(value)
    except ValueError:
        return 0
    if value <= 0:
        return 0

    unit = text[index:].strip()
    multipliers = {
        "s": 1, "sec": 1, "secs": 1,
        "min": 60, "mins": 60, "m": 60,
        "hour": 3600, "hours": 3600, "h": 3600,
        "day": 86400, "days": 86400, "d": 86400,
        "month": 86400 * 30, "months": 86400 * 30,
        "year": 86400 * 365, "years": 86400 * 365,
    }
    return value * multipliers.get(unit, 0)
