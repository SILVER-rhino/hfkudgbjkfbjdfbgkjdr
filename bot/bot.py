import os
import logging
import asyncio
import re
from typing import Optional
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import jdatetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db import (
    init_db,
    list_reservations_for_user,
    is_slot_reserved,
    get_slot_owner_user_id,
    try_reserve_slot,
    try_hold_slot_pending_payment,
    upsert_user,
    set_user_subscription,
    list_subscribed_user_ids,
    get_admin_stats,
    list_reservations_due_for_reminder,
    mark_reservation_reminded,
    create_verification_request,
    get_verification_request,
    set_verification_status,
    upsert_verified_card,
    get_verified_card_number,
    create_payment_request,
    get_payment_request,
    set_payment_status,
    create_discount_code,
    can_use_discount_code,
    normalize_discount_code,
    consume_discount_code,
    get_reservation,
    get_reservation_full,
    set_reservation_status,
    update_reservation_promo,
    update_reservation_destination_links,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ryno_sender_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
CHANNEL_JOIN_URL = os.getenv("CHANNEL_JOIN_URL", "").strip()
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "").strip()

TZ_NAME = os.getenv("TZ_NAME", "Asia/Tehran").strip() or "Asia/Tehran"
TZ = ZoneInfo(TZ_NAME)

DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "4").strip() or "4")

OWNER_CHAT_ID_RAW = os.getenv("OWNER_CHAT_ID", "").strip()
OWNER_CHAT_ID = int(OWNER_CHAT_ID_RAW) if OWNER_CHAT_ID_RAW.isdigit() else None

BOT_ADMIN_IDS_RAW = os.getenv("BOT_ADMIN_IDS", "").strip()
BOT_ADMIN_IDS: set[int] = set()
if BOT_ADMIN_IDS_RAW:
    for part in BOT_ADMIN_IDS_RAW.split(","):
        p = part.strip()
        if p.isdigit():
            BOT_ADMIN_IDS.add(int(p))
if OWNER_CHAT_ID is not None:
    BOT_ADMIN_IDS.add(OWNER_CHAT_ID)

BROADCAST_SLEEP_SECONDS = float(os.getenv("BROADCAST_SLEEP_SECONDS", "0.07").strip() or "0.07")

UD_BROADCAST_STEP = "broadcast_step"
BROADCAST_AWAIT_MESSAGE = "await_broadcast_message"

CB_CONFIRM = "confirm_membership"

CB_SLOT_PREFIX = "slot|"  # slot|YYYY-MM-DD|HH:MM

UD_VERIFICATION_STEP = "verification_step"
VERIF_AWAIT_PHOTO = "await_photo"
VERIF_AWAIT_CARD_NUMBER = "await_card_number"
UD_VERIFICATION_REQUEST_ID = "verification_request_id"

CB_VERIF_PREFIX = "verif|"  # verif|<request_id>|approve|reject_wrong|reject_incomplete

CB_DISCOUNT_PREFIX = "discount|"  # discount|<reservation_id>|yes|no

CB_PAYMENT_PREFIX = "pay|"  # pay|<payment_id>|approve|reject

UD_PAYMENT_STEP = "payment_step"
PAY_AWAIT_RECEIPT = "await_receipt"
PAY_AWAIT_COUPON = "await_coupon"
UD_PAYMENT_RESERVATION_ID = "payment_reservation_id"
UD_PAYMENT_COUPON_CODE = "payment_coupon_code"
UD_PAYMENT_COUPON_PERCENT = "payment_coupon_percent"

UD_TAKHFIF_STEP = "takhfif_step"
TAKHFIF_AWAIT_CODE = "await_code"
TAKHFIF_AWAIT_MAX_USES = "await_max_uses"
TAKHFIF_AWAIT_DURATION = "await_duration"
TAKHFIF_AWAIT_PERCENT = "await_percent"
UD_TAKHFIF_CODE = "takhfif_code"
UD_TAKHFIF_MAX_USES = "takhfif_max_uses"
UD_TAKHFIF_EXPIRES_AT = "takhfif_expires_at"

BOTDATA_OWNER_PENDING_REJECT = "owner_pending_payment_reject"  # owner_id -> payment_id
BOTDATA_USER_AWAIT_BANNER = "user_await_banner"  # user_id(str) -> True
CB_DEST_PREFIX = "dest|"  # dest|<reservation_id>|has|no
UD_DEST_STEP = "dest_step"
DEST_AWAIT_LINKS = "await_dest_links"
UD_DEST_RESERVATION_ID = "dest_reservation_id"
UD_DEST_LINKS_LIST = "dest_links_list"

DEST_FINISH_TEXT = "پایان"

DAY_SAT = "شنبه"
DAY_SUN = "یکشنبه"
DAY_MON = "دوشنبه"
DAY_TUE = "سه شنبه"
DAY_WED = "چهارشنبه"
DAY_THU = "پنجشنبه"
DAY_FRI = "جمعه"

DAY_TO_PERSIAN_WEEKDAY = {
    # Persian week order: Saturday=0 .. Friday=6
    DAY_SAT: 0,
    DAY_SUN: 1,
    DAY_MON: 2,
    DAY_TUE: 3,
    DAY_WED: 4,
    DAY_THU: 5,
    DAY_FRI: 6,
}

PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

WELCOME_TEXT = (
    "خوش امدید به ربات راینو سندر بزرگترین خدمات سندر تلگرام\n"
)


def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("حساب کاربری"), KeyboardButton("رزرو تایم")],
            [KeyboardButton("نرخ")],
            [KeyboardButton("ارتباط با ادمین")],
            [KeyboardButton("احراز هویت")],
        ],
        resize_keyboard=True,
    )


def _back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("بازگشت")]],
        resize_keyboard=True,
    )


def _finish_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(DEST_FINISH_TEXT)], [KeyboardButton("بازگشت")]],
        resize_keyboard=True,
    )


def _format_reserved_at_for_owner(reserved_at_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(reserved_at_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        else:
            dt = dt.astimezone(TZ)
        jdate = jdatetime.date.fromgregorian(date=dt.date())
        date_str = f"{jdate.year:04d}/{jdate.month:02d}/{jdate.day:02d}".translate(PERSIAN_DIGITS)
        time_str = dt.strftime("%H:%M").translate(PERSIAN_DIGITS)
        return f"{date_str} - {time_str}"
    except Exception:
        return reserved_at_iso


def _reserve_days_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(DAY_SAT), KeyboardButton(DAY_SUN)],
            [KeyboardButton(DAY_MON), KeyboardButton(DAY_TUE)],
            [KeyboardButton(DAY_WED), KeyboardButton(DAY_THU)],
            [KeyboardButton(DAY_FRI)],
            [KeyboardButton("بازگشت")],
        ],
        resize_keyboard=True,
    )


def _to_fa_digits(text: str) -> str:
    return text.translate(PERSIAN_DIGITS)


def _target_reservation_date(now: datetime) -> datetime.date:
    # Simplest practical behavior: show today's slots, unless it's already past 23:00 -> show tomorrow.
    if now.timetz() >= time(23, 0, tzinfo=TZ):
        return (now + timedelta(days=1)).date()
    return now.date()


def _persian_weekday(now: datetime) -> int:
    # Convert Python weekday (Mon=0..Sun=6) to Persian (Sat=0..Fri=6)
    return (now.weekday() + 2) % 7


def _next_date_for_persian_weekday(selected_persian_weekday: int, now: datetime) -> datetime.date:
    today_persian = _persian_weekday(now)
    days_ahead = (selected_persian_weekday - today_persian) % 7
    if days_ahead == 0 and now.timetz() >= time(23, 0, tzinfo=TZ):
        days_ahead = 7
    return (now + timedelta(days=days_ahead)).date()


def _time_slots() -> list[time]:
    return [
        time(20, 30),
        time(21, 0),
        time(21, 30),
        time(22, 0),
        time(22, 30),
        time(23, 0),
    ]


