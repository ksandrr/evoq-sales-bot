import os
import html
import logging
from datetime import time, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database as db

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
BRIEF, DETAILS = range(2)
SET_TIME = 100

REMINDER_JOB_PREFIX = "reminder:"


def idea_keyboard(idea_id: int, expanded: bool) -> InlineKeyboardMarkup:
    if expanded:
        first_row = [InlineKeyboardButton("🔼 Свернуть", callback_data=f"collapse:{idea_id}")]
    else:
        first_row = [InlineKeyboardButton("📖 Подробнее", callback_data=f"expand:{idea_id}")]
    return InlineKeyboardMarkup(
        [
            first_row,
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete:{idea_id}")],
        ]
    )


def brief_message(brief: str) -> str:
    return f"💡 {html.escape(brief)}"


def full_message(brief: str, details: str) -> str:
    return f"💡 <b>{html.escape(brief)}</b>\n\n{html.escape(details)}"


# --- Commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    schedule_user_reminder(context.application, user_id)

    hour, minute = db.get_user_time(user_id)
    text = (
        "👋 Привет! Я бот для записи твоих идей.\n\n"
        "Каждый день я буду напоминать тебе о них — сначала кратко, "
        "а если нажмёшь «Подробнее», покажу полное описание.\n\n"
        "<b>Команды</b>\n"
        "/add — добавить новую идею\n"
        "/list — посмотреть все идеи\n"
        f"/settime — изменить время напоминания (сейчас {hour:02d}:{minute:02d} UTC)\n"
        "/cancel — отменить текущее действие"
    )
    await update.message.reply_html(text)


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 Введи краткое описание идеи (одна короткая строка).\n\n"
        "Или /cancel для отмены."
    )
    return BRIEF


async def add_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    brief = (update.message.text or "").strip()
    if not brief:
        await update.message.reply_text("Краткое описание не может быть пустым. Попробуй ещё раз.")
        return BRIEF
    if len(brief) > 200:
        await update.message.reply_text("Слишком длинно для краткого описания (макс. 200 символов). Сократи, пожалуйста.")
        return BRIEF

    context.user_data["brief"] = brief
    await update.message.reply_text(
        "📖 Теперь напиши подробное описание идеи — что именно, зачем, как.\n\n"
        "Или /cancel для отмены."
    )
    return DETAILS


async def add_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    details = (update.message.text or "").strip()
    if not details:
        await update.message.reply_text("Подробное описание не может быть пустым. Попробуй ещё раз.")
        return DETAILS

    user_id = update.effective_user.id
    brief = context.user_data.get("brief", "")
    db.upsert_user(user_id)
    db.add_idea(user_id, brief, details)
    schedule_user_reminder(context.application, user_id)

    await update.message.reply_html(
        f"✅ Идея сохранена!\n\n{brief_message(brief)}"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


async def list_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ideas = db.get_user_ideas(user_id)

    if not ideas:
        await update.message.reply_text(
            "У тебя пока нет идей. Добавь первую командой /add"
        )
        return

    await update.message.reply_text(f"📚 Твои идеи ({len(ideas)}):")
    for idea_id, brief, _ in ideas:
        await update.message.reply_html(
            brief_message(brief),
            reply_markup=idea_keyboard(idea_id, expanded=False),
        )


async def settime_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hour, minute = db.get_user_time(update.effective_user.id)
    await update.message.reply_text(
        f"⏰ Сейчас напоминания приходят в {hour:02d}:{minute:02d} UTC.\n\n"
        "Введи новое время в формате HH:MM (UTC), например: 09:30\n\n"
        "Или /cancel для отмены."
    )
    return SET_TIME


async def settime_apply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        hh, mm = text.split(":")
        hour = int(hh)
        minute = int(mm)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Неверный формат. Используй HH:MM, например 09:30."
        )
        return SET_TIME

    user_id = update.effective_user.id
    db.upsert_user(user_id)
    db.update_user_time(user_id, hour, minute)
    schedule_user_reminder(context.application, user_id)

    await update.message.reply_text(
        f"✅ Готово. Напоминания будут приходить в {hour:02d}:{minute:02d} UTC."
    )
    return ConversationHandler.END


# --- Callback queries ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        action, idea_id_str = query.data.split(":", 1)
        idea_id = int(idea_id_str)
    except (ValueError, AttributeError):
        return

    user_id = query.from_user.id
    idea = db.get_idea(idea_id, user_id)
    if not idea:
        await query.edit_message_text("⚠️ Идея не найдена или была удалена.")
        return

    _id, brief, details = idea

    if action == "expand":
        await query.edit_message_text(
            full_message(brief, details),
            parse_mode=ParseMode.HTML,
            reply_markup=idea_keyboard(idea_id, expanded=True),
        )
    elif action == "collapse":
        await query.edit_message_text(
            brief_message(brief),
            parse_mode=ParseMode.HTML,
            reply_markup=idea_keyboard(idea_id, expanded=False),
        )
    elif action == "delete":
        db.delete_idea(idea_id, user_id)
        await query.edit_message_text(
            f"🗑 Идея удалена:\n\n{brief_message(brief)}",
            parse_mode=ParseMode.HTML,
        )


# --- Daily reminder job ---

async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    ideas = db.get_user_ideas(user_id)

    if not ideas:
        return

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🌅 Напоминаю про твои идеи ({len(ideas)}):",
        )
        for idea_id, brief, _ in ideas:
            await context.bot.send_message(
                chat_id=user_id,
                text=brief_message(brief),
                parse_mode=ParseMode.HTML,
                reply_markup=idea_keyboard(idea_id, expanded=False),
            )
    except Exception:
        logger.exception("Failed to send reminder to user %s", user_id)


def schedule_user_reminder(application: Application, user_id: int):
    job_queue = application.job_queue
    job_name = f"{REMINDER_JOB_PREFIX}{user_id}"

    for job in job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    hour, minute = db.get_user_time(user_id)
    job_queue.run_daily(
        send_daily_reminder,
        time=time(hour=hour, minute=minute, tzinfo=timezone.utc),
        data={"user_id": user_id},
        name=job_name,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Update %s caused error", update, exc_info=context.error)


async def post_init(application: Application):
    db.init_db()
    for user_id in db.get_all_user_ids():
        schedule_user_reminder(application, user_id)
    logger.info("Bot started; reminders scheduled.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is not set. Copy .env.example to .env and fill it in.")

    db.init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            BRIEF: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_brief)],
            DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_details)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    settime_conv = ConversationHandler(
        entry_points=[CommandHandler("settime", settime_start)],
        states={
            SET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, settime_apply)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("list", list_ideas))
    application.add_handler(add_conv)
    application.add_handler(settime_conv)
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
