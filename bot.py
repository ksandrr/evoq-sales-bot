import os
import json
import html
import logging
import tempfile
from datetime import time, timezone

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# OpenAI client (опционально, для распознавания голоса)
_openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI

        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI клиент инициализирован — голосовые сообщения будут распознаваться.")
    except ImportError:
        logger.warning("Установлен OPENAI_API_KEY, но пакет openai не установлен.")
else:
    logger.info("OPENAI_API_KEY не задан — голосовые сообщения будут отклоняться.")

# Состояния диалогов
BRIEF, DETAILS = range(2)
SET_TIME = 100

REMINDER_JOB_PREFIX = "reminder:"

# Подписи кнопок главного меню
BTN_ADD = "➕ Добавить идею"
BTN_LIST = "📚 Мои идеи"
BTN_TIME = "⏰ Время напоминаний"
BTN_TEST = "🔔 Тест напоминания"
BTN_HELP = "ℹ️ Помощь"
BTN_CANCEL = "✖️ Отмена"


# --- Клавиатуры ---

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_ADD],
            [BTN_LIST, BTN_TIME],
            [BTN_TEST, BTN_HELP],
        ],
        resize_keyboard=True,
        input_field_placeholder="Тапни кнопку, напиши или наговори идею...",
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_CANCEL]],
        resize_keyboard=True,
        input_field_placeholder="Введи текст, наговори голосом или нажми Отмена",
    )


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


def post_save_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📖 Подробнее", callback_data=f"expand:{idea_id}")],
            [
                InlineKeyboardButton("➕ Ещё идея", callback_data="menu:add"),
                InlineKeyboardButton("📚 Все идеи", callback_data="menu:list"),
            ],
            [InlineKeyboardButton("🗑 Удалить эту", callback_data=f"delete:{idea_id}")],
        ]
    )


def brief_message(brief: str) -> str:
    return f"💡 {html.escape(brief)}"


def full_message(brief: str, details: str) -> str:
    return f"💡 <b>{html.escape(brief)}</b>\n\n{html.escape(details)}"


# --- Голос: распознавание и парсинг ---