async def _render_slots_keyboard(target_date) -> tuple[InlineKeyboardMarkup, int]:
    rows = []
    reserved_count = 0
    for t in _time_slots():
        dt = datetime.combine(target_date, t, tzinfo=TZ)
        reserved = await asyncio.to_thread(is_slot_reserved, dt)
        if reserved:
            reserved_count += 1

        label_time = dt.strftime("%H:%M").translate(PERSIAN_DIGITS)
        label = f"{label_time} {'❌' if reserved else '✅'}"
        cb = f"{CB_SLOT_PREFIX}{target_date.isoformat()}|{t.strftime('%H:%M')}"
        rows.append((label, cb))

    # 2 columns
    keyboard = []
    for i in range(0, len(rows), 2):
        pair = rows[i : i + 2]
        keyboard.append([InlineKeyboardButton(pair[0][0], callback_data=pair[0][1])] + ([InlineKeyboardButton(pair[1][0], callback_data=pair[1][1])] if len(pair) > 1 else []))

    return InlineKeyboardMarkup(keyboard), reserved_count


def _quota_text(reserved_count: int) -> str:
    remaining = max(0, DAILY_LIMIT - reserved_count)
    return (
        f"محدودیت پخشی روزانه درحال حاضر {_to_fa_digits(str(DAILY_LIMIT))} کا پخشی\n"
        f"رزرو شده ها: {_to_fa_digits(str(reserved_count))} کا رزرو شده و فقط {_to_fa_digits(str(remaining))} کا دیگه میتونن رزرو کنند"
    )


async def show_reserve_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    if not await _ensure_member(update, context):
        return

    # This function is kept for internal use; main UX is: رزرو تایم -> choose day.
    now = datetime.now(TZ)
    target_date = _target_reservation_date(now)
    await _send_slots_panel(update, context, target_date)


async def _send_slots_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, target_date) -> None:
    msg = update.effective_message
    if msg is None:
        return

    jdate = jdatetime.date.fromgregorian(date=target_date)
    date_str = f"{jdate.year:04d}/{jdate.month:02d}/{jdate.day:02d}".translate(PERSIAN_DIGITS)

    kb, reserved_count = await _render_slots_keyboard(target_date)
    await msg.reply_text(
        f"رزرو تایم\n"
        f"{_quota_text(reserved_count)}\n\n"
        f"تاریخ: {date_str}\n"
        f"(از ۲۰:۳۰ تا ۲۳:۰۰، هر ۳۰ دقیقه)\n\n"
        f"✅ یعنی آزاد | ❌ یعنی رزرو شده",
        reply_markup=kb,
    )


async def reserve_day_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    if not await _ensure_member(update, context):
        return

    await msg.reply_text(
        "روز مورد نظر برای رزرو را انتخاب کنید:",
        reply_markup=_reserve_days_keyboard(),
    )
    raise ApplicationHandlerStop


async def on_day_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.text is None:
        return

    if not await _ensure_member(update, context):
        return

    day_name = msg.text.strip()
    persian_weekday = DAY_TO_PERSIAN_WEEKDAY.get(day_name)
    if persian_weekday is None:
        return

    # Switch to back keyboard while inside reservation section.
    await msg.reply_text("در حال بارگذاری تایم ها...", reply_markup=_back_keyboard())

    now = datetime.now(TZ)
    target_date = _next_date_for_persian_weekday(persian_weekday, now)
    await _send_slots_panel(update, context, target_date)
    raise ApplicationHandlerStop


async def _ensure_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if user is a member, otherwise sends gate message and returns False."""
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return False

    if not REQUIRED_CHANNEL:
        return True

    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user.id)
        if _is_member(member):
            return True
    except Exception:
        pass

    await msg.reply_text(
        WELCOME_TEXT,
        reply_markup=_build_gate_keyboard(),
        disable_web_page_preview=True,
    )
    return False


def _build_gate_keyboard() -> InlineKeyboardMarkup:
    join_url = CHANNEL_JOIN_URL
    if not join_url and REQUIRED_CHANNEL:
        # If REQUIRED_CHANNEL is like @channel
        join_url = f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"

    keyboard = []
    if join_url:
        keyboard.append([InlineKeyboardButton("عضویت در کانال", url=join_url)])
    else:
        # If no join URL is available, still render a non-link hint button.
        keyboard.append([InlineKeyboardButton("عضویت در کانال (لینک تنظیم نشده)", callback_data="noop")])

    keyboard.append([InlineKeyboardButton("تایید عضویت", callback_data=CB_CONFIRM)])
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return

    user = update.effective_user
    if user is not None:
        username = f"@{user.username}" if user.username else None
        await asyncio.to_thread(upsert_user, user.id, username)
    is_member = False

    if user and REQUIRED_CHANNEL:
        try:
            member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user.id)
            is_member = _is_member(member)
        except Exception:
            # If bot isn't admin or channel is wrong, we'll fall back to showing the gate.
            is_member = False

    # If member, show main menu keyboard; otherwise, show membership gate inline buttons.
    if is_member:
        await update.effective_message.reply_text(
            WELCOME_TEXT,
            reply_markup=_main_menu_keyboard(),
            disable_web_page_preview=True,
        )
    else:
        await update.effective_message.reply_text(
            WELCOME_TEXT,
            reply_markup=_build_gate_keyboard(),
            disable_web_page_preview=True,
        )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    if not await _ensure_member(update, context):
        return

    username = f"@{user.username}" if user.username else None
    await asyncio.to_thread(set_user_subscription, user.id, True, username)
    await msg.reply_text("عضویت شما در اطلاع رسانی فعال شد.", reply_markup=_main_menu_keyboard())


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    username = f"@{user.username}" if user.username else None
    await asyncio.to_thread(set_user_subscription, user.id, False, username)
    await msg.reply_text("عضویت شما در اطلاع رسانی غیرفعال شد.", reply_markup=_main_menu_keyboard())


def _owner_only(user_id: int | None) -> bool:
    return OWNER_CHAT_ID is not None and user_id is not None and user_id == OWNER_CHAT_ID


def _is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in BOT_ADMIN_IDS


def _format_seen_at(seen_at_iso_utc: str | None) -> str:
    if not seen_at_iso_utc:
        return "نامشخص"
    try:
        dt = datetime.fromisoformat(seen_at_iso_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt = dt.astimezone(TZ)
        jdate = jdatetime.date.fromgregorian(date=dt.date())
        date_str = f"{jdate.year:04d}/{jdate.month:02d}/{jdate.day:02d}".translate(PERSIAN_DIGITS)
        time_str = dt.strftime("%H:%M").translate(PERSIAN_DIGITS)
        return f"{date_str} - {time_str}"
    except Exception:
        return seen_at_iso_utc


async def amar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    if not _is_admin(user.id):
        await msg.reply_text("شما دسترسی ندارید.")
        return

    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    since_24h = (now_utc - timedelta(hours=24)).isoformat(timespec="seconds")
    since_7d = (now_utc - timedelta(days=7)).isoformat(timespec="seconds")

    stats = await asyncio.to_thread(get_admin_stats, since_24h, since_7d)

    now_local = datetime.now(TZ)
    jdate = jdatetime.date.fromgregorian(date=now_local.date())
    date_str = f"{jdate.year:04d}/{jdate.month:02d}/{jdate.day:02d}".translate(PERSIAN_DIGITS)
    time_str = now_local.strftime("%H:%M").translate(PERSIAN_DIGITS)

    text = (
        "📊 آمار ربات\n"
        f"گزارش: {date_str} - {time_str}\n\n"
        "👤 کاربران\n"
        f"- کل کاربران ثبت شده: {_to_fa_digits(str(stats.total_users))}\n"
        f"- عضو اطلاع رسانی (/subscribe): {_to_fa_digits(str(stats.subscribed_users))}\n"
        f"- فعال ۲۴ ساعت اخیر: {_to_fa_digits(str(stats.active_24h_users))}\n"
        f"- فعال ۷ روز اخیر: {_to_fa_digits(str(stats.active_7d_users))}\n"
        f"- آخرین فعالیت کاربر: {_format_seen_at(stats.last_user_seen_at)}\n\n"
        "⏱ رزروها\n"
        f"- کل رزروها: {_to_fa_digits(str(stats.reservations_total))}\n"
        f"- رزرو قطعی (booked): {_to_fa_digits(str(stats.reservations_booked))}\n"
        f"- در انتظار پرداخت: {_to_fa_digits(str(stats.reservations_pending_payment))}\n"
        f"- لغوشده: {_to_fa_digits(str(stats.reservations_cancelled))}\n\n"
        "💳 پرداخت ها\n"
        f"- کل رسیدها: {_to_fa_digits(str(stats.payment_total))}\n"
        f"- در انتظار بررسی: {_to_fa_digits(str(stats.payment_pending))}\n"
        f"- تایید شده: {_to_fa_digits(str(stats.payment_approved))}\n"
        f"- رد شده: {_to_fa_digits(str(stats.payment_rejected))}\n\n"
        "🪪 احراز هویت\n"
        f"- کل درخواست ها: {_to_fa_digits(str(stats.verification_total))}\n"
        f"- در انتظار بررسی: {_to_fa_digits(str(stats.verification_pending))}\n"
        f"- تایید شده: {_to_fa_digits(str(stats.verification_approved))}\n"
        f"- رد شده: {_to_fa_digits(str(stats.verification_rejected))}"
    )

    await msg.reply_text(text)


async def hamgani_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    if not _is_admin(user.id):
        await msg.reply_text("شما دسترسی ندارید.")
        return

    if OWNER_CHAT_ID is None:
        await msg.reply_text("OWNER_CHAT_ID تنظیم نشده.")
        return

    context.user_data[UD_BROADCAST_STEP] = BROADCAST_AWAIT_MESSAGE
    await msg.reply_text(
        "پیام/عکس/ویدیو/فایل مورد نظر برای ارسال را همینجا بفرستید.\n"
        "(فقط برای کسانی ارسال می شود که با /subscribe عضو اطلاع رسانی شده اند.)\n"
        "برای لغو: /cancel_hamgani",
    )


async def hamgani_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    if not _is_admin(user.id):
        await msg.reply_text("شما دسترسی ندارید.")
        return

    context.user_data[UD_BROADCAST_STEP] = None
    await msg.reply_text("ارسال همگانی لغو شد.")


async def on_owner_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return

    if not _is_admin(user.id):
        return

    if context.user_data.get(UD_BROADCAST_STEP) != BROADCAST_AWAIT_MESSAGE:
        return

    # Consume this message as broadcast content.
    context.user_data[UD_BROADCAST_STEP] = None

    owner_chat_id = msg.chat_id
    source_chat_id = msg.chat_id
    source_message_id = msg.message_id

    user_ids = await asyncio.to_thread(list_subscribed_user_ids)
    total = len(user_ids)

    if total == 0:
        await context.bot.send_message(chat_id=owner_chat_id, text="هیچ کاربری عضو اطلاع رسانی نیست.")
        raise ApplicationHandlerStop

    sent = 0
    failed = 0
    blocked = 0

    await context.bot.send_message(chat_id=owner_chat_id, text=f"شروع ارسال به {total} نفر...")

    for i, chat_id in enumerate(user_ids, start=1):
        try:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
            )
            sent += 1
        except Forbidden:
            blocked += 1
            failed += 1
            await asyncio.to_thread(set_user_subscription, int(chat_id), False, None)
        except BadRequest:
            failed += 1
        except Exception:
            failed += 1

        if i % 10 == 0 or i == total:
            await context.bot.send_message(
                chat_id=owner_chat_id,
                text=(
                    f"آمار: {i}/{total}\n"
                    f"ارسال موفق: {sent}\n"
                    f"ناموفق: {failed} (بلاک/غیرفعال: {blocked})"
                ),
            )

        if BROADCAST_SLEEP_SECONDS > 0:
            await asyncio.sleep(BROADCAST_SLEEP_SECONDS)

    await context.bot.send_message(
        chat_id=owner_chat_id,
        text=(
            "ارسال همگانی تمام شد.\n"
            f"کل: {total}\n"
            f"موفق: {sent}\n"
            f"ناموفق: {failed} (بلاک/غیرفعال: {blocked})"
        ),
    )

    # Prevent other handlers (e.g., photo/text flows) from processing this owner message.
    raise ApplicationHandlerStop


async def on_admin_capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture admin-only multi-step flows (broadcast, discount wizard, reject reason) safely.

    This avoids handler conflicts caused by overlapping MessageHandlers.
    """

    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return

    if not _is_admin(user.id):
        return

    # 1) If admin is in takhfif wizard, it must win for text messages.
    if msg.text is not None and context.user_data.get(UD_TAKHFIF_STEP):
        await on_takhfif_wizard(update, context)
        return

    # 2) If admin is sending a reject reason, consume it.
    if msg.text is not None:
        pending = context.bot_data.get(BOTDATA_OWNER_PENDING_REJECT, {})
        if pending.get(str(user.id)):
            await on_owner_reject_reason(update, context)
            raise ApplicationHandlerStop

    # 3) If admin is in broadcast mode, consume the next message of any type.
    if context.user_data.get(UD_BROADCAST_STEP) == BROADCAST_AWAIT_MESSAGE:
        await on_owner_broadcast_message(update, context)


