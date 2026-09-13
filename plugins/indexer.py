import time
import asyncio
from hydrogram import Client, filters, enums
from hydrogram.errors import FloodWait
from hydrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMINS, INDEX_CHANNELS
from database import save_file, save_files
from utils import temp, get_readable_time

lock = asyncio.Lock()


# ==========================================
# 📡 AUTO LIVE INDEXER  (was channel.py)
# ==========================================

MEDIA_FILTER = filters.document | filters.video | filters.audio

@Client.on_message(filters.chat(INDEX_CHANNELS) & MEDIA_FILTER & filters.incoming)
async def auto_channel_indexer(bot, message):
    """Automatically live-save new files arriving in index channels to the DB"""
    media = message.document or message.video or message.audio
    if not media:
        return
    media.caption = message.caption or ""
    await save_file(media)


# ==========================================
# 🗂️ MANUAL INDEXER — CALLBACK TRIGGER
# ==========================================

@Client.on_callback_query(filters.regex(r'^index'))
async def index_files(bot, query):
    """Callback handler to start or cancel channel indexing tasks."""
    if query.from_user.id not in ADMINS:
        return await query.answer("Admin access required", show_alert=True)

    data_parts = query.data.split("#")
    ident      = data_parts[1]
    chat_id    = data_parts[2]

    try:
        chat = int(chat_id)
    except ValueError:
        chat = chat_id

    if ident == 'yes':
        lst_msg_id = int(data_parts[3])
        skip       = int(data_parts[4])
        msg        = query.message
        await msg.edit("<b>Channel indexing is starting... ⏱️</b>")
        await index_files_to_db(lst_msg_id, chat, msg, bot, skip)

    elif ident == 'cancel':
        temp.INDEX_CANCEL.add(str(chat))
        await query.message.edit("<b>Progress: Canceling the indexing task... 🛑</b>")


# ==========================================
# 📨 MANUAL INDEXER — FORWARD / LINK TRIGGER
# ==========================================

