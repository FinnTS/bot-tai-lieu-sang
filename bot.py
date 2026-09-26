import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from dotenv import load_dotenv
from unidecode import unidecode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ------------------------------------------------------------------
# CẤU HÌNH
# ------------------------------------------------------------------
load_dotenv()  # đọc biến môi trường từ file .env cùng thư mục

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "learning_materials.db")
RESULTS_PER_PAGE = 5  # số tài liệu hiển thị mỗi trang khi /tim hoặc /list

if not BOT_TOKEN:
    raise RuntimeError(
        "Chưa tìm thấy BOT_TOKEN. Hãy tạo file .env cùng thư mục với bot.py "
        "và thêm dòng: BOT_TOKEN=xxxxxxxx:yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------------

@contextmanager
def get_db():
    """Context manager mở/đóng kết nối SQLite an toàn."""
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                subject TEXT NOT NULL,
                topic TEXT NOT NULL,
                topic_clean TEXT NOT NULL,
                file_type TEXT NOT NULL,
                telegram_file_id TEXT,
                canva_link TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_topic_clean ON documents(topic_clean)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_subject ON documents(subject)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON documents(user_id)')


init_db()

# Các trạng thái luồng Upload
SELECT_SUBJECT, INPUT_TOPIC = range(2)

# Danh sách các môn học
SUBJECTS = ["Toán", "Vật Lý", "Hóa Học", "Tin Học", "Tiếng Anh", "Lịch Sử", "Địa Lý", "Văn Học", "Khác"]

FILE_TYPE_EMOJI = {
    "document": "📄",
    "photo": "🖼️",
    "video": "🎬",
    "audio": "🎵",
    "voice": "🎙️",
    "canva": "🎨",
}

# ----------------- HÀM TIỆN ÍCH -----------------

def build_subject_keyboard():
    keyboard, row = [], []
    for sub in SUBJECTS:
        row.append(InlineKeyboardButton(sub, callback_data=f"sub_{sub}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


def build_pagination_keyboard(prefix: str, query_key: str, page: int, total_pages: int):
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Trước", callback_data=f"{prefix}_{query_key}_{page-1}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("Sau ➡️", callback_data=f"{prefix}_{query_key}_{page+1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError as e:
        logger.warning(f"Không kiểm tra được quyền admin: {e}")
        return False


# ----------------- LỆNH BẮT ĐẦU / TRỢ GIÚP -----------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Chào mừng đến với Bot Lưu Trữ Tài Liệu Học Tập!**\n\n"
        "📥 Thả File / Ảnh / Video / Audio / Link Canva vào nhóm để lưu.\n"
        "🔍 Gõ `/tim <từ khóa>` để tìm tài liệu.\n"
        "📖 Gõ `/help` để xem hướng dẫn đầy đủ.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 **HƯỚNG DẪN SỬ DỤNG BOT NHÓM**\n\n"
        "1️⃣ **Lưu tài liệu:**\n"
        "   - Thả **File (PDF/Word/ZIP)**, **Ảnh**, **Video**, **Audio/Voice** hoặc **Link Canva** vào nhóm.\n"
        "   - Chọn **Môn học** từ menu nút bấm.\n"
        "   - Reply tin nhắn của bot để nhập tên Bài học/Chủ đề.\n\n"
        "2️⃣ **Tìm kiếm tài liệu:**\n"
        "   - Cú pháp: `/tim <từ khóa>`\n"
        "   - Hỗ trợ gõ từ khóa ngắn, không dấu hoặc có dấu.\n"
        "   - *Ví dụ:* `/tim chiến dịch` hoặc `/tim dien bien phu`\n\n"
        "3️⃣ **Liệt kê theo môn:**\n"
        "   - Cú pháp: `/list <tên môn>` (hoặc `/list all` để xem tất cả)\n\n"
        "4️⃣ **Xem tài liệu của bạn:**\n"
        "   - Cú pháp: `/mytai`\n\n"
        "5️⃣ **Sửa tên chủ đề:**\n"
        "   - Cú pháp: `/sua <id> <tên mới>` (chỉ chủ bài hoặc Admin)\n"
        "   - ID lấy được từ kết quả `/tim` hoặc `/list`\n\n"
        "6️⃣ **Xóa tài liệu:**\n"
        "   - Cú pháp: `/xoa <từ khóa>`\n"
        "   - Chọn file từ menu, sau đó xác nhận xóa.\n"
        "   - *(Chỉ người upload bài đó hoặc Admin nhóm mới có quyền xóa)*\n\n"
        "7️⃣ **Thống kê nhóm:**\n"
        "   - Cú pháp: `/stats`\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ----------------- LUỒNG UPLOAD TÀI LIỆU -----------------

async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Khi thành viên gửi File, Ảnh, Video, Audio hoặc Link Canva"""
    message = update.message
    user_name = message.from_user.first_name or message.from_user.username or "Thành viên"
    user_id = message.from_user.id

    context.user_data['user_name'] = user_name
    context.user_data['user_id'] = user_id

    if message.document:
        context.user_data['file_id'] = message.document.file_id
        context.user_data['file_type'] = 'document'
        context.user_data['canva_link'] = ''
    elif message.photo:
        context.user_data['file_id'] = message.photo[-1].file_id
        context.user_data['file_type'] = 'photo'
        context.user_data['canva_link'] = ''
    elif message.video:
        context.user_data['file_id'] = message.video.file_id
        context.user_data['file_type'] = 'video'
        context.user_data['canva_link'] = ''
    elif message.audio:
        context.user_data['file_id'] = message.audio.file_id
        context.user_data['file_type'] = 'audio'
        context.user_data['canva_link'] = ''
    elif message.voice:
        context.user_data['file_id'] = message.voice.file_id
        context.user_data['file_type'] = 'voice'
        context.user_data['canva_link'] = ''
    elif message.text and "canva.com" in message.text:
        context.user_data['file_id'] = ''
        context.user_data['file_type'] = 'canva'
        context.user_data['canva_link'] = message.text.strip()
    else:
        return ConversationHandler.END

    await message.reply_text(
        f"📥 **{user_name}** vừa tải lên tài liệu!\nVui lòng chọn **Môn học** tương ứng:",
        reply_markup=build_subject_keyboard(),
        parse_mode="Markdown",
        reply_to_message_id=message.message_id,
    )
    return SELECT_SUBJECT


async def subject_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    selected_subject = query.data.replace("sub_", "")
    context.user_data['subject'] = selected_subject

    await query.edit_message_text(
        f"📚 Môn học: **{selected_subject}**\n\n"
        f"✏️ Hãy reply tin nhắn này để nhập **Tên chủ đề / Tên bài học**\n"
        f"(Ví dụ: `Chiến dịch Điện Biên Phủ`, `Đạo hàm`, `Monotonic Stack`):",
        parse_mode="Markdown",
    )
    return INPUT_TOPIC


async def save_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lưu dữ liệu vào database, cảnh báo nếu chủ đề đã tồn tại trong cùng môn."""
    raw_topic = update.message.text.strip()
    clean_topic = unidecode(raw_topic).lower()

    subject = context.user_data.get('subject')
    file_id = context.user_data.get('file_id')
    file_type = context.user_data.get('file_type')
    canva_link = context.user_data.get('canva_link')
    user_name = context.user_data.get('user_name')
    user_id = context.user_data.get('user_id')

    if not subject or not file_type:
        await update.message.reply_text("⚠️ Phiên tải lên đã hết hạn, vui lòng gửi lại tài liệu.")
        return ConversationHandler.END

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM documents WHERE subject = ? AND topic_clean = ?",
            (subject, clean_topic),
        )
        duplicate = cursor.fetchone()[0] > 0

        cursor.execute('''
            INSERT INTO documents (user_id, user_name, subject, topic, topic_clean, file_type, telegram_file_id, canva_link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, user_name, subject, raw_topic, clean_topic, file_type, file_id, canva_link))

    warning = "\n\n⚠️ *Lưu ý: đã có tài liệu khác cùng tên chủ đề trong môn này.*" if duplicate else ""

    await update.message.reply_text(
        f"✅ **Lưu tài liệu thành công!**\n"
        f"👤 **Người đăng:** {user_name}\n"
        f"📚 **Môn:** {subject}\n"
        f"🏷️ **Bài/Chủ đề:** `{raw_topic}`\n\n"
        f"💡 *Để tìm lại, chỉ cần gõ:* `/tim {raw_topic}`{warning}",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Đã hủy quá trình lưu tài liệu.")
    return ConversationHandler.END


# ----------------- LUỒNG TRA CỨU TÀI LIỆU -----------------

def run_search(query_raw: str):
    query_clean = unidecode(query_raw).lower()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, user_name, subject, topic, file_type, telegram_file_id, canva_link, created_at
            FROM documents
            WHERE topic_clean LIKE ? OR lower(subject) LIKE ? OR lower(topic) LIKE ?
            ORDER BY created_at DESC
        ''', (f"%{query_clean}%", f"%{query_clean}%", f"%{query_raw.lower()}%"))
        return cursor.fetchall()


async def send_results_page(update_or_query, results, query_key: str, page: int, edit=False):
    total_pages = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * RESULTS_PER_PAGE
    chunk = results[start:start + RESULTS_PER_PAGE]

    header = f"🔎 **{len(results)}** kết quả — Trang {page + 1}/{total_pages}"
    keyboard = build_pagination_keyboard("timpage", query_key, page, total_pages)

    if edit:
        await update_or_query.edit_message_text(header, parse_mode="Markdown", reply_markup=keyboard)
        target = update_or_query.message
    else:
        target = update_or_query
        await target.reply_text(header, parse_mode="Markdown", reply_markup=keyboard)

    for doc_id, user_name, subject, topic, file_type, file_id, canva_link, created_at in chunk:
        emoji = FILE_TYPE_EMOJI.get(file_type, "📎")
        caption = (
            f"{emoji} **[ID {doc_id}] {subject}**\n"
            f"🏷️ {topic}\n"
            f"👤 Đăng bởi: {user_name}"
        )
        try:
            if file_type == 'document' and file_id:
                await target.reply_document(document=file_id, caption=caption, parse_mode="Markdown")
            elif file_type == 'photo' and file_id:
                await target.reply_photo(photo=file_id, caption=caption, parse_mode="Markdown")
            elif file_type == 'video' and file_id:
                await target.reply_video(video=file_id, caption=caption, parse_mode="Markdown")
            elif file_type == 'audio' and file_id:
                await target.reply_audio(audio=file_id, caption=caption, parse_mode="Markdown")
            elif file_type == 'voice' and file_id:
                await target.reply_voice(voice=file_id, caption=caption, parse_mode="Markdown")
            elif file_type == 'canva' and canva_link:
                await target.reply_text(f"{caption}\n🎨 **Link Canva:** {canva_link}", parse_mode="Markdown")
        except TelegramError as e:
            logger.error(f"Lỗi gửi file (id={doc_id}): {e}")
            await target.reply_text(f"⚠️ Không gửi được tài liệu ID {doc_id} (có thể file đã hết hạn).")


async def search_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ **Cú pháp chưa đúng!**\n\nHãy gõ: `/tim <tên_bài_hoặc_môn>`\n"
            "Ví dụ: `/tim chiến dịch` hoặc `/tim dien bien phu` hoặc `/tim lich su`",
            parse_mode="Markdown",
        )
        return

    query_raw = " ".join(context.args).strip()
    results = run_search(query_raw)

    if not results:
        await update.message.reply_text(
            f"🔍 Không tìm thấy tài liệu nào trùng với từ khóa: `{query_raw}`", parse_mode="Markdown"
        )
        return

    query_key = query_raw.replace(" ", "+")[:50]
    context.bot_data.setdefault("search_cache", {})[query_key] = results
    await send_results_page(update.message, results, query_key, page=0)


async def search_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, query_key, page = query.data.split("_", 2)
    results = context.bot_data.get("search_cache", {}).get(query_key)
    if results is None:
        await query.edit_message_text("⚠️ Phiên tìm kiếm đã hết hạn, vui lòng tìm lại.")
        return
    await send_results_page(query, results, query_key, int(page), edit=True)


# ----------------- /list VÀ /mytai -----------------

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        subjects_text = ", ".join(f"`{s}`" for s in SUBJECTS)
        await update.message.reply_text(
            f"⚠️ Hãy gõ: `/list <tên môn>` hoặc `/list all`\nCác môn hợp lệ: {subjects_text}",
            parse_mode="Markdown",
        )
        return

    subject_arg = " ".join(context.args).strip()
    with get_db() as conn:
        cursor = conn.cursor()
        if subject_arg.lower() == "all":
            cursor.execute("SELECT subject, topic, id FROM documents ORDER BY subject, topic")
        else:
            cursor.execute("SELECT subject, topic, id FROM documents WHERE subject = ? ORDER BY topic", (subject_arg,))
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text(f"📭 Chưa có tài liệu nào cho `{subject_arg}`.", parse_mode="Markdown")
        return

    lines = [f"📖 **Danh sách tài liệu — {subject_arg}**\n"]
    for subject, topic, doc_id in rows:
        lines.append(f"• [{doc_id}] {topic}" + (f" ({subject})" if subject_arg.lower() == "all" else ""))

    text = "\n".join(lines)
    # Telegram giới hạn ~4096 ký tự/tin nhắn
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i:i + 3500], parse_mode="Markdown")