async def on_photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route incoming photos to the correct active user flow."""

    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    # Payment receipt photo
    if context.user_data.get(UD_PAYMENT_STEP) == PAY_AWAIT_RECEIPT:
        await on_payment_receipt_photo(update, context)
        return

    # Verification card photo
    if context.user_data.get(UD_VERIFICATION_STEP) == VERIF_AWAIT_PHOTO:
        await on_verification_photo(update, context)
        return

    # Banner promo photo (after approval)
    awaiting = context.bot_data.get(BOTDATA_USER_AWAIT_BANNER, {})
    if awaiting.get(str(user.id)):
        await on_banner_or_link(update, context)


async def on_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route incoming text to the correct active user flow."""

    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.text is None:
        return

    # Payment: awaiting coupon code
    if context.user_data.get(UD_PAYMENT_STEP) == PAY_AWAIT_COUPON:
        await on_coupon_code(update, context)
        return

    # Verification: awaiting card number
    if context.user_data.get(UD_VERIFICATION_STEP) == VERIF_AWAIT_CARD_NUMBER:
        await on_verification_card_number(update, context)
        return

    # Banner/link step (after payment approval)
    awaiting = context.bot_data.get(BOTDATA_USER_AWAIT_BANNER, {})
    if awaiting.get(str(user.id)):
        await on_banner_or_link(update, context)
        return

    # Destination links collection
    if context.user_data.get(UD_DEST_STEP) == DEST_AWAIT_LINKS:
        await on_destination_links(update, context)
        return


def _is_member(member) -> bool:
    # In channels, statuses include: member, administrator, creator, left, kicked, restricted
    if member.status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    }:
        return True

    # Some chat types may return RESTRICTED for members.
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))

    return False


async def confirm_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    user = update.effective_user
    if user is None:
        return

    if not REQUIRED_CHANNEL:
        await query.answer("کانال قفل عضویت تنظیم نشده.", show_alert=True)
        return

    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user.id)
        if _is_member(member):
            await query.answer()
            await query.edit_message_text(
                "عضویت شما تایید شد.",
                disable_web_page_preview=True,
            )

            # Show main menu after successful confirmation
            await context.bot.send_message(
                chat_id=update.effective_chat.id if update.effective_chat else query.message.chat_id,
                text="منوی اصلی:",
                reply_markup=_main_menu_keyboard(),
            )
        else:
            await query.answer("عضو نیستید", show_alert=True)

    except Forbidden:
        # Bot has no access to the chat or isn't admin (common in channels)
        logger.exception("Forbidden while checking membership")
        await query.answer(
            "ربات دسترسی لازم را ندارد. ربات باید ادمین کانال باشد.",
            show_alert=True,
        )
    except BadRequest as e:
        # e.g. chat not found / user not found
        logger.exception("BadRequest while checking membership: %s", e)
        await query.answer(
            "خطا در بررسی عضویت. نام کانال/آیدی کانال را چک کنید.",
            show_alert=True,
        )


async def noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()


