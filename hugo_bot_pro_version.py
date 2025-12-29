import subprocess
import sys
import logging

# قائمة المكتبات المطلوبة
REQUIRED_LIBRARIES = [
    'python-telegram-bot',
    'aiosqlite',
    'httpx'
]

def install_dependencies():
    """تثبيت المكتبات المفقودة تلقائياً"""
    for lib in REQUIRED_LIBRARIES:
        try:
            if lib == 'python-telegram-bot':
                import telegram
            else:
                __import__(lib.replace('-', '_'))
        except ImportError:
            print(f"Installing {lib}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", lib])

# تشغيل عملية التثبيت قبل استيراد المكتبات الرئيسية
install_dependencies()

# الآن يمكن استيراد المكتبات بأمان
import aiosqlite
import asyncio
from datetime import datetime, timedelta
import httpx
from urllib.parse import quote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    ContextTypes, 
    MessageHandler, 
    filters, 
    CallbackQueryHandler, 
    PicklePersistence,
    ConversationHandler
)

# تعريف حالات المحادثة (Conversation States)
(
    WAITING_FOR_URL,
    WAITING_FOR_REDEEM,
    WAITING_FOR_TRANSFER,
    WAITING_FOR_BROADCAST,
    WAITING_FOR_CODE_DATA,
    WAITING_FOR_ADD_CHANNEL,
    WAITING_FOR_REMOVE_CHANNEL
) = range(7)

# إعدادات البوت الأساسية
TOKEN = "7584042175:AAEA1aexccKGbDKgA32xDCOvHSiBeDpgG-E"
DEVELOPER_USERNAME = "vca_4"
ADMIN_ID = 1654215357 
DAILY_GIFT_AMOUNT = 20
SHORTEN_COST = 20
REFERRAL_REWARD = 80
MIN_TRANSFER = 20000
TRANSFER_TAX = 0.05
DB_PATH = 'bot_database.db'

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

class Database:
    def __init__(self, db_path):
        self.db_path = db_path
        self.db = None

    async def connect(self):
        if self.db is None:
            self.db = await aiosqlite.connect(self.db_path)
            self.db.row_factory = aiosqlite.Row
        return self.db

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None

    async def execute(self, query, params=None, fetchone=False, fetchall=False, commit=True):
        db = await self.connect()
        async with db.execute(query, params or ()) as cursor:
            if fetchone:
                result = await cursor.fetchone()
            elif fetchall:
                result = await cursor.fetchall()
            else:
                result = None
            if commit:
                await db.commit()
            return result

    async def execute_transaction(self, queries):
        db = await self.connect()
        try:
            for query, params in queries:
                await db.execute(query, params)
            await db.commit()
            return True
        except Exception as e:
            await db.rollback()
            logging.error(f"Transaction failed: {e}")
            return False

db_manager = Database(DB_PATH)

async def init_db():
    await db_manager.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, points INTEGER DEFAULT 0, last_daily_gift TEXT, last_active TEXT)''')
    await db_manager.execute('''CREATE TABLE IF NOT EXISTS codes
                 (code TEXT PRIMARY KEY, points INTEGER, max_uses INTEGER DEFAULT 1, current_uses INTEGER DEFAULT 0)''')
    await db_manager.execute('''CREATE TABLE IF NOT EXISTS code_usage
                 (user_id INTEGER, code TEXT, PRIMARY KEY (user_id, code))''')
    await db_manager.execute('''CREATE TABLE IF NOT EXISTS channels
                 (channel_id TEXT PRIMARY KEY)''')
    await db_manager.execute('''CREATE TABLE IF NOT EXISTS stats
                 (date TEXT PRIMARY KEY, shortened_count INTEGER DEFAULT 0)''')
    await db_manager.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value INTEGER DEFAULT 1)''')
    await db_manager.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('transfer_enabled', 1)")
    await db_manager.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('shorten_enabled', 1)")

async def get_user(user_id, username=None, update_activity=False):
    user = await db_manager.execute("SELECT * FROM users WHERE user_id=?", (user_id,), fetchone=True)
    today = datetime.now().strftime('%Y-%m-%d')
    is_new = False
    if not user:
        await db_manager.execute("INSERT INTO users (user_id, username, points, last_active) VALUES (?, ?, ?, ?)", (user_id, username, 0, today))
        user = await db_manager.execute("SELECT * FROM users WHERE user_id=?", (user_id,), fetchone=True)
        is_new = True
    elif update_activity:
        # تحديث البيانات فقط إذا تغير اليوزر نيم أو مر يوم كامل على آخر نشاط
        last_active = user['last_active']
        if last_active != today or user['username'] != username:
            await db_manager.execute("UPDATE users SET last_active = ?, username = ? WHERE user_id = ?", (today, username, user_id))
            user = await db_manager.execute("SELECT * FROM users WHERE user_id=?", (user_id,), fetchone=True)
    return user, is_new

