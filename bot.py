import os
import re
import time
import sqlite3
import logging
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
from typing import List, Tuple, Optional
from unidecode import unidecode

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError
from telegram.ext import PicklePersistence

# ==============================================================================
# 0. HTTP SERVER GIẢ (Xử lý cả GET và HEAD cho UptimeRobot & Render)
# ==============================================================================
def _run_fake_http_server():
    port = int(os.getenv("PORT", "10000"))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Bot dang chay OK")

        def do_HEAD(self):
            # Trả về 200 OK cho UptimeRobot / Ping services dùng HEAD request
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()

        def log_message(self, format, *args):
            pass  # Tắt log HTTP để tránh làm rác terminal/console log

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
        server.daemon_threads = True
        logger.info(f"🌐 Đã mở cổng giả {port} để Render nhận diện Web Service & hỗ trợ HEAD request.")
        server.serve_forever()
    except OSError as e:
        logger.warning(f"⚠️ Không thể mở cổng {port}: {e}")


# ==============================================================================
# 1. CẤU HÌNH BAN ĐẦU & LOGGING
# ==============================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Lấy Token từ biến môi trường
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE").strip()

# Đặt DATA_DIR thành thư mục persistent disk của nền tảng (ví dụ /var/data trên Render).
# Mặc định lưu cạnh file bot; không dùng thư mục tạm nếu cần giữ dữ liệu sau deploy/restart.
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", os.path.dirname(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
DB_NAME = os.path.join(DATA_DIR, os.getenv("DB_NAME", "documents.db"))
PERSISTENCE_NAME = os.path.join(DATA_DIR, "telegram_bot_state.pkl")

# Các trạng thái của ConversationHandler khi Upload
SELECT_SUBJECT, INPUT_TOPIC, CONFIRM_REUSE = range(3)

# Thời gian (giây) gợi ý dùng lại chủ đề vừa lưu
REUSE_TOPIC_WINDOW = 15 * 60  # 15 phút

# Danh sách các môn học hỗ trợ
SUBJECTS = [
    "Toán", "Vật Lý", "Hóa Học", "Sinh Học",
    "Ngữ Văn", "Lịch Sử", "Địa Lý", "Tiếng Anh",
    "Tin Học", "GDCD / KTPL", "Khác"
]

SUBJECT_EMOJI = {
    "Toán": "🧮", "Vật Lý": "⚛️", "Hóa Học": "🧪", "Sinh Học": "🧬",
    "Ngữ Văn": "📖", "Lịch Sử": "🏛️", "Địa Lý": "🌍", "Tiếng Anh": "🇬🇧",
    "Tin Học": "💻", "GDCD / KTPL": "⚖️", "Khác": "📦",
}

FILE_TYPE_META = {
    "document": ("📄", "Tài liệu"),
    "photo": ("🖼️", "Ảnh"),
    "video": ("🎥", "Video"),
    "audio": ("🎵", "Audio"),
    "voice": ("🎙️", "Voice"),
    "canva": ("🎨", "Canva"),
    "link": ("🔗", "Link"),
}

MAX_SEND_RESULTS = 30


# ==============================================================================
# 2. XỬ LÝ CƠ SỞ DỮ LIỆU (SQLITE)
# ==============================================================================
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Lỗi Database: {e}")
        raise e
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                subject TEXT NOT NULL,
                topic TEXT NOT NULL,
                topic_clean TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_topic_clean ON documents(topic_clean)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_subject ON documents(subject)")


init_db()


# ==============================================================================
# 3. HÀM BỔ TRỢ (HELPERS)
# ==============================================================================
async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    chat = update.effective_chat
    if chat.type == "private":
        return True

    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in ["administrator", "creator"]
    except TelegramError:
        return False


def run_search(query_raw: str) -> List[Tuple]:
    q_clean = unidecode(query_raw).lower().strip()
    keywords = [k for k in q_clean.split() if k]

    if not keywords:
        return []

    conditions = ["topic_clean LIKE ?" for _ in keywords]
    where_clause = " AND ".join(conditions)
    params = [f"%{k}%" for k in keywords]

    sql = f"""
        SELECT id, user_name, subject, topic, file_type, file_id, message_id, chat_id
        FROM documents
        WHERE {where_clause}
        ORDER BY id DESC
    """

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return cursor.fetchall()


