import os
import re
import time
import sqlite3
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
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

# ==============================================================================
# 0. HTTP SERVER GIẢ (chỉ để Render Web Service thấy có cổng mở)
# ==============================================================================
# Render Free plan chỉ hỗ trợ loại service "Web Service" chạy free (loại
# "Background Worker" đúng bản chất cho bot thì tốn phí). Web Service bắt
# buộc phải bind $PORT, nếu không Render sẽ liên tục khởi động lại/spawn
# thêm instance mới trong khi instance cũ (đang polling Telegram) vẫn còn
# sống — gây lỗi "Conflict: terminated by other getUpdates request".
# Server này không phục vụ chức năng gì cho bot, chỉ để Render thấy "còn sống".
def _run_fake_http_server():
    port = int(os.getenv("PORT", "10000"))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot dang chay OK")

        def log_message(self, format, *args):
            pass  # tắt log HTTP để đỡ rác log chính

    try:
        server = HTTPServer(("0.0.0.0", port), _Handler)
        logger.info(f"🌐 Đã mở cổng giả {port} để Render nhận diện Web Service.")
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

# Lấy Token từ biến môi trường hoặc thay trực tiếp token của bạn vào đây
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE").strip()

# ------------------------------------------------------------------------------
# LƯU Ý: Render Free plan có filesystem TẠM THỜI — mỗi lần service restart/deploy
# (kể cả tự sleep rồi wake up), mọi file tạo ra lúc chạy — bao gồm file SQLite
# này — sẽ bị XÓA SẠCH và bot bắt đầu lại với kho tài liệu trống. Đây là lựa
# chọn có chủ đích để khỏi phải cấu hình database ngoài; nếu sau này muốn giữ
# dữ liệu vĩnh viễn, cách khắc phục là dùng PostgreSQL (Render có bản Free
# riêng) hoặc nâng cấp lên gói có Persistent Disk.
# ------------------------------------------------------------------------------
DB_NAME = "documents.db"

# Các trạng thái của ConversationHandler khi Upload
SELECT_SUBJECT, INPUT_TOPIC, CONFIRM_REUSE = range(3)

# Thời gian (giây) mà bot còn "nhớ" chủ đề vừa lưu để gợi ý dùng lại
# cho các file upload liên tiếp — tránh phải gõ lại chủ đề nhiều lần
REUSE_TOPIC_WINDOW = 15 * 60  # 15 phút

# Danh sách các môn học hỗ trợ
SUBJECTS = [
    "Toán", "Vật Lý", "Hóa Học", "Sinh Học",
    "Ngữ Văn", "Lịch Sử", "Địa Lý", "Tiếng Anh",
    "Tin Học", "GDCD / KTPL", "Khác"
]

# Emoji riêng cho từng môn học, dùng để tin nhắn trực quan hơn
SUBJECT_EMOJI = {
    "Toán": "🧮", "Vật Lý": "⚛️", "Hóa Học": "🧪", "Sinh Học": "🧬",
    "Ngữ Văn": "📖", "Lịch Sử": "🏛️", "Địa Lý": "🌍", "Tiếng Anh": "🇬🇧",
    "Tin Học": "💻", "GDCD / KTPL": "⚖️", "Khác": "📦",
}

# Emoji + tên hiển thị riêng cho từng loại file
FILE_TYPE_META = {
    "document": ("📄", "Tài liệu"),
    "photo": ("🖼️", "Ảnh"),
    "video": ("🎥", "Video"),
    "audio": ("🎵", "Audio"),
    "voice": ("🎙️", "Voice"),
    "canva": ("🎨", "Canva"),
    "link": ("🔗", "Link"),
}

# Giới hạn số kết quả gửi file trực tiếp trong 1 lần /tim, tránh spam chat
MAX_SEND_RESULTS = 30


# ==============================================================================
# 2. XỬ LÝ CƠ SỞ DỮ LIỆU (SQLITE)
# ==============================================================================
@contextmanager
def get_db():
    """Context manager giúp mở và đóng kết nối DB an toàn."""
    conn = sqlite3.connect(DB_NAME)
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
    """Khởi tạo bảng cơ sở dữ liệu và chỉ mục nếu chưa tồn tại."""
    with get_db() as conn:
        cursor = conn.cursor()
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


# Khởi tạo DB ngay khi khởi động
init_db()


