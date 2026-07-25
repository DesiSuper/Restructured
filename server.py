import math
import mimetypes
import aiofiles
from typing import Union
from urllib.parse import quote
from aiohttp import web

from hydrogram import Client, raw
from hydrogram.session import Session, Auth
from hydrogram.errors import AuthBytesInvalid
from hydrogram.file_id import FileId, FileType
from hydrogram.types import Message

from config import URL, BIN_CHANNEL
from utils import temp, get_size
from database import get_search_results, get_file_details
from web_auth import login_required, check_credentials, create_session_token, SESSION_MAX_AGE

routes = web.RouteTableDef()


async def inject_theme_script(html: str) -> str:
    """Sabhi web pages (watch/login/panel) me shared theme-toggle script inject karo"""
    async with aiofiles.open('web/template/theme_script.html', mode='r', encoding='utf-8') as r:
        theme_script = await r.read()
    return html.replace('<!--THEME_SCRIPT-->', theme_script)


# ==========================================
# 🚀 CORE STREAMING & CHUNK YIELDER ENGINE
# ==========================================

async def chunk_size(length):
    return 2 ** max(min(math.ceil(math.log2(length / 1024)), 10), 2) * 1024

async def offset_fix(offset, chunksize):
    offset -= offset % chunksize
    return offset


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
        media_session = client.media_sessions.get(file_id_obj.dc_id, None)
        if media_session is None:
            is_test_mode = await client.storage.test_mode()
            if file_id_obj.dc_id != await client.storage.dc_id():
                media_session = Session(
                    client, file_id_obj.dc_id,
                    await Auth(client, file_id_obj.dc_id, is_test_mode).create(),
                    is_test_mode, is_media=True
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
                    else:
                        break
                else:
                    await media_session.stop()
                    raise AuthBytesInvalid
            else:
                media_session = Session(
                    client, file_id_obj.dc_id,
                    await client.storage.auth_key(),
                    is_test_mode, is_media=True
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
        self, file_id_obj: FileId,
        offset: int, first_part_cut: int,
        last_part_cut: int, part_count: int, chunk_size: int
    ) -> Union[bytes, None]:
        media_session = await self.generate_media_session(self.client, file_id_obj)
        current_part  = 1
        location      = await self.get_location(file_id_obj)

        r = await media_session.send(
            raw.functions.upload.GetFile(location=location, offset=offset, limit=chunk_size)
        )
        if isinstance(r, raw.types.upload.File):
            while current_part <= part_count:
                chunk = r.bytes
                if not chunk:
                    break
                offset += chunk_size
                if part_count == 1:
                    yield chunk[first_part_cut:last_part_cut]
                    break
                if current_part == 1:
                    yield chunk[first_part_cut:]
                if 1 < current_part <= part_count:
                    yield chunk
                r = await media_session.send(
                    raw.functions.upload.GetFile(location=location, offset=offset, limit=chunk_size)
                )
                current_part += 1


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
        file_name       = file_properties.file_name
        src             = f"{URL}download/{message_id}"
        mime_type       = file_properties.mime_type or 'video/mp4'
        tag             = mime_type.split('/')[0].strip()

        if tag == 'video':
            async with aiofiles.open('web/template/watch.html', mode='r', encoding='utf-8') as r:
                template_content = await r.read()

            safe_name = file_name.replace("{", "{{").replace("}", "}}")
            html = template_content.format(
                heading=f"Watch - {safe_name}",
                file_name=safe_name,
                src=src,
                mime_type=mime_type
            )
            html = await inject_theme_script(html)
            return web.Response(text=html, content_type='text/html')
        else:
            return web.Response(
                text="<h1>This file format is not supported for online streaming! 😕</h1>",
                content_type='text/html'
            )
    except Exception as e:
        return web.Response(text=f"<h1>Watch Engine Error: {e}</h1>", content_type='text/html')


@routes.get("/download/{message_id}")
async def download_handler(request):
    """Direct high-speed chunk streamer using BIN_CHANNEL fresh references"""
    try:
        message_id = int(request.match_info['message_id'])
        media_msg  = await temp.BOT.get_messages(BIN_CHANNEL, message_id)

        if not media_msg or media_msg.empty:
            return web.Response(text="<h1>File not found! ❌</h1>", content_type='text/html')

        file_properties = await TGCustomYield.generate_file_properties(media_msg)
        media_obj       = media_msg.document or media_msg.video or media_msg.audio
        file_id_obj     = FileId.decode(media_obj.file_id)
        file_size       = file_properties.file_size

        range_header = request.headers.get('Range', 0)
        if range_header:
            from_bytes, until_bytes = range_header.replace('bytes=', '').split('-')
            from_bytes  = int(from_bytes)
            until_bytes = int(until_bytes) if until_bytes else file_size - 1
        else:
            from_bytes  = request.http_range.start or 0
            until_bytes = request.http_range.stop or file_size - 1

        req_length     = (until_bytes - from_bytes) + 1
        new_chunk_size = await chunk_size(req_length)
        offset         = await offset_fix(from_bytes, new_chunk_size)
        first_part_cut = from_bytes - offset
        last_part_cut  = (until_bytes % new_chunk_size) + 1
        part_count     = math.ceil(req_length / new_chunk_size)

        body           = TGCustomYield().yield_file(
            file_id_obj, offset, first_part_cut, last_part_cut, part_count, new_chunk_size
        )
        mime_type      = file_properties.mime_type or 'application/octet-stream'
        safe_file_name = quote(file_properties.file_name)

        headers = {
            "Content-Type":        mime_type,
            "Content-Range":       f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Disposition": f'attachment; filename="{file_properties.file_name}"; filename*=UTF-8\'\'{safe_file_name}',
            "Accept-Ranges":       "bytes",
        }
        return web.Response(
            status=206 if range_header else 200,
            body=body,
            headers=headers
        )
    except Exception as e:
        return web.Response(text=f"<h1>Streaming Core Error: {e}</h1>", content_type='text/html')


# ==========================================
# 🔐 WEB PANEL — LOGIN (Admin Only)
# ==========================================

@routes.get("/login")
async def login_page(request):
    async with aiofiles.open('web/template/login.html', mode='r', encoding='utf-8') as r:
        template_content = await r.read()
    error = request.query.get('error', '')
    error_html = f'<div class="error">{error}</div>' if error else ''
    html = template_content.replace('<!--ERROR-->', error_html)
    html = await inject_theme_script(html)
    return web.Response(text=html, content_type='text/html')


@routes.post("/login")
async def login_submit(request):
    data     = await request.post()
    username = data.get('username', '')
    password = data.get('password', '')

    if check_credentials(username, password):
        resp = web.HTTPFound("/panel")
        resp.set_cookie("session", create_session_token(), max_age=SESSION_MAX_AGE, httponly=True)
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
    async with aiofiles.open('web/template/panel.html', mode='r', encoding='utf-8') as r:
        template_content = await r.read()
    html = await inject_theme_script(template_content)
    return web.Response(text=html, content_type='text/html')


@routes.get("/panel/api/search")
@login_required
async def panel_search_api(request):
    query  = request.query.get('q', '')
    offset = int(request.query.get('offset', 0) or 0)
    files, next_offset, total = await get_search_results(query, offset=offset)

    results = [{
        "file_id":   f.file_id,
        "file_name": f.file_name,
        "file_size": get_size(f.file_size) if f.file_size else "0 Bytes"
    } for f in files]

    return web.json_response({"results": results, "next_offset": next_offset, "total": total})


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
    msg  = await temp.BOT.send_cached_media(chat_id=BIN_CHANNEL, file_id=file.file_id)

    if mode == 'download':
        return web.HTTPFound(f"{URL}download/{msg.id}")
    return web.HTTPFound(f"{URL}watch/{msg.id}")
