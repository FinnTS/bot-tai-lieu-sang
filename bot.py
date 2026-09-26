import os
import re
import sqlite3
import logging
from contextlib import contextmanager
from typing import List, Tuple, Optional
from unidecode import unidecode

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# ==============================================================================
# 1. CẤU HÌNH BAN ĐẦU & LOGGING
# ==============================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Lấy Token từ biến môi trường hoặc thay trực tiếp token của bạn vào đây
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
DB_NAME = "documents.db"

# Các trạng thái của ConversationHandler khi Upload
SELECT_SUBJECT, INPUT_TOPIC = range(2)

# Danh sách các môn học hỗ trợ
SUBJECTS = [
    "Toán", "Vật Lý", "Hóa Học", "Sinh Học",
    "Ngữ Văn", "Lịch Sử", "Địa Lý", "Tiếng Anh",
    "Tin Học", "GDCD / KTPL", "Khác"
]


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
        "📥 **Cách upload:** Bạn chỉ cần gửi/chuyển tiếp File, Ảnh, Video, Audio, Voice hoặc link Canva vào đây.\n"
        "🔍 **Cách tìm kiếm:** Dùng lệnh `/tim <từ khóa>` (VD: `/tim de thi giua ky`)\n"
        "📜 Gõ `/help` để xem danh sách đầy đủ các lệnh.",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **DANH SÁCH LỆNH HỖ TRỢ**\n\n"
        "📂 **Lưu trữ & Tìm kiếm:**\n"
        "• Gửi File/Ảnh/Canva link: Tải tài liệu lên kho\n"
        "• `/tim <từ khóa>`: Tìm kiếm tài liệu theo chủ đề/tên\n"
        "• `/list [tên môn]`: Xem danh sách tài liệu (hoặc lọc theo môn)\n"
        "• `/mytai`: Xem danh sách tài liệu do bạn đã tải lên\n\n"
        "🛠️ **Quản lý tài liệu:**\n"
        "• `/sua <ID> <Tên mới>`: Sửa tên/chủ đề của tài liệu\n"
        "• `/xoa <từ khóa>`: Tìm và chọn tài liệu cần xóa\n\n"
        "📊 **Khác:**\n"
        "• `/stats`: Xem thống kê kho tài liệu\n"
        "• `/cancel`: Hủy thao tác hiện tại"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


