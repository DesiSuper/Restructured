import html
import math
import random
import secrets
import time

from hydrogram import Client, enums, filters
from hydrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

from config import (
    ADMINS, INDEX_CHANNELS,
    PICS, REACTIONS, BIN_CHANNEL, URL, MAX_BTN
)
from utils import get_bin_message, get_readable_time, get_size, get_wish, temp
from database import (
    Media,
    db,
    delete_files,
    delete_all_files,
    delete_files_by_query,
    get_file_details,
    get_search_results,
)

# Short-lived process-local callback state keeps Telegram callback_data small
# even when an admin searches for a long filename.
BUTTONS = {}
STREAM_BUTTONS = {}
DELETE_BUTTONS = {}


def build_file_links(files):
    """Render one consistent Telegram link list for search pages."""
    return "".join(
        f"\n\n📁 <a href='https://t.me/{temp.U_NAME}?start=file_{file.file_id}'>"
        f"[{get_size(file.file_size)}] {html.escape(file.file_name)}</a>"
        for file in files
    )


# ==========================================
# 🚀 /start  COMMAND
# ==========================================

@Client.on_message(filters.command("start") & filters.incoming)
async def start(client, message):
    if message.from_user.id not in ADMINS:
        return

    try:
        await message.react(emoji=random.choice(REACTIONS), big=True)
    except Exception:
        await message.react(emoji="⚡️", big=True)

    mc = message.command[1] if len(message.command) == 2 else None

    # Deep link — file delivery
    if mc and (mc.startswith("file") or mc.startswith("all")):
        try:
            _, file_id = mc.split("_", 1)
        except ValueError:
            return await message.reply("Invalid Link! ❌")

        file_details = await get_file_details(file_id)
        if not file_details:
            return await message.reply("File not found in database! 😕")

        file = file_details[0]
        from config import script
        cap = script.FILE_CAPTION.format(file_name=html.escape(file.file_name))
        if len(STREAM_BUTTONS) > 1000:
            STREAM_BUTTONS.clear()
        stream_token = secrets.token_urlsafe(8)
        STREAM_BUTTONS[stream_token] = file.file_id
        btn = [[
            InlineKeyboardButton(
                "🚀 Watch And Download ⚡",
                callback_data=f"stream#{stream_token}",
            )
        ], [
            InlineKeyboardButton("🙅 Close", callback_data="close_data")
        ]]
        await client.send_cached_media(
            chat_id=message.from_user.id,
            file_id=file.file_id,
            caption=cap,
            reply_markup=InlineKeyboardMarkup(btn)
        )
        return

    # Normal /start UI
    from config import script
    buttons = [
        [InlineKeyboardButton("⚙️ Commands List", callback_data="help")]
    ]
    await message.reply_photo(
        photo=random.choice(PICS),
        caption=script.START_TXT.format(message.from_user.mention, get_wish()),
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ==========================================
# 📂 /index_channels  COMMAND
# ==========================================

@Client.on_message(filters.command('index_channels') & filters.incoming)
async def channels_info(bot, message):
    if message.from_user.id not in ADMINS:
        return

    if not INDEX_CHANNELS:
        return await message.reply("INDEX_CHANNELS is not configured! ⚙️")

    text = '<b>📂 Indexed Channels:</b>\n\n'
    for id in INDEX_CHANNELS:
        try:
            chat  = await bot.get_chat(id)
            text += f'🔹 {chat.title} (<code>{id}</code>)\n'
        except Exception:
            text += f'❌ {id} (Channel not found / Bot is not admin)\n'
    text += f'\n<b>📊 Total Channels: {len(INDEX_CHANNELS)}</b>'
    await message.reply(text)


# ==========================================
# 📊 /stats  COMMAND
# ==========================================

@Client.on_message(filters.command('stats') & filters.incoming)
async def stats(bot, message):
    if message.from_user.id not in ADMINS:
        return

    try:
        await message.react(emoji=random.choice(REACTIONS), big=True)
    except Exception:
        await message.react(emoji="⚡️", big=True)

    from config import script
    files = await Media.count_documents()
    admins_count = len(ADMINS)
    uptime = get_readable_time(time.time() - temp.START_TIME)
    database_size = await db.get_db_size()
    u_size = get_size(database_size)
    f_size = get_size(max(0, 536870912 - database_size))

    await message.reply_text(script.STATUS_TXT.format(files, admins_count, u_size, f_size, uptime))


# ==========================================
# 🗑️ /delete  COMMAND
# ==========================================

@Client.on_message(filters.command('delete') & filters.incoming)
async def delete_file(bot, message):
    if message.from_user.id not in ADMINS:
        return

    parts = (message.text or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return await message.reply_text(
            "<b>Command Incomplete!\nUsage: <code>/delete keyword</code></b>"
        )
    query = parts[1].strip()

    msg = await message.reply_text("Searching... ⏱️")
    total, _ = await delete_files(query)

    if int(total) == 0:
        return await msg.edit('No files found in the database with this keyword! ❌')

    if len(DELETE_BUTTONS) > 500:
        DELETE_BUTTONS.clear()
    token = secrets.token_urlsafe(8)
    DELETE_BUTTONS[token] = (message.from_user.id, query)
    btn = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"delete#{token}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="close_data")],
    ]
    await msg.edit(
        f"🔍 Found total <b>{total}</b> files for your query: <code>{html.escape(query)}</code>.\n\n"
        "Are you sure you want to delete them from the database permanently?",
        reply_markup=InlineKeyboardMarkup(btn),
    )


# ==========================================
# 💣 /delete_all  COMMAND
# ==========================================

@Client.on_message(filters.command('delete_all') & filters.incoming)
async def delete_all_index(bot, message):
    if message.from_user.id not in ADMINS:
        return

    files = await Media.count_documents()
    if int(files) == 0:
        return await message.reply_text('Database is already empty! 🗃️')

    btn = [
        [InlineKeyboardButton("⚠️ Yes, Wipe Entire Database", callback_data="delete_all")],
        [InlineKeyboardButton("❌ Cancel",                     callback_data="close_data")]
    ]
    await message.reply_text(
        f'❗ <b>Warning:</b> Total <b>{files}</b> files are saved in the database.\n'
        f'Are you absolutely sure you want to delete the entire database?',
        reply_markup=InlineKeyboardMarkup(btn)
    )


# ==========================================
# ⏱️ /ping  COMMAND
# ==========================================

@Client.on_message(filters.command('ping') & filters.incoming)
async def ping(client, message):
    if message.from_user.id not in ADMINS:
        return

    start_time = time.monotonic()
    msg        = await message.reply("⚡")
    end_time   = time.monotonic()
    await msg.edit(f'<b>⏱️ Response Speed: {round((end_time - start_time) * 1000)} ms</b>')


# ==========================================
# 🔗 /link  COMMAND — reply to a file to get watch/download links
# ==========================================

@Client.on_message(filters.command('link') & filters.incoming)
async def link_command(client, message):
    if message.from_user.id not in ADMINS:
        return

    replied = message.reply_to_message
    if not replied:
        return await message.reply_text("<b>Kisi file/video par reply karke /link command bhejein! 📂</b>")

    media = replied.document or replied.video or replied.audio
    if not media:
        return await message.reply_text("<b>Ye ek valid file, video ya audio nahi hai! ❌</b>")

    sts = await message.reply_text("<b>Generating links... ⏱️</b>")
    msg = await get_bin_message(client, BIN_CHANNEL, media.file_id)

    watch    = f"{URL}watch/{msg.id}"
    download = f"{URL}download/{msg.id}"

    btn = [[
        InlineKeyboardButton("⚡ Watch Online", url=watch),
        InlineKeyboardButton("🚀 Fast Download", url=download)
    ], [
        InlineKeyboardButton("🙅 Close", callback_data="close_data")
    ]]
    await sts.edit(
        "<b>🔗 Links generated successfully!</b>",
        reply_markup=InlineKeyboardMarkup(btn)
    )


# ==========================================
# 🔍 /search on|off  COMMAND — group aur PM ke liye alag-alag
# ==========================================

@Client.on_message(filters.command('search') & filters.incoming)
async def toggle_search(client, message):
    if message.from_user.id not in ADMINS:
        return

    try:
        mode = message.command[1].lower()
    except IndexError:
        return await message.reply_text("<b>Usage: <code>/search on</code> ya <code>/search off</code></b>")

    if mode not in ('on', 'off'):
        return await message.reply_text("<b>Usage: <code>/search on</code> ya <code>/search off</code></b>")

    enable = mode == 'on'

    if message.chat.type == enums.ChatType.PRIVATE:
        await db.update_pm_search_status(temp.ME, enable)
        scope = "PM"
    else:
        await db.update_group_search_status(message.chat.id, enable)
        scope = "Group"

    status = "ON ✅" if enable else "OFF ❌"
    await message.reply_text(f"<b>{scope} search is now {status}</b>")


# ==========================================
# 🆔 /id  COMMAND
# ==========================================

@Client.on_message(filters.command('id') & filters.incoming)
async def showid(client, message):
    if message.from_user.id not in ADMINS:
        return

    if message.reply_to_message:
        reply = message.reply_to_message
        if reply.forward_from_chat:
            return await message.reply_text(
                f"📣 Forwarded Channel/Chat Name: <b>{reply.forward_from_chat.title}</b>\n"
                f"🆔 ID: <code>{reply.forward_from_chat.id}</code>"
            )
        elif reply.from_user:
            return await message.reply_text(
                f"🦹 User: {reply.from_user.mention}\n"
                f"🆔 ID: <code>{reply.from_user.id}</code>"
            )

    await message.reply_text(
        f'<b>🦹 Your Telegram ID: <code>{message.from_user.id}</code>\n'
        f'💬 This Private Chat ID: <code>{message.chat.id}</code></b>'
    )


# ==========================================
# 🔍 PM + GROUP SEARCH  (was pm_filter.py)
# ==========================================

@Client.on_message(filters.text & filters.incoming & ~filters.regex(r"^/"))
async def pm_search(client, message):
    if message.from_user.id not in ADMINS:
        return

    if message.chat.type == enums.ChatType.PRIVATE:
        if not await db.get_pm_search_status(temp.ME):
            return
    elif message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        if not await db.get_group_search_status(message.chat.id):
            return
    else:
        return

    search = message.text.strip()
    files, offset, total_results = await get_search_results(search)

    if not files:
        from config import script
        await message.reply(
            script.NOT_FILE_TXT.format(
                message.from_user.mention, html.escape(search)
            ),
            quote=True
        )
        return

    req = message.from_user.id
    key = f"{message.chat.id}-{message.id}"
    if len(BUTTONS) > 500:
        BUTTONS.clear()
    BUTTONS[key] = search

    files_link = build_file_links(files)

    btn = []
    if offset != "":
        btn.append([
            InlineKeyboardButton(text=f"🗓 1/{math.ceil(int(total_results) / MAX_BTN)}", callback_data="buttons"),
            InlineKeyboardButton(text="NEXT ⏩", callback_data=f"next_{req}_{key}_{offset}")
        ])
    btn.append([InlineKeyboardButton("🙅 Close", callback_data=f"close#{req}")])

    caption = f"<blockquote>🎬 <b>Total {total_results} Files</b> </blockquote>{files_link}"
    await message.reply(
        text=caption,
        reply_markup=InlineKeyboardMarkup(btn),
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML,
        quote=True
    )


# ==========================================
# ⏩ NEXT / BACK PAGINATION
# ==========================================

@Client.on_callback_query(filters.regex(r"^next"))
async def next_page(bot, query):
    ident, req, key, offset = query.data.split("_")
    if int(req) != query.from_user.id:
        return await query.answer("This is not for you! ❌", show_alert=True)

    search = BUTTONS.get(key)
    if not search:
        return await query.answer("Please search again with a new keyword! 🔄", show_alert=True)

    files, n_offset, total = await get_search_results(search, offset=int(offset))
    if not files:
        return

    files_link = build_file_links(files)

    current_page = math.ceil(int(offset) / MAX_BTN) + 1
    total_pages  = math.ceil(total / MAX_BTN)

    p_buttons = []
    if int(offset) > 0:
        p_buttons.append(InlineKeyboardButton("⏪ BACK", callback_data=f"next_{req}_{key}_{max(0, int(offset) - MAX_BTN)}"))
    p_buttons.append(InlineKeyboardButton(f"🗓 {current_page}/{total_pages}", callback_data="buttons"))
    if n_offset != "":
        p_buttons.append(InlineKeyboardButton("NEXT ⏩", callback_data=f"next_{req}_{key}_{n_offset}"))

    btn     = [p_buttons, [InlineKeyboardButton("🙅 Close", callback_data=f"close#{req}")]]
    caption = f"<blockquote>🎬 <b>Total {total} Files found</b> </blockquote>{files_link}"

    await query.message.edit_text(
        text=caption,
        reply_markup=InlineKeyboardMarkup(btn),
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML
    )


# ==========================================
# 🎛️ ALL CALLBACKS  (was cb_handler)
# ==========================================

@Client.on_callback_query(~filters.regex(r"^(index|next)"))
async def cb_handler(client: Client, query: CallbackQuery):
    data = query.data or ""
    user_id = query.from_user.id

    # All bot functions are admin-only.  Checking callbacks as well as
    # commands prevents a copied callback payload from being reused by a
    # different Telegram account.
    if user_id not in ADMINS:
        return await query.answer("Admin access required", show_alert=True)

    # --- Stream / Download links ---
    if data.startswith("stream#"):
        stream_token = data.split("#", 1)[1]
        file_id = STREAM_BUTTONS.get(stream_token)
        if not file_id:
            return await query.answer("This link expired, please search again", show_alert=True)
        await query.answer("Generating streaming links... ⏱️")
        msg = await get_bin_message(client, BIN_CHANNEL, file_id)
        watch = f"{URL}watch/{msg.id}"
        download = f"{URL}download/{msg.id}"
        btn = [
            [
                InlineKeyboardButton("⚡ Watch Online", url=watch),
                InlineKeyboardButton("🚀 Fast Download", url=download),
            ],
            [InlineKeyboardButton("🙅 Close", callback_data="close_data")],
        ]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(btn))

    # --- Delete one query ---
    elif data.startswith("delete#"):
        token = data.split("#", 1)[1]
        owner_and_query = DELETE_BUTTONS.pop(token, None)
        if not owner_and_query or owner_and_query[0] != user_id:
            return await query.answer("This confirmation is not for you", show_alert=True)
        result = await delete_files_by_query(owner_and_query[1])
        await query.answer(f"Deleted {result.deleted_count} file(s)")
        await query.message.edit_text(
            f"✅ Deleted <b>{result.deleted_count}</b> file(s) from the database."
        )

    # --- Delete the whole index ---
    elif data == "delete_all":
        result = await delete_all_files()
        await query.answer(f"Deleted {result.deleted_count} file(s)")
        await query.message.edit_text(
            f"✅ Database wiped. <b>{result.deleted_count}</b> file(s) removed."
        )

    # --- Close (generic) ---
    elif data == "close_data":
        await query.message.delete()

    # --- Close (user-specific) ---
    elif data.startswith("close#"):
        try:
            requested_user = int(data.split("#", 1)[1])
        except (IndexError, ValueError):
            return await query.answer("Invalid button", show_alert=True)
        if requested_user == user_id:
            await query.message.delete()
        else:
            await query.answer("This is not for you! ❌", show_alert=True)

    # --- Page indicator (no-op) ---
    elif data == "buttons":
        await query.answer("⚙️")

    # --- Commands List ---
    elif data == "help":
        from config import script

        btn = [
            [InlineKeyboardButton("🔙 Back", callback_data="start_back")],
            [InlineKeyboardButton("🙅 Close", callback_data="close_data")],
        ]
        await query.message.edit_caption(
            caption=script.ADMIN_COMMAND_TXT,
            reply_markup=InlineKeyboardMarkup(btn),
        )

    # --- Back to /start ---
    elif data == "start_back":
        from config import script

        btn = [[InlineKeyboardButton("⚙️ Commands List", callback_data="help")]]
        await query.message.edit_caption(
            caption=script.START_TXT.format(query.from_user.mention, get_wish()),
            reply_markup=InlineKeyboardMarkup(btn),
        )

    else:
        await query.answer("Unknown or expired button", show_alert=True)