async def on_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    if not await _ensure_member(update, context):
        return

    reservations = await asyncio.to_thread(list_reservations_for_user, user.id, 20)
    if reservations:
        lines = []
        for idx, r in enumerate(reservations, start=1):
            dt = datetime.fromisoformat(r.reserved_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            else:
                dt = dt.astimezone(TZ)

            jdate = jdatetime.date.fromgregorian(date=dt.date())
            date_str = f"{jdate.year:04d}/{jdate.month:02d}/{jdate.day:02d}".translate(PERSIAN_DIGITS)
            time_str = dt.strftime("%H:%M").translate(PERSIAN_DIGITS)
            lines.append(f"{_to_fa_digits(str(idx))}) {date_str} - {time_str}")
        reservations_text = "\n".join(lines)
    else:
        reservations_text = "هیچ تایمی رزرو نکرده اید."

    await msg.reply_text(
        f"حساب کاربری شما:\n"
        f"آیدی عددی: {_to_fa_digits(str(user.id))}\n\n"
        f"تایم های رزرو شده شما:\n{reservations_text}",
        reply_markup=_back_keyboard(),
    )
    raise ApplicationHandlerStop


async def on_slot_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    user = update.effective_user
    if user is None:
        await query.answer()
        return

    if not REQUIRED_CHANNEL:
        # No gating configured
        pass
    else:
        try:
            member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user.id)
            if not _is_member(member):
                await query.answer("عضو نیستید", show_alert=True)
                return
        except Exception:
            await query.answer("ربات دسترسی لازم را ندارد. ربات باید ادمین کانال باشد.", show_alert=True)
            return

    data = query.data or ""
    if not data.startswith(CB_SLOT_PREFIX):
        await query.answer()
        return

    try:
        _, date_iso, hhmm = data.split("|", 2)
        target_date = datetime.fromisoformat(date_iso).date()
        hh, mm = map(int, hhmm.split(":", 1))
        slot_dt = datetime.combine(target_date, time(hh, mm), tzinfo=TZ)
    except Exception:
        await query.answer("داده نامعتبر است.", show_alert=True)
        return

    owner_id = await asyncio.to_thread(get_slot_owner_user_id, slot_dt)
    if owner_id is not None:
        if owner_id == user.id:
            await query.answer("این تایم قبلاً توسط شما رزرو شده.", show_alert=True)
        else:
            await query.answer("این تایم قبلاً رزرو شده.", show_alert=True)
        return

    # Enforce daily quota based on real reserved count for this date.
    _, reserved_count = await _render_slots_keyboard(target_date)
    if reserved_count >= DAILY_LIMIT:
        await query.answer("ظرفیت رزرو امروز تکمیل است.", show_alert=True)
        return

    # Require verification before proceeding to payment.
    verified_card = await asyncio.to_thread(get_verified_card_number, user.id)
    if not verified_card:
        await query.answer("ابتدا احراز هویت را انجام دهید.", show_alert=True)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="برای رزرو و خرید، ابتدا از منوی اصلی وارد «احراز هویت» شوید.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    reservation_id = await asyncio.to_thread(try_hold_slot_pending_payment, user.id, slot_dt)
    if reservation_id is None:
        await query.answer("این تایم همین الان رزرو شد.", show_alert=True)
        return

    # Ask discount code question
    jdate = jdatetime.date.fromgregorian(date=target_date)
    date_str = f"{jdate.year:04d}/{jdate.month:02d}/{jdate.day:02d}".translate(PERSIAN_DIGITS)
    time_str = slot_dt.strftime("%H:%M").translate(PERSIAN_DIGITS)

    kb_discount = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "بله ✅",
                    callback_data=f"{CB_DISCOUNT_PREFIX}{reservation_id}|yes",
                ),
                InlineKeyboardButton(
                    "خیر ❌",
                    callback_data=f"{CB_DISCOUNT_PREFIX}{reservation_id}|no",
                ),
            ]
        ]
    )

    await query.answer()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"تایم انتخابی شما: {date_str} - {time_str}\n\nآیا کد تخفیف دارید؟",
        reply_markup=kb_discount,
    )


async def on_discount_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    user = update.effective_user
    if user is None:
        await query.answer()
        return

    data = query.data or ""
    if not data.startswith(CB_DISCOUNT_PREFIX):
        await query.answer()
        return

    try:
        rest = data[len(CB_DISCOUNT_PREFIX) :]
        res_id_str, choice = rest.split("|", 1)
        reservation_id = int(res_id_str)
    except Exception:
        await query.answer("داده نامعتبر است.", show_alert=True)
        return

    verified_card = await asyncio.to_thread(get_verified_card_number, user.id)
    if not verified_card:
        await query.answer("ابتدا احراز هویت را انجام دهید.", show_alert=True)
        return

    if choice == "yes":
        context.user_data[UD_PAYMENT_STEP] = PAY_AWAIT_COUPON
        context.user_data[UD_PAYMENT_RESERVATION_ID] = reservation_id
        await query.answer()
        await query.edit_message_text("کد تخفیف خود را ارسال کنید (اعداد/حروف انگلیسی).")
        return

    if choice == "no":
        context.user_data[UD_PAYMENT_STEP] = PAY_AWAIT_RECEIPT
        context.user_data[UD_PAYMENT_RESERVATION_ID] = reservation_id

        await query.answer()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"• با کارتی که احرازهویت و انتخاب کردید یعنی ( {verified_card} ) به کارت زیر ارسال کنید و فیش واریز خود را همینجا ارسال کنید.\n\n"
                "[ 6219861845420602 ]\n"
                "   به نام : نامق احمدی\n\n"
                "• عکس واریزی رو ارسال کن\n"
                "• ربات اماده دریافت عکس فیش واریزی شما است:"
            ),
        )
        return

    await query.answer("گزینه نامعتبر است.", show_alert=True)


async def on_coupon_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.text is None:
        return

    if not await _ensure_member(update, context):
        return

    if context.user_data.get(UD_PAYMENT_STEP) != PAY_AWAIT_COUPON:
        return

    reservation_id = context.user_data.get(UD_PAYMENT_RESERVATION_ID)
    if not isinstance(reservation_id, int):
        await msg.reply_text("خطا در روند پرداخت. دوباره تلاش کنید: /start")
        return

    code = msg.text.strip()
    if not code or len(code) > 64:
        await msg.reply_text("کد تخفیف نامعتبر است. دوباره ارسال کنید.")
        return

    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")).isoformat(timespec="seconds")
    ok, reason, percent = await asyncio.to_thread(can_use_discount_code, code, now_utc)
    if not ok:
        if reason == "expired":
            await msg.reply_text("این کد تخفیف منقضی شده است.")
        elif reason == "used_up":
            await msg.reply_text("سهمیه این کد تخفیف تمام شده است.")
        else:
            await msg.reply_text("این کد تخفیف معتبر نیست.")
        return

    verified_card = await asyncio.to_thread(get_verified_card_number, user.id)
    if not verified_card:
        await msg.reply_text("ابتدا احراز هویت را انجام دهید.")
        return

    context.user_data[UD_PAYMENT_COUPON_CODE] = normalize_discount_code(code)
    context.user_data[UD_PAYMENT_COUPON_PERCENT] = int(percent or 0)
    context.user_data[UD_PAYMENT_STEP] = PAY_AWAIT_RECEIPT

    await msg.reply_text(
        (
            f"کد تخفیف شما ثبت شد: {code} ({int(percent)}٪)\n\n"
            f"• با کارتی که احرازهویت و انتخاب کردید یعنی ( {verified_card} ) به کارت زیر ارسال کنید و فیش واریز خود را همینجا ارسال کنید.\n\n"
            "[ 6219861845420602 ]\n"
            "   به نام : نامق احمدی\n\n"
            "• عکس واریزی رو ارسال کن\n"
            "• ربات اماده دریافت عکس فیش واریزی شما است:"
        )
    )


