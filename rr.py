import os
from functools import wraps
import json
import requests
import smtplib
from email.mime.text import MIMEText

from email.mime.multipart import MIMEMultipart
import time
import asyncio
import random
import datetime
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ========================= متغيرات أوامر المطور =========================
CMD_CREATE_VERIFICATION = "إنشاء رمز تحقق"
CMD_PAID_SERVICES = "الخدمات المدفوعة"
CMD_FREE_SERVICES = "الخدمات المجانية"
ADD_EMAIL, ADD_PASSWORD = range(2)
CMD_MANAGE_USERS = "إدارة المستخدمين"
CMD_MANAGE_DEVELOPERS = "إدارة المطورين"
CMD_STATS = "إحصائيات"
DELETE_EMAIL = 2
CMD_ADD_DEVELOPER = "رفع مطور"
CMD_REMOVE_DEVELOPER = "تنزيل مطور"

# ========================= إعدادات البيانات =========================
DATA_MARKER = "#DATA_START\n"
MAX_ACTIVE_ELAIM = 40
  # حدث هذا برقم معرّف المطور الأساسي
MAX_ACTIVE_USERS = 1000
WHIS_ID = 6947105506
# سنستخدم Semaphore للتحكم بعدد المستخدمين النشطين
active_users_sem = asyncio.Semaphore(MAX_ACTIVE_USERS)

pending_approvals = {}    # { user_id: chat_id }

allowed_users = set()     # المستخدمون المسموح لهم (VIP)
vip_users = {}            # { user_id: {"name": ..., "date": ..., "emails": [...] } }
normal_users = {}         # بيانات المستخدمين العاديين
restricted_users = {}     # المستخدمون المحظورون؛ "inf" تعني حظر دائم

free_max_messages = 200   # للمجانية: بريد واحد فقط حتى 200 رسالة

extra_developers = []     # المطورون الإضافيون
user_email_passwords = {} # لتسجيل كلمة مرور البريد (اختياري)
mandatory_channels = []   # قائمة قنوات الاشتراك الإجباري

current_verification_code = None
global_comm_disabled = False  # لتعطيل التواصل من قبل المطور

# ========================= متغيرات تفعيل التواصل =========================
communication_enabled_users = {}  # (user_id: bool)
communication_mapping = {}        # (forwarded_message_id: user_id)

# =========================والايميلات دوال الكليشات والردود =========================
templates = {}  # {template_name: template_content}
replies = {}    # {reply_name: reply_content}
Emals = {}
user_emails = {}
# ========================= دوال تحليل المعطيات =========================