# ==============================================================================
# 3. HÀM BỔ TRỢ (HELPERS)
# ==============================================================================
async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Kiểm tra xem người dùng có phải là Admin/Creator của nhóm hay không."""
    chat = update.effective_chat
    if chat.type == "private":
        return True

    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in ["administrator", "creator"]
    except TelegramError:
        return False


def run_search(query_raw: str) -> List[Tuple]:
    """Tìm kiếm tài liệu trong DB dựa trên từ khóa không dấu."""
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
# 4. LỆNH CƠ BẢN (/start, /help)
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
        "• `/xoa <từ khóa>`: Tìm và chọn tài liệu cần xóa\n\n"
        "📊 **Khác:**\n"
        "• `/stats`: Xem thống kê kho tài liệu\n"
        "• `/cancel`: Hủy thao tác hiện tại\n\n"
        "💡 **Mẹo:**\n"
        "• Gửi nhiều ảnh/file cùng lúc (album) → bot chỉ hỏi chủ đề **1 lần** cho cả lô.\n"
        "• Upload liên tiếp cùng chủ đề → bot tự nhớ và gợi ý \"Dùng lại chủ đề vừa rồi?\" — bấm 1 nút là xong!"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


# ==============================================================================
# 5. QUY TRÌNH UPLOAD TÀI LIỆU (CONVERSATION HANDLER)
# ==============================================================================
def _build_subject_keyboard(prefix: str = "sub_") -> InlineKeyboardMarkup:
    """Tạo bàn phím chọn môn học, có emoji cho trực quan.
    `prefix` khác nhau giữa flow upload đơn ('sub_') và flow upload theo lô ('batchsub_')."""
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
    """Xóa dữ liệu tạm của lượt upload hiện tại, KHÔNG xóa 'last_subject/last_topic'
    vì những key đó dùng để gợi ý dùng lại chủ đề cho lần upload tiếp theo."""
    for key in ("upload_file_type", "upload_file_id", "upload_msg_id",
                "upload_caption", "upload_subject"):
        user_data.pop(key, None)


async def _finish_upload(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, subject: str, topic: str):
    """Lưu tài liệu vào DB, ghi nhớ chủ đề vừa dùng, và trả về (doc_id, text thông báo đẹp)."""
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

    # Ghi nhớ chủ đề vừa lưu để gợi ý "dùng lại" cho file tiếp theo
    context.user_data["last_subject"] = subject
    context.user_data["last_topic"] = topic
    context.user_data["last_topic_time"] = time.time()

    emoji = SUBJECT_EMOJI.get(subject, "📁")
    minutes = REUSE_TOPIC_WINDOW // 60
    text = (
        f"🎉 **ĐÃ LƯU TÀI LIỆU THÀNH CÔNG!**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 **ID:** `{doc_id}`\n"
        f"{emoji} **Môn:** {subject}\n"
        f"🏷️ **Chủ đề:** {topic}\n"
        f"👤 **Người đăng:** {user_name}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💡 *Gửi file tiếp theo trong {minutes} phút, bot sẽ gợi ý dùng lại chủ đề này ngay!*"
    )
    return doc_id, text


async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Nhận file/link và khởi động quy trình upload."""
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
        # Hỗ trợ link bất kỳ (Canva được nhận diện riêng để hiển thị emoji 🎨,
        # các link khác — Drive, YouTube, web... — vẫn được lưu bình thường)
        file_type = "canva" if "canva.com" in message.text else "link"
        match = re.search(r'https?://[^\s]+', message.text)
        file_id = match.group(0) if match else message.text
    else:
        await message.reply_text("⚠️ Định dạng không được hỗ trợ!")
        return ConversationHandler.END

    # Lưu tạm thông tin file vào user_data
    context.user_data["upload_file_type"] = file_type
    context.user_data["upload_file_id"] = file_id
    context.user_data["upload_msg_id"] = message.message_id
    context.user_data["upload_caption"] = message.caption or ""

    # Nếu vừa lưu 1 chủ đề gần đây (trong khoảng REUSE_TOPIC_WINDOW),
    # gợi ý dùng lại luôn để tránh gõ lại nhiều lần
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
    """Xử lý khi người dùng bấm 'Dùng lại chủ đề' hoặc 'Chọn chủ đề khác'."""
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

    # reuse_no → hiện bàn phím chọn môn học như bình thường
    await query.edit_message_text(
        "📚 **Bước 1/2:** Chọn **Môn Học** cho tài liệu này:",
        reply_markup=_build_subject_keyboard(),
        parse_mode="Markdown"
    )
    return SELECT_SUBJECT