async def on_payment_receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    if not await _ensure_member(update, context):
        return

    if context.user_data.get(UD_PAYMENT_STEP) != PAY_AWAIT_RECEIPT:
        return

    reservation_id = context.user_data.get(UD_PAYMENT_RESERVATION_ID)
    if not isinstance(reservation_id, int):
        await msg.reply_text("خطا در روند پرداخت. دوباره تلاش کنید: /start")
        return

    if not BOT_ADMIN_IDS and OWNER_CHAT_ID is None:
        await msg.reply_text("پرداخت در حال حاضر فعال نیست (ادمین تنظیم نشده).")
        return

    verified_card = await asyncio.to_thread(get_verified_card_number, user.id)
    if not verified_card:
        await msg.reply_text("ابتدا احراز هویت را انجام دهید.")
        return

    if not getattr(msg, "photo", None):
        return
    receipt_file_id = msg.photo[-1].file_id

    username = f"@{user.username}" if user.username else None
    coupon = context.user_data.pop(UD_PAYMENT_COUPON_CODE, None)
    coupon_percent = context.user_data.pop(UD_PAYMENT_COUPON_PERCENT, None)

    payment_id = await asyncio.to_thread(
        create_payment_request,
        reservation_id,
        user.id,
        username,
        verified_card,
        coupon,
        int(coupon_percent) if isinstance(coupon_percent, int) else None,
        receipt_file_id,
    )

    res = await asyncio.to_thread(get_reservation, reservation_id)
    reserved_at_text = res.reserved_at if res else "(نامشخص)"

    caption = (
        "خرید کاربر\n\n"
        f"آیدی عددی: {user.id}\n"
        f"یوزرنیم: {username or 'ندارد'}\n"
        f"شماره کارت: {verified_card}\n"
        f"رزرو: {reserved_at_text}\n"
        f"کد پرداخت: {payment_id}" + (f"\nکد تخفیف: {coupon} ({coupon_percent}٪)" if coupon and coupon_percent else (f"\nکد تخفیف: {coupon}" if coupon else ""))
    )

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("تایید ✅", callback_data=f"{CB_PAYMENT_PREFIX}{payment_id}|approve")],
            [InlineKeyboardButton("رد ❌", callback_data=f"{CB_PAYMENT_PREFIX}{payment_id}|reject")],
        ]
    )

    for admin_id in sorted(BOT_ADMIN_IDS) if BOT_ADMIN_IDS else ([OWNER_CHAT_ID] if OWNER_CHAT_ID else []):
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=receipt_file_id,
                caption=caption,
                reply_markup=kb,
            )
        except Exception:
            continue

    context.user_data[UD_PAYMENT_STEP] = None
    context.user_data.pop(UD_PAYMENT_RESERVATION_ID, None)
    await msg.reply_text("فیش شما ارسال شد و در حال بررسی است.", reply_markup=_main_menu_keyboard())