def load_user_emails():
    """
    تحميل بيانات البريد من ملف JSON عند بدء تشغيل البوت.
    """
    global user_emails
    try:
        with open("data.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            user_emails = data.get("user_emails", {})
    except Exception as e:
        print("لم يتم تحميل بيانات البريد:", e)
        user_emails = {}

def save_user_emails():
    """
    حفظ بيانات البريد في ملف JSON.
    """
    data = {
        "user_emails": user_emails,
    }
    try:
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print("خطأ في حفظ بيانات البريد:", e)
# ========================= دوال تحليل المعطيات =========================

def get_args(update: Update, command: str) -> list:
    text = update.message.text.strip()
    if text.startswith(command):
        remainder = text[len(command):].strip()
        if remainder:
            return remainder.split()
    return []
# ========================= دوال تحليل التحقق =========================
def subscription_required(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        # التحقق من وجود قنوات إجبارية في القائمة mandatory_channels
        if mandatory_channels:
            missing = await check_subscription_status(user_id, context)
            if missing:
                # اختيار أول قناة لم يشترك بها المستخدم
                missing_channel = missing[0]
                try:
                    chat = await context.bot.get_chat(missing_channel)
                    button_text = chat.title  # استخدام عنوان القناة في زر الاشتراك
                    channel_url = f"https://t.me/{chat.username}" if chat.username else None
                except Exception:
                    button_text = str(missing_channel)
                    channel_url = None

                user_mention = update.effective_user.mention_markdown()
                msg = (
                    f"**• عـذراً .. عـزيـزي** {user_mention} 🤷🏻‍♀\n"
                    f"**• لـ إستخـدام البوت** 👨🏻‍💻\n"
                    f"**• عليك الإشتـراك في القنـاة التالية أولاً:**\n"
                    f"**{button_text}**"
                )

                buttons = []
                if channel_url:
                    buttons.append([InlineKeyboardButton(text=button_text, url=channel_url)])
                else:
                    buttons.append([InlineKeyboardButton(text=button_text, callback_data="no_url")])
                buttons.append([InlineKeyboardButton("تحقق من الاشتراك", callback_data="check_subscription")])
                keyboard = InlineKeyboardMarkup(buttons)
                
                await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
                return  # إيقاف التنفيذ إذا كان المستخدم غير مشترك
        return await func(update, context, *args, **kwargs)
    return wrapped

# ========================= دوال حفظ واسترجاع البيانات =========================
def load_data():
    global allowed_users, vip_users, normal_users, restricted_users, extra_developers, user_email_passwords, templates, replies, mandatory_channels
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print("خطأ في قراءة الملف:", e)
        return
    if DATA_MARKER in content:
        data_str = content.split(DATA_MARKER, 1)[1]
        try:
            data = json.loads(data_str)
            allowed_users = set(data.get("allowed_users", []))
            vip_users = data.get("vip_users", {})
            normal_users = data.get("normal_users", {})
            restricted_users_temp = data.get("restricted_users", {})
            restricted_users.clear()
            for k, v in restricted_users_temp.items():
                if v == "inf":
                    restricted_users[int(k)] = "inf"
                else:
                    try:
                        restricted_users[int(k)] = datetime.datetime.strptime(v, '%Y-%m-%d %H:%M:%S')
                    except Exception as e:
                        print(f"خطأ في تحويل تاريخ الحظر للمستخدم {k}: {e}")
            extra_developers = data.get("extra_developers", [])
            user_email_passwords = data.get("user_email_passwords", {})
            templates = data.get("templates", {})
            replies = data.get("replies", {})
            mandatory_channels = data.get("mandatory_channels", [])
        except Exception as e:
            print("خطأ في تحميل بيانات JSON:", e)
    else:
        allowed_users = set()
        vip_users = {}
        normal_users = {}
        restricted_users = {}
        extra_developers = []
        user_email_passwords = {}
        templates = {}
        replies = {}
        mandatory_channels = []

def save_data():
    global allowed_users, vip_users, normal_users, restricted_users, extra_developers, user_email_passwords, templates, replies, mandatory_channels
    data = {
        "allowed_users": list(allowed_users),
        "vip_users": vip_users,
        "normal_users": normal_users,
        "restricted_users": {str(k): ("inf" if v=="inf" else v.strftime('%Y-%m-%d %H:%M:%S')) for k, v in restricted_users.items()},
        "extra_developers": extra_developers,
        "user_email_passwords": user_email_passwords,
        "templates": templates,
        "replies": replies,
        "mandatory_channels": mandatory_channels
    }
    json_data = json.dumps(data, ensure_ascii=False, indent=4)
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print("خطأ في قراءة الملف:", e)
        return
    if DATA_MARKER in content:
        code_part = content.split(DATA_MARKER, 1)[0]
    else:
        code_part = content
    new_content = code_part + DATA_MARKER + json_data
    try:
        with open(__file__, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        print("خطأ في كتابة الملف:", e)

# ========================= دوال العودة (Reply Keyboard) =========================
def get_back_reply_keyboard():
    return ReplyKeyboardMarkup([["رجوع للقائمة الرئيسية"]], resize_keyboard=True, one_time_keyboard=False)

# ========================= دوال أخرى =========================
def generate_new_verification_code():
    global current_verification_code
    current_verification_code = str(random.randint(10000, 99999))
    print(f"[BP_BP] عــزيـزي المطور\n تم إنشاء رمز تحقق جديد: {current_verification_code}")

# دالة إرسال البريد (تبقى كما هي لأنها تستخدم smtplib)
def send_email(sender_email, sender_password, recipient_email, subject, body, delay):
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(sender_email, sender_password)
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = recipient_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        server.sendmail(sender_email, recipient_email, msg.as_string())
        print(f"تم إرسال الرسالة من {sender_email}")
        server.quit()
        time.sleep(delay)
    except Exception as e:
        print(f"فشل إرسال الرسالة من {sender_email}. الخطأ: {e}")
        raise e



# دالة مساعدة لإرسال البريد في الخلفية (إذا دعت الحاجة يمكن استدعاؤها عبر asyncio.to_thread)
async def send_email_async(sender_email, sender_password, recipient_email, subject, body, delay):
    await asyncio.to_thread(send_email, sender_email, sender_password, recipient_email, subject, body, delay)

# دالة لإرسال الرسائل في الخلفية على شكل مهمة
async def background_email_sending(chat_id, emails, passwords, recipient_email, subject, body, num_messages, delay, user_data, bot):
    counter = 0
    for m in range(num_messages):
        for i, email in enumerate(emails):
            if user_data.get("cancel"):
                await bot.send_message(chat_id=chat_id, text="تم إيقاف الإرسال.", reply_markup=get_back_reply_keyboard())
                active_users_sem.release()
                return
            try:
                await asyncio.to_thread(send_email, email, passwords[i], recipient_email, subject, body, delay)
                counter += 1
                await bot.send_message(chat_id=chat_id, text=f"• تم رفع البلاغ رقم {counter}.. بنجاح ☑️", reply_markup=get_back_reply_keyboard())
            except Exception:
                await bot.send_message(chat_id=chat_id, text= "عــذرا عزيــزي\nإيـميـلك يمكن تبن\nأو كلمة المرور غير صحيحة", reply_markup=get_back_reply_keyboard())
                active_users_sem.release()
                return
    await bot.send_message(chat_id=chat_id, text="تم إرسال جميع الرسائل.", reply_markup=get_back_reply_keyboard())
    active_users_sem.release()
    user_data.clear()

# دالة إنشاء رمز التحقق وإرساله
async def process_create_verification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id == WHIS_ID or user_id in extra_developers:
        generate_new_verification_code()
        await update.message.reply_text(f"عــزيـزي المطور\n تم إنشاء رمز تحقق جديد: {current_verification_code}", reply_markup=get_back_reply_keyboard())
    else:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بمطـــــــور السورس")

generate_new_verification_code()

async def add_vip_user(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    try:
        chat_member = await context.bot.get_chat(user_id)
        user_name = chat_member.first_name if chat_member and chat_member.first_name else "غير متوفر"
    except Exception:
        user_name = "غير متوفر"
    addition_date = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    vip_users[user_id] = {"name": user_name, "date": addition_date}
    allowed_users.add(user_id)
    save_data()
    return user_name, addition_date

# ========================= دالة التحقق من اشتراك المستخدم في قناة معينة =========================



# --------------------- دوال الاشتراك الإجباري المُعدّلة ---------------------


async def is_subscribed(user_id: int, channel, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يُفترض أن channel هو معرف القناة (ID) وليس ايدي.
    يتحقق مما إذا كان المستخدم مشتركًا في القناة.
    """
    try:
        bot_member = await context.bot.get_chat_member(channel, context.bot.id)
        # إذا لم يكن البوت أدمن في القناة فقد لا يستطيع التحقق؛ نعتبر المستخدم مشتركاً بهذه الحالة.
        if bot_member.status not in ["administrator", "creator"]:
            return True
        member = await context.bot.get_chat_member(channel, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

async def check_subscription_status(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> list:
    """
    يتحقق من اشتراك المستخدم في القنوات المخزنة في mandatory_channels واحدة تلو الأخرى.
    يعيد قائمة تحتوي على أول قناة غير مشتركة، أو قائمة فارغة إذا كان المستخدم مشتركًا في جميعها.
    """
    for channel in mandatory_channels:
        if not await is_subscribed(user_id, channel, context):
            return [channel]
    return []

# ========================= دالة التحقق من اشتراك المستخدم في كل القنوات الإجباريّة =========================
async def check_subscription_status(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> list:
    missing = []
    for channel in mandatory_channels:
        if not await is_subscribed(user_id, channel, context):
            missing.append(channel)
    return missing

# ========================= دالة check_subscription_callback =========================
async def check_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    missing = await check_subscription_status(user_id, context)
    if missing:
        await query.edit_message_text("عذراً، عليك الاشتراك في جميع القنوات لاستخدام الأوامر")
    else:
        await query.edit_message_text("تم الاشتراك بنجاح، اضغط /start")

# ========================= تعديل لوحة مفاتيح مستخدمي VIP =========================
def get_vip_keyboard(user_id):
    comm_enabled = communication_enabled_users.get(user_id, False)
    comm_button = "تعطيل التواصل" if comm_enabled else "تفعيل التواصل"
    buttons = [
        ["بدء الشد", "البريدات"],
        ["طريقة الشد", "رجوع للقائمة الرئيسية"],
        [comm_button]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)

# ========================= خدمات مجانية ومدفوعة =========================
def get_vip_keyboard_for(user_id: int):
    if user_id == WHIS_ID:
        buttons = [
            [CMD_CREATE_VERIFICATION, CMD_PAID_SERVICES],
            [CMD_FREE_SERVICES],
            [CMD_MANAGE_USERS, CMD_MANAGE_DEVELOPERS],
            [CMD_STATS],
            ["قسم الاشتراك"]
        ]
    else:
        buttons = [
            [CMD_CREATE_VERIFICATION, CMD_PAID_SERVICES],
            [CMD_FREE_SERVICES],
            [CMD_MANAGE_USERS],
            [CMD_STATS, "مطور ثانوي"]
        ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)
@subscription_required
async def handle_free_paid_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if text == "الخدمات المجانية":
        context.user_data['service'] = 'free'
        context.user_data['num_emails'] = 1
        await update.message.reply_text(f"تم تفعيل الخدمات المجانية (بريد واحد فقط، حتى {free_max_messages} رسالة)\nأدخل بريدك الإلكتروني:", reply_markup=get_back_reply_keyboard())
        context.user_data['step'] = 'email'
        return
    if text == "الخدمات المدفوعة":
        context.user_data['service'] = 'paid'
        if user_id in allowed_users or user_id in (WHIS_ID, *extra_developers):
            await update.message.reply_text(f"مرحبـا عــزيـزي {update.effective_user.first_name}\n\nفي قسـم الـ VIP.", reply_markup=get_vip_keyboard(user_id))
            context.user_data['step'] = 'vip_menu'
        else:
            await update.message.reply_text(f"مرحبــا عــزيـزي {update.effective_user.first_name}\n\n رجاءا أدخل رمز التحقق", reply_markup=get_back_reply_keyboard())
            context.user_data['step'] = 'verify_paid'
        return
    if text == "بدء الشد":
        if user_id not in allowed_users and user_id not in (WHIS_ID, *extra_developers):
            await update.message.reply_text(f"عــذرا عــزيـزي {update.effective_user.first_name}\n هذه الخدمة مخصصة فقط للمشتركين\nلطلب الاشتراك: @BP_BP.")
            return
        context.user_data['service'] = 'paid'
        await update.message.reply_text(f"اختر عدد البريدات (من 1 إلى {MAX_ACTIVE_ELAIM}):", reply_markup=get_back_reply_keyboard())
        context.user_data['step'] = 'num_emails'
        return
    if text == "البريدات":
        if user_id not in allowed_users and user_id not in (WHIS_ID, *extra_developers):
            await update.message.reply_text("هذه الخدمة خاصة بمستخدمي VIP.")
            return
        context.user_data["step"] = "verify_emails"
        await update.message.reply_text("أدخل رمز التحقق للبريدات:", reply_markup=get_back_reply_keyboard())
        return
    if text == "طريقة الشد":
        await update.message.reply_text("طريقة الشد: يتم توزيع الرسائل بالتساوي بين الحسابات لتحقيق أفضل أداء.", reply_markup=get_back_reply_keyboard())
        return
# ========================= دالة التحقق من انتهاء فترة الاشتراك =========================
async def check_subscription_expiry(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.datetime.now()
    to_remove = []
    for user_id in allowed_users.copy():
        sub_date_str = None
        if user_id in vip_users:
            sub_date_str = vip_users[user_id].get("date")
        elif user_id in normal_users:
            sub_date_str = normal_users[user_id].get("date")
        if sub_date_str:
            try:
                sub_date = datetime.datetime.strptime(sub_date_str, '%Y-%m-%d %H:%M:%S')
            except Exception:
                continue
            if now - sub_date >= datetime.timedelta(days=30):
                to_remove.append(user_id)
    for user_id in to_remove:
        normal_users.pop(user_id, None)
        vip_users.pop(user_id, None)
        allowed_users.discard(user_id)
        user_email_passwords.pop(user_id, None)
        communication_enabled_users.pop(user_id, None)
        if user_id in pending_approvals:
            del pending_approvals[user_id]
        try:
            await context.bot.send_message(chat_id=user_id, text="انتهت فترة اشتراكك قم بتجديد الاشتراك")
        except Exception:
            pass
    if to_remove:
        save_data()

# تعديل دالة الـ JobQueue لتكون async
async def subscription_expiry_job(context: ContextTypes.DEFAULT_TYPE):
    await check_subscription_expiry(context)
# ========================= الدالة الرئيسية (التحقق من الاشتراك الإجباري عند استقبال أي رسالة) =========================
# ========================= الدالة الرئيسية (التحقق من الاشتراك الإجباري) =========================



# دالة start_handler كنقطة بدء للبوت (يمكنك تعديلها بما يتناسب مع منطق مشروعك)
@subscription_required
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    # مثال على فحص إذا ما كان المستخدم محظوراً
    if is_banned(user_id):
        await update.message.reply_text("عذراً، أنت محظور من البوت من قبل المطورين. تواصل مع المطور @bp_bp")
        return

    try:
        await active_users_sem.acquire()
    except Exception:
        await update.message.reply_text("عذراً، يوجد ضغط على البوت.", reply_markup=get_back_reply_keyboard())
        return

    stats_message = (
        f"دخل البوت بتاريخ: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"المستخدم: {update.effective_user.first_name}\n"
        f"معرفه: {update.effective_user.username if update.effective_user.username else 'غير متوفر'}\n"
        f"ايديه: {user_id}"
    )
    await context.bot.send_message(chat_id=WHIS_ID, text=stats_message, reply_markup=get_back_reply_keyboard())
    normal_users[user_id] = {
        "name": update.effective_user.first_name,
        "date": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    save_data()
    

    

    # تابع باقي الكود كما هو...


    if user_id == WHIS_ID or user_id in extra_developers:
        await update.message.reply_text("مرحباً مطور، اختر الخيار المطلوب:", reply_markup=get_vip_keyboard_for(user_id))
        context.user_data['step'] = 'service_selection'
        context.user_data['service'] = 'paid'
    else:
        kb = ReplyKeyboardMarkup([["الخدمات المجانية", "الخدمات المدفوعة"], ["رجوع للقائمة الرئيسية"]],
                                   resize_keyboard=True, one_time_keyboard=False)
      
   
        await update.message.reply_text(f"🤷🏻‍♀• مرحبـــا .. عـزيـزي {update.effective_user.first_name} \n👨🏻‍💻• إليـك أزرار البوت\n• إضغط على أي أمر تريده:\n", reply_markup=kb)
        context.user_data['step'] = 'service_selection'



# ========================= معالجة الأزرار (Inline) =========================
@subscription_required
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    if data == "back_to_main":
        await query.edit_message_text("القائمة الرئيسية:", reply_markup=build_main_menu_keyboard(user_id))
        return
    if data == "dev_generate":
        if user_id == WHIS_ID or user_id in extra_developers:
            generate_new_verification_code()
            await query.edit_message_text(f"عــزيـزي المطور\n تم إنشاء رمز تحقق جديد: {current_verification_code}")
        else:
            await query.answer(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بمطـــــــور السورس", show_alert=True)
        return
    if data == "check_subscription":
        await check_subscription_callback(update, context)
        return
    if data.startswith("approve_user_"):
        try:
            target = int(data.split("_")[-1])
            chat_member = await context.bot.get_chat(target)
            vip_users[target] = {"name": chat_member.first_name, "date": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            allowed_users.add(target)
            save_data()
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("الخدمات المدفوعة", callback_data="paid_services")]])
            await context.bot.send_message(chat_id=target, text="تم اشتراكك في VIP وتمت الموافقة على وصولك للخدمات المدفوعة مدى الحياة.", reply_markup=keyboard)
            await query.edit_message_text(f"تم قبول المستخدم {target}.")
            if target in pending_approvals:
                del pending_approvals[target]
        except Exception as e:
            print(f"خطأ في موافقة المستخدم: {e}")
        return
    if data.startswith("reject_user_"):
        try:
            target = int(data.split("_")[-1])
            restricted_users[target] = datetime.datetime.now() + datetime.timedelta(minutes=30)
            save_data()
            await context.bot.send_message(chat_id=target, text="عذراً، أنت محظور من البوت من قبل المطورين. تواصل مع المطور @bp_bp")
            await query.edit_message_text(f"تم رفض المستخدم {target}.")
            if target in pending_approvals:
                del pending_approvals[target]
        except Exception as e:
            print(f"خطأ في رفض المستخدم: {e}")
        return


# ========================= تعديل لوحة مفاتيح المستخدمين الرئيسية =========================
def build_main_menu_keyboard(user_id: int):
    if user_id == WHIS_ID or user_id in extra_developers:
        return get_vip_keyboard_for(user_id)
    else:
        buttons = [
            [CMD_FREE_SERVICES, CMD_PAID_SERVICES],
            ["رجوع للقائمة الرئيسية"]
        ]
        return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)

# ========================= أمر إيقاف الإرسال =========================
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data['cancel'] = True
    await update.message.reply_text("تم إيقاف الإرسال.", reply_markup=get_back_reply_keyboard())

# ========================= دالة لإخفاء الكيبورد (أمر للمستخدمين) =========================
@subscription_required
async def hide_keyboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("تم إخفاء الكيبورد.", reply_markup=ReplyKeyboardRemove())

# ========================= دوال منع المحظورين =========================
def is_banned(user_id: int) -> bool:
    if user_id in restricted_users:
        ban_value = restricted_users[user_id]
        if ban_value == "inf" or (isinstance(ban_value, datetime.datetime) and ban_value > datetime.datetime.now()):
            return True
        else:
            del restricted_users[user_id]
            save_data()
    return False

# ========================= معالجة الرسائل =========================
@subscription_required
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global global_comm_disabled
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("عذراً، أنت محظور من البوت من قبل المطورين. تواصل مع المطور @bp_bp")
        return
    text = update.message.text.strip()

    # خاصية إدارة قنوات الاشتراك (للمطور الأساسي فقط)
    if user_id == WHIS_ID:
        if text == "قسم الاشتراك" and not context.user_data.get("action"):
            context.user_data["action"] = "subscription_management"
            keyboard = ReplyKeyboardMarkup([["إضافة قناة", "حذف قناة"], ["رجوع للقائمة الرئيسية"]], resize_keyboard=True, one_time_keyboard=False)
            await update.message.reply_text(f"مرحبـا بـك عـزيـزي مطور الســورس {update.effective_user.first_name}\n\nإلـيك قسم الاشتراك الإجبـــاري:", reply_markup=keyboard)
            return
        if context.user_data.get("action") == "subscription_management":
            if text == "إضافة قناة":
                context.user_data["action"] = "add_channel"
                await update.message.reply_text(f"مرحبـا بـك عـزيـزي مطور الســورس {update.effective_user.first_name}\n\nقــم بإرسال أيـدي القناة المـراد حفــظها:")
                return
            elif text == "حذف قناة":
                context.user_data["action"] = "delete_channel"
                await update.message.reply_text(f"مرحبـا بـك عـزيـزي مطور الســورس {update.effective_user.first_name}\n\nقــم بإرسال أيـدي القناة المـراد حـذفــها:")
                return
            elif text == "رجوع للقائمة الرئيسية":
                context.user_data.pop("action", None)
                await update.message.reply_text("تم الرجوع للقائمة الرئيسية.", reply_markup=get_vip_keyboard_for(user_id))
                return
        if context.user_data.get("action") == "add_channel":
            channel_input = text.strip()
            try:
                # نحاول جلب معلومات القناة باستخدام المُدخل (يمكن أن يكون username أو ID)
                chat = await context.bot.get_chat(channel_input)
                channel_id = chat.id  # الحصول على معرف القناة
            except Exception as e:
                await update.message.reply_text("تعذر الحصول على معلومات القناة، تأكد من صحة الأيــدي.", reply_markup=get_vip_keyboard_for(user_id))
                context.user_data.pop("action", None)
                return
            if channel_id not in mandatory_channels:
                mandatory_channels.append(channel_id)
                save_data()
                await update.message.reply_text("تم حفظ القناة وإضافتها لقائمة الاشتراك الإجباري.", reply_markup=get_vip_keyboard_for(user_id))
            else:
                await update.message.reply_text("القناة موجودة بالفعل في قائمة الاشتراك.", reply_markup=get_vip_keyboard_for(user_id))
            context.user_data.pop("action", None)
            return

        if context.user_data.get("action") == "delete_channel":
            channel_input = text.strip()
            try:
                chat = await context.bot.get_chat(channel_input)
                channel_id = chat.id
            except Exception as e:
                await update.message.reply_text("تعذر الحصول على معلومات القناة، تأكد من صحة المعرف.", reply_markup=get_vip_keyboard_for(user_id))
                context.user_data.pop("action", None)
                return
            if channel_id in mandatory_channels:
                mandatory_channels.remove(channel_id)
                save_data()
                await update.message.reply_text("تم حذف القناة من قائمة الاشتراك الإجباري.", reply_markup=get_vip_keyboard_for(user_id))
            else:
                await update.message.reply_text("القناة غير موجودة في القائمة.", reply_markup=get_vip_keyboard_for(user_id))
            context.user_data.pop("action", None)
            return


    if text == "رجوع للقائمة الرئيسية":
        context.user_data.clear()
        await update.message.reply_text("القائمة الرئيسية:", reply_markup=build_main_menu_keyboard(user_id))
        return

    if (user_id in allowed_users or user_id == WHIS_ID or user_id in extra_developers):
        if not context.user_data.get("action"):
            if text in templates:
                await update.message.reply_text(templates[text], reply_markup=get_back_reply_keyboard())
                return
            if text in replies:
                await update.message.reply_text(replies[text], reply_markup=get_back_reply_keyboard())
                return
        if text == "الكلايش":
            if templates:
                reply = "الكلايش المحفوظة:\n\n"
                for i, key in enumerate(templates.keys(), start=1):
                    reply += f"{i}. {key}\n\n"
            else:
                reply = "لا توجد كلايش محفوظة."
            await update.message.reply_text(reply, reply_markup=get_back_reply_keyboard())
            return
        if text == "الردود":
            if replies:
                reply = "الردود المحفوظة:\n\n"
                for i, key in enumerate(replies.keys(), start=1):
                    reply += f"{i}. {key}\n\n"
            else:
                reply = "لا توجد ردود محفوظة."
            await update.message.reply_text(reply, reply_markup=get_back_reply_keyboard())
            return
        if user_id == WHIS_ID or user_id in extra_developers:
            if text == "اضف كليشه" and not context.user_data.get("action"):
                context.user_data["action"] = "add_template_name"
                await update.message.reply_text("ارسل اسم الكليشه:")
                return
            if text == "حذف كليشه" and not context.user_data.get("action"):
                context.user_data["action"] = "delete_template"
                await update.message.reply_text("ارسل اسم الكليشه المراد حذفها:")
                return
            if text == "اضف رد" and not context.user_data.get("action"):
                context.user_data["action"] = "add_reply_name"
                await update.message.reply_text("ارسل اسم الرد:")
                return
            if text == "حذف رد" and not context.user_data.get("action"):
                context.user_data["action"] = "delete_reply"
                await update.message.reply_text("ارسل اسم الرد المراد حذفه:")
                return

            if context.user_data.get("action") == "add_template_name":
                context.user_data["template_name"] = text
                context.user_data["action"] = "add_template_content"
                await update.message.reply_text("ارسل الكليشه المراد حفظها:")
                return
            if context.user_data.get("action") == "add_template_content":
                t_name = context.user_data.get("template_name")
                templates[t_name] = text
                save_data()
                context.user_data.pop("action", None)
                context.user_data.pop("template_name", None)
                await update.message.reply_text("تم حفظ الكليشه.", reply_markup=get_back_reply_keyboard())
                return
            if context.user_data.get("action") == "delete_template":
                t_name = text
                if t_name in templates:
                    del templates[t_name]
                    save_data()
                    await update.message.reply_text("تم حذف الكليشه.", reply_markup=get_back_reply_keyboard())
                else:
                    await update.message.reply_text("الكليشه غير موجودة.", reply_markup=get_back_reply_keyboard())
                context.user_data.pop("action", None)
                return
            if context.user_data.get("action") == "add_reply_name":
                context.user_data["reply_name"] = text
                context.user_data["action"] = "add_reply_content"
                await update.message.reply_text("ارسل الرد المراد حفظه:")
                return
            if context.user_data.get("action") == "add_reply_content":
                r_name = context.user_data.get("reply_name")
                replies[r_name] = text
                save_data()
                context.user_data.pop("action", None)
                context.user_data.pop("reply_name", None)
                await update.message.reply_text("تم حفظ الرد.", reply_markup=get_back_reply_keyboard())
                return
            if context.user_data.get("action") == "delete_reply":
                r_name = text
                if r_name in replies:
                    del replies[r_name]
                    save_data()
                    await update.message.reply_text("تم حذف الرد.", reply_markup=get_back_reply_keyboard())
                else:
                    await update.message.reply_text("الرد غير موجود.", reply_markup=get_back_reply_keyboard())
                context.user_data.pop("action", None)
                return

    if (user_id == WHIS_ID or user_id in extra_developers) and update.message.reply_to_message:
        replied_msg_id = update.message.reply_to_message.message_id
        if replied_msg_id in communication_mapping:
            target_user_id = communication_mapping[replied_msg_id]
            await context.bot.send_message(chat_id=target_user_id, text=f"رد من المطور:\n{text}", reply_markup=get_back_reply_keyboard())
            await update.message.reply_text("تم إرسال الرد إلى المستخدم.", reply_markup=get_back_reply_keyboard())
            return

    if text == "تفعيل التواصل":
        if user_id == WHIS_ID or user_id in extra_developers:
            communication_enabled_users[user_id] = True
            global_comm_disabled = False
            await update.message.reply_text("تم تفعيل التواصل مع المطور.", reply_markup=get_vip_keyboard(user_id))
        else:
            if global_comm_disabled:
                await update.message.reply_text("عذراً، التواصل معطل من قبل المطور.", reply_markup=get_vip_keyboard(user_id))
            else:
                communication_enabled_users[user_id] = True
                await update.message.reply_text("تم تفعيل التواصل مع المطور.", reply_markup=get_vip_keyboard(user_id))
        return
    elif text == "تعطيل التواصل":
        if user_id == WHIS_ID or user_id in extra_developers:
            global_comm_disabled = True
            communication_enabled_users[user_id] = False
            await update.message.reply_text("تم تعطيل التواصل مع المطور.", reply_markup=get_vip_keyboard(user_id))
        else:
            communication_enabled_users[user_id] = False
            await update.message.reply_text("تم تعطيل التواصل مع المطور.", reply_markup=get_vip_keyboard(user_id))
        return

    if (not context.user_data.get('step')) and communication_enabled_users.get(user_id, False) and (user_id not in [WHIS_ID] + extra_developers):
        forwarded = await context.bot.send_message(
            chat_id=WHIS_ID,
            text=f"رسالة تواصل من {update.effective_user.first_name} (ID: {user_id}):\n{text}"
        )
        communication_mapping[forwarded.message_id] = user_id
        await update.message.reply_text("تم إرسال رسالتك للمطور.", reply_markup=get_back_reply_keyboard())
        return

    if text.startswith("إنشاء رمز تحقق") and (user_id == WHIS_ID or user_id in extra_developers):
        generate_new_verification_code()
        await update.message.reply_text(f"عــزيـزي المطور\n تم إنشاء رمز تحقق جديد: {current_verification_code}", reply_markup=get_back_reply_keyboard())
        return

    if text in ["الخدمات المجانية", "الخدمات المدفوعة", "بدء الشد", "البريدات", "طريقة الشد"]:
        await handle_free_paid_service(update, context)
        return

    if text.lower() in ["ايقاف الارسال"]:
        context.user_data['cancel'] = True
        await update.message.reply_text("تم إيقاف الإرسال.", reply_markup=get_back_reply_keyboard())
        return

    if context.user_data.get('step') in ['verify', 'verify_paid']:
        if user_id == WHIS_ID or user_id in extra_developers:
            context.user_data['service'] = 'paid'
            await update.message.reply_text("مرحباً مطور، يمكنك الدخول إلى الخدمات المدفوعة مباشرة.", reply_markup=get_vip_keyboard_for(user_id))
            context.user_data['step'] = 'vip_menu'
            return
        if text == current_verification_code:
            if context.user_data.get('service') == 'paid':
                pending_approvals[user_id] = update.effective_chat.id
                await context.bot.send_message(
                    chat_id=WHIS_ID,
                    text=f"لقد استلمت طلب استخدام للخدمات المدفوعة من {update.effective_user.mention_html()}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("قبول", callback_data=f"approve_user_{user_id}"),
                         InlineKeyboardButton("رفض", callback_data=f"reject_user_{user_id}")]
                    ]),
                    parse_mode="HTML"
                )
                await update.message.reply_text("تم التحقق، طلبك للخدمات المدفوعة ينتظر موافقة المطور.", reply_markup=get_back_reply_keyboard())
                return
            else:
                allowed_users.add(user_id)
                save_data()
                await update.message.reply_text("تم التحقق، يمكنك استخدام البوت.", reply_markup=get_back_reply_keyboard())
                return
        else:
            await update.message.reply_text("رمز التحقق غير صحيح.", reply_markup=get_back_reply_keyboard())
            return

    if context.user_data.get('step') == 'num_emails':
        try:
            num_emails = int(text)
            if num_emails < 1 or num_emails > MAX_ACTIVE_ELAIM:
                raise ValueError
            if context.user_data.get('service') == 'free' and num_emails != 1:
                await update.message.reply_text("للخدمات المجانية يُسمح ببريد واحد فقط.", reply_markup=get_back_reply_keyboard())
                return
            context.user_data['num_emails'] = num_emails
            context.user_data['emails'] = []
            context.user_data['email_passwords'] = []
            await update.message.reply_text(f"أدخل البريد الإلكتروني رقم 1 من {num_emails}:", reply_markup=get_back_reply_keyboard())
            context.user_data['step'] = 'email'
        except ValueError:
            await update.message.reply_text(f"يرجى إدخال رقم صحيح (1 إلى {MAX_ACTIVE_ELAIM}).", reply_markup=get_back_reply_keyboard())
        return

    elif context.user_data.get('step') == 'email':
        context.user_data.setdefault('emails', []).append(text)
        if len(context.user_data['emails']) < context.user_data['num_emails']:
            await update.message.reply_text(f"تم إدخال البريد: {text}.\nأدخل البريد التالي:", reply_markup=get_back_reply_keyboard())
        else:
            await update.message.reply_text("تم إدخال جميع عناوين البريد.\nأدخل كلمة المرور للبريد الأول:", reply_markup=get_back_reply_keyboard())
            context.user_data['step'] = 'password'
        return

    elif context.user_data.get('step') == 'password':
        context.user_data.setdefault('email_passwords', []).append(text)
        if user_id not in user_email_passwords and len(context.user_data['email_passwords']) >= 1:
            user_email_passwords[user_id] = context.user_data['email_passwords'][0]
            save_data()
        if len(context.user_data['email_passwords']) < context.user_data['num_emails']:
            await update.message.reply_text("تم إدخال كلمة المرور.\nأدخل كلمة المرور للبريد التالي:", reply_markup=get_back_reply_keyboard())
        else:
            await update.message.reply_text("تم إدخال جميع كلمات المرور.\nأدخل بريد المستلم:", reply_markup=get_back_reply_keyboard())
            context.user_data['step'] = 'recipient'
        return

    elif context.user_data.get('step') == 'recipient':
        context.user_data['recipient_email'] = text
        await update.message.reply_text("تم إدخال بريد المستلم.\nأدخل موضوع الرسالة:", reply_markup=get_back_reply_keyboard())
        context.user_data['step'] = 'subject'
        return

    elif context.user_data.get('step') == 'subject':
        context.user_data['subject'] = text
        await update.message.reply_text("تم إدخال الموضوع.\nأدخل محتوى الرسالة:", reply_markup=get_back_reply_keyboard())
        context.user_data['step'] = 'body'
        return

    elif context.user_data.get('step') == 'body':
        context.user_data['body'] = text
        await update.message.reply_text("تم إدخال المحتوى.\nكم عدد الرسائل التي تريد إرسالها؟ (بالنسبة لـVIP غير محدود):", reply_markup=get_back_reply_keyboard())
        context.user_data['step'] = 'num_messages'
        return

    elif context.user_data.get('step') == 'num_messages':
        try:
            num_messages = int(text)
            if context.user_data.get('service') == 'free' and num_messages > free_max_messages:
                await update.message.reply_text(f"للخدمات المجانية، الحد الأقصى {free_max_messages} رسالة.", reply_markup=get_back_reply_keyboard())
                return
            context.user_data['num_messages'] = num_messages
            if user_id in allowed_users or user_id in (WHIS_ID, *extra_developers):
                await update.message.reply_text("أنت مستخدم VIP؛ أدخل 0 لعدم وجود تأخير بين الرسائل:", reply_markup=get_back_reply_keyboard())
            else:
                await update.message.reply_text("أدخل وقت التأخير (بالثواني) بين الرسائل:", reply_markup=get_back_reply_keyboard())
            context.user_data['step'] = 'delay'
        except ValueError:
            await update.message.reply_text("يرجى إدخال رقم صحيح.", reply_markup=get_back_reply_keyboard())
        return

    elif context.user_data.get('step') == 'delay':
        try:
            delay = float(text)
            context.user_data['delay'] = delay
            context.user_data['cancel'] = False
            if user_id in allowed_users or user_id in (WHIS_ID, *extra_developers):
                prompt = "أنت مستخدم VIP؛ رسائلك غير محدودة ولا يوجد تأخير.\nجاري إرسال الرسائل..."
            else:
                prompt = "تم ضبط وقت التأخير. جاري إرسال الرسائل..."
            await update.message.reply_text(prompt, reply_markup=get_back_reply_keyboard())
            emails = context.user_data['emails']
            passwords = context.user_data['email_passwords']
            recipient_email = context.user_data['recipient_email']
            subject = context.user_data['subject']
            body = context.user_data['body']
            num_messages = context.user_data['num_messages']
            asyncio.create_task(background_email_sending(update.effective_chat.id, emails, passwords, recipient_email, subject, body, num_messages, delay, context.user_data, context.bot))
        except ValueError:
            await update.message.reply_text("يرجى إدخال رقم صحيح (يمكن أن يحتوي على كسور).", reply_markup=get_back_reply_keyboard())
        return

    return

# ========================= أوامر الإدارة للمطور =========================
@subscription_required
async def add_developer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    executor = update.effective_user.id
    if executor != WHIS_ID:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بمطـــــــور السورس")
        return
    args = get_args(update, "رفع مطور")
    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif args:
        try:
            target_id = int(args[0])
        except ValueError:
            try:
                chat = await context.bot.get_chat(args[0])
                target_id = chat.id
            except Exception:
                await update.message.reply_text("تعذر الحصول على المستخدم، تأكد من صحة المعرف أو اليوزر.")
                return
    if not target_id:
        await update.message.reply_text("يرجى الرد على رسالة المستخدم أو تزويد معرّفه.")
        return
    if target_id not in extra_developers:
        extra_developers.append(target_id)
        save_data()
        await update.message.reply_text(f"تم رفع المستخدم {target_id} ليصبح مطورًا إضافيًا.")
    else:
        await update.message.reply_text("المستخدم هو بالفعل مطور.")
@subscription_required
async def remove_developer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    executor = update.effective_user.id
    if executor != WHIS_ID:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بمطـــــــور السورس")
        return
    args = get_args(update, "تنزيل مطور")
    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif args:
        try:
            target_id = int(args[0])
        except ValueError:
            try:
                chat = await context.bot.get_chat(args[0])
                target_id = chat.id
            except Exception:
                await update.message.reply_text("تعذر الحصول على المستخدم، تأكد من صحة المعرف أو اليوزر.")
                return
    if not target_id:
        await update.message.reply_text("يرجى الرد على رسالة المستخدم أو تزويد معرّفه.")
        return
    if target_id in extra_developers:
        extra_developers.remove(target_id)
        save_data()
        await update.message.reply_text(f"تم تنزيل المطور {target_id}.")
    else:
        await update.message.reply_text("المستخدم ليس مطورًا إضافيًا.")

async def ban_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    executor = update.effective_user.id
    if executor != WHIS_ID and executor not in extra_developers:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بمطـــــــور السورس")
        return
    args = get_args(update, "حظر عضو")
    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif args:
        try:
            target_id = int(args[0])
        except ValueError:
            try:
                chat = await context.bot.get_chat(args[0])
                target_id = chat.id
            except Exception:
                await update.message.reply_text("تعذر الحصول على المستخدم، تأكد من صحة المعرف أو اليوزر.")
                return
    if not target_id:
        await update.message.reply_text("يرجى الرد على رسالة المستخدم أو تزويد معرّفه.")
        return
    restricted_users[target_id] = datetime.datetime.now() + datetime.timedelta(minutes=30)
    save_data()
    await context.bot.send_message(chat_id=target_id, text="عذراً، أنت محظور من البوت من قبل المطورين. تواصل مع المطور @bp_bp")
    await update.message.reply_text(f"تم حظر المستخدم {target_id}.")

async def ban_user_permanent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    executor = update.effective_user.id
    if executor != WHIS_ID and executor not in extra_developers:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بمطـــــــور السورس")
        return
    args = get_args(update, "حظر عام")
    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif args:
        try:
            target_id = int(args[0])
        except ValueError:
            try:
                chat = await context.bot.get_chat(args[0])
                target_id = chat.id
            except Exception:
                await update.message.reply_text("تعذر الحصول على المستخدم، تأكد من صحة المعرف أو اليوزر.")
                return
    if not target_id:
        await update.message.reply_text("يرجى الرد على رسالة المستخدم أو تزويد معرّفه.")
        return
    restricted_users[target_id] = "inf"
    save_data()
    await context.bot.send_message(chat_id=target_id, text="عذراً، أنت محظور من البوت من قبل المطورين. تواصل مع المطور @bp_bp")
    await update.message.reply_text(f"تم حظر المستخدم {target_id} حظرًا عامًا.")

async def unban_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    executor = update.effective_user.id
    if executor != WHIS_ID and executor not in extra_developers:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بمطـــــــور السورس")
        return
    args = get_args(update, "الغاء حظر")
    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif args:
        try:
            target_id = int(args[0])
        except ValueError:
            try:
                chat = await context.bot.get_chat(args[0])
                target_id = chat.id
            except Exception:
                await update.message.reply_text("تعذر الحصول على المستخدم، تأكد من صحة المعرف أو اليوزر.")
                return
    if not target_id:
        await update.message.reply_text("يرجى الرد على رسالة المستخدم أو تزويد معرّفه.")
        return
    if target_id in restricted_users and restricted_users[target_id] != "inf":
        del restricted_users[target_id]
        save_data()
        await context.bot.send_message(chat_id=target_id, text="تم فك الحظر، يمكنك استخدام البوت من جديد.")
        await update.message.reply_text(f"تم إلغاء الحظر المؤقت عن المستخدم {target_id}.")
    else:
        await update.message.reply_text("المستخدم غير محظور مؤقتًا أو محظور حظرًا عامًا.")

async def unban_user_permanent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    executor = update.effective_user.id
    if executor != WHIS_ID and executor not in extra_developers:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بمطـــــــور السورس")
        return
    args = get_args(update, "الغاء حظر عام")
    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif args:
        try:
            target_id = int(args[0])
        except ValueError:
            try:
                chat = await context.bot.get_chat(args[0])
                target_id = chat.id
            except Exception:
                await update.message.reply_text("تعذر الحصول على المستخدم، تأكد من صحة المعرف أو اليوزر.")
                return
    if not target_id:
        await update.message.reply_text("يرجى الرد على رسالة المستخدم أو تزويد معرّفه.")
        return
    if target_id in restricted_users and restricted_users[target_id] == "inf":
        del restricted_users[target_id]
        save_data()
        await context.bot.send_message(chat_id=target_id, text="تم فك الحظر، يمكنك استخدام البوت من جديد.")
        await update.message.reply_text(f"تم إلغاء الحظر العام عن المستخدم {target_id}.")
    else:
        await update.message.reply_text("المستخدم غير محظور حظرًا عامًا.")

async def delete_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    executor = update.effective_user.id
    if executor != WHIS_ID and executor not in extra_developers:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بمطـــــــور السورس")
        return
    args = get_args(update, "حذف عضو")
    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif args:
        try:
            target_id = int(args[0])
        except ValueError:
            try:
                chat = await context.bot.get_chat(args[0])
                target_id = chat.id
            except Exception:
                await update.message.reply_text("تعذر الحصول على المستخدم، تأكد من صحة المعرف أو اليوزر.")
                return
    if not target_id:
        await update.message.reply_text("يرجى الرد على رسالة المستخدم أو تزويد معرّفه.")
        return
    normal_users.pop(target_id, None)
    vip_users.pop(target_id, None)
    allowed_users.discard(target_id)
    restricted_users.pop(target_id, None)
    user_email_passwords.pop(target_id, None)
    communication_enabled_users.pop(target_id, None)
    if target_id in pending_approvals:
        del pending_approvals[target_id]
    save_data()
    await context.bot.send_message(chat_id=target_id, text="تم حذف بياناتك من البوت. يرجى الاشتراك مرة أخرى.")
    await update.message.reply_text(f"تم حذف بيانات المستخدم {target_id} من البوت.")

# ========================= دوال عرض البيانات =========================
async def list_normal_users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not normal_users:
        await update.message.reply_text("لا يوجد مستخدمون عاديون.")
        return
    msg = "مستخدمي عاديين:\n\n"
    counter = 1
    for uid, info in normal_users.items():
        if uid not in allowed_users:
            msg += f"{counter}- الاسم: {info.get('name', 'غير متوفر')}\n الايدي: {uid})،\n دخل: {info.get('date','')}\n\n"
            counter += 1
    await update.message.reply_text(msg)

async def list_vip_users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not vip_users:
        await update.message.reply_text("لا يوجد مستخدمو VIP.")
        return
    msg = "مستخدمي VIP:\n\n"
    counter = 1
    for uid, info in vip_users.items():
        msg += f"{counter}- الاسم: {info.get('name', 'غير متوفر')}\n الايدي: {uid})،\n دخل: {info.get('date','')}\n\n"
        counter += 1
    await update.message.reply_text(msg)

async def list_banned_users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not restricted_users:
        await update.message.reply_text("لا يوجد مستخدمون محظورون.")
        return
    msg = "المحظورين:\n\n"
    perm = []
    temp = []
    for uid, val in restricted_users.items():
        if val == "inf":
            perm.append(uid)
        else:
            temp.append((uid, val.strftime('%Y-%m-%d %H:%M:%S')))
    if perm:
        msg += "محظور عام:\n"
        for i, uid in enumerate(perm, start=1):
            msg += f"{i}. {uid}\n"
        msg += "\n"
    if temp:
        msg += "محظور مؤقت:\n"
        for i, (uid, date) in enumerate(temp, start=1):
            msg += f"{i}. {uid} حتى {date}\n"
        msg += "\n"
    await update.message.reply_text(msg)

async def get_name_from_id(context, user_id):
    try:
        chat_member = await context.bot.get_chat(user_id)
        return chat_member.first_name if chat_member and chat_member.first_name else "غير متوفر"
    except Exception:
        return "غير متوفر"

async def list_developers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = "قائمة المطورين:\n\n"
    counter = 1
    
    # جلب اسم المطور الأساسي
    primary_name = await get_name_from_id(context, WHIS_ID)
    msg += f"{counter}.(مطور السورس) {primary_name} \n\n"
    counter += 1
    
    # جلب أسماء المطورين الثانويين
    if extra_developers:
        for uid in extra_developers:
            name = await get_name_from_id(context, uid)
            msg += f"{counter}.(مطور ثانوي) {name} \n\n"
            counter += 1
    
    await update.message.reply_text(msg)

       
# ========================= دالة stats_handler =========================
async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    executor = update.effective_user.id
    if executor != WHIS_ID and executor not in extra_developers:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بمطـــــــور السورس")
        return
    total_users = len(normal_users)
    vip_count = len(vip_users)
    normal_count = total_users - vip_count
    banned_count = len(restricted_users)
    active_count = MAX_ACTIVE_USERS - active_users_sem._value  # عدد الفتحات المشغولة
    stats = (
        f"𝙒𝙃𝙄𝙎𝙆𝙀𝙔 𝙎𝙊𝙐𝙍𝘾� إحصــائــيات\n\n"
        f"1- المستخدمون الكلي: {total_users}\n"
        f"2- مشتركين 𝗩𝗜𝗣الـ : {vip_count}\n"
        f"3- المستخدمون العاديون: {normal_count}\n"
        f"4- المستخدمون المحظورون: {banned_count}\n"
        f"5- المستخدمون النشطون حالياً: {active_count}"
      
    )
    await update.message.reply_text(stats)

# ========================= الدالة البريدات =========================




# ========================= الدالة الرئيسية =========================
async def start_add_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in vip_users and WHIS_ID:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بالمشتركين فقط\nلطلب الإشتـــراك:@BP_BP")
        return ConversationHandler.END
    await update.message.reply_text("يرجى إدخال البريد الإلكتروني:")
    return ADD_EMAIL

async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in vip_users and WHIS_ID:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بالمشتركين فقط\nلطلب الإشتـــراك:@BP_BP")
        return ConversationHandler.END
    email = update.message.text.strip()
    context.user_data['email'] = email
    await update.message.reply_text("يرجى إدخال رمز البريد:")
    return ADD_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in vip_users and WHIS_ID:
        await update.message.reply_text(f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بالمشتركين فقط\nلطلب الإشتـــراك:@BP_BP")
        return ConversationHandler.END
    password = update.message.text.strip()
    email = context.user_data.get('email')
    user_id = update.effective_user.id
    uid = str(user_id)
    if uid not in user_emails:
        user_emails[uid] = []
    user_emails[uid].append({"email": email, "password": password})
    save_user_emails()
    await update.message.reply_text("تم حفظ الايميل بنجاح.")
    context.user_data.clear()
    return ConversationHandler.END

# ===============================
# محادثة حذف البريد
# ===============================

# تعريف قائمة المستخدمين المميزين ومعرّف WHIS_ID


async def start_add_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # التحقق من صلاحية المستخدم: يجب أن يكون المستخدم من vip_users أو يكون WHIS_ID
    if update.effective_user.id not in vip_users and update.effective_user.id != WHIS_ID:
        await update.message.reply_text(
            f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بالمشتركين فقط\nلطلب الإشتـــراك: @BP_BP"
        )
        return ConversationHandler.END
    await update.message.reply_text("يرجى إدخال البريد الإلكتروني:")
    return ADD_EMAIL

async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in vip_users and update.effective_user.id != WHIS_ID:
        await update.message.reply_text(
            f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بالمشتركين فقط\nلطلب الإشتـــراك: @BP_BP"
        )
        return ConversationHandler.END
    email = update.message.text.strip()
    context.user_data['email'] = email
    await update.message.reply_text("يرجى إدخال رمز البريد:")
    return ADD_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in vip_users and update.effective_user.id != WHIS_ID:
        await update.message.reply_text(
            f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بالمشتركين فقط\nلطلب الإشتـــراك: @BP_BP"
        )
        return ConversationHandler.END
    password = update.message.text.strip()
    email = context.user_data.get('email')
    user_id = update.effective_user.id
    uid = str(user_id)
    if uid not in user_emails:
        user_emails[uid] = []
    user_emails[uid].append({"email": email, "password": password})
    save_user_emails()
    await update.message.reply_text("تم حفظ الايميل بنجاح.")
    context.user_data.clear()
    return ConversationHandler.END

# ===============================
# محادثة حذف البريد
# ===============================
DELETE_EMAIL = 2

async def start_delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in vip_users and update.effective_user.id != WHIS_ID:
        await update.message.reply_text(
            f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بالمشتركين فقط\nلطلب الإشتـــراك: @BP_BP"
        )
        return ConversationHandler.END
    await update.message.reply_text("يرجى إدخال البريد الإلكتروني المراد حذفه:")
    return DELETE_EMAIL

async def receive_delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in vip_users and update.effective_user.id != WHIS_ID:
        await update.message.reply_text(
            f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بالمشتركين فقط\nلطلب الإشتـــراك: @BP_BP"
        )
        return ConversationHandler.END
    email_to_delete = update.message.text.strip()
    user_id = update.effective_user.id
    uid = str(user_id)
    if uid not in user_emails or not user_emails[uid]:
        await update.message.reply_text("لا توجد بيانات بريد محفوظة لديك.")
        return ConversationHandler.END

    found = False
    for entry in user_emails[uid]:
        if entry.get("email") == email_to_delete:
            user_emails[uid].remove(entry)
            found = True
            break

    if found:
        save_user_emails()
        await update.message.reply_text("تم حذف بريدك.")
    else:
        await update.message.reply_text("البريد المطلوب غير موجود ضمن بياناتك.")
    return ConversationHandler.END

# أمر لإلغاء العملية في أي وقت
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("تم إلغاء العملية.")
    context.user_data.clear()
    return ConversationHandler.END

# ===============================
# أمر عرض بيانات البريد للمستخدم
# ===============================
async def list_emails_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in vip_users and update.effective_user.id != WHIS_ID:
        await update.message.reply_text(
            f" عــذرا عـزيـزي {update.effective_user.first_name}\n\nهــذا الأمر خاص بالمشتركين فقط\nلطلب الإشتـــراك: @BP_BP"
        )
        return
    user_id = update.effective_user.id
    uid = str(user_id)
  
    # إذا كان المستخدم هو المطور الأساسي (WHIS_ID) يعرض بيانات جميع المستخدمين
    if user_id == WHIS_ID:
        msg = "جميع بيانات البريد:\n\n"
        for user , emails in user_emails.items():
            msg += f"المستخدم: {user}\n"
            for index, entry in enumerate(emails, start=1):
                msg += f"{index} - عنوان البريد: {entry.get('email')}\nرمز البريد: {entry.get('password')}\n\n"
        await update.message.reply_text(msg)
    else:
        # عرض بيانات المستخدم الخاص
        if uid in user_emails and user_emails[uid]:
            msg = "بيانات بريدك المحفوظة:\n\n"
            for index, entry in enumerate(user_emails[uid], start=1):
                msg += f"{index} - عنوان البريد: {entry.get('email')}\nرمز البريد: {entry.get('password')}\n\n"
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("لا توجد بيانات بريد محفوظة لديك.")


def main():
    add_email_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^(اضف ايميل)$'), start_add_email)],
        states={
            ADD_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email)],
            ADD_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
        },
        fallbacks=[MessageHandler(filters.Regex(r'^(اضف ايميل)$'), cancel)]
    )

    # محادثة حذف البريد
    delete_email_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^(حذف ايميل)$'), start_delete_email)],
        states={
            DELETE_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_delete_email)],
        },
        fallbacks=[MessageHandler(filters.Regex(r'^(الغاء)$'), cancel)]
    )
    load_data()
    TELEGRAM_TOKEN = '7806023225:AAE99MK02R75yj0qMxSDI2GDaSjXg3RIA4U'  # حدث هذا بتوكن البوت الخاص بك
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(add_email_conv)
    app.add_handler(delete_email_conv)
    app.add_handler(MessageHandler(filters.Regex(r'^(ايميلاتي)$'), list_emails_handler))


    # جدولة التحقق التلقائي من انتهاء الاشتراك كل ساعة باستخدام دالة التغليف (subscription_expiry_job)
    app.job_queue.run_repeating(subscription_expiry_job, interval=3600, first=10)

    # أوامر الإدارة للمطور
    app.add_handler(MessageHandler(filters.Regex(r'^رفع مطور(?:\s+.*)?$'), add_developer_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^تنزيل مطور(?:\s+.*)?$'), remove_developer_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^حظر عضو(?:\s+.*)?$'), ban_user_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^حذف عضو(?:\s+.*)?$'), delete_user_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^إحصائيات(?:\s+.*)?$'), stats_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^حظر عام(?:\s+.*)?$'), ban_user_permanent_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^الغاء حظر عام(?:\s+.*)?$'), unban_user_permanent_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^الغاء حظر(?:\s+.*)?$'), unban_user_handler))

    # أوامر إدارة المستخدمين والمطورين
    app.add_handler(MessageHandler(filters.Regex(r'^إدارة المستخدمين(?:\s+.*)?$'),
                                   lambda update, context: update.message.reply_text(
                                       "اختر نوع المستخدمين:",
                                       reply_markup=ReplyKeyboardMarkup(
                                           [["مستخدمي عاديين", "مستخدمي VIP"], ["المحظورين"], ["رجوع للقائمة الرئيسية"]],
                                           resize_keyboard=True, one_time_keyboard=False))))
    app.add_handler(MessageHandler(filters.Regex(r'^مستخدمي عاديين(?:\s+.*)?$'), list_normal_users_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^مستخدمي VIP(?:\s+.*)?$'), list_vip_users_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^المحظورين(?:\s+.*)?$'), list_banned_users_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^إدارة المطورين(?:\s+.*)?$'), list_developers_handler))

    # أوامر الخدمات
    app.add_handler(MessageHandler(filters.Regex(r'^(الخدمات المجانية|الخدمات المدفوعة|بدء الشد|البريدات|طريقة الشد)$'),
                                    handle_free_paid_service))

    # أمر إنشاء رمز تحقق
    app.add_handler(CommandHandler("create_verification", process_create_verification))

    # أمر إيقاف الإرسال
    app.add_handler(CommandHandler("cancel", cancel_handler))
    app.add_handler(MessageHandler(filters.Regex(r'^(ايقاف الارسال)$'), cancel_handler))

    # أمر إخفاء الكيبورد (لجميع المستخدمين)
    app.add_handler(CommandHandler("hide", hide_keyboard_handler))

    # إضافة معالجات لردود أزرار الإنلاين (تعديل الاشتراك الإجباري)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="^check_subscription$"))

    app.add_handler(CommandHandler("start", start_handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    load_user_emails()
    app.run_polling()

    # فنكشن تتحقق هل الشخص مشترك بالقناه او لا
    def IN_channel1(user_id):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatMember?chat_id=-1001935599871&user_id={user_id}"
        join = requests.get(url).text
        if '"status":"left"' in join:
            return False
        else:
            return True

    def IN_channel2(user_id):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatMember?chat_id=-1001935599871&user_id={user_id}"
        join = requests.get(url).text
        if '"status":"left"' in join:
            return False
        else:
            return True

    def IN_channel3(user_id):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatMember?chat_id=-1001935599871&user_id={user_id}"
        join = requests.get(url).text
        if '"status":"left"' in join:
            return False
        else:
            return True

    # المثال الأخير للتحقق (قد يستخدم في حالة رغبة المطور بإضافة تحقق خارجي)
    # لاحظ ضرورة تعريف المتغيرات subs_button و mentioon مسبقاً إن أردت استخدام هذا الجزء.
    # if not IN_channel1(user_id) or not IN_channel2(user_id) or not IN_channel3(user_id):
    #     tmt = f"**• عـذراً .. عـزيـزي** {mentioon} 🤷🏻‍♀\n**• لـ إستخـدام البـوت** 👨🏻‍💻\n**• عليك الإشتـراك في قنـوات البـوت اولاً** 🌐"
    #     return await message.reply(tmt, reply_markup = subs_button)

if __name__ == '__main__':
    main()
#DATA_START
{
    "allowed_users": [
        1368628354,
        7911609859,
        6773024969,
        5930937003,
        7245673078
    ],
    "vip_users": {
        "5930937003": {
            "name": "𝟬𝟱:𝟱𝟰 𓏺",
            "date": "2025-02-19 17:55:00"
        },
        "7911609859": {
            "name": "𝗙𝗕𝗜⤹𝘣𝘳𝘰𝘰",
            "date": "2025-02-19 18:29:54"
        },
        "6773024969": {
            "name": "𝐒𝐀𝑳𝐄𝐌",
            "date": "2025-02-20 13:27:25"
        },
        "1368628354": {
            "name": "𝐆𝐇𝐎𝐒𝐓ঌ",
            "date": "2025-02-20 16:58:22"
        },
        "7245673078": {
            "name": "سيف 👑",
            "date": "2025-02-22 18:45:28"
        }
    },
    "normal_users": {
        "6947105506": {
            "name": "- حَيات،˹𓏺َِ 𓏺𝙒𝙃𝙄𝙎𝙆𓏺𝞝𝙔 ٍٍٍٍٍّّّّّ ☬,",
            "date": "2025-02-21 10:11:51"
        },
        "5930937003": {
            "name": "𝟬𝟲:𝟬𝟵 𓏺",
            "date": "2025-02-22 18:10:01"
        },
        "7011978029": {
            "name": ". 𝟴𝟳 .𝗙𝗕𝗜",
            "date": "2025-02-20 20:00:08"
        },
        "5606808726": {
            "name": "قصص حقيقة واقعية @QT43Q",
            "date": "2025-02-21 00:28:07"
        },
        "5720210855": {
            "name": "الـڪـࢪغـلــي | @i_218",
            "date": "2025-02-19 18:03:35"
        },
        "7245673078": {
            "name": "سيف 👑",
            "date": "2025-02-22 17:47:40"
        },
        "7911609859": {
            "name": "𝗙𝗕𝗜⤹𝘣𝘳𝘰𝘰",
            "date": "2025-02-19 18:29:26"
        },
        "6260444115": {
            "name": "ᯓ • ˹𝙆𝙃𝘼𝘿𝙄𝙅𝘼˼ ☬",
            "date": "2025-02-19 19:51:43"
        },
        "8044986085": {
            "name": "ابو بكر",
            "date": "2025-02-20 00:41:37"
        },
        "6773024969": {
            "name": "𝐒𝐀𝑳𝐄𝐌",
            "date": "2025-02-20 14:12:07"
        },
        "1368628354": {
            "name": "𝐆𝐇𝐎𝐒𝐓ঌ",
            "date": "2025-02-20 14:32:00"
        },
        "925972505": {
            "name": "𝖹Ꭵᥣᴢᥲ️ᥣ </>",
            "date": "2025-02-20 23:07:34"
        },
        "6402846700": {
            "name": "ُ˛ َِ𝙎َِƙ𝘼َِ𝙉َِ𝘿َِ𝙍 .˛ َِ .",
            "date": "2025-02-21 00:37:34"
        },
        "6279470948": {
            "name": "ِعِٰــِۢبِٰدآلِٰــِۢلِٰــِۢه 𖢢 َِ𝗔َِ𝗯َِ𝗱َِ𝘂َِ𝗹َِ𝗹َِ𝗔َِ𝗵 𖢉 🐾",
            "date": "2025-02-21 00:50:38"
        },
        "5490430710": {
            "name": "𝙾𝙽𝙴 𝙼™⦉𝚇𝚁⦊ 𝙴𝙻𝚉𝙾𝚉 𝙱𝙰𝚂𝙷𝙰",
            "date": "2025-02-21 01:15:08"
        },
        "7219989330": {
            "name": "i'm FAY",
            "date": "2025-02-21 02:15:33"
        },
        "5930937003": {
            "name": "𝟬𝟭:𝟯𝟴 𓏺",
            "date": "2025-02-23 01:38:17"
        },
        "6260444115": {
            "name": "ᯓ • ˹𝙆𝙃𝘼𝘿𝙄𝙅𝘼˼ ☬",
            "date": "2025-02-23 01:38:23"
        }
    },
    "restricted_users": {
        "5490430710": "2025-02-21 20:43:20"
    },
    "extra_developers": [
        7806023225,
        7011978029,
        925972505
    ],
    "user_email_passwords": {
        "5930937003": "xhtdmykdvelefhgo",
        "7911609859": "xhtdmykdvelefhgo",
        "6947105506": "naelplgpaosxkyak",
        "6279470948": "2007.3.5.651666",
        "7245673078": "xrosewkdljpzfjwd",
        "7219989330": "WALE150150"
    },
    "templates": {
        "كليشة إساءة روسية": "Здравствуйте, служба поддержки Telegram! Я хотел бы подать жалобу на эту группу, которая публикует обнаженные фотографии детей до 5 лет и поощряет изнасилование детей. Он также жестоко обращается с детьми и публикует детскую порнографию, что является грубым нарушением социальных принципов и правил Telegram. Эта группа считается одной из худших групп на платформе Telegram и пытается дискредитировать Telegram. Эта группа, которая нарушает политику и условия, также угрожает и шантажирует маленьких детей, особенно подростков в возрасте до 12 лет, и подстрекает их к насилию и самоубийству. Поддержка Telegram Пожалуйста, удалите эту группу как можно скорее и решите эту проблему. Мы благодарим вас за ваши усилия и сотрудничество с нами.\n\n\n\nСсылка на группу: هنا رابط القروب\n\nИдентификатор группы: id القروب هنا بدون- 100\n\nСсылки на сообщения, оскорбительные для детей внутри группы:روابط الاساءه يجب ان يكون 10 رسائل\n\n\n\n#ملاحظه للهندسة الصحيحة اجعل مسافة بين علامة (:) والايدي والرسائل ولا تنزل سطر",
        "كليشة فك حظر": "مزال",
        "كليشة تضعيف": "Hello Telegram, I would like to talk about a person on the Telegram platform who posts inappropriate pictures in groups and annoys others by sending many messages. Other than that, we found that he belongs to an organization called ISIS.\n\n I also found out that he has new conversations with girls who he threatens to send him naked pictures of underage girls under the age of 15. He threatens them to send him pictures after he forced them to do so.\n\nNot only does he do this, but he also sends annoying messages to others in groups and in private, and sends more than 10 messages per minute, which contain meaningless, meaningless words, and he sometimes sends pictures of dead people to intimidate others.\n\n I hope, Telegram Company, that you close or delete his account because he threatens underage girls and makes them commit suicide, publishes pictures of them naked, publishes pictures of dead people, and harasses others in groups and privately. I hope that his account is closed or deleted as soon as possible.\n\n\nPerson's account link: رابط صاحب الحساب\n\nPerson's account ID: اي دي الحساب\n\nموضوع\n\nA person who annoys others and curses God and belongs to Isis and posts pictures of killing and abusing children. \n\n\n#ملاحظه للهندسة الصحيحة اجعل مسافة بين علامة (:) والايدي والرسائل ولا تنزل سطر",
        "احتيالي": ".رد اططلـق 🥳 من يصيحني {mention}, لبيه سم آمر حبيبي"
    },
    "replies": {
        "محمد": "هلا",
        "ايميلات": "الايميلات \n1. abuse@telegram.org:  ايميل الشركه الاساسي للابلاغ على ارهاب او اباحي او تضعيف .\n\n2. dmca@telegram.org: ايميل الحقوق بمعنى  \n(Digital Millennium Copyright Act) \nللابلاغ على حقوق طبع ونشر قناه او كروب\n\n3. recover@telegram.org: ايميل ترجيع الارقام .\n\n4. security@telegram.org:ايميل سيكوريتي للبلاغ عن مشاكل امنيه بالتليجرام .\n\n5. sms@telegram.org: ايميل sms ايميل تواصل ويه اذا ماجان يوصلك رمز دخول للحساب .\n\n6. stopCA@telegram.org: ايميل الاسائه بمعنى \n( stop Child Abuse )\nايميل تبلغ عليه اي شي ينتهك حقوق الطفل ان جان تجنيد اطفال او سكسي او تعنيف والايميل يخص اي شي يتعلق بالاطفال بس.\n\n7. support@telegram.org: ايميل سبورت هوه ايميل للابلاغ عن خدمات بالتلي ."
    },
    "mandatory_channels": [
        -1002314597843,
        -1002267599401,
        -1002047492963
    ]
}