async def my_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, subject, topic FROM documents WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("📭 Bạn chưa đăng tài liệu nào.")
        return

    lines = ["🗂️ **Tài liệu bạn đã đăng:**\n"] + [f"• [{i}] {t} ({s})" for i, s, t in rows]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ----------------- SỬA TÊN CHỦ ĐỀ -----------------

async def edit_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Cú pháp: `/sua <id> <tên mới>`\nLấy ID từ kết quả `/tim` hoặc `/list`.",
            parse_mode="Markdown",
        )
        return

    try:
        doc_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID phải là số. Ví dụ: `/sua 12 Đạo hàm nâng cao`", parse_mode="Markdown")
        return

    new_topic = " ".join(context.args[1:]).strip()
    user_id = update.message.from_user.id

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, topic FROM documents WHERE id = ?", (doc_id,))
        doc = cursor.fetchone()

        if not doc:
            await update.message.reply_text("❌ Không tìm thấy tài liệu với ID này.")
            return

        owner_id, old_topic = doc
        if user_id != owner_id and not await is_group_admin(update, context, user_id):
            await update.message.reply_text("🚫 Chỉ người đăng bài hoặc Admin nhóm mới có quyền sửa.")
            return

        clean_topic = unidecode(new_topic).lower()
        cursor.execute(
            "UPDATE documents SET topic = ?, topic_clean = ? WHERE id = ?",
            (new_topic, clean_topic, doc_id),
        )

    await update.message.reply_text(
        f"✏️ Đã đổi tên: `{old_topic}` ➜ `{new_topic}`", parse_mode="Markdown"
    )


