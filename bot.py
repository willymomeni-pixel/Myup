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

# ── Config (از environment variables خونده می‌شه) ──
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8617689209:AAHhDPRENnJw8Az6SXjnmGjvFXFfA8ZYKoM")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/tg_downloads")
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "2000"))
CHUNK_SIZE = 1024 * 1024  # 1 MB

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────

def get_filename_from_url(url: str, content_disposition: str = "") -> str:
    if content_disposition:
        m = re.search(
            r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)',
            content_disposition,
            re.IGNORECASE,
        )
        if m:
            return unquote(m.group(1).strip())
    name = Path(unquote(urlparse(url).path)).name
    return name or "downloaded_file"


def fmt_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def file_type(filename: str, mime: str = "") -> str:
    ext = Path(filename).suffix.lower()
    if ext in {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv"}:
        return "video"
    if ext in {".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a"}:
        return "audio"
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return "photo"
    if mime.startswith("video"):
        return "video"
    if mime.startswith("audio"):
        return "audio"
    if mime.startswith("image"):
        return "photo"
    return "document"


async def download_file(url: str, progress_cb=None) -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TgUploaderBot/1.0)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, allow_redirects=True) as r:
            r.raise_for_status()
            mime = r.headers.get("Content-Type", "").split(";")[0].strip()
            disp = r.headers.get("Content-Disposition", "")
            total = int(r.headers.get("Content-Length", 0))

            if total and total > MAX_FILE_SIZE_MB * 1024 * 1024:
                raise ValueError(
                    f"حجم فایل ({fmt_size(total)}) بیشتر از حد مجاز {MAX_FILE_SIZE_MB} MB است."
                )

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


def extract_file_id(msg) -> str:
    for attr in ("video", "audio", "document", "photo"):
        obj = getattr(msg, attr, None)
        if obj:
            return (obj[-1] if isinstance(obj, list) else obj).file_id
    return "N/A"


async def send_file(update: Update, path: str, fname: str, ftype: str, mime: str):
    with open(path, "rb") as f:
        if ftype == "video":
            return await update.message.reply_video(video=f, filename=fname, supports_streaming=True)
        if ftype == "audio":
            return await update.message.reply_audio(audio=f, filename=fname)
        if ftype == "photo":
            return await update.message.reply_photo(photo=f)
        return await update.message.reply_document(document=f, filename=fname)


# ── Handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *سلام! ربات آپلودر*\n\n"
        "• لینک دانلود بفرست → آپلود می‌کنم\n"
        "• فایل بفرست → File ID می‌دم\n\n"
        "/help راهنما",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *راهنما*\n\n"
        "1️⃣ لینک مستقیم بفرست:\n"
        "`https://example.com/file.mp4`\n\n"
        "2️⃣ یا فایل مستقیم بفرست\n"
        "   (ویدیو / موزیک / عکس / سند)\n\n"
        f"⚠️ حداکثر حجم: {MAX_FILE_SIZE_MB} MB",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not re.match(r"https?://", url):
        await update.message.reply_text("❌ لینک باید با http:// یا https:// شروع بشه.")
        return

    msg = await update.message.reply_text("⏳ در حال بررسی لینک...")
    last_pct = [-1]

    async def on_progress(done: int, total: int):
        pct = done / total * 100
        if pct - last_pct[0] >= 10:
            last_pct[0] = pct
            bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
            await msg.edit_text(
                f"📥 *دانلود...*\n`{bar}` {pct:.0f}%\n{fmt_size(done)} / {fmt_size(total)}",
                parse_mode=ParseMode.MARKDOWN,
            )

    filepath = None
    try:
        await msg.edit_text("📥 شروع دانلود...")
        filepath, mime = await download_file(url, on_progress)

        fname = Path(filepath).name
        fsize = os.path.getsize(filepath)
        ftype = file_type(fname, mime)

        await msg.edit_text(
            f"📤 *آپلود به تلگرام...*\n📄 `{fname}`\n📦 {fmt_size(fsize)}",
            parse_mode=ParseMode.MARKDOWN,
        )

        sent = await send_file(update, filepath, fname, ftype, mime)
        fid = extract_file_id(sent)

        await msg.edit_text(
            f"✅ *آپلود موفق!*\n\n📄 `{fname}`\n📦 {fmt_size(fsize)}\n🆔 File ID:\n`{fid}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    except ValueError as e:
        await msg.edit_text(f"❌ {e}")
    except aiohttp.ClientError as e:
        await msg.edit_text(f"❌ خطا در دانلود:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.exception("handle_url error")
        await msg.edit_text(f"❌ خطای غیرمنتظره:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    if m.document:
        obj, fname = m.document, m.document.file_name or "file"
    elif m.video:
        obj, fname = m.video, m.video.file_name or "video.mp4"
    elif m.audio:
        obj, fname = m.audio, m.audio.file_name or "audio.mp3"
    elif m.photo:
        obj, fname = m.photo[-1], "photo.jpg"
    elif m.voice:
        obj, fname = m.voice, "voice.ogg"
    else:
        return

    fsize = getattr(obj, "file_size", 0) or 0
    await m.reply_text(
        f"✅ *فایل دریافت شد!*\n\n📄 `{fname}`\n📦 {fmt_size(fsize)}\n🆔 File ID:\n`{obj.file_id}`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Entry point ──────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO | filters.VOICE,
        handle_file,
    ))
    logger.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