async def transcribe_voice(voice_file) -> str | None:
    """Скачивает voice-файл из Telegram, отправляет в Whisper, возвращает текст."""
    if not _openai_client:
        return None

    with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await voice_file.download_to_drive(tmp_path)
        with open(tmp_path, "rb") as f:
            transcript = await _async_call(
                _openai_client.audio.transcriptions.create,
                model="whisper-1",
                file=f,
                language="ru",
            )
        return (transcript.text or "").strip() or None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def parse_idea_with_gpt(text: str) -> tuple[str, str] | None:
    """Просит GPT извлечь из произвольного текста brief + details. Возвращает (brief, details)."""
    if not _openai_client:
        return None

    system = (
        "Ты ассистент, который из произвольного сообщения пользователя извлекает идею. "
        "Верни JSON с двумя полями: brief (короткое название идеи, до 100 символов, "
        "одна строка, без точки в конце) и details (подробное описание, как пользователь рассказал, "
        "можно слегка причесать). Если в сообщении только название — придумай адекватное "
        "описание-расширение. Если только описание — придумай короткое название по смыслу. "
        "Отвечай на русском."
    )
    try:
        response = await _async_call(
            _openai_client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        raw = response.choices[0].message.content
        data = json.loads(raw)
        brief = (data.get("brief") or "").strip()
        details = (data.get("details") or "").strip()
        if not brief or not details:
            return None
        return brief[:200], details
    except Exception:
        logger.exception("GPT парсинг идеи упал")
        return None


async def _async_call(fn, *args, **kwargs):
    """Запускает синхронный вызов OpenAI SDK в отдельном потоке, чтобы не блокировать event loop."""
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# --- Обработчики верхнего уровня ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    schedule_user_reminder(context.application, user_id)

    hour, minute = db.get_user_time(user_id)
    text = (
        "👋 Привет! Я бот для записи твоих идей.\n\n"
        "Каждый день я буду напоминать тебе о них — кратко, "
        "а кнопкой «📖 Подробнее» можно развернуть полное описание.\n\n"
        f"⏰ Сейчас напоминания приходят в <b>{hour:02d}:{minute:02d} UTC</b>.\n"
        "Это можно изменить кнопкой «⏰ Время напоминаний».\n\n"
        "Жми кнопки внизу или просто наговори идею голосом."
    )
    await update.message.reply_html(text, reply_markup=main_menu_keyboard())


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice_note = (
        "🎤 <b>Голосовые сообщения</b>: можно надиктовать идею. "
        "Бот сам распознает речь и разделит её на название и описание."
        if _openai_client
        else "🎤 <b>Голосовые сообщения</b> временно недоступны — администратор не настроил OpenAI API."
    )
    text = (
        "<b>Как это работает</b>\n\n"
        f"• <b>{BTN_ADD}</b> — пошагово введи краткое название и подробное описание.\n"
        f"• <b>{BTN_LIST}</b> — посмотреть все идеи. У каждой кнопка «📖 Подробнее» и «🗑 Удалить».\n"
        f"• <b>{BTN_TIME}</b> — поменять время ежедневного напоминания (UTC).\n"
        f"• <b>{BTN_TEST}</b> — прислать тестовое напоминание прямо сейчас.\n\n"
        f"{voice_note}\n\n"
        "Дневное напоминание выглядит так: бот пишет «🌅 Напоминаю про твои идеи (N):» "
        "и затем шлёт каждую идею отдельным сообщением с кнопкой «📖 Подробнее»."
    )
    await update.message.reply_html(text, reply_markup=main_menu_keyboard())


async def list_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ideas = db.get_user_ideas(user_id)

    if not ideas:
        await update.message.reply_text(
            f"У тебя пока нет идей. Нажми «{BTN_ADD}» или просто наговори/напиши.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await update.message.reply_text(
        f"📚 Твои идеи ({len(ideas)}):",
        reply_markup=main_menu_keyboard(),
    )
    for idea_id, brief, _ in ideas:
        await update.message.reply_html(
            brief_message(brief),
            reply_markup=idea_keyboard(idea_id, expanded=False),
        )


async def test_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ideas = db.get_user_ideas(user_id)
    if not ideas:
        await update.message.reply_text(
            "Сначала добавь хотя бы одну идею — тогда я смогу показать, как выглядит напоминание.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await update.message.reply_text(
        "🔔 Так будет выглядеть дневное напоминание:",
        reply_markup=main_menu_keyboard(),
    )
    await _send_reminder(context.bot, user_id, ideas)


# --- Диалог: добавить идею ---

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice_hint = " или наговори голосом 🎤" if _openai_client else ""
    await update.message.reply_text(
        f"📝 Введи краткое описание идеи (одна строка){voice_hint}.\n\n"
        f"Или нажми «{BTN_CANCEL}».",
        reply_markup=cancel_keyboard(),
    )
    return BRIEF


async def _handle_brief_text(update: Update, context: ContextTypes.DEFAULT_TYPE, brief: str):
    brief = (brief or "").strip()
    if not brief:
        await update.message.reply_text(
            "Краткое описание не может быть пустым. Попробуй ещё раз.",
            reply_markup=cancel_keyboard(),
        )
        return BRIEF
    if len(brief) > 200:
        await update.message.reply_text(
            "Слишком длинно для краткого описания (макс. 200 символов). Сократи, пожалуйста.",
            reply_markup=cancel_keyboard(),
        )
        return BRIEF

    context.user_data["brief"] = brief
    voice_hint = " или наговори голосом 🎤" if _openai_client else ""
    await update.message.reply_text(
        f"📖 Теперь напиши подробное описание идеи (что именно, зачем, как){voice_hint}.\n\n"
        f"Или нажми «{BTN_CANCEL}».",
        reply_markup=cancel_keyboard(),
    )
    return DETAILS


async def add_brief_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_brief_text(update, context, update.message.text)


async def add_brief_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context)
    if text is None:
        return BRIEF
    return await _handle_brief_text(update, context, text)


async def _handle_details_text(update: Update, context: ContextTypes.DEFAULT_TYPE, details: str):
    details = (details or "").strip()
    if not details:
        await update.message.reply_text(
            "Подробное описание не может быть пустым. Попробуй ещё раз.",
            reply_markup=cancel_keyboard(),
        )
        return DETAILS

    user_id = update.effective_user.id
    brief = context.user_data.get("brief", "")
    db.upsert_user(user_id)
    idea_id = db.add_idea(user_id, brief, details)
    schedule_user_reminder(context.application, user_id)

    await update.message.reply_html(
        f"✅ Идея сохранена!\n\n{brief_message(brief)}",
        reply_markup=main_menu_keyboard(),
    )
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=post_save_keyboard(idea_id),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def add_details_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_details_text(update, context, update.message.text)


async def add_details_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context)
    if text is None:
        return DETAILS
    return await _handle_details_text(update, context, text)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# --- Диалог: настройка времени ---