# ==============================================================================
# 4. LỆNH CƠ BẢN (/start, /help, /stats)
# ==============================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Xin chào! Tôi là Bot Lưu Trữ & Tìm Kiếm Tài Liệu.**\n\n"
        "📥 **Cách upload:** Gửi/chuyển tiếp File, Ảnh, Video, Audio, Voice hoặc bất kỳ link nào "
        "(Canva, Drive, YouTube...) vào đây.\n"
        "📸 *Gửi NHIỀU ảnh/file cùng lúc (dạng album)? Bot chỉ hỏi chủ đề 1 LẦN cho cả lô!*\n"
        "🔁 *Upload nhiều lần cùng chủ đề? Bot sẽ tự gợi ý dùng lại, khỏi gõ lại nhiều lần!*\n\n"
        "🔍 **Cách tìm kiếm:** Dùng lệnh `/tim <từ khóa>` (VD: `/tim de thi giua ky`) — "
        "bot gửi luôn TOÀN BỘ ảnh/file liên quan.\n"
        "📜 Gõ `/help` để xem danh sách đầy đủ các lệnh.",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **DANH SÁCH LỆNH HỖ TRỢ**\n\n"
        "📂 **Lưu trữ & Tìm kiếm:**\n"
        "• Gửi File/Ảnh/Video/Audio/Voice/Link bất kỳ: Tải tài liệu lên kho\n"
        "• `/tim <từ khóa>`: Tìm & gửi HẾT ảnh/file liên quan ngay lập tức\n"
        "• `/list [tên môn]`: Xem danh sách tài liệu (hoặc lọc theo môn)\n"
        "• `/mytai`: Xem danh sách tài liệu do bạn đã tải lên\n\n"
        "🛠️ **Quản lý tài liệu:**\n"
        "• `/sua <ID> <Tên mới>`: Sửa tên/chủ đề của tài liệu\n"
        "• `/xoa <ID>`: Xóa tài liệu theo ID\n\n"
        "📊 **Khác:**\n"
        "• `/stats`: Xem thống kê kho tài liệu\n"
        "• `/cancel`: Hủy thao tác hiện tại\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM documents")
        total_docs = cursor.fetchone()[0]

        cursor.execute("SELECT subject, COUNT(*) FROM documents GROUP BY subject ORDER BY COUNT(*) DESC")
        by_subject = cursor.fetchall()

    text = f"📊 **THỐNG KÊ KHO TÀI LIỆU**\n\n🔹 Tổng số tài liệu: **{total_docs}**\n\n"
    for sub, cnt in by_subject:
        emoji = SUBJECT_EMOJI.get(sub, "📁")
        text += f"{emoji} **{sub}:** {cnt} file\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# ==============================================================================
# 5. QUY TRÌNH UPLOAD TÀI LIỆU (CONVERSATION HANDLER)
# ==============================================================================
def _build_subject_keyboard(prefix: str = "sub_") -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for idx, sub in enumerate(SUBJECTS, start=1):
        emoji = SUBJECT_EMOJI.get(sub, "📁")
        row.append(InlineKeyboardButton(f"{emoji} {sub}", callback_data=f"{prefix}{sub}"))
        if idx % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def _clear_upload_temp(user_data: dict) -> None:
    for key in ("upload_file_type", "upload_file_id", "upload_msg_id",
                "upload_caption", "upload_subject"):
        user_data.pop(key, None)


async def _finish_upload(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, subject: str, topic: str):
    topic_clean = unidecode(topic).lower()
    user_name = user.first_name or user.username or "Người dùng"

    file_type = context.user_data.get("upload_file_type")
    file_id = context.user_data.get("upload_file_id")
    msg_id = context.user_data.get("upload_msg_id")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO documents (message_id, chat_id, user_id, user_name, subject, topic, topic_clean, file_type, file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (msg_id, chat_id, user.id, user_name, subject, topic, topic_clean, file_type, file_id))
        doc_id = cursor.lastrowid

    context.user_data["last_subject"] = subject
    context.user_data["last_topic"] = topic
    context.user_data["last_topic_time"] = time.time()

    emoji = SUBJECT_EMOJI.get(subject, "📁")
    text = (
        f"🎉 **ĐÃ LƯU TÀI LIỆU THÀNH CÔNG!**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 **ID:** `{doc_id}`\n"
        f"{emoji} **Môn:** {subject}\n"
        f"🏷️ **Chủ đề:** {topic}\n"
        f"👤 **Người đăng:** {user_name}\n"
        f"━━━━━━━━━━━━━━━"
    )
    return doc_id, text