async def on_payment_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    actor = update.effective_user
    if actor is None:
        await query.answer()
        return

    if not _is_admin(actor.id):
        await query.answer("شما دسترسی ندارید.", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith(CB_PAYMENT_PREFIX):
        await query.answer()
        return

    try:
        rest = data[len(CB_PAYMENT_PREFIX) :]
        pay_id_str, action = rest.split("|", 1)
        payment_id = int(pay_id_str)
    except Exception:
        await query.answer("داده نامعتبر است.", show_alert=True)
        return

    pay = await asyncio.to_thread(get_payment_request, payment_id)
    if pay is None:
        await query.answer("پرداخت پیدا نشد.", show_alert=True)
        return

    if pay.status != "pending":
        await query.answer("این پرداخت قبلاً بررسی شده.", show_alert=True)
        return

    if action == "approve":
        await asyncio.to_thread(set_payment_status, payment_id, "approved", actor.id, None)
        await asyncio.to_thread(set_reservation_status, pay.reservation_id, "booked")

        # Consume coupon only on approved purchase
        if pay.coupon_code:
            now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")).isoformat(timespec="seconds")
            consumed = await asyncio.to_thread(consume_discount_code, pay.coupon_code, now_utc)
            if not consumed:
                logger.warning("Coupon could not be consumed (expired/used up): %s", pay.coupon_code)

        # After approval, ask user for banner/link and forward it to owner.
        await context.bot.send_message(
            chat_id=pay.user_id,
            text=(
                "بنر تبلیغاتی خود را ارسال نمایید\n"
                "در صورت نداشتن بنر تبلیغاتی فقط لینک گروه خودتون رو بفرستید\n"
                "(بنر پریمیوم مشکلی نداره؛ فقط محتوای نامناسب ارسال نشود.)"
            ),
        )
        awaiting = context.bot_data.setdefault(BOTDATA_USER_AWAIT_BANNER, {})
        awaiting[str(pay.user_id)] = pay.reservation_id

        await query.answer("تایید شد ✅")
        await query.edit_message_caption(
            caption=(query.message.caption or "") + "\n\nوضعیت: تایید شد ✅",
            reply_markup=None,
        )
        return

    if action == "reject":
        # Ask owner for reason in chat
        pending = context.bot_data.setdefault(BOTDATA_OWNER_PENDING_REJECT, {})
        pending[str(actor.id)] = payment_id
        await query.answer()
        await context.bot.send_message(
            chat_id=actor.id,
            text="دلیل رد کردن واریزی کاربر را بنویسید:",
        )
        return

    await query.answer("عملیات ناشناخته.", show_alert=True)


def _parse_duration_to_timedelta(text: str) -> timedelta | None:
    t = text.strip()
    m = re.fullmatch(r"(\d+)\s*(روز|ساعت|دقیقه)", t)
    if not m:
        m = re.fullmatch(r"(\d+)\s*([dhm])", t, flags=re.IGNORECASE)
    if not m:
        return None

    value = int(m.group(1))
    unit = m.group(2).lower()
    if value <= 0:
        return None

    if unit in ("روز", "d"):
        return timedelta(days=value)
    if unit in ("ساعت", "h"):
        return timedelta(hours=value)
    if unit in ("دقیقه", "m"):
        return timedelta(minutes=value)
    return None


async def takhfif_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    if not _is_admin(user.id):
        await msg.reply_text("شما دسترسی ندارید.")
        return

    context.user_data[UD_TAKHFIF_STEP] = TAKHFIF_AWAIT_CODE
    context.user_data.pop(UD_TAKHFIF_CODE, None)
    context.user_data.pop(UD_TAKHFIF_MAX_USES, None)
    context.user_data.pop(UD_TAKHFIF_EXPIRES_AT, None)
    await msg.reply_text("کد تخفیف را ارسال کنید (مثال: mobin)")


async def takhfif_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    if not _is_admin(user.id):
        await msg.reply_text("شما دسترسی ندارید.")
        return

    context.user_data[UD_TAKHFIF_STEP] = None
    context.user_data.pop(UD_TAKHFIF_CODE, None)
    context.user_data.pop(UD_TAKHFIF_MAX_USES, None)
    context.user_data.pop(UD_TAKHFIF_EXPIRES_AT, None)
    await msg.reply_text("عملیات کد تخفیف لغو شد.")


async def on_takhfif_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.text is None:
        return

    if not _is_admin(user.id):
        return

    step = context.user_data.get(UD_TAKHFIF_STEP)
    if not step:
        return

    text = msg.text.strip()

    if step == TAKHFIF_AWAIT_CODE:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{2,64}", text):
            await msg.reply_text("کد نامعتبر است. فقط حروف/عدد انگلیسی و _ یا - (۲ تا ۶۴ کاراکتر).")
            raise ApplicationHandlerStop
        context.user_data[UD_TAKHFIF_CODE] = normalize_discount_code(text)
        context.user_data[UD_TAKHFIF_STEP] = TAKHFIF_AWAIT_MAX_USES
        await msg.reply_text("این کد چند بار قابل استفاده باشد؟ (مثال: 5)")
        raise ApplicationHandlerStop

    if step == TAKHFIF_AWAIT_MAX_USES:
        if not text.isdigit() or int(text) <= 0:
            await msg.reply_text("عدد نامعتبر است. یک عدد مثبت ارسال کنید (مثال: 5)")
            raise ApplicationHandlerStop
        context.user_data[UD_TAKHFIF_MAX_USES] = int(text)
        context.user_data[UD_TAKHFIF_STEP] = TAKHFIF_AWAIT_DURATION
        await msg.reply_text("مدت اعتبار را ارسال کنید (مثال: 20 روز | 20 ساعت | 20 دقیقه)")
        raise ApplicationHandlerStop

    if step == TAKHFIF_AWAIT_DURATION:
        delta = _parse_duration_to_timedelta(text)
        if delta is None:
            await msg.reply_text("فرمت مدت نامعتبر است. مثال درست: 20 روز یا 20 ساعت یا 20 دقیقه")
            raise ApplicationHandlerStop
        now_utc_dt = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        expires_at = (now_utc_dt + delta).replace(tzinfo=None).isoformat(timespec="seconds")
        context.user_data[UD_TAKHFIF_EXPIRES_AT] = expires_at
        context.user_data[UD_TAKHFIF_STEP] = TAKHFIF_AWAIT_PERCENT
        await msg.reply_text("درصد تخفیف چند درصد باشد؟ (مثال: 30)")
        raise ApplicationHandlerStop

    if step == TAKHFIF_AWAIT_PERCENT:
        if not text.isdigit():
            await msg.reply_text("درصد نامعتبر است. یک عدد بین 1 تا 100 ارسال کنید.")
            raise ApplicationHandlerStop
        percent = int(text)
        if percent <= 0 or percent > 100:
            await msg.reply_text("درصد نامعتبر است. یک عدد بین 1 تا 100 ارسال کنید.")
            raise ApplicationHandlerStop

        code = context.user_data.get(UD_TAKHFIF_CODE)
        max_uses = context.user_data.get(UD_TAKHFIF_MAX_USES)
        expires_at = context.user_data.get(UD_TAKHFIF_EXPIRES_AT)
        if not isinstance(code, str) or not isinstance(max_uses, int) or not isinstance(expires_at, str):
            context.user_data[UD_TAKHFIF_STEP] = None
            await msg.reply_text("خطا در مراحل. دوباره تلاش کنید: /takhfif")
            raise ApplicationHandlerStop

        try:
            await asyncio.to_thread(create_discount_code, code, percent, max_uses, expires_at, user.id)
        except Exception:
            await msg.reply_text("این کد قبلاً ثبت شده است. یک کد دیگر ارسال کنید: /takhfif")
            context.user_data[UD_TAKHFIF_STEP] = None
            raise ApplicationHandlerStop

        context.user_data[UD_TAKHFIF_STEP] = None
        context.user_data.pop(UD_TAKHFIF_CODE, None)
        context.user_data.pop(UD_TAKHFIF_MAX_USES, None)
        context.user_data.pop(UD_TAKHFIF_EXPIRES_AT, None)

        await msg.reply_text(
            (
                "کد تخفیف با موفقیت ثبت شد ✅\n\n"
                f"کد: {code}\n"
                f"درصد: {percent}٪\n"
                f"تعداد استفاده: {max_uses}\n"
                f"انقضا (UTC): {expires_at}"
            )
        )
        raise ApplicationHandlerStop


async def on_owner_reject_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    actor = update.effective_user
    if msg is None or actor is None or msg.text is None:
        return

    if not _is_admin(actor.id):
        return

    pending = context.bot_data.get(BOTDATA_OWNER_PENDING_REJECT, {})
    payment_id = pending.get(str(actor.id))
    if not payment_id:
        return

    reason = msg.text.strip()
    pay = await asyncio.to_thread(get_payment_request, int(payment_id))
    if pay is None or pay.status != "pending":
        pending.pop(str(actor.id), None)
        return

    await asyncio.to_thread(set_payment_status, int(payment_id), "rejected", actor.id, reason)
    # Free the slot by cancelling the pending reservation
    await asyncio.to_thread(set_reservation_status, pay.reservation_id, "cancelled")

    await context.bot.send_message(
        chat_id=pay.user_id,
        text=f"{reason}\n\n/start",
    )

    pending.pop(str(actor.id), None)
    await msg.reply_text("دلیل ارسال شد.")


async def on_banner_or_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """After payment approval, forward user's banner photo or group link to owner."""
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    awaiting = context.bot_data.get(BOTDATA_USER_AWAIT_BANNER, {})
    reservation_id = awaiting.get(str(user.id))
    if not reservation_id:
        return

    if not BOT_ADMIN_IDS and OWNER_CHAT_ID is None:
        return

    username = f"@{user.username}" if user.username else None

    # Save what user sent to the reservation
    group_link = None
    promo_photo_file_id = None
    if msg.text and msg.text.strip().lower().startswith("http"):
        group_link = msg.text.strip()
    if getattr(msg, "photo", None):
        promo_photo_file_id = msg.photo[-1].file_id

    await asyncio.to_thread(
        update_reservation_promo,
        int(reservation_id),
        username,
        group_link,
        promo_photo_file_id,
    )

    # Forward exactly what user sent (photo, text, etc.)
    try:
        targets = sorted(BOT_ADMIN_IDS) if BOT_ADMIN_IDS else [OWNER_CHAT_ID]
        for admin_id in targets:
            try:
                await context.bot.forward_message(
                    chat_id=admin_id,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                )
            except Exception:
                continue
    finally:
        awaiting.pop(str(user.id), None)

    # Next step: ask for destination group links
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("دارم ✅", callback_data=f"{CB_DEST_PREFIX}{int(reservation_id)}|has"),
                InlineKeyboardButton("ندارم ❌", callback_data=f"{CB_DEST_PREFIX}{int(reservation_id)}|no"),
            ]
        ]
    )

    await msg.reply_text(
        "کاربر عزیز لینک گروه مقصد رو ارسال کنید\n"
        "لینک گروه ارسال به منظور این هستش که ممبر هایی که میخواهید بنر شما به پیوی اون ها ارسال بشه از چه گروه هایی میخواهید باشه",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def on_destination_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    user = update.effective_user
    if user is None:
        await query.answer()
        return

    data = query.data or ""
    if not data.startswith(CB_DEST_PREFIX):
        await query.answer()
        return

    try:
        rest = data[len(CB_DEST_PREFIX) :]
        res_id_str, choice = rest.split("|", 1)
        reservation_id = int(res_id_str)
    except Exception:
        await query.answer("داده نامعتبر است.", show_alert=True)
        return

    if choice == "no":
        await asyncio.to_thread(update_reservation_destination_links, reservation_id, None)

        # Send admin summary now
        targets = sorted(BOT_ADMIN_IDS) if BOT_ADMIN_IDS else ([OWNER_CHAT_ID] if OWNER_CHAT_ID else [])
        if targets:
            full = await asyncio.to_thread(get_reservation_full, reservation_id)
            reserved_str = _format_reserved_at_for_owner(full.reserved_at) if full else "(نامشخص)"
            username = full.username if full and full.username else (f"@{user.username}" if user.username else None)
            for admin_id in targets:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            "اطلاعات رزرو (پس از دریافت بنر/لینک)\n\n"
                            f"کد رزرو: {reservation_id}\n"
                            f"آیدی عددی: {user.id}\n"
                            f"یوزرنیم: {username or 'ندارد'}\n"
                            f"تایم رزرو: {reserved_str}\n"
                            "لینک گروه مقصد: ندارد"
                        ),
                        disable_web_page_preview=True,
                    )
                except Exception:
                    continue

        await query.answer("ثبت شد")
        await query.edit_message_text("ثبت شد.\nبرای ادامه از منوی اصلی استفاده کنید.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="منوی اصلی:", reply_markup=_main_menu_keyboard())
        return

    if choice == "has":
        context.user_data[UD_DEST_STEP] = DEST_AWAIT_LINKS
        context.user_data[UD_DEST_RESERVATION_ID] = reservation_id
        context.user_data[UD_DEST_LINKS_LIST] = []
        await query.answer()
        await query.edit_message_text(
            "لینک گروه مقصد رو ارسال کن:"
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="هر لینک را جداگانه ارسال کنید.\nبعد از تمام شدن، دکمه پایان را بزنید.",
            reply_markup=_finish_keyboard(),
            disable_web_page_preview=True,
        )
        return

    await query.answer("گزینه نامعتبر است.", show_alert=True)


async def on_destination_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.text is None:
        return

    if context.user_data.get(UD_DEST_STEP) != DEST_AWAIT_LINKS:
        return

    reservation_id = context.user_data.get(UD_DEST_RESERVATION_ID)
    if not isinstance(reservation_id, int):
        context.user_data.pop(UD_DEST_STEP, None)
        context.user_data.pop(UD_DEST_RESERVATION_ID, None)
        return

    text = msg.text.strip()

    if text == "بازگشت":
        context.user_data.pop(UD_DEST_STEP, None)
        context.user_data.pop(UD_DEST_RESERVATION_ID, None)
        context.user_data.pop(UD_DEST_LINKS_LIST, None)
        await msg.reply_text("منوی اصلی:", reply_markup=_main_menu_keyboard())
        return

    if text == DEST_FINISH_TEXT:
        links_list = context.user_data.get(UD_DEST_LINKS_LIST, [])
        links_text = "\n".join([s for s in links_list if s]) or None
        await asyncio.to_thread(update_reservation_destination_links, reservation_id, links_text)

        # Send admin summary now (with links)
        targets = sorted(BOT_ADMIN_IDS) if BOT_ADMIN_IDS else ([OWNER_CHAT_ID] if OWNER_CHAT_ID else [])
        if targets:
            full = await asyncio.to_thread(get_reservation_full, reservation_id)
            reserved_str = _format_reserved_at_for_owner(full.reserved_at) if full else "(نامشخص)"
            username = full.username if full and full.username else (f"@{user.username}" if user.username else None)
            for admin_id in targets:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            "اطلاعات رزرو (پس از دریافت بنر/لینک)\n\n"
                            f"کد رزرو: {reservation_id}\n"
                            f"آیدی عددی: {user.id}\n"
                            f"یوزرنیم: {username or 'ندارد'}\n"
                            f"تایم رزرو: {reserved_str}\n\n"
                            "لینک(های) گروه مقصد:\n"
                            f"{links_text or 'ندارد'}"
                        ),
                        disable_web_page_preview=True,
                    )
                except Exception:
                    continue

        context.user_data.pop(UD_DEST_STEP, None)
        context.user_data.pop(UD_DEST_RESERVATION_ID, None)
        context.user_data.pop(UD_DEST_LINKS_LIST, None)

        await msg.reply_text("ثبت شد.", reply_markup=_main_menu_keyboard())
        return

    # Otherwise treat as one destination link and ask for next
    links_list = context.user_data.get(UD_DEST_LINKS_LIST)
    if not isinstance(links_list, list):
        links_list = []
        context.user_data[UD_DEST_LINKS_LIST] = links_list
    links_list.append(text)

    await msg.reply_text(
        "لینک بعدی رو ارسال کن\n"
        "یا اگر ندارید دکمه پایان رو بزنید",
        reply_markup=_finish_keyboard(),
        disable_web_page_preview=True,
    )


