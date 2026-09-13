import base64
import logging
import re
from struct import pack

from hydrogram.file_id import FileId
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import InsertOne
from pymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure

from config import DATABASE_NAME, DATABASE_URL, MAX_BTN, COLLECTION_NAME


# One shared Motor client keeps connection setup cheap and avoids a pool per
# feature.  Timeouts make a broken MongoDB connection fail quickly instead of
# freezing Telegram handlers indefinitely.
_client = AsyncIOMotorClient(
    DATABASE_URL,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=15000,
    retryWrites=True,
)
_db = _client[DATABASE_NAME]

col = _db[COLLECTION_NAME]
users = _db["Users"]
bot_col = _db["bot_id"]


async def ensure_indexes():
    """Create indexes used by search and frequent lookups.

    This is intentionally called during startup, rather than waiting for the
    first search request.  Existing deployments may already have the indexes;
    MongoDB makes this operation idempotent.
    """
    index_requests = (
        (
            col,
            [("file_name", "text"), ("caption", "text")],
            {"name": "file_text_search", "default_language": "english"},
        ),
        (col, [("file_id", 1)], {"name": "file_id_lookup"}),
        (users, [("id", 1)], {"unique": True, "name": "user_id_unique"}),
        (bot_col, [("id", 1)], {"unique": True, "name": "bot_id_unique"}),
    )
    for collection, keys, options in index_requests:
        try:
            await collection.create_index(keys, **options)
        except Exception as error:
            # An older deployment can already have the same keys under a
            # generated name.  Search still works, so do not block startup.
            logging.warning("Index %s unavailable: %s", options.get("name"), error)


class Media:
    """Small wrapper around a file document."""

    def __init__(self, data):
        self.file_id = data.get("_id")
        self.file_name = data.get("file_name") or "Unnamed file"
        self.file_size = data.get("file_size") or 0
        self.caption = data.get("caption", "") or ""

    @staticmethod
    async def count_documents(filter_query=None):
        return await col.count_documents(filter_query or {})

    @staticmethod
    def find(filter_query=None):
        return col.find(filter_query or {})


def _clean_name(value):
    return re.sub(r"@\w+|([_\-\.+])", " ", str(value or "")).strip()


def _media_document(media):
    file_id = unpack_new_file_id(media.file_id)
    return {
        "_id": file_id,
        "file_name": _clean_name(getattr(media, "file_name", "Unnamed file")),
        "file_size": int(getattr(media, "file_size", 0) or 0),
        "caption": _clean_name(getattr(media, "caption", "")),
    }


def _search_filter(query, use_text=True):
    """Build a fast search filter while retaining all-word matching.

    ``$text`` uses the MongoDB text index to narrow the candidate set.  The
    escaped regex checks preserve the old behaviour where every entered
    keyword must occur, including names containing punctuation.  The fallback
    without ``$text`` keeps older deployments working while indexes build.
    """
    query = str(query or "").strip()
    if not query:
        return {}

    keywords = query.split()
    patterns = [re.compile(re.escape(keyword), re.IGNORECASE) for keyword in keywords]
    result = {
        "$and": [
            {"$or": [{"file_name": pattern}, {"caption": pattern}]}
            for pattern in patterns
        ]
    }
    if use_text:
        result["$text"] = {"$search": query}
    return result


async def save_file(media):
    """Save one Telegram media item to the database."""
    try:
        await col.insert_one(_media_document(media))
        return "suc"
    except DuplicateKeyError:
        return "dup"
    except Exception:
        logging.exception("Saving error")
        return "err"


async def save_files(media_items):
    """Insert a batch of media items with one MongoDB round trip.

    Manual indexing used to await one insert per Telegram message.  Unordered
    bulk inserts preserve duplicate/error counts while being much faster over
    a network database.
    """
    documents = []
    errors = 0
    for media in media_items:
        try:
            documents.append(_media_document(media))
        except Exception:
            errors += 1

    if not documents:
        return {"suc": 0, "dup": 0, "err": errors}

    try:
        result = await col.bulk_write(
            [InsertOne(document) for document in documents], ordered=False
        )
        return {"suc": result.inserted_count, "dup": 0, "err": errors}
    except BulkWriteError as exc:
        duplicate_count = 0
        failed_count = errors
        for write_error in exc.details.get("writeErrors", []):
            if write_error.get("code") == 11000:
                duplicate_count += 1
            else:
                failed_count += 1
        inserted_count = exc.details.get("nInserted", 0)
        return {"suc": inserted_count, "dup": duplicate_count, "err": failed_count}
    except Exception:
        logging.exception("Bulk file save failed")
        return {"suc": 0, "dup": 0, "err": len(documents) + errors}