async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    file_type = None
    file_id = None

    if message.document:
        file_type = "document"
        file_id = message.document.file_id
    elif message.photo:
        file_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        file_type = "video"
        file_id = message.video.file_id
    elif message.audio:
        file_type = "audio"
        file_id = message.audio.file_id
    elif message.voice:
        file_type = "voice"
        file_id = message.voice.file_id
    elif message.text and re.search(r'https?://[^\s]+', message.text):
        file_type = "canva" if "canva.com" in message.text else "link"
        match = re.search(r'https?://[^\s]+', message.text)
        file_id = match.group(0) if match else message.text
    else:
        await message.reply_text("⚠️ Định dạng không được hỗ trợ!")
        return ConversationHandler.END

    context.user_data["upload_file_type"] = file_type
    context.user_data["upload_file_id"] = file_id
    context.user_data["upload_msg_id"] = message.message_id
    context.user_data["upload_caption"] = message.caption or ""

    last_subject = context.user_data.get("last_subject")
    last_topic = context.user_data.get("last_topic")
    last_time = context.user_data.get("last_topic_time")

    if last_subject and last_topic and last_time and (time.time() - last_time) <= REUSE_TOPIC_WINDOW:
        emoji = SUBJECT_EMOJI.get(last_subject, "📁")
        label = f"✅ Dùng lại: {last_subject} — {last_topic}"
        if len(label) > 60:
            label = label[:57] + "..."

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data="reuse_yes")],
            [InlineKeyboardButton("🆕 Chọn chủ đề khác", callback_data="reuse_no")],
        ])
        await message.reply_text(
            "📥 **Đã nhận file mới!**\n\n"
            f"💡 Bạn vừa lưu {emoji} **{last_subject} — {last_topic}** gần đây.\n"
            "Dùng lại chủ đề này luôn không?",
            reply_markup=buttons,
            parse_mode="Markdown"
        )
        return CONFIRM_REUSE

    await message.reply_text(
        "📚 **Bước 1/2:** Chọn **Môn Học** cho tài liệu này:",
        reply_markup=_build_subject_keyboard(),
        parse_mode="Markdown"
    )
    return SELECT_SUBJECT


async def confirm_reuse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "reuse_yes":
        subject = context.user_data.get("last_subject")
        topic = context.user_data.get("last_topic")
        chat_id = update.effective_chat.id

        doc_id, text = await _finish_upload(context, chat_id, query.from_user, subject, topic)
        await query.edit_message_text(text, parse_mode="Markdown")

        _clear_upload_temp(context.user_data)
        return ConversationHandler.END

    await query.edit_message_text(
        "📚 **Bước 1/2:** Chọn **Môn Học** cho tài liệu này:",
        reply_markup=_build_subject_keyboard(),
        parse_mode="Markdown"
    )
    return SELECT_SUBJECT


async def subject_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    selected_sub = query.data.replace("sub_", "")
    context.user_data["upload_subject"] = selected_sub
    emoji = SUBJECT_EMOJI.get(selected_sub, "📁")

    caption = context.user_data.get("upload_caption", "")
    prompt_text = (
        f"✅ Môn học: {emoji} **{selected_sub}**\n\n"
        f"🏷️ **Bước 2/2:** Nhập **Tên/Chủ đề** cho tài liệu này (VD: *Đề thi học kỳ 1 2024*):"
    )

    if caption:
        prompt_text += f"\n\n💡 *Gợi ý (từ chú thích):* `{caption}`"

    await query.edit_message_text(prompt_text, parse_mode="Markdown")
    return INPUT_TOPIC