@Client.on_message(
    (filters.forwarded | filters.text)
    & filters.private
    & filters.incoming
    & filters.user(ADMINS)
)
async def send_for_index(bot, message):
    """Triggers indexing from a forwarded message or a valid telegram link"""
    if lock.locked():
        return await message.reply('<b>Please wait until the previous indexing task is completed! ❌</b>')

    msg = message
    if msg.text and msg.text.startswith(("https://t.me/", "http://t.me/", "https://telegram.me/")):
        try:
            msg_link = [part for part in msg.text.split("/") if part]
            last_msg_id = int(msg_link[-1].split("?", 1)[0])
            chat_id = msg_link[-2]
            if chat_id.isnumeric():
                chat_id = int("-100" + chat_id)
        except (IndexError, TypeError, ValueError):
            return await message.reply('<b>Invalid message link! ❌</b>')
    elif msg.forward_from_chat and msg.forward_from_chat.type == enums.ChatType.CHANNEL:
        last_msg_id = msg.forward_from_message_id
        chat_id     = msg.forward_from_chat.username or msg.forward_from_chat.id
    else:
        return await message.reply('<b>This is not a forwarded message or a valid channel link! 📂</b>')

    try:
        chat = await bot.get_chat(chat_id)
    except Exception as e:
        return await message.reply(f'<b>Error: {e}</b>')

    if chat.type != enums.ChatType.CHANNEL:
        return await message.reply("<b>I can only index Telegram channels! 📣</b>")

    s = await message.reply("<b>How many messages do you want to skip? (Send a number, e.g., 0):</b>")
    try:
        input_msg = await bot.listen(chat_id=message.chat.id, user_id=message.from_user.id, timeout=300)
        skip = int(input_msg.text)
        await s.delete()
    except asyncio.TimeoutError:
        await s.delete()
        return await message.reply("<b>Timeout! Please try again. ⏱️</b>")
    except ValueError:
        await s.delete()
        return await message.reply("<b>Invalid number! Indexing canceled. ❌</b>")

    buttons = [
        [InlineKeyboardButton('✅ Yes, Start', callback_data=f'index#yes#{chat_id}#{last_msg_id}#{skip}')],
        [InlineKeyboardButton('❌ Close',      callback_data='close_data')]
    ]
    await message.reply(
        f'<b>Do you want to index the channel <u>{chat.title}</u>?\n'
        f'📊 Total Messages: <code>{last_msg_id}</code>\n'
        f'⏩ Skip Count: <code>{skip}</code></b>',
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ==========================================
# ⚙️ CORE INDEXING LOOP
# ==========================================

async def index_files_to_db(lst_msg_id, chat, msg, bot, skip):
    """Core loop — parse and save files to MongoDB without lag"""
    start_time  = time.time()
    total_files = duplicate = errors = deleted = no_media = unsupported = 0
    current = skip
    batch = []

    async def flush_batch():
        nonlocal total_files, duplicate, errors
        if not batch:
            return
        result = await save_files(batch)
        total_files += result["suc"]
        duplicate += result["dup"]
        errors += result["err"]
        batch.clear()

    async with lock:
        try:
            async for message in bot.iter_messages(chat, lst_msg_id, skip):
                time_taken = get_readable_time(time.time() - start_time)

                # Cancel check
                if str(chat) in temp.INDEX_CANCEL:
                    temp.INDEX_CANCEL.remove(str(chat))
                    await flush_batch()
                    await msg.edit_text(
                        f"<b>🛑 Indexing Task Cancelled Successfully!</b>\n\n"
                        f"⏳ Time Taken: {time_taken}\n"
                        f"📂 Total Saved Files: <code>{total_files}</code>\n"
                        f"♻️ Duplicates Skipped: <code>{duplicate}</code>\n"
                        f"🗑️ Deleted Skipped: <code>{deleted}</code>\n"
                        f"❌ Non-Media Skipped: <code>{no_media + unsupported}</code>\n"
                        f"⚠️ Errors: <code>{errors}</code>"
                    )
                    return

                current += 1

                # Progress update every 30 messages
                if current % 30 == 0:
                    await flush_batch()
                    btn = [[InlineKeyboardButton(
                        '🛑 STOP INDEXING (CANCEL)',
                        callback_data=f'index#cancel#{chat}#{lst_msg_id}#{skip}'
                    )]]
                    try:
                        await msg.edit_text(
                            text=f"<b>📊 Indexing Progress Report:</b>\n\n"
                                 f"🔹 Total Processed: <code>{current}</code>\n"
                                 f"📥 Saved Files: <code>{total_files}</code>\n"
                                 f"♻️ Duplicates: <code>{duplicate}</code>\n"
                                 f"🗑️ Deleted: <code>{deleted}</code>\n"
                                 f"⚠️ Errors: <code>{errors}</code>",
                            reply_markup=InlineKeyboardMarkup(btn)
                        )
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception:
                        pass

                # Media type checks
                if message.empty:
                    deleted += 1
                    continue
                elif not message.media:
                    no_media += 1
                    continue
                elif message.media not in [
                    enums.MessageMediaType.VIDEO,
                    enums.MessageMediaType.DOCUMENT,
                    enums.MessageMediaType.AUDIO,
                ]:
                    unsupported += 1
                    continue

                media = getattr(message, message.media.value, None)
                if not media:
                    unsupported += 1
                    continue

                media.caption = message.caption or ""
                batch.append(media)
                if len(batch) >= 50:
                    await flush_batch()

            await flush_batch()

        except Exception as e:
            await msg.reply(f'<b>❌ Index task interrupted:</b>\n<code>{e}</code>')
        else:
            time_taken = get_readable_time(time.time() - start_time)
            await msg.edit_text(
                f"<b>✅ Channel Indexed Successfully!</b>\n\n"
                f"⏳ Total Time Taken: {time_taken}\n"
                f"📥 Files Saved: <code>{total_files}</code>\n"
                f"♻️ Duplicates Skipped: <code>{duplicate}</code>\n"
                f"🗑️ Deleted Skipped: <code>{deleted}</code>\n"
                f"❌ Non-Media Skipped: <code>{no_media + unsupported}</code>\n"
                f"⚠️ Total Errors: <code>{errors}</code>"
            )
