import asyncio
import html
import math
import re
from typing import AsyncIterator
from urllib.parse import quote

import aiofiles
from aiohttp import web

from hydrogram import Client, raw
from hydrogram.errors import AuthBytesInvalid
from hydrogram.file_id import FileId, FileType
from hydrogram.session import Auth, Session
from hydrogram.types import Message

from config import BIN_CHANNEL, URL
from database import get_file_details, get_search_results
from utils import get_bin_message, get_size, temp
from web_auth import (
    SESSION_MAX_AGE,
    check_credentials,
    create_session_token,
    login_required,
)

routes = web.RouteTableDef()
_TEMPLATE_CACHE = {}
_TEMPLATE_LOCK = asyncio.Lock()


async def load_template(path: str) -> str:
    """Read a static template once and reuse it for subsequent requests."""
    cached = _TEMPLATE_CACHE.get(path)
    if cached is not None:
        return cached
    async with _TEMPLATE_LOCK:
        cached = _TEMPLATE_CACHE.get(path)
        if cached is None:
            async with aiofiles.open(path, mode="r", encoding="utf-8") as reader:
                cached = await reader.read()
            _TEMPLATE_CACHE[path] = cached
    return cached


async def inject_theme_script(page: str) -> str:
    """Inject the shared theme-toggle script into a web page."""
    theme_script = await load_template("web/template/theme_script.html")
    return page.replace("<!--THEME_SCRIPT-->", theme_script)


# ==========================================
# 🚀 CORE STREAMING & CHUNK YIELDER ENGINE
# ==========================================

# Telegram accepts upload.getFile chunks up to 1 MiB.  The old implementation
# topped out at 10 KiB, which made large downloads needlessly slow.
MIN_CHUNK_SIZE = 64 * 1024
MAX_CHUNK_SIZE = 1024 * 1024
_MEDIA_SESSION_LOCK = asyncio.Lock()