async def get_search_results(query, max_results=MAX_BTN, offset=0):
    """Return one page of indexed search results."""
    try:
        max_results = max(1, min(int(max_results), 100))
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        max_results, offset = MAX_BTN, 0

    filter_dict = _search_filter(query)
    try:
        cursor = (
            col.find(
                filter_dict,
                {"_id": 1, "file_name": 1, "file_size": 1, "caption": 1},
            )
            .sort("$natural", -1)
            .skip(offset)
            .limit(max_results)
        )
        files_data = await cursor.to_list(length=max_results)
        total = await col.count_documents(filter_dict)
    except OperationFailure as error:
        # A first request can race with index creation, or an old deployment
        # may have no text index yet.  Fall back to the compatible regex query.
        if "$text" not in filter_dict:
            raise
        logging.warning("Text search unavailable; using regex fallback: %s", error)
        filter_dict = _search_filter(query, use_text=False)
        cursor = (
            col.find(
                filter_dict,
                {"_id": 1, "file_name": 1, "file_size": 1, "caption": 1},
            )
            .sort("$natural", -1)
            .skip(offset)
            .limit(max_results)
        )
        files_data = await cursor.to_list(length=max_results)
        total = await col.count_documents(filter_dict)

    files = [Media(data) for data in files_data]

    # The panel and Telegram pagination need a total.  count_documents is
    # cheaper than materialising all matching documents.
    next_offset = offset + len(files)
    if next_offset >= total or not files:
        next_offset = ""
    return files, next_offset, total


async def delete_files(query):
    """Find files matching a query for the admin delete confirmation."""
    filter_dict = _search_filter(query)
    try:
        total = await col.count_documents(filter_dict)
        files = [Media(data) async for data in col.find(filter_dict)]
    except OperationFailure:
        filter_dict = _search_filter(query, use_text=False)
        total = await col.count_documents(filter_dict)
        files = [Media(data) async for data in col.find(filter_dict)]
    return total, files


async def delete_files_by_query(query):
    """Delete all records matching a query and return Mongo's result."""
    filter_dict = _search_filter(query)
    try:
        return await col.delete_many(filter_dict)
    except OperationFailure:
        return await col.delete_many(_search_filter(query, use_text=False))


async def delete_all_files():
    """Remove every indexed file document."""
    return await col.delete_many({})


async def get_file_details(query):
    """Fetch one file by its Telegram-compatible encoded file id."""
    if not query:
        return []
    files_data = await col.find(
        {"_id": str(query)},
        {"_id": 1, "file_name": 1, "file_size": 1, "caption": 1},
    ).to_list(length=1)
    return [Media(data) for data in files_data]


def encode_file_id(value: bytes) -> str:
    result = bytearray()
    zero_count = 0
    for byte in value + bytes([22, 4]):
        if byte == 0:
            zero_count += 1
            continue
        if zero_count:
            result.extend((0, zero_count))
            zero_count = 0
        result.append(byte)
    return base64.urlsafe_b64encode(bytes(result)).decode().rstrip("=")


def unpack_new_file_id(new_file_id):
    decoded = FileId.decode(new_file_id)
    return encode_file_id(
        pack(
            "<iiqq",
            int(decoded.file_type),
            int(decoded.dc_id),
            int(decoded.media_id),
            int(decoded.access_hash),
        )
    )


class Database:
    """Database wrapper for users and bot settings."""

    async def add_user(self, id, name):
        await users.update_one(
            {"id": int(id)},
            {"$set": {"name": name}},
            upsert=True,
        )

    async def is_user_exist(self, id):
        return bool(await users.find_one({"id": int(id)}, {"_id": 1}))

    async def total_users_count(self):
        return await users.count_documents({})

    async def get_all_users(self):
        return users.find({})

    async def delete_user(self, user_id):
        await users.delete_many({"id": int(user_id)})

    async def get_db_size(self):
        return (await _db.command("dbstats"))["dataSize"]

    async def get_pm_search_status(self, bot_id):
        bot = await bot_col.find_one({"id": int(bot_id)}, {"bot_pm_search": 1})
        return bot.get("bot_pm_search", True) if bot else True

    async def update_pm_search_status(self, bot_id, enable):
        await bot_col.update_one(
            {"id": int(bot_id)},
            {"$set": {"bot_pm_search": bool(enable)}},
            upsert=True,
        )

    async def get_group_search_status(self, chat_id):
        chat = await bot_col.find_one({"id": int(chat_id)}, {"group_search": 1})
        return chat.get("group_search", True) if chat else True

    async def update_group_search_status(self, chat_id, enable):
        await bot_col.update_one(
            {"id": int(chat_id)},
            {"$set": {"group_search": bool(enable)}},
            upsert=True,
        )


db = Database()