# ==============================================================================
# 5. QUY TRÌNH UPLOAD TÀI LIỆU (CONVERSATION HANDLER)
# ==============================================================================
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
    elif message.text and "canva.com" in message.text:
        file_type = "canva"
        match = re.search(r'https?://[^\s]*canva\.com[^\s]*', message.text)
        file_id = match.group(0) if match else message.text
    else:
        await message.reply_text("⚠️ Định dạng không được hỗ trợ!")
        return ConversationHandler.END

    # Lưu tạm thông tin file vào user_data
    context.user_data["upload_file_type"] = file_type
    context.user_data["upload_file_id"] = file_id
    context.user_data["upload_msg_id"] = message.message_id
    context.user_data["upload_caption"] = message.caption or ""

    # Tạo bàn phím chọn Môn học
    buttons = []
    row = []
    for idx, sub in enumerate(SUBJECTS, start=1):
        row.append(InlineKeyboardButton(sub, callback_data=f"sub_{sub}"))
        if idx % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await message.reply_text(
        "📚 **Bước 1/2:** Chọn **Môn Học** cho tài liệu này:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return SELECT_SUBJECT


async def subject_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Xử lý sau khi người dùng chọn môn học."""
    query = update.callback_query
    await query.answer()

    selected_sub = query.data.replace("sub_", "")
    context.user_data["upload_subject"] = selected_sub

    caption = context.user_data.get("upload_caption", "")
    prompt_text = (
        f"✅ Môn học: **{selected_sub}**\n\n"
        f"🏷️ **Bước 2/2:** Nhập **Tên/Chủ đề** cho tài liệu này (VD: *Đề thi học kỳ 1 2024*):"
    )

    if caption:
        prompt_text += f"\n\n💡 *Gợi ý (từ chú thích):* `{caption}`"

    await query.edit_message_text(prompt_text, parse_mode="Markdown")
    return INPUT_TOPIC


async def save_material(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Lưu thông tin tài liệu vào Cơ sở dữ liệu."""
    topic = update.message.text.strip()
    topic_clean = unidecode(topic).lower()

    user = update.message.from_user
    user_name = user.first_name or user.username or "Người dùng"
    user_id = user.id
    chat_id = update.effective_chat.id

    subject = context.user_data.get("upload_subject")
    file_type = context.user_data.get("upload_file_type")
    file_id = context.user_data.get("upload_file_id")
    msg_id = context.user_data.get("upload_msg_id")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO documents (message_id, chat_id, user_id, user_name, subject, topic, topic_clean, file_type, file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (msg_id, chat_id, user_id, user_name, subject, topic, topic_clean, file_type, file_id))
        doc_id = cursor.lastrowid

    await update.message.reply_text(
        f"🎉 **ĐÃ LƯU TÀI LIỆU THÀNH CÔNG!**\n\n"
        f"🆔 **ID:** `{doc_id}`\n"
        f"📚 **Môn:** {subject}\n"
        f"🏷️ **Chủ đề:** {topic}\n"
        f"👤 **Người đăng:** {user_name}",
        parse_mode="Markdown"
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Hủy thao tác upload."""
    context.user_data.clear()
    await update.message.reply_text("❌ Đã hủy thao tác lưu tài liệu.")
    return ConversationHandler.END


# ==============================================================================
# 6. TÌM KIẾM TÀI LIỆU (/tim)
# ==============================================================================
async def search_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Cú pháp: `/tim <từ khóa>` (Ví dụ: `/tim hoa 12`)", parse_mode="Markdown")
        return

    query_raw = " ".join(context.args).strip()
    results = run_search(query_raw)

    if not results:
        await update.message.reply_text(f"🔍 Không tìm thấy tài liệu nào khớp với từ khóa: `{query_raw}`")
        return

    # Lưu kết quả vào bot_data với key dọn dẹp bộ nhớ đệm đơn giản
    if "search_cache" not in context.bot_data:
        context.bot_data["search_cache"] = {}

    cache_key = f"{update.effective_user.id}_{int(update.message.date.timestamp())}"
    context.bot_data["search_cache"][cache_key] = results

    await send_results_page(update, context, cache_key, page=1)


async def send_results_page(update: Update, context: ContextTypes.DEFAULT_TYPE, cache_key: str, page: int):
    results = context.bot_data.get("search_cache", {}).get(cache_key, [])

    if not results:
        msg = "⚠️ Kết quả tìm kiếm đã hết hạn. Vui lòng thực hiện lại lệnh `/tim`."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    per_page = 5
    total_items = len(results)
    total_pages = (total_items + per_page - 1) // per_page
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_items = results[start_idx:end_idx]

    text = f"🔍 **KẾT QUẢ TÌM KIẾM** (Trang {page}/{total_pages} - Tổng {total_items}):\n\n"

    for doc in page_items:
        doc_id, user_name, subject, topic, file_type, file_id, msg_id, chat_id = doc
        text += f"📌 **ID {doc_id}:** [{subject}] {topic}\n👤 *Đăng bởi:* {user_name}\n\n"

        # Gửi file trực tiếp
        try:
            if file_type == "document":
                await context.bot.send_document(update.effective_chat.id, file_id, caption=f"📄 ID: {doc_id} | {topic}")
            elif file_type == "photo":
                await context.bot.send_photo(update.effective_chat.id, file_id, caption=f"🖼️ ID: {doc_id} | {topic}")
            elif file_type == "video":
                await context.bot.send_video(update.effective_chat.id, file_id, caption=f"🎥 ID: {doc_id} | {topic}")
            elif file_type == "audio":
                await context.bot.send_audio(update.effective_chat.id, file_id, caption=f"🎵 ID: {doc_id} | {topic}")
            elif file_type == "voice":
                await context.bot.send_voice(update.effective_chat.id, file_id, caption=f"🎙️ ID: {doc_id} | {topic}")
            elif file_type == "canva":
                await context.bot.send_message(update.effective_chat.id, f"🎨 **Canva Link (ID {doc_id}):** {topic}\n🔗 {file_id}")
        except TelegramError as e:
            logger.warning(f"Không thể gửi trực tiếp file ID {doc_id}: {e}")

    # Nút chuyển trang
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Trước", callback_data=f"timpage_{cache_key}_{page-1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("Sau ➡️", callback_data=f"timpage_{cache_key}_{page+1}"))

    markup = InlineKeyboardMarkup([nav_buttons]) if nav_buttons else None

    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def search_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    cache_key = parts[1]
    page = int(parts[2])

    await send_results_page(update, context, cache_key, page)


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
        text += f"• `{doc_id}` | **[{subject}]** {topic} *(bởi {user_name})*\n"

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
        text += f"• `{doc_id}` | **[{subject}]** {topic}\n"

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
        lines.append(f"• **{subject}:** {count} tài liệu")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ==============================================================================
# 11. HÀM MAIN & ĐĂNG KÝ HANDLERS
# ==============================================================================
def main():
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE" or not BOT_TOKEN:
        logger.error("❌ Vui lòng cấu hình BOT_TOKEN hợp lệ trong file code hoặc môi trường!")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ConversationHandler cho Upload tài liệu
    upload_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Regex(r'https?://[^\s]*canva\.com[^\s]*'),
                start_upload
            )
        ],
        states={
            SELECT_SUBJECT: [CallbackQueryHandler(subject_selected, pattern=r"^sub_")],
            INPUT_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_material)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    # Đăng ký Command Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("tim", search_topic))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("mytai", my_materials))
    app.add_handler(CommandHandler("sua", edit_topic))
    app.add_handler(CommandHandler("xoa", delete_command))
    app.add_handler(CommandHandler("stats", stats_command))

    # Đăng ký CallbackQuery Handlers
    app.add_handler(CallbackQueryHandler(search_page_callback, pattern=r"^timpage_"))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^(delconfirm_|delyes_|delcancel)"))

    # Đăng ký ConversationHandler
    app.add_handler(upload_handler)

    logger.info("🤖 Bot Lưu Trữ Tài Liệu đã sẵn sàng hoạt động!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
