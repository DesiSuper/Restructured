import os
import time
import asyncio
from typing import Union, Optional, AsyncGenerator

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from hydrogram import Client, types
from hydrogram.errors import FloodWait
from aiohttp import web

from server import routes
from config import LOG_CHANNEL, API_ID, API_HASH, BOT_TOKEN, PORT, BIN_CHANNEL, ADMINS
from database import ensure_indexes
from utils import temp, get_readable_time
from plugins.blacklist import load_blacklist_cache, load_pending_deletions


class Bot(Client):
    def __init__(self):
        super().__init__(
            name='Auto_Filter_Bot',
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            plugins={"root": "plugins"}
        )
        self.web_runner = None

    async def start(self):
        temp.START_TIME = time.time()
        await super().start()

        # Restart message handler
        if os.path.exists('restart.txt'):
            try:
                with open("restart.txt") as f:
                    chat_id, msg_id = map(int, f.read().split())
                await self.edit_message_text(chat_id=chat_id, message_id=msg_id, text='Restarted Successfully!')
            except Exception:
                pass
            try:
                os.remove('restart.txt')
            except Exception:
                pass

        temp.BOT = self
        await ensure_indexes()
        me = await self.get_me()
        temp.ME     = me.id
        temp.U_NAME = me.username
        temp.B_NAME = me.first_name

        # Blacklist feature: patterns cache load karo, aur agar restart se pehle
        # koi delete pending thi to use RAM mein wapas schedule/turant clear karo
        await load_blacklist_cache()
        await load_pending_deletions(self)

        print(f"🔥 {me.first_name} started in Admin-Only Mode!")

        # aiohttp web server start (streaming engine ke liye)
        web_app = web.Application()
        web_app.add_routes(routes)
        self.web_runner = web.AppRunner(web_app, access_log=None)
        await self.web_runner.setup()
        await web.TCPSite(self.web_runner, "0.0.0.0", PORT).start()
        print(f"🌐 Streaming Web Server active on port {PORT}!")

        # Log channel verification
        try:
            await self.send_message(chat_id=LOG_CHANNEL, text=f"<b>✅ {me.mention} has restarted! (Admin Only Mode)</b>")
        except Exception:
            print("Error - Check LOG_CHANNEL, the bot must be an admin.")
            exit()

        # BIN_CHANNEL verification
        try:
            m = await self.send_message(chat_id=BIN_CHANNEL, text="⚡ ʙɪɴ ᴄʜᴀɴɴᴇʟ ᴛᴇsᴛ")
            await m.delete()
        except Exception:
            print("Error - Check BIN_CHANNEL, the bot must be an admin.")
            exit()

        # Admins ko startup alert
        for admin in ADMINS:
            try:
                await self.send_message(chat_id=admin, text="<b>🔥 ✅ Bot has restarted successfully!</b>")
            except Exception:
                pass

    async def stop(self, *args):
        if self.web_runner is not None:
            await self.web_runner.cleanup()
            self.web_runner = None
        await super().stop()
        temp.BOT = None
        print("Bot Stopped! Bye...")

    async def iter_messages(
        self,
        chat_id: Union[int, str],
        limit: int,
        offset: int = 0
    ) -> Optional[AsyncGenerator["types.Message", None]]:
        """Indexing ke liye messages iterate karne ka optimized method"""
        current = offset
        while True:
            new_diff = min(200, limit - current)
            if new_diff <= 0:
                return
            messages = await self.get_messages(chat_id, list(range(current, current + new_diff)))
            for message in messages:
                yield message
                current += 1


async def main():
    app = Bot()
    await app.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FloodWait as vp:
        print(f"Flood Wait: {get_readable_time(vp.value)} ke liye sleep kar rahe hain...")
        time.sleep(vp.value)
        asyncio.run(main())