async def save_material(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    topic = update.message.text.strip()
    subject = context.user_data.get("upload_subject")
    chat_id = update.effective_chat.id

    doc_id, text = await _finish_upload(context, chat_id, update.message.from_user, subject, topic)
    await update.message.reply_text(text, parse_mode="Markdown")

    _clear_upload_temp(context.user_data)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_upload_temp(context.user_data)
    await update.message.reply_text("❌ Đã hủy thao tác lưu tài liệu.")
    return ConversationHandler.END


# ==============================================================================
# 5B. UPLOAD THEO LÔ (BATCH ALBUM UPLOAD)
# ==============================================================================
ALBUM_COLLECT_DELAY = 1.5

async def route_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.media_group_id:
        return

    if message.document:
        file_type, file_id = "document", message.document.file_id
    elif message.photo:
        file_type, file_id = "photo", message.photo[-1].file_id
    elif message.video:
        file_type, file_id = "video", message.video.file_id
    elif message.audio:
        file_type, file_id = "audio", message.audio.file_id
    else:
        return

    mgid = message.media_group_id
    buffers = context.chat_data.setdefault("album_buffers", {})
    entry = buffers.setdefault(mgid, {"items": [], "user_id": message.from_user.id})
    entry["items"].append({
        "file_type": file_type,
        "file_id": file_id,
        "message_id": message.message_id,
    })

    job_name = f"album_{message.chat_id}_{mgid}"
    if context.job_queue:
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
        context.job_queue.run_once(
            process_album_job,
            when=ALBUM_COLLECT_DELAY,
            chat_id=message.chat_id,
            user_id=message.from_user.id,
            name=job_name,
            data={"media_group_id": mgid},
        )

    raise ApplicationHandlerStop


async def process_album_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    mgid = job.data["media_group_id"]

    buffers = context.chat_data.get("album_buffers", {})
    entry = buffers.pop(mgid, None)
    if not entry or not entry["items"]:
        return

    items = entry["items"]
    count = len(items)

    context.user_data["batch_items"] = items
    context.user_data["batch_stage"] = "select_subject"

    last_subject = context.user_data.get("last_subject")
    last_topic = context.user_data.get("last_topic")
    last_time = context.user_data.get("last_topic_time")

    if last_subject and last_topic and last_time and (time.time() - last_time) <= REUSE_TOPIC_WINDOW:
        emoji = SUBJECT_EMOJI.get(last_subject, "📁")
        label = f"✅ Dùng lại: {last_subject} — {last_topic}"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data="batchreuse_yes")],
            [InlineKeyboardButton("🆕 Chọn chủ đề khác", callback_data="batchreuse_no")],
        ])
        await context.bot.send_message(
            job.chat_id,
            f"📥 **Đã nhận {count} ảnh/file cùng lúc!**\n\n"
            f"💡 Bạn vừa lưu {emoji} **{last_subject} — {last_topic}** gần đây.\n"
            f"Dùng chủ đề này cho cả **{count} ảnh/file** luôn không?",
            reply_markup=buttons,
            parse_mode="Markdown",
        )
        return

    await context.bot.send_message(
        job.chat_id,
        f"📥 **Đã nhận {count} ảnh/file cùng lúc!**\n\n"
        f"📚 **Bước 1/2:** Chọn **1 Môn Học chung** cho toàn bộ {count} ảnh/file này:",
        reply_markup=_build_subject_keyboard(prefix="batchsub_"),
        parse_mode="Markdown",
    )


async def batch_reuse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    items = context.user_data.get("batch_items")
    if not items:
        await query.edit_message_text("⚠️ Phiên đã hết hạn, vui lòng gửi lại ảnh/file.")
        return

    if query.data == "batchreuse_yes":
        subject = context.user_data.get("last_subject")
        topic = context.user_data.get("last_topic")
        doc_ids = await _finish_batch_upload(context, update.effective_chat.id, query.from_user, subject, topic, items)

        await query.edit_message_text(_batch_success_text(subject, topic, doc_ids), parse_mode="Markdown")
        context.user_data.pop("batch_items", None)
        context.user_data.pop("batch_stage", None)
        return

    count = len(items)
    context.user_data["batch_stage"] = "select_subject"
    await query.edit_message_text(
        f"📚 **Bước 1/2:** Chọn **1 Môn Học chung** cho toàn bộ {count} ảnh/file này:",
        reply_markup=_build_subject_keyboard(prefix="batchsub_"),
        parse_mode="Markdown",
    )


async def batch_subject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if context.user_data.get("batch_stage") != "select_subject" or not context.user_data.get("batch_items"):
        await query.answer("⚠️ Phiên đã hết hạn, vui lòng gửi lại ảnh/file.", show_alert=True)
        return

    await query.answer()
    subject = query.data.replace("batchsub_", "")
    count = len(context.user_data["batch_items"])
    emoji = SUBJECT_EMOJI.get(subject, "📁")

    context.user_data["batch_subject"] = subject
    context.user_data["batch_stage"] = "input_topic"

    await query.edit_message_text(
        f"✅ Môn học: {emoji} **{subject}**\n\n"
        f"🏷️ **Bước 2/2:** Nhập **1 Tên/Chủ đề chung** cho cả {count} ảnh/file này:",
        parse_mode="Markdown",
    )