async def settime_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hour, minute = db.get_user_time(update.effective_user.id)
    await update.message.reply_text(
        f"⏰ Сейчас напоминания приходят в {hour:02d}:{minute:02d} UTC.\n\n"
        f"Введи новое время в формате HH:MM (UTC), например: 09:30.\n\n"
        f"Или нажми «{BTN_CANCEL}».",
        reply_markup=cancel_keyboard(),
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
            "⚠️ Неверный формат. Используй HH:MM, например 09:30.",
            reply_markup=cancel_keyboard(),
        )
        return SET_TIME

    user_id = update.effective_user.id
    db.upsert_user(user_id)
    db.update_user_time(user_id, hour, minute)
    schedule_user_reminder(context.application, user_id)

    await update.message.reply_text(
        f"✅ Готово. Напоминания будут приходить в {hour:02d}:{minute:02d} UTC.",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


# --- Голос вне диалога: создаём идею целиком ---

async def voice_top_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _openai_client:
        await update.message.reply_text(
            "🎤 Голосовые сообщения сейчас не настроены. "
            "Попроси администратора добавить переменную окружения OPENAI_API_KEY.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    text = await _transcribe_or_warn(update, context)
    if not text:
        return

    parsed = await parse_idea_with_gpt(text)
    if not parsed:
        await update.message.reply_text(
            "Не получилось распознать идею из голосового. "
            "Попробуй наговорить ещё раз или нажми «➕ Добавить идею» и введи пошагово.",
            reply_markup=main_menu_keyboard(),
        )
        return

    brief, details = parsed
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    idea_id = db.add_idea(user_id, brief, details)
    schedule_user_reminder(context.application, user_id)

    await update.message.reply_html(
        f"✅ Идея сохранена из голосового!\n\n{full_message(brief, details)}",
        reply_markup=main_menu_keyboard(),
    )
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=post_save_keyboard(idea_id),
    )


async def _transcribe_or_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    if not _openai_client:
        await update.message.reply_text(
            "🎤 Голосовые сейчас не настроены. Напиши текстом, пожалуйста.",
            reply_markup=cancel_keyboard(),
        )
        return None

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    voice = update.message.voice or update.message.audio
    if not voice:
        return None

    try:
        file = await voice.get_file()
        text = await transcribe_voice(file)
    except Exception:
        logger.exception("Не удалось распознать голосовое")
        await update.message.reply_text(
            "Не удалось распознать голосовое. Попробуй ещё раз или введи текстом.",
            reply_markup=cancel_keyboard(),
        )
        return None

    if not text:
        await update.message.reply_text(
            "Голос распознался как пустой. Попробуй ещё раз или введи текстом.",
            reply_markup=cancel_keyboard(),
        )
        return None

    return text


# --- Inline-кнопки ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        action, payload = query.data.split(":", 1)
    except (ValueError, AttributeError):
        return

    user_id = query.from_user.id

    if action == "menu":
        if payload == "list":
            ideas = db.get_user_ideas(user_id)
            if not ideas:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"У тебя пока нет идей. Нажми «{BTN_ADD}».",
                    reply_markup=main_menu_keyboard(),
                )
                return
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📚 Твои идеи ({len(ideas)}):",
                reply_markup=main_menu_keyboard(),
            )
            for iid, brief, _ in ideas:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=brief_message(brief),
                    parse_mode=ParseMode.HTML,
                    reply_markup=idea_keyboard(iid, expanded=False),
                )
            return
        if payload == "add":
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Нажми кнопку «{BTN_ADD}» внизу — и начнём вводить новую идею.",
                reply_markup=main_menu_keyboard(),
            )
            return

    idea_id = int(payload)
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


# --- Ежедневное напоминание ---

async def _send_reminder(bot, user_id: int, ideas):
    await bot.send_message(
        chat_id=user_id,
        text=f"🌅 Напоминаю про твои идеи ({len(ideas)}):",
    )
    for idea_id, brief, _ in ideas:
        await bot.send_message(
            chat_id=user_id,
            text=brief_message(brief),
            parse_mode=ParseMode.HTML,
            reply_markup=idea_keyboard(idea_id, expanded=False),
        )


async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    ideas = db.get_user_ideas(user_id)
    if not ideas:
        return
    try:
        await _send_reminder(context.bot, user_id, ideas)
    except Exception:
        logger.exception("Не удалось отправить напоминание пользователю %s", user_id)


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


# --- Прочее ---

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

    # Точки входа в диалог добавления идеи: команда, кнопка меню
    add_entry_filters = (
        filters.Regex(f"^{BTN_ADD}$")
    )
    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(add_entry_filters, add_start),
        ],
        states={
            BRIEF: [
                MessageHandler(filters.VOICE | filters.AUDIO, add_brief_voice),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{BTN_CANCEL}$"),
                    add_brief_text,
                ),
            ],
            DETAILS: [
                MessageHandler(filters.VOICE | filters.AUDIO, add_details_voice),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{BTN_CANCEL}$"),
                    add_details_text,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel),
        ],
        allow_reentry=True,
    )

    settime_conv = ConversationHandler(
        entry_points=[
            CommandHandler("settime", settime_start),
            MessageHandler(filters.Regex(f"^{BTN_TIME}$"), settime_start),
        ],
        states={
            SET_TIME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{BTN_CANCEL}$"),
                    settime_apply,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel),
        ],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CommandHandler("list", list_ideas))
    application.add_handler(CommandHandler("test", test_reminder))

    application.add_handler(add_conv)
    application.add_handler(settime_conv)

    # Кнопки главного меню (вне диалогов)
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_LIST}$"), list_ideas))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_TEST}$"), test_reminder))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_HELP}$"), show_help))

    # Голосовые сообщения вне диалогов — создаём идею целиком через GPT
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_top_level))

    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