# ----------------- LUỒNG XÓA TÀI LIỆU (có xác nhận) -----------------

async def delete_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ **Cú pháp chưa đúng!**\n\nHãy gõ: `/xoa <từ_khóa_tài_liệu>`\n"
            "Ví dụ: `/xoa đao ham` hoặc `/xoa dien bien phu`",
            parse_mode="Markdown",
        )
        return

    query_raw = " ".join(context.args).strip()
    query_clean = unidecode(query_raw).lower()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, subject, topic, user_name
            FROM documents
            WHERE topic_clean LIKE ? OR lower(subject) LIKE ? OR lower(topic) LIKE ?
        ''', (f"%{query_clean}%", f"%{query_clean}%", f"%{query_raw.lower()}%"))
        results = cursor.fetchall()

    if not results:
        await update.message.reply_text(f"🔍 Không tìm thấy tài liệu nào trùng với từ khóa: `{query_raw}`", parse_mode="Markdown")
        return

    keyboard = [
        [InlineKeyboardButton(f"🗑️ [{subject}] {topic} (bởi {user_name})", callback_data=f"delask_{doc_id}")]
        for doc_id, subject, topic, user_name in results
    ]
    await update.message.reply_text(
        "🗑️ Chọn tài liệu muốn **XÓA** bên dưới:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def delete_ask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bước xác nhận trung gian trước khi xóa thật sự."""
    query = update.callback_query
    await query.answer()
    doc_id = int(query.data.replace("delask_", ""))

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT subject, topic FROM documents WHERE id = ?", (doc_id,))
        doc = cursor.fetchone()

    if not doc:
        await query.edit_message_text("❌ Tài liệu này không còn tồn tại trên hệ thống!")
        return

    subject, topic = doc
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Xác nhận xóa", callback_data=f"del_{doc_id}"),
        InlineKeyboardButton("↩️ Hủy", callback_data="delcancel"),
    ]])
    await query.edit_message_text(
        f"⚠️ Bạn có chắc muốn xóa:\n**[{subject}] {topic}**?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def delete_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("↩️ Đã hủy thao tác xóa.")


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    doc_id = int(query.data.replace("del_", ""))
    user_id = query.from_user.id

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, topic, subject FROM documents WHERE id = ?", (doc_id,))
        doc = cursor.fetchone()

        if not doc:
            await query.edit_message_text("❌ Tài liệu này không còn tồn tại trên hệ thống!")
            return

        owner_id, topic, subject = doc
        is_admin = await is_group_admin(update, context, user_id)

        if user_id == owner_id or is_admin:
            cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            await query.edit_message_text(f"🗑️ **Đã xóa thành công:** [{subject}] `{topic}`", parse_mode="Markdown")
        else:
            await query.edit_message_text(
                "🚫 **Bạn không có quyền xóa tài liệu này!** (Chỉ người đăng bài hoặc Admin nhóm mới có quyền xóa).",
                parse_mode="Markdown",
            )


# ----------------- THỐNG KÊ -----------------

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM documents")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT subject, COUNT(*) FROM documents GROUP BY subject ORDER BY COUNT(*) DESC")
        by_subject = cursor.fetchall()

        cursor.execute(
            "SELECT user_name, COUNT(*) as c FROM documents GROUP BY user_id ORDER BY c DESC LIMIT 5"
        )
        top_users = cursor.fetchall()

    lines = [f"📊 **THỐNG KÊ TÀI LIỆU NHÓM**\n\n📁 Tổng số: **{total}** tài liệu\n"]
    if by_subject:
        lines.append("**Theo môn học:**")
        lines += [f"  • {subject}: {count}" for subject, count in by_subject]
    if top_users:
        lines.append("\n**Top người đóng góp:**")
        lines += [f"  🏅 {name}: {count} tài liệu" for name, count in top_users]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ----------------- XỬ LÝ LỖI TOÀN CỤC -----------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Lỗi không mong muốn:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Đã có lỗi xảy ra, vui lòng thử lại sau."
            )
        except TelegramError:
            pass


# ----------------- CHƯƠNG TRÌNH CHÍNH -----------------

if __name__ == '__main__':
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    upload_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE,
                start_upload,
            ),
            MessageHandler(filters.TEXT & filters.Regex(r'canva\.com'), start_upload),
        ],
        states={
            SELECT_SUBJECT: [CallbackQueryHandler(subject_selected, pattern=r"^sub_")],
            INPUT_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_material)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    app.add_handler(upload_handler)
    app.add_handler(CommandHandler('start', start_command))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('tim', search_topic))
    app.add_handler(CommandHandler('list', list_command))
    app.add_handler(CommandHandler('mytai', my_materials))
    app.add_handler(CommandHandler('sua', edit_topic))
    app.add_handler(CommandHandler('xoa', delete_search))
    app.add_handler(CommandHandler('stats', stats_command))

    app.add_handler(CallbackQueryHandler(search_page_callback, pattern=r"^timpage_"))
    app.add_handler(CallbackQueryHandler(delete_ask_confirm, pattern=r"^delask_"))
    app.add_handler(CallbackQueryHandler(delete_cancel, pattern=r"^delcancel$"))
    app.add_handler(CallbackQueryHandler(delete_confirm, pattern=r"^del_"))

    app.add_error_handler(error_handler)

    print("Bot đang khởi động...")
    app.run_polling()