def chunk_size(length: int) -> int:
    """Choose a Telegram-friendly chunk size for a requested byte range."""
    length = max(1, int(length))
    target = min(MAX_CHUNK_SIZE, max(MIN_CHUNK_SIZE, length // 8 or MIN_CHUNK_SIZE))
    # upload.getFile limits are 1 KiB aligned.
    return max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, (target // 1024) * 1024))


def offset_fix(offset: int, chunksize: int) -> int:
    return max(0, int(offset) - (int(offset) % int(chunksize)))


def parse_range_header(value: str, file_size: int):
    """Parse a single HTTP byte range and return ``(start, end)``.

    Multi-range responses are not useful for a video element and would require
    multipart encoding, so they are rejected with a normal 416 response.
    """
    if not value:
        return 0, file_size - 1, False
    if not value.lower().startswith("bytes=") or "," in value:
        raise ValueError("Only one byte range is supported")

    raw_range = value[6:].strip()
    if "-" not in raw_range:
        raise ValueError("Invalid byte range")
    start_text, end_text = (part.strip() for part in raw_range.split("-", 1))

    if not start_text:
        # Suffix range: bytes=-N
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("Invalid suffix range")
        start = max(0, file_size - suffix_length)
        return start, file_size - 1, True

    start = int(start_text)
    if start < 0 or start >= file_size:
        raise ValueError("Range starts past end of file")
    end = file_size - 1 if not end_text else int(end_text)
    if end < start:
        raise ValueError("Range end precedes start")
    return start, min(end, file_size - 1), True


class TGCustomYield:
    def __init__(self):
        self.client = temp.BOT

    @staticmethod
    async def generate_file_properties(msg: Message):
        """Live Telegram messages se fresh file properties decode karo"""
        media       = msg.document or msg.video or msg.audio
        file_id_obj = FileId.decode(media.file_id)
        setattr(file_id_obj, "file_size", getattr(media, "file_size", 0))
        setattr(file_id_obj, "mime_type", getattr(media, "mime_type", ""))
        setattr(file_id_obj, "file_name", getattr(media, "file_name", ""))
        return file_id_obj

    async def generate_media_session(self, client: Client, file_id_obj: FileId):
        # Several browser range requests commonly arrive together.  Serialize
        # session creation so they cannot all try to export authorization at
        # the same time.
        async with _MEDIA_SESSION_LOCK:
            media_session = client.media_sessions.get(file_id_obj.dc_id)
            if media_session is not None:
                return media_session

            is_test_mode = await client.storage.test_mode()
            if file_id_obj.dc_id != await client.storage.dc_id():
                media_session = Session(
                    client,
                    file_id_obj.dc_id,
                    await Auth(client, file_id_obj.dc_id, is_test_mode).create(),
                    is_test_mode,
                    is_media=True,
                )
                await media_session.start()
                for _ in range(3):
                    exported_auth = await client.invoke(
                        raw.functions.auth.ExportAuthorization(dc_id=file_id_obj.dc_id)
                    )
                    try:
                        await media_session.send(
                            raw.functions.auth.ImportAuthorization(
                                id=exported_auth.id, bytes=exported_auth.bytes
                            )
                        )
                    except AuthBytesInvalid:
                        continue
                    break
                else:
                    await media_session.stop()
                    raise AuthBytesInvalid
            else:
                media_session = Session(
                    client,
                    file_id_obj.dc_id,
                    await client.storage.auth_key(),
                    is_test_mode,
                    is_media=True,
                )
                await media_session.start()
            client.media_sessions[file_id_obj.dc_id] = media_session
            return media_session

    @staticmethod
    async def get_location(file_id: FileId):
        if file_id.file_type == FileType.PHOTO:
            return raw.types.InputPhotoFileLocation(
                id=file_id.media_id, access_hash=file_id.access_hash,
                file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size
            )
        return raw.types.InputDocumentFileLocation(
            id=file_id.media_id, access_hash=file_id.access_hash,
            file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size
        )

    async def yield_file(
        self,
        file_id_obj: FileId,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
    ) -> AsyncIterator[bytes]:
        media_session = await self.generate_media_session(self.client, file_id_obj)
        location = await self.get_location(file_id_obj)

        for part_number in range(part_count):
            response = await media_session.send(
                raw.functions.upload.GetFile(
                    location=location,
                    offset=offset + part_number * chunk_size,
                    limit=chunk_size,
                )
            )
            if not isinstance(response, raw.types.upload.File) or not response.bytes:
                break

            chunk = response.bytes
            if part_count == 1:
                # Both cuts refer to the same Telegram chunk.
                chunk = chunk[first_part_cut:last_part_cut]
            elif part_number == 0:
                chunk = chunk[first_part_cut:]
            elif part_number == part_count - 1:
                # On later chunks the start is already aligned.
                chunk = chunk[:last_part_cut]
            if chunk:
                yield chunk


# ==========================================
# 🛠️ ROUTING CONTROLLERS
# ==========================================

@routes.get("/", allow_head=True)
async def root_route_handler(request):
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Fast Finder</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700;900&display=swap" rel="stylesheet">
<style>
:root{--red:#e50914;--bg1:#000;--bg2:#111;--txt:#fff;--box-bd:rgba(255,255,255,.1);--txt-muted:#b3b3b3;--btn-c:rgba(255,255,255,.08);}
html.light{--bg1:#f8f9fa;--bg2:#e9ecef;--txt:#121212;--box-bd:rgba(0,0,0,.15);--txt-muted:#555;--btn-c:rgba(0,0,0,.06);}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:linear-gradient(to bottom,var(--bg1),var(--bg2));font-family:'DM Sans',sans-serif;color:var(--txt);min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:20px;transition:background .3s,color .3s;}
.theme-btn{position:fixed;top:16px;right:16px;background:transparent;border:1px solid var(--box-bd);color:var(--txt);padding:7px 16px;border-radius:4px;font-family:'DM Sans',sans-serif;font-weight:700;font-size:13px;cursor:pointer;transition:.3s;}
.theme-btn:hover{background:var(--btn-c);}
.logo{font-size:28px;font-weight:900;color:var(--red);display:flex;align-items:center;gap:8px;margin-bottom:18px;}
.nf-icon{background:var(--red);color:#fff;padding:2px 8px;border-radius:4px;}
h1{font-size:18px;font-weight:700;color:var(--txt-muted);margin-bottom:30px;}
.login-btn{padding:13px 34px;border-radius:6px;background:var(--red);color:#fff;text-decoration:none;font-weight:700;font-size:15px;box-shadow:0 0 18px rgba(229,9,20,.35);transition:.25s;}
.login-btn:hover{background:#ff1a1a;transform:translateY(-2px);}
</style>
</head>
<body>
<button class="theme-btn" id="theme-btn">Theme</button>
<div class="logo"><span class="nf-icon">F</span> FAST FINDER</div>
<h1>🚀 High-Performance Stream Server Active</h1>
<a href="/login" class="login-btn">Admin Login</a>
<!--THEME_SCRIPT-->
</body></html>"""
    html = await inject_theme_script(html)
    return web.Response(text=html, content_type='text/html')


@routes.get("/watch/{message_id}")
async def watch_handler(request):
    """BIN_CHANNEL message ID se cinematic video player render karo"""
    try:
        message_id = int(request.match_info['message_id'])
        media_msg  = await temp.BOT.get_messages(BIN_CHANNEL, message_id)

        if not media_msg or media_msg.empty:
            return web.Response(
                text="<h1>This file has been deleted from the server! ❌</h1>",
                content_type='text/html'
            )

        file_properties = await TGCustomYield.generate_file_properties(media_msg)
        file_name = str(file_properties.file_name or "Unnamed file")
        src = f"{URL}download/{message_id}?inline=1"
        mime_type = str(file_properties.mime_type or "video/mp4")
        tag = mime_type.split("/", 1)[0].strip().lower()

        if tag == "video":
            template_content = await load_template("web/template/watch.html")

            # Escape values before inserting them into HTML and protect the
            # template's literal braces for str.format().
            safe_name = html.escape(file_name, quote=True).replace("{", "{{").replace("}", "}}")
            safe_mime = html.escape(mime_type, quote=True).replace("{", "{{").replace("}", "}}")
            safe_src = html.escape(src, quote=True).replace("{", "{{").replace("}", "}}")
            rendered = template_content.format(
                heading=f"Watch - {safe_name}",
                file_name=safe_name,
                src=safe_src,
                mime_type=safe_mime,
            )
            return web.Response(
                text=await inject_theme_script(rendered),
                content_type="text/html",
                headers={"Cache-Control": "no-store"},
            )
        else:
            return web.Response(
                text="<h1>This file format is not supported for online streaming! 😕</h1>",
                content_type='text/html'
            )
    except (TypeError, ValueError):
        return web.Response(status=400, text="Invalid message id", content_type="text/plain")
    except Exception:
        import logging

        logging.exception("Watch page error")
        return web.Response(status=502, text="Watch page is temporarily unavailable", content_type="text/plain")


@routes.get("/download/{message_id}", allow_head=True)
async def download_handler(request):
    """Stream a Telegram file with correct, resumable HTTP range support."""
    try:
        message_id = int(request.match_info["message_id"])
        media_msg = await temp.BOT.get_messages(BIN_CHANNEL, message_id)
        if not media_msg or media_msg.empty:
            return web.Response(status=404, text="File not found", content_type="text/plain")

        file_properties = await TGCustomYield.generate_file_properties(media_msg)
        media_obj = media_msg.document or media_msg.video or media_msg.audio
        file_id_obj = FileId.decode(media_obj.file_id)
        file_size = int(file_properties.file_size or 0)
        if file_size <= 0:
            return web.Response(status=404, text="File has no readable size", content_type="text/plain")

        try:
            start, end, is_partial = parse_range_header(
                request.headers.get("Range", ""), file_size
            )
        except (TypeError, ValueError, OverflowError):
            return web.Response(
                status=416,
                text="Requested range is not satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        requested_length = end - start + 1
        selected_chunk_size = chunk_size(requested_length)
        aligned_offset = offset_fix(start, selected_chunk_size)
        first_part_cut = start - aligned_offset
        # Number of chunks needed includes the unaligned bytes before start.
        part_count = math.ceil((first_part_cut + requested_length) / selected_chunk_size)
        last_part_cut = (end % selected_chunk_size) + 1

        file_name = str(file_properties.file_name or f"file-{message_id}")
        # Header values must never contain CR/LF.  RFC 5987 filename* keeps
        # non-ASCII names intact while the ASCII fallback remains compatible.
        ascii_name = re.sub(r"[^\x20-\x7e]", "_", file_name).replace('"', "'")
        encoded_name = quote(file_name, safe="")
        disposition = "inline" if request.query.get("inline") == "1" else "attachment"
        headers = {
            "Content-Type": str(file_properties.mime_type or "application/octet-stream"),
            "Content-Length": str(requested_length),
            "Content-Disposition": (
                f"{disposition}; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{encoded_name}"
            ),
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
        }
        if is_partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

        response = web.StreamResponse(status=206 if is_partial else 200, headers=headers)
        await response.prepare(request)
        if request.method == "HEAD":
            await response.write_eof()
            return response

        body = TGCustomYield().yield_file(
            file_id_obj,
            aligned_offset,
            first_part_cut,
            last_part_cut,
            part_count,
            selected_chunk_size,
        )
        try:
            async for chunk in body:
                await response.write(chunk)
        except ConnectionResetError:
            # The browser often cancels a request after seeking.  Do not turn
            # a normal client disconnect into a noisy server error.
            return response
        finally:
            if not response._eof_sent:
                try:
                    await response.write_eof()
                except ConnectionResetError:
                    pass
        return response
    except (ValueError, TypeError):
        return web.Response(status=400, text="Invalid message id", content_type="text/plain")
    except Exception:
        # Do not leak Telegram credentials, file references, or stack details
        # to a browser.  The full traceback remains in the process logs.
        import logging

        logging.exception("Streaming error")
        return web.Response(status=502, text="Streaming is temporarily unavailable", content_type="text/plain")


# ==========================================
# 🔐 WEB PANEL — LOGIN (Admin Only)
# ==========================================

@routes.get("/login")
async def login_page(request):
    template_content = await load_template("web/template/login.html")
    error = request.query.get("error", "")
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    page_html = template_content.replace('<!--ERROR-->', error_html)
    page_html = await inject_theme_script(page_html)
    return web.Response(text=page_html, content_type='text/html')


@routes.post("/login")
async def login_submit(request):
    data     = await request.post()
    username = data.get('username', '')
    password = data.get('password', '')

    if check_credentials(username, password):
        resp = web.HTTPFound("/panel")
        resp.set_cookie(
            "session",
            create_session_token(),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=URL.startswith("https://"),
            samesite="Strict",
            path="/",
        )
        return resp
    return web.HTTPFound("/login?error=Invalid username or password")


@routes.get("/logout")
async def logout(request):
    resp = web.HTTPFound("/login")
    resp.del_cookie("session")
    return resp


# ==========================================
# 🔎 WEB PANEL — SEARCH & STREAM (Admin Only)
# ==========================================

@routes.get("/panel")
@login_required
async def panel_page(request):
    template_content = await load_template("web/template/panel.html")
    html = await inject_theme_script(template_content)
    return web.Response(text=html, content_type='text/html')


@routes.get("/panel/api/search")
@login_required
async def panel_search_api(request):
    query = request.query.get("q", "")[:200]
    try:
        offset = max(0, int(request.query.get("offset", 0) or 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "Invalid offset"}, status=400)

    files, next_offset, total = await get_search_results(query, offset=offset)
    results = [
        {
            "file_id": file.file_id,
            "file_name": file.file_name,
            "file_size": get_size(file.file_size),
        }
        for file in files
    ]
    return web.json_response(
        {"results": results, "next_offset": next_offset, "total": total},
        headers={"Cache-Control": "no-store"},
    )


@routes.get("/panel/stream/{file_id}")
@login_required
async def panel_stream(request):
    """Web panel se select ki gayi file ko BIN_CHANNEL me forward karke watch/download link par redirect karo"""
    file_id = request.match_info['file_id']
    mode    = request.query.get('mode', 'watch')

    file_details = await get_file_details(file_id)
    if not file_details:
        return web.Response(text="<h1>File not found! ❌</h1>", content_type='text/html')

    file = file_details[0]
    msg = await get_bin_message(temp.BOT, BIN_CHANNEL, file.file_id)

    if mode == "download":
        return web.HTTPFound(f"{URL}download/{msg.id}")
    return web.HTTPFound(f"{URL}watch/{msg.id}")