async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    # Treat back as a global cancel for user multi-step flows
    context.user_data.pop(UD_PAYMENT_STEP, None)
    context.user_data.pop(UD_PAYMENT_RESERVATION_ID, None)
    context.user_data.pop(UD_PAYMENT_COUPON_CODE, None)
    context.user_data.pop(UD_PAYMENT_COUPON_PERCENT, None)

    context.user_data.pop(UD_VERIFICATION_STEP, None)
    context.user_data.pop(UD_VERIFICATION_REQUEST_ID, None)
    context.user_data.pop("verification_card_photo_file_id", None)

    context.user_data.pop(UD_DEST_STEP, None)
    context.user_data.pop(UD_DEST_RESERVATION_ID, None)
    context.user_data.pop(UD_DEST_LINKS_LIST, None)

    await msg.reply_text("منوی اصلی:", reply_markup=_main_menu_keyboard())
    raise ApplicationHandlerStop


async def on_contact_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    if not await _ensure_member(update, context):
        return

    await msg.reply_text(
        "برای ارتباط با ادمین/پشتیبانی یا راهنمایی خرید، به یکی از آیدی های زیر پیام بده:\n\n"
        "@silverrmb\n"
        "@OLDKASEB\n\n"
        "پشتیبانی سریع تر: لطفاً آیدی عددی + اسکرین شات مشکل/رسید + توضیح کوتاه رو هم بفرست.",
        reply_markup=_back_keyboard(),
        disable_web_page_preview=True,
    )
    raise ApplicationHandlerStop


async def on_rates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    if not await _ensure_member(update, context):
        return

    await msg.reply_text(
        "🛒200 پخشی = 150 هزارتومان. + 50 پخش اشانتیون 🔥\n\n"
        "🛒300 پخشی =240هزارتومان+70پخش اشانتیون🔥\n\n"
        "🛒400 پخشی =330هزارتومان+80پخش اشانتیون🔥\n\n"
        "🛒500 پخشی =430هزارتومان+ 100پخش اشانتیون🔥\n\n"
        "🛒600 پخشی =500هزارتومان.+110پخش اشانتیون🔥\n\n"
        "🛒800 پخشی =600هزارتومان.+ 120پخش اشانتیون🔥\n\n"
        "🛒1000 پخشی =650 هزارتومان+ 150 پخش اشانتیون🔥",
        reply_markup=_back_keyboard(),
    )
    raise ApplicationHandlerStop


async def on_verification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    if not await _ensure_member(update, context):
        return

    context.user_data[UD_VERIFICATION_STEP] = VERIF_AWAIT_PHOTO
    context.user_data.pop(UD_VERIFICATION_REQUEST_ID, None)

    await msg.reply_text(
        "به بخش احراز هویت خوش آمدید.\n"
        "نکات :\n"
        "1) شماره کارت و نام صاحب کارت کاملا مشخص باشد.\n"
        "2) لطفا تاریخ اعتبار و Cvv2 کارت خود را بپوشانید!\n"
        "3) فقط با کارتی که احراز هویت میکنید میتوانید خرید انجام بدید و اگر با کارت دیگری اقدام کنید تراکنش ناموفق میشود و هزینه از سمت خودِ بانک به شما بازگشت داده میشود.\n"
        "4) در صورتی که توانایی ارسال عکس از کارت را ندارید تنها راه حل ارسال عکس از کارت ملی یا شناسنامه صاحب کارت است.\n\n"
        "لطفا عکس از کارتی که میخواهید با آن خرید انجام دهید ارسال کنید.",
        reply_markup=_back_keyboard(),
    )
    raise ApplicationHandlerStop


async def on_verification_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return

    if not await _ensure_member(update, context):
        return

    if context.user_data.get(UD_VERIFICATION_STEP) != VERIF_AWAIT_PHOTO:
        return

    # Store file_id if you later want to forward it to admins.
    if getattr(msg, "photo", None):
        best = msg.photo[-1]
        context.user_data["verification_card_photo_file_id"] = best.file_id

    context.user_data[UD_VERIFICATION_STEP] = VERIF_AWAIT_CARD_NUMBER

    await msg.reply_text(
        "• لطفا شماره کارت خود را به صورت اعداد انگلیسی ارسال کنید\n"
        "در صورتی که منصرف شدید ربات را مجدد استارت کنید : [ /start ]",
        reply_markup=_back_keyboard(),
    )


def _normalize_card_number(text: str) -> str | None:
    # Accept 16 English digits, optionally separated by spaces or dashes.
    compact = re.sub(r"[\s-]", "", text.strip())
    if not re.fullmatch(r"[0-9]{16}", compact):
        return None
    return compact


def _mask_card(card_number: str) -> str:
    # Show only last 4 for admin UX
    return f"**** **** **** {card_number[-4:]}"


async def on_verification_card_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.text is None:
        return

    if not await _ensure_member(update, context):
        return

    if context.user_data.get(UD_VERIFICATION_STEP) != VERIF_AWAIT_CARD_NUMBER:
        return

    card = _normalize_card_number(msg.text)
    if card is None:
        await msg.reply_text(
            "شماره کارت نامعتبر است. لطفاً فقط ۱۶ رقم انگلیسی ارسال کنید (بدون حروف).",
            reply_markup=_back_keyboard(),
        )
        return

    photo_file_id = context.user_data.get("verification_card_photo_file_id")
    if not photo_file_id:
        context.user_data[UD_VERIFICATION_STEP] = VERIF_AWAIT_PHOTO
        await msg.reply_text(
            "ابتدا عکس کارت را ارسال کنید.",
            reply_markup=_back_keyboard(),
        )
        return

    if not BOT_ADMIN_IDS and OWNER_CHAT_ID is None:
        await msg.reply_text(
            "احراز هویت در حال حاضر فعال نیست (OWNER_CHAT_ID تنظیم نشده).",
            reply_markup=_back_keyboard(),
        )
        return

    username = f"@{user.username}" if user.username else None
    request_id = await asyncio.to_thread(create_verification_request, user.id, username, card, photo_file_id)
    context.user_data[UD_VERIFICATION_REQUEST_ID] = request_id

    caption = (
        "درخواست احراز هویت ارسال شد\n\n"
        f"آیدی عددی: {user.id}\n"
        f"یوزرنیم: {username or 'ندارد'}\n"
        f"شماره کارت: {card}\n"
        f"کد درخواست: {request_id}"
    )

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("تایید ✅", callback_data=f"{CB_VERIF_PREFIX}{request_id}|approve")],
            [
                InlineKeyboardButton("اشتباه ❌", callback_data=f"{CB_VERIF_PREFIX}{request_id}|reject_wrong"),
                InlineKeyboardButton("کامل نیست ❌", callback_data=f"{CB_VERIF_PREFIX}{request_id}|reject_incomplete"),
            ],
        ]
    )

    targets = sorted(BOT_ADMIN_IDS) if BOT_ADMIN_IDS else [OWNER_CHAT_ID]
    for admin_id in targets:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=caption,
                reply_markup=kb,
            )
        except Exception:
            continue

    context.user_data[UD_VERIFICATION_STEP] = None
    await msg.reply_text(
        "درخواست شما برای بررسی ارسال شد.",
        reply_markup=_main_menu_keyboard(),
    )


