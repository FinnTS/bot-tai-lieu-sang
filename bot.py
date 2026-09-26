import logging
import sqlite3
import re
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ----------------------------------------------------
# 1. CẤU HÌNH BAN ĐẦU & CƠ SỞ DỮ LIỆU
# ----------------------------------------------------
TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"  # Thay TOKEN bot của bạn vào đây
ADMIN_IDS = [123456789]  # Thay ID Telegram của Admin vào đây

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

def init_db():
    conn = sqlite3.connect("documents.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT,
            link_or_file_id TEXT NOT NULL,
            type TEXT CHECK(type IN ('link', 'file', 'photo')) NOT NULL,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def remove_accents(input_str: str) -> str:
    """Hàm bỏ dấu tiếng Việt để phục vụ tìm kiếm thông minh."""
    s1 = u'ÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝàáâãèéêìíòóôõùúýĂăĐđĨĩŨũƠơƯưẠạẢảẤấẦầẨẩẪẫẬậẮắẰằẲẳẴẵẶặẸẹẺẻẼẽẾếỀềỂểỄễỆệỈỉỊịỌọỎỏỐốỒồỔổỖỗỘộỚớỜờỞởỠỡỢợỤụỦủỨứỪừỬửỮữỰựỲỳỴỵỶỷỸỹ'
    s0 = u'AAAAEEEIIOOOOUUYaaaaeeeiioooouuyAaDdIiUuOoUuAaAaAaAaAaAaAaAaAaAaAaAaEeEeEeEeEeEeEeEeIiIiOoOoOoOoOoOoOoOoOoOoOoOoUuUuUuUuUuUuUuYyYyYyYy'
    s = ''
    for char in input_str:
        if char in s1:
            s += s0[s1.index(char)]
        else:
            s += char
    return s.lower()

# ----------------------------------------------------
# 2. CÁC HÀM XỬ LÝ LỆNH (HANDLERS)
# ----------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 Chào mừng bạn đến với Bot Quản Lý Tài Liệu Học Tập!\n\n"
        "Các lệnh khả dụng:\n"
        "🔹 /addlink <Tên tài liệu> | <Link Canva/Drive> - Lưu đường dẫn\n"
        "🔹 Gửi file/ảnh trực tiếp kèm caption: `#add <Tên tài liệu>` - Lưu file\n"
        "🔹 /search <Từ khóa> - Tìm kiếm tài liệu\n"
        "🔹 /list - Xem toàn bộ tài liệu\n"
        "🔹 /delete <ID> - Xóa tài liệu (Chỉ Admin)\n"
        "🔹 /stats - Thống kê tài liệu\n"
    )
    await update.message.reply_text(msg)

async def add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lưu đường dẫn (Canva, Drive, web...)"""
    text = " ".join(context.args)
    if "|" not in text:
        await update.message.reply_text("⚠️ Vui lòng nhập đúng định dạng:\n`/addlink <Tên tài liệu> | <Link>`", parse_mode="Markdown")
        return

    title, link = map(str.strip, text.split("|", 1))
    user_id = update.effective_user.id

    conn = sqlite3.connect("documents.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO docs (title, link_or_file_id, type, created_by) VALUES (?, ?, 'link', ?)",
        (title, link, user_id)
    )
    doc_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ Đã lưu link thành công! (ID: `{doc_id}`)\n📌 *{title}*", parse_mode="Markdown")

async def add_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lưu file tài liệu hoặc ảnh khi người dùng gửi kèm caption #add <Tên>"""
    caption = update.message.caption
    if not caption or not caption.startswith("#add"):
        return

    title = caption.replace("#add", "").strip()
    if not title:
        title = "Tài liệu không tên"

    user_id = update.effective_user.id
    doc_type = 'file'
    file_id = None

    if update.message.document:
        file_id = update.message.document.file_id
        doc_type = 'file'
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
        doc_type = 'photo'

    if file_id:
        conn = sqlite3.connect("documents.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO docs (title, link_or_file_id, type, created_by) VALUES (?, ?, ?, ?)",
            (title, file_id, doc_type, user_id)
        )
        doc_id = cursor.lastrowid
        conn.commit()
        conn.close()

        await update.message.reply_text(f"✅ Đã lưu {doc_type} thành công! (ID: `{doc_id}`)\n📌 *{title}*", parse_mode="Markdown")