async def subject_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Xử lý sau khi người dùng chọn môn học."""
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
    """Lưu thông tin tài liệu vào Cơ sở dữ liệu."""
    topic = update.message.text.strip()
    subject = context.user_data.get("upload_subject")
    chat_id = update.effective_chat.id

    doc_id, text = await _finish_upload(context, chat_id, update.message.from_user, subject, topic)
    await update.message.reply_text(text, parse_mode="Markdown")

    _clear_upload_temp(context.user_data)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Hủy thao tác upload (vẫn giữ 'nhớ chủ đề gần nhất' cho lần sau)."""
    _clear_upload_temp(context.user_data)
    await update.message.reply_text("❌ Đã hủy thao tác lưu tài liệu.")
    return ConversationHandler.END


# ==============================================================================
# 5B. UPLOAD THEO LÔ (gửi nhiều ảnh/file cùng lúc — hỏi chủ đề 1 LẦN DUY NHẤT)
# ==============================================================================
ALBUM_COLLECT_DELAY = 1.5


async def route_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Chạy TRƯỚC ConversationHandler (group=-1). Nếu tin nhắn là 1 phần của album
    (nhiều ảnh/file gửi cùng lúc, có chung media_group_id), gom vào bộ đệm và
    chặn không cho ConversationHandler xử lý riêng từng ảnh (tránh hỏi chủ đề
    lặp lại nhiều lần). Nếu KHÔNG phải album, bỏ qua để flow upload đơn lẻ
    (start_upload) xử lý như bình thường.
    """
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
    """Chạy sau khi đã gom đủ các phần của 1 album — hỏi chủ đề 1 LẦN cho cả lô."""
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
    """Xử lý khi bấm 'Dùng lại chủ đề' / 'Chọn chủ đề khác' cho cả lô ảnh."""
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
    """Xử lý khi chọn môn học cho cả lô ảnh."""
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
    """Nhận tên chủ đề chung, lưu TOÀN BỘ ảnh/file trong lô cùng lúc."""
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
    """Lưu TOÀN BỘ file trong 1 lô vào DB cùng 1 chủ đề, trả về danh sách ID đã tạo."""
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
    """Tạo tin nhắn xác nhận đẹp sau khi lưu cả lô ảnh/file."""
    emoji = SUBJECT_EMOJI.get(subject, "📁")
    minutes = REUSE_TOPIC_WINDOW // 60
    id_range = f"{doc_ids[0]}–{doc_ids[-1]}" if len(doc_ids) > 1 else str(doc_ids[0])
    return (
        f"🎉 **ĐÃ LƯU {len(doc_ids)} FILE THÀNH CÔNG!**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 **ID:** `{id_range}`\n"
        f"{emoji} **Môn:** {subject}\n"
        f"🏷️ **Chủ đề:** {topic}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💡 *File tiếp theo trong {minutes} phút, bot sẽ gợi ý dùng lại chủ đề này ngay!*"
    )


# ==============================================================================
# 6. TÌM KIẾM TÀI LIỆU (/tim) — gửi HẾT tất cả ảnh & file liên quan
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
            f"\n⚠️ *Chỉ gửi {sending_count}/{total} kết quả đầu (giới hạn chống spam). "
            f"Hãy thu hẹp từ khóa để tìm chính xác hơn.*"
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

    photos = []
    others = []
    link_items = []

    for doc in send_list:
        doc_id, user_name, subject, topic, file_type, file_id, msg_id, doc_chat_id = doc
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
        await update.message.reply_text(f"⚠️ Có {failed} file gửi không thành công (có thể đã bị Telegram thu hồi).")


# ==============================================================================
# 7. DANH SÁCH & TÀI LIỆU CỦA TÔI (/list, /mytai)
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
        cursor.execute("SELECT id, subject, topic FROM documents WHERE user_id = ? ORDER BY id DESC", (user_id,))
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("📭 Bạn chưa đăng tải tài liệu nào.")
        return

    text = "📂 **TÀI LIỆU CỦA BẠN:**\n\n"
    for doc_id, subject, topic in rows:
        emoji = SUBJECT_EMOJI.get(subject, "📁")
        text += f"{emoji} `#{doc_id}` **[{subject}]** {topic}\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# ==============================================================================
# 8. SỬA TÀI LIỆU (/sua)
# ==============================================================================
async def edit_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ **Cú pháp chưa đúng!**\nHãy gõ: `/sua <ID> <Tên chủ đề mới>`\nVí dụ: `/sua 5 Đề thi giữa kỳ môn Toán`",
            parse_mode="Markdown"
        )
        return

    doc_id_str = context.args[0]
    new_topic = " ".join(context.args[1:]).strip()

    if not doc_id_str.isdigit():
        await update.message.reply_text("⚠️ ID tài liệu phải là một số nguyên.")
        return

    doc_id = int(doc_id_str)
    user_id = update.effective_user.id
    is_admin = await is_group_admin(update, context, user_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, subject FROM documents WHERE id = ?", (doc_id,))
        row = cursor.fetchone()

        if not row:
            await update.message.reply_text(f"❌ Không tìm thấy tài liệu với ID `{doc_id}`.", parse_mode="Markdown")
            return

        owner_id, subject = row
        if owner_id != user_id and not is_admin:
            await update.message.reply_text("⛔ Bạn không có quyền sửa tên tài liệu này (Chỉ chủ sở hữu hoặc Admin).")
            return

        new_topic_clean = unidecode(new_topic).lower()
        cursor.execute(
            "UPDATE documents SET topic = ?, topic_clean = ? WHERE id = ?",
            (new_topic, new_topic_clean, doc_id)
        )

    await update.message.reply_text(
        f"✅ **Đã cập nhật thành công ID {doc_id}!**\n"
        f"📚 Môn: {subject}\n"
        f"🏷️ Tên mới: `{new_topic}`",
        parse_mode="Markdown"
    )


# ==============================================================================
# 9. XÓA TÀI LIỆU (/xoa)
# ==============================================================================
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ **Cú pháp chưa đúng!**\nHãy gõ: `/xoa <từ khóa>`\nVí dụ: `/xoa dien bien phu`",
            parse_mode="Markdown"
        )
        return

    query_raw = " ".join(context.args).strip()
    results = run_search(query_raw)

    if not results:
        await update.message.reply_text(f"🔍 Không tìm thấy tài liệu nào khớp với từ khóa: `{query_raw}`")
        return

    user_id = update.effective_user.id
    is_admin = await is_group_admin(update, context, user_id)

    filtered_results = []
    with get_db() as conn:
        cursor = conn.cursor()
        for doc in results:
            doc_id = doc[0]
            cursor.execute("SELECT user_id FROM documents WHERE id = ?", (doc_id,))
            owner = cursor.fetchone()
            if owner and (owner[0] == user_id or is_admin):
                filtered_results.append(doc)

    if not filtered_results:
        await update.message.reply_text("⛔ Bạn không có quyền xóa các tài liệu tìm thấy (hoặc bạn không phải Admin).")
        return

    buttons = []
    for doc_id, user_name, subject, topic, _, _, _, _ in filtered_results[:10]:
        btn_text = f"❌ [{subject}] {topic[:20]}"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"delconfirm_{doc_id}")])

    markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        "🗑️ **Chọn tài liệu bạn muốn xóa:**\n*(Chỉ hiển thị tài liệu bạn có quyền xóa)*",
        reply_markup=markup,
        parse_mode="Markdown"
    )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id
    is_admin = await is_group_admin(update, context, user_id)

    if data.startswith("delconfirm_"):
        doc_id = int(data.split("_")[1])
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT topic, subject, user_id FROM documents WHERE id = ?", (doc_id,))
            row = cursor.fetchone()

            if not row:
                await query.edit_message_text("❌ Tài liệu không tồn tại hoặc đã bị xóa trước đó.")
                return

            topic, subject, owner_id = row
            if owner_id != user_id and not is_admin:
                await query.edit_message_text("⛔ Bạn không có quyền xóa tài liệu này.")
                return

            confirm_buttons = [
                [
                    InlineKeyboardButton("⚠️ Có, Xóa ngay", callback_data=f"delyes_{doc_id}"),
                    InlineKeyboardButton("🚫 Hủy", callback_data="delcancel")
                ]
            ]
            await query.edit_message_text(
                f"❓ Bạn có chắc chắn muốn xóa tài liệu:\n"
                f"👉 **[{subject}] {topic}** (ID: {doc_id})?",
                reply_markup=InlineKeyboardMarkup(confirm_buttons),
                parse_mode="Markdown"
            )

    elif data.startswith("delyes_"):
        doc_id = int(data.split("_")[1])
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

        await query.edit_message_text(f"✅ Đã xóa thành công tài liệu ID `{doc_id}`!", parse_mode="Markdown")

    elif data == "delcancel":
        await query.edit_message_text("❌ Đã hủy thao tác xóa.")


# ==============================================================================
# 10. THỐNG KÊ TÀI LIỆU (/stats)
# ==============================================================================
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM documents")
        total_docs = cursor.fetchone()[0]

        cursor.execute("SELECT subject, COUNT(*) FROM documents GROUP BY subject ORDER BY COUNT(*) DESC")
        by_subject = cursor.fetchall()

    if total_docs == 0:
        await update.message.reply_text("📊 Kho tài liệu hiện tại đang trống!")
        return

    lines = [f"📊 **THỐNG KÊ KHO TÀI LIỆU (Tổng: {total_docs} tài liệu)**\n"]
    for subject, count in by_subject:
        emoji = SUBJECT_EMOJI.get(subject, "📁")
        lines.append(f"{emoji} **{subject}:** {count} tài liệu")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ==============================================================================
# 11. HÀM MAIN & ĐĂNG KÝ HANDLERS
# ==============================================================================
async def post_init(application):
    """
    Chạy 1 lần ngay sau khi Application khởi tạo, trước khi bắt đầu polling.
    Chủ động xóa mọi webhook cũ và pending updates để tránh lỗi
    'Conflict: terminated by other getUpdates request' khi có instance
    cũ chưa kịp giải phóng phiên getUpdates với Telegram.
    """
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Đã xóa webhook cũ (nếu có) và bỏ qua các update đang chờ.")
    except TelegramError as e:
        logger.warning(f"⚠️ Không thể xóa webhook cũ: {e}")


def main():
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE" or not BOT_TOKEN:
        logger.error("❌ Vui lòng cấu hình BOT_TOKEN hợp lệ trong file code hoặc môi trường!")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    upload_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE
                | (filters.TEXT & filters.Regex(r'https?://[^\s]+')),
                start_upload
            )
        ],
        states={
            CONFIRM_REUSE: [CallbackQueryHandler(confirm_reuse_callback, pattern=r"^reuse_")],
            SELECT_SUBJECT: [CallbackQueryHandler(subject_selected, pattern=r"^sub_")],
            INPUT_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_material)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("tim", search_topic))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("mytai", my_materials))
    app.add_handler(CommandHandler("sua", edit_topic))
    app.add_handler(CommandHandler("xoa", delete_command))
    app.add_handler(CommandHandler("stats", stats_command))

    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^(delconfirm_|delyes_|delcancel)"))

    app.add_handler(
        MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO, route_media_group),
        group=-1
    )
    app.add_handler(CallbackQueryHandler(batch_reuse_callback, pattern=r"^batchreuse_"), group=-1)
    app.add_handler(CallbackQueryHandler(batch_subject_callback, pattern=r"^batchsub_"), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, batch_topic_message), group=-1)

    app.add_handler(upload_handler)

    # Mở cổng giả trong thread nền TRƯỚC khi polling, để Render nhận cổng
    # ngay từ đầu và không khởi động lại/spawn thêm instance thứ 2.
    threading.Thread(target=_run_fake_http_server, daemon=True).start()

    logger.info("🤖 Bot Lưu Trữ Tài Liệu đã sẵn sàng hoạt động!")
    try:
        app.run_polling(drop_pending_updates=True)
    except TelegramError as e:
        if "Conflict" in str(e):
            logger.error(
                "❌ Có một instance khác của bot đang chạy cùng BOT_TOKEN này "
                "(có thể ở máy khác, service Render khác, hoặc webhook cũ chưa xóa). "
                "Hãy dừng hết các instance khác rồi khởi động lại."
            )
        else:
            raise


if __name__ == "__main__":
    main()