async def get_existing_user(user_id):
    user = await db_manager.execute("SELECT * FROM users WHERE user_id=?", (user_id,), fetchone=True)
    return user

async def update_points(user_id, points_change):
    await db_manager.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points_change, user_id))

async def log_shorten():
    today = datetime.now().strftime('%Y-%m-%d')
    await db_manager.execute("INSERT OR IGNORE INTO stats (date, shortened_count) VALUES (?, 0)", (today,))
    await db_manager.execute("UPDATE stats SET shortened_count = shortened_count + 1 WHERE date = ?", (today,))

async def safe_edit_text(update_or_query, text, reply_markup=None, parse_mode=None):
    """تعديل النص بأمان لتجنب الأخطاء الشائعة"""
    try:
        if hasattr(update_or_query, 'callback_query') and update_or_query.callback_query:
            await update_or_query.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        elif hasattr(update_or_query, 'message') and update_or_query.message:
            await update_or_query.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            # في حال كان كائن query مباشرة
            await update_or_query.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logging.error(f"Safe edit error: {e}")
            # محاولة إرسال رسالة جديدة كحل بديل
            try:
                if hasattr(update_or_query, 'effective_chat'):
                    await update_or_query.get_bot().send_message(chat_id=update_or_query.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            except: pass

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.now()
    cache = context.user_data.get('sub_cache', {})
    if cache.get('status') is True and (now - cache.get('time', now)) < timedelta(minutes=5):
        return True
    channels_rows = await db_manager.execute("SELECT channel_id FROM channels", fetchall=True)
    channels = [row[0] for row in channels_rows]
    if not channels: return True
    async def check_single_channel(channel):
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return channel
        except Exception:
            return channel
        return None

    results = await asyncio.gather(*[check_single_channel(ch) for ch in channels])
    not_subscribed = [ch for ch in results if ch is not None]
    if not_subscribed:
        context.user_data['sub_cache'] = {'status': False, 'time': now}
        keyboard = [[InlineKeyboardButton(f"اشترك في {ch}", url=f"https://t.me/{ch.replace('@', '')}")] for ch in not_subscribed]
        keyboard.append([InlineKeyboardButton("تم الاشتراك ✅", callback_data="check_sub")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg_text = "عذراً، يجب عليك الاشتراك في القنوات أولاً لاستخدام البوت:"
        if update.callback_query:
            await safe_edit_text(update.callback_query, msg_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(msg_text, reply_markup=reply_markup)
        return False
    context.user_data['sub_cache'] = {'status': True, 'time': now}
    return True

def main_inline_keyboard(user_id):
    keyboard = [
        [InlineKeyboardButton("📂 الخدمات", callback_data="services_menu")],
        [InlineKeyboardButton("💰 تجميع نقاط", callback_data="collect_points"), InlineKeyboardButton("🔄 تحويل نقاط", callback_data="transfer_points")],
        [InlineKeyboardButton("🏆 قائمة الأثرياء", callback_data="rich_list")],
        [InlineKeyboardButton("📖 شرح البوت", callback_data="bot_explanation"), InlineKeyboardButton("💡 فكرة البوت", callback_data="bot_idea")]
    ]
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("⚙️ لوحة التحكم", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_db, is_new = await get_user(user.id, user.username, update_activity=True)
    
    if is_new and context.args and context.args[0].isdigit():
        referrer_id = int(context.args[0])
        if referrer_id != user.id:
            referrer = await get_existing_user(referrer_id)
            if referrer:
                await update_points(referrer_id, REFERRAL_REWARD)
                try: await context.bot.send_message(chat_id=referrer_id, text=f"🎉 حصلت على {REFERRAL_REWARD} نقطة لدعوة صديق!")
                except: pass

    welcome_text = f"👋 أهلاً بك {user.first_name}!\n💰 نقاطك: {user_db[2]:,} نقطة\n🆔 معرفك: `{user.id}`"
    await update.message.reply_text(welcome_text, reply_markup=main_inline_keyboard(user.id), parse_mode='Markdown')
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    await query.answer()

    if data == "check_sub":
        if await check_subscription(update, context):
            user_db, _ = await get_user(user_id)
            await safe_edit_text(query, f"✅ شكراً لاشتراكك!\n💰 نقاطك: {user_db[2]:,} نقطة", reply_markup=main_inline_keyboard(user_id), parse_mode='Markdown')
        return ConversationHandler.END

    # الخدمات وتجميع النقاط تتطلب اشتراك
    if data in ["services_menu", "collect_points", "transfer_points", "shorten_url", "redeem_code"]:
        if not await check_subscription(update, context): return ConversationHandler.END

    if data == "services_menu":
        keyboard = [
            [InlineKeyboardButton("🔗 اختصار رابط (20 نقطة)", callback_data="shorten_url")],
            [InlineKeyboardButton("🎁 تفعيل كود", callback_data="redeem_code")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
        ]
        await safe_edit_text(query, "📂 **قائمة الخدمات:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data == "shorten_url":
        await query.message.reply_text("🔗 أرسل الرابط الذي تريد اختصاره:")
        return WAITING_FOR_URL

    elif data == "collect_points":
        keyboard = [
            [InlineKeyboardButton("🎁 هدية يومية", callback_data="daily_gift")],
            [InlineKeyboardButton("🔗 رابط الدعوة", callback_data="referral_link")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
        ]
        await safe_edit_text(query, "💰 **طرق تجميع النقاط:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data == "daily_gift":
        user_db, _ = await get_user(user_id)
        today = datetime.now().strftime('%Y-%m-%d')
        if user_db['last_daily_gift'] == today:
            await query.message.reply_text("❌ حصلت على هديتك اليوم، عد غداً!")
        else:
            await db_manager.execute("UPDATE users SET points = points + ?, last_daily_gift = ? WHERE user_id = ?", (DAILY_GIFT_AMOUNT, today, user_id))
            await query.message.reply_text(f"✅ حصلت على {DAILY_GIFT_AMOUNT} نقطة هدية!")

    elif data == "referral_link":
        link = f"https://t.me/{(await context.bot.get_me()).username}?start={user_id}"
        await query.message.reply_text(f"🔗 **رابط الدعوة:**\n`{link}`\n\n💰 مكافأة الدعوة: {REFERRAL_REWARD} نقطة.", parse_mode='Markdown')

    elif data == "transfer_points":
        await query.message.reply_text(f"🔄 أرسل ID الشخص ثم مسافة ثم المبلغ:\nمثال: `1654215357 20000`", parse_mode='Markdown')
        return WAITING_FOR_TRANSFER

    elif data == "redeem_code":
        await query.message.reply_text("🎁 أرسل الكود:")
        return WAITING_FOR_REDEEM

    elif data == "rich_list":
        rich_users = await db_manager.execute("SELECT username, points, user_id FROM users ORDER BY points DESC LIMIT 10", fetchall=True)
        user_db, _ = await get_user(user_id)
        text = "🏆 **قائمة الأثرياء:**\n\n"
        for i, u in enumerate(rich_users, 1):
            name = f"@{u[0]}" if u[0] else f"مستخدم ({u[2]})"
            text += f"{i}. {name} — {u[1]:,} نقطة\n"
        text += f"\n💰 **نقاطك:** {user_db[2]:,} نقطة"
        await safe_edit_text(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]]), parse_mode='Markdown')

    elif data == "back_to_main":
        user_db, _ = await get_user(user_id)
        await safe_edit_text(query, f"👋 أهلاً بك!\n💰 نقاطك: {user_db[2]:,} نقطة", reply_markup=main_inline_keyboard(user_id), parse_mode='Markdown')
        return ConversationHandler.END

    # لوحة التحكم (للأدمن فقط)
    elif user_id == ADMIN_ID:
        if data == "admin_panel":
            keyboard = [
                [InlineKeyboardButton("📢 إذاعة", callback_data="admin_broadcast"), InlineKeyboardButton("➕ كود", callback_data="admin_create_code")],
                [InlineKeyboardButton("📺 القنوات", callback_data="admin_channels"), InlineKeyboardButton("⚙️ الإعدادات", callback_data="admin_settings")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
            ]
            await safe_edit_text(query, "⚙️ **لوحة التحكم:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        elif data == "admin_broadcast":
            await query.message.reply_text("📢 أرسل نص الإذاعة:")
            return WAITING_FOR_BROADCAST
        elif data == "admin_create_code":
            await query.message.reply_text("🎁 أرسل: الكود النقاط الاستخدامات")
            return WAITING_FOR_CODE_DATA
        elif data == "admin_channels":
            channels = await db_manager.execute("SELECT channel_id FROM channels", fetchall=True)
            text = "📺 **القنوات:**\n" + "\n".join([f"- {ch[0]}" for ch in channels])
            keyboard = [[InlineKeyboardButton("➕ إضافة", callback_data="admin_add_channel"), InlineKeyboardButton("❌ حذف", callback_data="admin_remove_channel")], [InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")]]
            await safe_edit_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "admin_add_channel":
            await query.message.reply_text("📺 أرسل معرف القناة:")
            return WAITING_FOR_ADD_CHANNEL
        elif data == "admin_remove_channel":
            await query.message.reply_text("❌ أرسل معرف القناة لحذفها:")
            return WAITING_FOR_REMOVE_CHANNEL

    return ConversationHandler.END

# معالجات النصوص (States Handlers)
async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    url = update.message.text
    user_db, _ = await get_user(user_id)
    if user_db[2] < SHORTEN_COST:
        await update.message.reply_text("❌ نقاطك غير كافية.")
        return ConversationHandler.END
    
    wait_msg = await update.message.reply_text("⏳ جاري الاختصار...")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"https://tinyurl.com/api-create.php?url={quote(url)}", timeout=7.0)
            if response.status_code == 200:
                await update_points(user_id, -SHORTEN_COST)
                await log_shorten()
                await wait_msg.edit_text(f"✅ تم الاختصار:\n{response.text}")
            else: await wait_msg.edit_text("❌ فشل الاختصار.")
    except Exception: await wait_msg.edit_text("❌ انتهى وقت الانتظار.")
    return ConversationHandler.END

async def process_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id, amount = map(int, update.message.text.split())
        user_id = update.effective_user.id
        user_db, _ = await get_user(user_id)
        if user_db[2] < amount or amount < MIN_TRANSFER:
            await update.message.reply_text("❌ فشل التحويل (نقاط غير كافية أو أقل من الحد الأدنى).")
            return ConversationHandler.END
        
        target = await get_existing_user(target_id)
        if not target:
            await update.message.reply_text("❌ المستخدم غير موجود.")
            return ConversationHandler.END
        
        tax = int(amount * TRANSFER_TAX)
        final = amount - tax
        await db_manager.execute_transaction([
            ("UPDATE users SET points = points - ? WHERE user_id = ?", (amount, user_id)),
            ("UPDATE users SET points = points + ? WHERE user_id = ?", (final, target_id))
        ])
        await update.message.reply_text(f"✅ تم تحويل {final:,} نقطة.")
    except: await update.message.reply_text("❌ خطأ في التنسيق.")
    return ConversationHandler.END

async def process_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text
    user_id = update.effective_user.id
    code_data = await db_manager.execute("SELECT * FROM codes WHERE code=?", (code,), fetchone=True)
    if not code_data: await update.message.reply_text("❌ كود خاطئ.")
    else:
        used = await db_manager.execute("SELECT * FROM code_usage WHERE user_id=? AND code=?", (user_id, code), fetchone=True)
        if used or code_data['current_uses'] >= code_data['max_uses']:
            await update.message.reply_text("❌ الكود غير صالح أو استخدمته مسبقاً.")
        else:
            await db_manager.execute_transaction([
                ("UPDATE users SET points = points + ? WHERE user_id = ?", (code_data['points'], user_id)),
                ("UPDATE codes SET current_uses = current_uses + 1 WHERE code = ?", (code,)),
                ("INSERT INTO code_usage (user_id, code) VALUES (?, ?)", (user_id, code))
            ])
            await update.message.reply_text(f"✅ حصلت على {code_data['points']} نقطة!")
    return ConversationHandler.END

# دوال الأدمن
async def process_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    users = await db_manager.execute("SELECT user_id FROM users", fetchall=True)
    await update.message.reply_text(f"⏳ جاري الإذاعة لـ {len(users)} مستخدم...")
    for u in users:
        try: await context.bot.send_message(chat_id=u[0], text=text)
        except: pass
        await asyncio.sleep(0.05)
    await update.message.reply_text("✅ اكتملت الإذاعة.")
    return ConversationHandler.END

async def process_create_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        c, p, m = update.message.text.split()
        await db_manager.execute("INSERT INTO codes (code, points, max_uses) VALUES (?, ?, ?