async def search_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tìm kiếm thông minh hỗ trợ tiếng Việt có dấu và không dấu."""
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("⚠️ Vui lòng nhập từ khóa tìm kiếm: `/search <từ khóa>`", parse_mode="Markdown")
        return

    query_no_accent = remove_accents(query)

    conn = sqlite3.connect("documents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, link_or_file_id, type FROM docs")
    rows = cursor.fetchall()
    conn.close()

    results = []
    for doc_id, title, link_or_id, doc_type in rows:
        if query_no_accent in remove_accents(title):
            results.append((doc_id, title, link_or_id, doc_type))

    if not results:
        await update.message.reply_text(f"🔍 Không tìm thấy tài liệu nào khớp với từ khóa: *{query}*", parse_mode="Markdown")
        return

    msg = f"🔍 *Kết quả tìm kiếm cho '{query}':*\n\n"
    for doc_id, title, link_or_id, doc_type in results:
        if doc_type == 'link':
            msg += f"🔹 [{doc_id}] [{title}]({link_or_id})\n"
        else:
            msg += f"🔹 [{doc_id}] *{title}* ({doc_type.upper()}) - Dùng /get_{doc_id} để lấy\n"

    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

async def get_file_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lấy file/ảnh theo lệnh /get_<ID>"""
    command = update.message.text
    doc_id = command.replace("/get_", "").strip()

    if not doc_id.isdigit():
        return

    conn = sqlite3.connect("documents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT title, link_or_file_id, type FROM docs WHERE id = ?", (int(doc_id),))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await update.message.reply_text("❌ Không tìm thấy tài liệu tương ứng.")
        return

    title, file_id, doc_type = row
    if doc_type == 'file':
        await update.message.reply_document(document=file_id, caption=f"📄 *{title}*", parse_mode="Markdown")
    elif doc_type == 'photo':
        await update.message.reply_photo(photo=file_id, caption=f"🖼 *{title}*", parse_mode="Markdown")
    elif doc_type == 'link':
        await update.message.reply_text(f"🔗 *{title}*:\n{file_id}", parse_mode="Markdown")

async def list_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect("documents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, link_or_file_id, type FROM docs ORDER BY id DESC LIMIT 20")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📂 Kho tài liệu hiện đang trống.")
        return

    msg = "📚 *Danh sách tài liệu mới nhất:*\n\n"
    for doc_id, title, link_or_id, doc_type in rows:
        if doc_type == 'link':
            msg += f"• `{doc_id}` | [{title}]({link_or_id})\n"
        else:
            msg += f"• `{doc_id}` | *{title}* (/get\_{doc_id})\n"

    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

async def delete_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Bạn không có quyền thực hiện lệnh này.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Cú pháp: `/delete <ID_tài_liệu>`", parse_mode="Markdown")
        return

    doc_id = int(context.args[0])
    conn = sqlite3.connect("documents.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()

    if affected > 0:
        await update.message.reply_text(f"🗑 Đã xóa tài liệu ID `{doc_id}` thành công!", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Không tìm thấy ID tài liệu này.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect("documents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), type FROM docs GROUP BY type")
    rows = cursor.fetchall()
    conn.close()

    stat_dict = {doc_type: count for count, doc_type in rows}
    total = sum(stat_dict.values())

    msg = (
        "📊 *Thống kê hệ thống tài liệu:*\n\n"
        f"🌐 Đường dẫn (Links): {stat_dict.get('link', 0)}\n"
        f"📄 Tập tin (Documents): {stat_dict.get('file', 0)}\n"
        f"🖼 Hình ảnh (Photos): {stat_dict.get('photo', 0)}\n"
        f"---------------------\n"
        f"📦 *Tổng cộng:* {total} tài liệu"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ----------------------------------------------------
# 3. CHƯƠNG TRÌNH CHÍNH
# ----------------------------------------------------
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    # Đăng ký các handler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addlink", add_link))
    app.add_handler(CommandHandler("search", search_doc))
    app.add_handler(CommandHandler("list", list_all))
    app.add_handler(CommandHandler("delete", delete_doc))
    app.add_handler(CommandHandler("stats", stats))
    
    # Handler nhận file / ảnh gửi trực tiếp
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, add_file))
    
    # Handler bắt lệnh /get_<ID>
    app.add_handler(MessageHandler(filters.Regex(r"^/get_\d+$"), get_file_by_id))

    print("🤖 Bot Telegram đã bắt đầu chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
