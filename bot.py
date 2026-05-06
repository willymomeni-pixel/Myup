import os
import re
import logging
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, unquote

import aiohttp
import aiofiles
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# ── Config ───────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8617689209:AAHhDPRENnJw8Az6SXjnmGjvFXFfA8ZYKoM")
DOWNLOAD_DIR = "/tmp/tg_bot_files"
MAX_FILE_SIZE_MB = 500
CHUNK_SIZE = 1024 * 1024  # 1 MB

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Gofile API ───────────────────────────────────────────────────────

async def get_gofile_server() -> str:
    """بهترین سرور Gofile رو پیدا می‌کنه."""
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.gofile.io/servers") as r:
            data = await r.json()
            return data["data"]["servers"][0]["name"]


async def upload_to_gofile(filepath: str, filename: str) -> str:
    """فایل رو آپلود می‌کنه به Gofile و لینک مستقیم برمی‌گردونه."""
    server = await get_gofile_server()
    url = f"https://{server}.gofile.io/uploadFile"

    async with aiohttp.ClientSession() as session:
        with open(filepath, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f, filename=filename)
            async with session.post(url, data=form) as r:
                data = await r.json()
                if data["status"] != "ok":
                    raise Exception(f"Gofile error: {data}")
                return data["data"]["downloadPage"]


# ── Helpers ──────────────────────────────────────────────────────────

def fmt_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def get_filename_from_url(url: str, content_disposition: str = "") -> str:
    if content_disposition:
        m = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', content_disposition, re.IGNORECASE)
        if m:
            return unquote(m.group(1).strip())
    name = Path(unquote(urlparse(url).path)).name
    return name or "file"


async def download_from_url(url: str, progress_cb=None) -> tuple[str, str]:
    """لینک رو دانلود می‌کنه، مسیر فایل و mime رو برمی‌گردونه."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TgUploaderBot/2.0)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, allow_redirects=True) as r:
            r.raise_for_status()
            mime = r.headers.get("Content-Type", "").split(";")[0].strip()
            disp = r.headers.get("Content-Disposition", "")
            total = int(r.headers.get("Content-Length", 0))

            if total and total > MAX_FILE_SIZE_MB * 1024 * 1024:
                raise ValueError(f"حجم فایل ({fmt_size(total)}) بیشتر از {MAX_FILE_SIZE_MB} MB است.")

            fname = get_filename_from_url(str(r.url), disp)
            if not Path(fname).suffix and mime:
                fname += mimetypes.guess_extension(mime) or ""

            path = os.path.join(DOWNLOAD_DIR, fname)
            done = 0
            async with aiofiles.open(path, "wb") as f:
                async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                    await f.write(chunk)
                    done += len(chunk)
                    if progress_cb and total:
                        await progress_cb(done, total)

            return path, mime


async def download_tg_file(tg_file, filename: str) -> str:
    """فایل تلگرام رو دانلود می‌کنه."""
    path = os.path.join(DOWNLOAD_DIR, filename)
    await tg_file.download_to_drive(path)
    return path


# ── Progress bar ─────────────────────────────────────────────────────

def make_progress_cb(msg, label: str):
    last = [-1]
    async def cb(done, total):
        pct = done / total * 100
        if pct - last[0] >= 10:
            last[0] = pct
            bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
            await msg.edit_text(
                f"{label}\n`{bar}` {pct:.0f}%\n{fmt_size(done)} / {fmt_size(total)}",
                parse_mode=ParseMode.MARKDOWN,
            )
    return cb


# ── Core: process any file and return download link ──────────────────

async def process_and_upload(filepath: str, filename: str, msg) -> str:
    """فایل رو به Gofile آپلود می‌کنه و لینک دانلود برمی‌گردونه."""
    size = os.path.getsize(filepath)
    await msg.edit_text(
        f"☁️ *آپلود به فضای ابری...*\n📄 `{filename}`\n📦 {fmt_size(size)}",
        parse_mode=ParseMode.MARKDOWN,
    )
    link = await upload_to_gofile(filepath, filename)
    return link, size


# ── Handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *سلام! ربات آپلودر*\n\n"
        "هر چیزی بفرست یا لینک بده:\n"
        "• 🔗 لینک دانلود\n"
        "• 🖼 عکس\n"
        "• 🎬 ویدیو\n"
        "• 🎵 موزیک\n"
        "• 📄 هر فایلی\n\n"
        "یه *لینک مستقیم دانلود* برات می‌سازم! ⬇️",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """لینک دانلود رو پردازش می‌کنه."""
    url = update.message.text.strip()
    if not re.match(r"https?://", url):
        await update.message.reply_text("❌ لینک معتبر نیست.")
        return

    msg = await update.message.reply_text("⏳ در حال بررسی لینک...")
    filepath = None
    try:
        cb = make_progress_cb(msg, "📥 *دانلود...*")
        await msg.edit_text("📥 شروع دانلود...")
        filepath, mime = await download_from_url(url, cb)
        filename = Path(filepath).name

        link, size = await process_and_upload(filepath, filename, msg)

        await msg.edit_text(
            f"✅ *آماده‌ست!*\n\n"
            f"📄 `{filename}`\n"
            f"📦 {fmt_size(size)}\n\n"
            f"🔗 *لینک دانلود:*\n{link}",
            parse_mode=ParseMode.MARKDOWN,
        )

    except ValueError as e:
        await msg.edit_text(f"❌ {e}")
    except Exception as e:
        logger.exception("handle_url error")
        await msg.edit_text(f"❌ خطا:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """فایل ارسالی کاربر رو پردازش می‌کنه."""
    m = update.message

    if m.document:
        tg_file = await m.document.get_file()
        filename = m.document.file_name or "file"
        size = m.document.file_size or 0
    elif m.video:
        tg_file = await m.video.get_file()
        filename = m.video.file_name or "video.mp4"
        size = m.video.file_size or 0
    elif m.audio:
        tg_file = await m.audio.get_file()
        filename = m.audio.file_name or "audio.mp3"
        size = m.audio.file_size or 0
    elif m.photo:
        tg_file = await m.photo[-1].get_file()
        filename = "photo.jpg"
        size = m.photo[-1].file_size or 0
    elif m.voice:
        tg_file = await m.voice.get_file()
        filename = "voice.ogg"
        size = m.voice.file_size or 0
    else:
        return

    if size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await m.reply_text(f"❌ حجم فایل بیشتر از {MAX_FILE_SIZE_MB} MB است.")
        return

    msg = await m.reply_text("📥 در حال دریافت فایل از تلگرام...")
    filepath = None
    try:
        filepath = await download_tg_file(tg_file, filename)
        link, fsize = await process_and_upload(filepath, filename, msg)

        await msg.edit_text(
            f"✅ *آماده‌ست!*\n\n"
            f"📄 `{filename}`\n"
            f"📦 {fmt_size(fsize)}\n\n"
            f"🔗 *لینک دانلود:*\n{link}",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as e:
        logger.exception("handle_file error")
        await msg.edit_text(f"❌ خطا:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO | filters.VOICE,
        handle_file,
    ))
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