async def on_verification_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    actor = update.effective_user
    if actor is None:
        await query.answer()
        return

    # Only admins can decide
    if not _is_admin(actor.id):
        await query.answer("شما دسترسی ندارید.", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith(CB_VERIF_PREFIX):
        await query.answer()
        return

    try:
        _, rest = data.split("verif|", 1)
        req_id_str, action = rest.split("|", 1)
        request_id = int(req_id_str)
    except Exception:
        await query.answer("داده نامعتبر است.", show_alert=True)
        return

    req = await asyncio.to_thread(get_verification_request, request_id)
    if req is None:
        await query.answer("درخواست پیدا نشد.", show_alert=True)
        return

    if req.status != "pending":
        await query.answer("این درخواست قبلاً بررسی شده.", show_alert=True)
        return

    if action == "approve":
        await asyncio.to_thread(set_verification_status, request_id, "approved", actor.id, None)
        await asyncio.to_thread(upsert_verified_card, req.user_id, req.username, req.card_number, actor.id)

        await context.bot.send_message(
            chat_id=req.user_id,
            text=(
                f"• درخواست احراز هویت کارت ( {req.card_number} ) تایید شد.\n"
                "شما هم اکنون میتوانید از بخش خرید / تمدید اشتراک ، خرید خود را انجام دهید."
            ),
        )

        await query.answer("تایید شد ✅")
        await query.edit_message_caption(
            caption=(query.message.caption or "") + "\n\nوضعیت: تایید شد ✅",
            reply_markup=None,
        )
        return

    if action == "reject_wrong":
        await asyncio.to_thread(set_verification_status, request_id, "rejected", actor.id, "wrong")
        await context.bot.send_message(
            chat_id=req.user_id,
            text=(
                f"• درخواست احراز هویت کارت ( {req.card_number} ) به دلیل اشتباه بودن عکس ارسالی شما ، رد شد.\n"
                "شما میتوانید مجددا برای احراز هویت با رعایت شرایط، درخواست دهید."
            ),
        )
        await query.answer("رد شد ❌")
        await query.edit_message_caption(
            caption=(query.message.caption or "") + "\n\nوضعیت: رد شد (اشتباه) ❌",
            reply_markup=None,
        )
        return

    if action == "reject_incomplete":
        await asyncio.to_thread(set_verification_status, request_id, "rejected", actor.id, "incomplete")
        await context.bot.send_message(
            chat_id=req.user_id,
            text=(
                f"• درخواست احراز هویت کارت ( {req.card_number} ) به دلیل کامل نبودن شرایط احراز هویتی که در ابتدا به شما گفته شد ، رد شد.\n"
                "شما میتوانید مجددا برای احراز هویت با رعایت شرایط، درخواست دهید."
            ),
        )
        await query.answer("رد شد ❌")
        await query.edit_message_caption(
            caption=(query.message.caption or "") + "\n\nوضعیت: رد شد (کامل نیست) ❌",
            reply_markup=None,
        )
        return

    await query.answer("عملیات ناشناخته.", show_alert=True)


REMINDER_MINUTES_BEFORE = int(os.getenv("REMINDER_MINUTES_BEFORE", "30").strip() or "30")
REMINDER_INTERVAL_SECONDS = int(os.getenv("REMINDER_INTERVAL_SECONDS", "30").strip() or "30")
REMINDER_WINDOW_SECONDS = int(os.getenv("REMINDER_WINDOW_SECONDS", "90").strip() or "90")


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not BOT_ADMIN_IDS and OWNER_CHAT_ID is None:
        return

    now = datetime.now(TZ)
    target = now + timedelta(minutes=REMINDER_MINUTES_BEFORE)
    window_start = target - timedelta(seconds=REMINDER_WINDOW_SECONDS)
    window_end = target + timedelta(seconds=REMINDER_WINDOW_SECONDS)

    window_start_iso = window_start.isoformat(timespec="seconds")
    window_end_iso = window_end.isoformat(timespec="seconds")

    candidates = await asyncio.to_thread(list_reservations_due_for_reminder, window_start_iso, window_end_iso)
    if not candidates:
        return

    targets = sorted(BOT_ADMIN_IDS) if BOT_ADMIN_IDS else [OWNER_CHAT_ID]

    for c in candidates:
        full = await asyncio.to_thread(get_reservation_full, int(c.reservation_id))
        reserved_at = full.reserved_at if full else c.reserved_at
        reserved_str = _format_reserved_at_for_owner(reserved_at)
        username = (full.username if full and full.username else None) or (c.username or "ندارد")
        group_link = full.group_link if full else c.group_link
        dest_links = full.destination_links if full else None
        has_banner = bool((full.promo_photo_file_id if full else c.promo_photo_file_id))

        text = (
            f"⏰ یادآوری رزرو ({_to_fa_digits(str(REMINDER_MINUTES_BEFORE))} دقیقه مانده)\n\n"
            f"کد رزرو: {c.reservation_id}\n"
            f"آیدی عددی: {c.user_id}\n"
            f"یوزرنیم: {username}\n"
            f"تایم رزرو: {reserved_str}\n"
            f"لینک گروه: {group_link or 'ندارد'}\n"
            f"بنر: {'دارد' if has_banner else 'ندارد'}\n"
            f"لینک(های) گروه مقصد: {dest_links or 'ندارد'}"
        )

        for admin_id in targets:
            try:
                await context.bot.send_message(chat_id=admin_id, text=text, disable_web_page_preview=True)
            except Exception:
                continue

        await asyncio.to_thread(mark_reservation_reminded, int(c.reservation_id), now.isoformat(timespec="seconds"))


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Create .env and set BOT_TOKEN.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    if app.job_queue is not None and (BOT_ADMIN_IDS or OWNER_CHAT_ID is not None):
        app.job_queue.run_repeating(reminder_job, interval=REMINDER_INTERVAL_SECONDS, first=10)

    # Admin captures that must run before other handlers
    app.add_handler(MessageHandler(filters.ALL, on_admin_capture), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("hamgani", hamgani_start))
    app.add_handler(CommandHandler("cancel_hamgani", hamgani_cancel))
    app.add_handler(CommandHandler("amar", amar))
    app.add_handler(CommandHandler("takhfif", takhfif_start))
    app.add_handler(CommandHandler("cancel_takhfif", takhfif_cancel))
    app.add_handler(CallbackQueryHandler(confirm_membership, pattern=f"^{CB_CONFIRM}$"))
    app.add_handler(CallbackQueryHandler(noop, pattern="^noop$"))

    app.add_handler(CallbackQueryHandler(on_slot_click, pattern=f"^{CB_SLOT_PREFIX}"))
    app.add_handler(CallbackQueryHandler(on_discount_choice, pattern=f"^{CB_DISCOUNT_PREFIX}"))
    app.add_handler(CallbackQueryHandler(on_verification_decision, pattern=f"^{CB_VERIF_PREFIX}"))
    app.add_handler(CallbackQueryHandler(on_payment_decision, pattern=f"^{CB_PAYMENT_PREFIX}"))
    app.add_handler(CallbackQueryHandler(on_destination_choice, pattern=f"^{CB_DEST_PREFIX}"))

    app.add_handler(MessageHandler(filters.Regex(r"^حساب کاربری$"), on_account))
    app.add_handler(MessageHandler(filters.Regex(r"^رزرو تایم$"), reserve_day_menu))
    app.add_handler(MessageHandler(filters.Regex(r"^نرخ$"), on_rates))
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^(شنبه|یکشنبه|دوشنبه|سه شنبه|چهارشنبه|پنجشنبه|جمعه)$"),
            on_day_selected,
        )
    )
    app.add_handler(MessageHandler(filters.Regex(r"^ارتباط با ادمین$"), on_contact_admin))
    app.add_handler(MessageHandler(filters.Regex(r"^احراز هویت$"), on_verification))
    app.add_handler(MessageHandler(filters.Regex(r"^بازگشت$"), on_back))

    # Routers for multi-step flows (must be after menu buttons)
    app.add_handler(MessageHandler(filters.PHOTO, on_photo_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_router))

    logger.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