async def batch_topic_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("batch_stage") != "input_topic":
        return

    items = context.user_data.get("batch_items")
    subject = context.user_data.get("batch_subject")
    if not items or not subject:
        context.user_data.pop("batch_stage", None)
        return

    topic = update.message.text.strip()
    doc_ids = await _finish_batch_upload(context, update.effective_chat.id, update.message.from_user, subject, topic, items)

    await update.message.reply_text(_batch_success_text(subject, topic, doc_ids), parse_mode="Markdown")

    context.user_data.pop("batch_items", None)
    context.user_data.pop("batch_subject", None)
    context.user_data.pop("batch_stage", None)

    raise ApplicationHandlerStop


async def _finish_batch_upload(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, subject: str, topic: str, items: list) -> list:
    topic_clean = unidecode(topic).lower()
    user_name = user.first_name or user.username or "Người dùng"
    doc_ids = []

    with get_db() as conn:
        cursor = conn.cursor()
        for item in items:
            cursor.execute("""
                INSERT INTO documents (message_id, chat_id, user_id, user_name, subject, topic, topic_clean, file_type, file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (item["message_id"], chat_id, user.id, user_name, subject, topic, topic_clean, item["file_type"], item["file_id"]))
            doc_ids.append(cursor.lastrowid)

    context.user_data["last_subject"] = subject
    context.user_data["last_topic"] = topic
    context.user_data["last_topic_time"] = time.time()

    return doc_ids


def _batch_success_text(subject: str, topic: str, doc_ids: list) -> str:
    emoji = SUBJECT_EMOJI.get(subject, "📁")
    id_range = f"{doc_ids[0]}–{doc_ids[-1]}" if len(doc_ids) > 1 else str(doc_ids[0])
    return (
        f"🎉 **ĐÃ LƯU {len(doc_ids)} FILE THÀNH CÔNG!**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 **ID:** `{id_range}`\n"
        f"{emoji} **Môn:** {subject}\n"
        f"🏷️ **Chủ đề:** {topic}\n"
        f"━━━━━━━━━━━━━━━"
    )


# ==============================================================================
# 6. TÌM KIẾM TÀI LIỆU (/tim)
# ==============================================================================
def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _build_summary_text(query_raw: str, results: List[Tuple], sending_count: int) -> str:
    total = len(results)
    by_subject = {}
    for doc in results:
        subject = doc[2]
        by_subject[subject] = by_subject.get(subject, 0) + 1

    lines = [
        f"🔍 **KẾT QUẢ CHO** `{query_raw}`",
        f"📦 Tìm thấy **{total}** tài liệu liên quan:\n",
    ]
    for subject, count in sorted(by_subject.items(), key=lambda x: -x[1]):
        emoji = SUBJECT_EMOJI.get(subject, "📁")
        lines.append(f"{emoji} **{subject}:** {count} tài liệu")

    lines.append("\n📤 Đang gửi toàn bộ ảnh/file liên quan bên dưới...")
    if sending_count < total:
        lines.append(
            f"\n⚠️ *Chỉ gửi {sending_count}/{total} kết quả đầu (giới hạn chống spam).*"
        )

    return "\n".join(lines)


async def search_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Cú pháp: `/tim <từ khóa>` (Ví dụ: `/tim hoa 12`)", parse_mode="Markdown")
        return

    query_raw = " ".join(context.args).strip()
    results = run_search(query_raw)

    if not results:
        await update.message.reply_text(f"🔍 Không tìm thấy tài liệu nào khớp với từ khóa: `{query_raw}`")
        return

    chat_id = update.effective_chat.id
    send_list = results[:MAX_SEND_RESULTS]

    await update.message.reply_text(
        _build_summary_text(query_raw, results, len(send_list)),
        parse_mode="Markdown"
    )

    photos, others, link_items = [], [], []

    for doc in send_list:
        file_type = doc[4]
        if file_type == "photo":
            photos.append(doc)
        elif file_type in ("canva", "link"):
            link_items.append(doc)
        else:
            others.append(doc)

    failed = 0

    for group in _chunked(photos, 10):
        media_group = []
        for doc in group:
            doc_id, user_name, subject, topic, file_type, file_id, msg_id, doc_chat_id = doc
            emoji = SUBJECT_EMOJI.get(subject, "📁")
            caption = f"{emoji} #{doc_id} • {subject}\n🏷️ {topic}"
            media_group.append(InputMediaPhoto(media=file_id, caption=caption))
        try:
            await context.bot.send_media_group(chat_id, media_group)
        except TelegramError as e:
            failed += len(group)
            logger.warning(f"Không thể gửi album ảnh: {e}")

    for doc in others:
        doc_id, user_name, subject, topic, file_type, file_id, msg_id, doc_chat_id = doc
        emoji, label = FILE_TYPE_META.get(file_type, ("📎", "File"))
        subj_emoji = SUBJECT_EMOJI.get(subject, "📁")
        caption = f"{emoji} **{label} #{doc_id}**\n{subj_emoji} {subject} • {topic}\n👤 {user_name}"
        try:
            if file_type == "document":
                await context.bot.send_document(chat_id, file_id, caption=caption, parse_mode="Markdown")
            elif file_type == "video":
                await context.bot.send_video(chat_id, file_id, caption=caption, parse_mode="Markdown")
            elif file_type == "audio":
                await context.bot.send_audio(chat_id, file_id, caption=caption, parse_mode="Markdown")
            elif file_type == "voice":
                await context.bot.send_voice(chat_id, file_id, caption=caption, parse_mode="Markdown")
        except TelegramError as e:
            failed += 1
            logger.warning(f"Không thể gửi file ID {doc_id}: {e}")

    if link_items:
        lines = ["🔗 **LINK LIÊN QUAN:**\n"]
        for doc in link_items:
            doc_id, user_name, subject, topic, file_type, file_id, msg_id, doc_chat_id = doc
            type_emoji, _ = FILE_TYPE_META.get(file_type, ("🔗", "Link"))
            subj_emoji = SUBJECT_EMOJI.get(subject, "📁")
            lines.append(f"{type_emoji} #{doc_id} • {subj_emoji} {subject} • {topic}\n{file_id}\n")
        try:
            await context.bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)
        except TelegramError as e:
            failed += len(link_items)
            logger.warning(f"Không thể gửi danh sách link: {e}")

    if failed:
        await update.message.reply_text(f"⚠️ Có {failed} file gửi không thành công.")


# ==============================================================================
# 7. QUẢN LÝ TÀI LIỆU (/list, /mytai, /sua, /xoa)
# ==============================================================================
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filter_sub = " ".join(context.args).strip() if context.args else None

    with get_db() as conn:
        cursor = conn.cursor()
        if filter_sub:
            cursor.execute(
                "SELECT id, subject, topic, user_name FROM documents WHERE subject LIKE ? ORDER BY id DESC LIMIT 20",
                (f"%{filter_sub}%",)
            )
        else:
            cursor.execute("SELECT id, subject, topic, user_name FROM documents ORDER BY id DESC LIMIT 20")
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("📭 Không có tài liệu nào trong danh sách.")
        return

    text = f"📜 **DANH SÁCH TÀI LIỆU MỚI NHẤT** {f'({filter_sub})' if filter_sub else ''}:\n\n"
    for doc_id, subject, topic, user_name in rows:
        emoji = SUBJECT_EMOJI.get(subject, "📁")
        text += f"{emoji} `#{doc_id}` **[{subject}]** {topic} *(bởi {user_name})*\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def my_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, subject, topic FROM documents WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,))
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("📭 Bạn chưa tải lên tài liệu nào.")
        return

    text = "📂 **TÀI LIỆU CỦA BẠN:**\n\n"
    for doc_id, subject, topic in rows:
        emoji = SUBJECT_EMOJI.get(subject, "📁")
        text += f"{emoji} `#{doc_id}` **[{subject}]** {topic}\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def edit_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Cú pháp: `/sua <ID> <Tên/Chủ đề mới>`", parse_mode="Markdown")
        return

    try:
        doc_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID phải là một số nguyên!")
        return

    new_topic = " ".join(context.args[1:]).strip()
    new_topic_clean = unidecode(new_topic).lower()
    user_id = update.effective_user.id

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM documents WHERE id = ?", (doc_id,))
        row = cursor.fetchone()

        if not row:
            await update.message.reply_text("❌ Không tìm thấy tài liệu có ID này.")
            return

        is_admin = await is_group_admin(update, context, user_id)
        if row[0] != user_id and not is_admin:
            await update.message.reply_text("⛔ Bạn không có quyền sửa tài liệu của người khác.")
            return

        cursor.execute("UPDATE documents SET topic = ?, topic_clean = ? WHERE id = ?", (new_topic, new_topic_clean, doc_id))

    await update.message.reply_text(f"✅ Đã cập nhật tên mới cho ID `#{doc_id}`: **{new_topic}**", parse_mode="Markdown")


async def delete_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Cú pháp: `/xoa <ID>`", parse_mode="Markdown")
        return

    try:
        doc_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID phải là một số nguyên!")
        return

    user_id = update.effective_user.id

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, topic FROM documents WHERE id = ?", (doc_id,))
        row = cursor.fetchone()

        if not row:
            await update.message.reply_text("❌ Không tìm thấy tài liệu có ID này.")
            return

        is_admin = await is_group_admin(update, context, user_id)
        if row[0] != user_id and not is_admin:
            await update.message.reply_text("⛔ Bạn không có quyền xóa tài liệu này.")
            return

        cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

    await update.message.reply_text(f"🗑️ Đã xóa thành công tài liệu `#{doc_id}` (**{row[1]}**).", parse_mode="Markdown")


# ==============================================================================
# 8. KHỞI CHẠY BOT & REGISTER HANDLERS
# ==============================================================================
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ghi log lỗi handler để một update lỗi không làm chết tiến trình."""
    logger.error("Lỗi khi xử lý update %r: %s", update, context.error, exc_info=(type(context.error), context.error, context.error.__traceback__) if context.error else None)


def build_application():
    persistence = PicklePersistence(filepath=PERSISTENCE_NAME, store_data=True)
    app = (ApplicationBuilder()
           .token(BOT_TOKEN)
           .persistence(persistence)
           .connect_timeout(30)
           .read_timeout(30)
           .write_timeout(30)
           .pool_timeout(30)
           .build())

    # Handlers cho Album/Media Group (xử lý ưu tiên trước với group=-1)
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO, route_media_group), group=-1)
    app.add_handler(CallbackQueryHandler(batch_reuse_callback, pattern=r"^batchreuse_"))
    app.add_handler(CallbackQueryHandler(batch_subject_callback, pattern=r"^batchsub_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, batch_topic_message), group=-1)

    upload_conv = ConversationHandler(
        entry_points=[MessageHandler(
            filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Entity("url"),
            start_upload)],
        states={
            CONFIRM_REUSE: [CallbackQueryHandler(confirm_reuse_callback, pattern=r"^reuse_")],
            SELECT_SUBJECT: [CallbackQueryHandler(subject_selected, pattern=r"^sub_")],
            INPUT_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_material)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="upload_conversation",
        persistent=True,
    )
    app.add_handler(upload_conv)

    for command, callback in [
        ("start", start_command), ("help", help_command), ("stats", stats_command),
        ("tim", search_topic), ("list", list_command), ("mytai", my_materials),
        ("sua", edit_material), ("xoa", delete_material),
    ]:
        app.add_handler(CommandHandler(command, callback))

    app.add_error_handler(global_error_handler)
    return app


def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise RuntimeError("Chưa cấu hình BOT_TOKEN. Hãy đặt BOT_TOKEN trong biến môi trường.")

    threading.Thread(target=_run_fake_http_server, name="health-http", daemon=True).start()
    retry_delay = 5
    max_retry_delay = 300

    # PTB tự retry các lỗi polling thông thường. Vòng ngoài này khởi tạo lại app nếu
    # polling kết thúc do lỗi nghiêm trọng; trạng thái hội thoại được lưu bằng persistence.
    while True:
        try:
            logger.info("🤖 Khởi động Telegram bot (polling)...")
            app = build_application()
            app.run_polling(
                poll_interval=1.0,
                timeout=30,
                bootstrap_retries=-1,
                drop_pending_updates=False,
                close_loop=True,
            )
            # run_polling chỉ kết thúc bình thường khi có yêu cầu dừng; thoát vòng lặp.
            logger.info("Bot đã dừng theo yêu cầu.")
            break
        except KeyboardInterrupt:
            logger.info("Nhận yêu cầu dừng từ bàn phím.")
            break
        except Exception:
            logger.exception("Bot gặp lỗi nghiêm trọng; sẽ khởi động lại sau %s giây.", retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_retry_delay)


if __name__ == "__main__":
    main()

