import os
import json
import html
import logging
import re
import subprocess
import tempfile
import urllib.request
import zipfile
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# OpenAI клиент (опционально — для голосовых)
_openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI

        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI клиент инициализирован — голосовые сообщения будут распознаваться через Whisper.")
    except ImportError:
        logger.warning("Установлен OPENAI_API_KEY, но пакет openai не установлен.")
else:
    logger.info("OPENAI_API_KEY не задан — будет использован Vosk (если установлен).")

# Vosk — оффлайн-распознавание (fallback, если OpenAI не настроен).
VOSK_MODEL_URL = os.getenv(
    "VOSK_MODEL_URL",
    "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
)
_vosk_model = None
_vosk_checked = False


def _vosk_model_dir() -> str:
    explicit = os.getenv("VOSK_MODEL_PATH")
    if explicit:
        return explicit
    if os.path.isdir("/data"):
        return "/data/vosk-model"
    return "/tmp/vosk-model"


def _download_vosk_model(target_dir: str):
    logger.info("Скачиваем Vosk модель: %s", VOSK_MODEL_URL)
    work_dir = tempfile.mkdtemp(prefix="vosk-dl-")
    zip_path = os.path.join(work_dir, "model.zip")
    urllib.request.urlretrieve(VOSK_MODEL_URL, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(work_dir)
    for name in os.listdir(work_dir):
        src = os.path.join(work_dir, name)
        if os.path.isdir(src) and name.startswith("vosk-model"):
            os.makedirs(os.path.dirname(target_dir) or ".", exist_ok=True)
            os.rename(src, target_dir)
            logger.info("Vosk модель распакована в %s", target_dir)
            return
    raise RuntimeError("Не нашёл папку с моделью внутри архива")


def _get_vosk_model():
    global _vosk_model, _vosk_checked
    if _vosk_model is not None:
        return _vosk_model
    if _vosk_checked:
        return None
    _vosk_checked = True

    try:
        from vosk import Model, SetLogLevel
    except ImportError:
        logger.info("Пакет vosk не установлен — Vosk fallback недоступен.")
        return None

    SetLogLevel(-1)
    model_dir = _vosk_model_dir()
    try:
        if not os.path.isdir(model_dir):
            _download_vosk_model(model_dir)
        _vosk_model = Model(model_dir)
        logger.info("Vosk модель загружена из %s", model_dir)
    except Exception:
        logger.exception("Не удалось инициализировать Vosk")
        _vosk_model = None
    return _vosk_model


def _vosk_available() -> bool:
    try:
        import vosk  # noqa: F401
        return True
    except ImportError:
        return False


def voice_available() -> bool:
    return bool(_openai_client) or _vosk_available()


# Состояния диалогов
BRIEF, DETAILS = range(2)
ADD_REMINDER_TIME = 200
TASK_TEXT = 300

REMINDER_JOB_PREFIX = "reminder:"

# Подписи кнопок главного меню
BTN_ADD = "➕ Идея"
BTN_LIST = "💡 Мои идеи"
BTN_ADD_TASK = "✅ Задача"
BTN_LIST_TASKS = "📋 Мои задачи"
BTN_REMINDERS = "⏰ Напоминания"
BTN_TEST = "🔔 Тест"
BTN_HELP = "ℹ️ Помощь"
BTN_CANCEL = "✖️ Отмена"

# Преднастроенные часовые пояса (можно выбрать одной кнопкой)
PRESET_TIMEZONES = [
    ("UTC", "UTC"),
    ("Europe/Kaliningrad", "Калининград (UTC+2)"),
    ("Europe/Moscow", "Москва (UTC+3)"),
    ("Europe/Samara", "Самара (UTC+4)"),
    ("Asia/Yekaterinburg", "Екатеринбург (UTC+5)"),
    ("Asia/Omsk", "Омск (UTC+6)"),
    ("Asia/Novosibirsk", "Новосибирск (UTC+7)"),
    ("Asia/Irkutsk", "Иркутск (UTC+8)"),
    ("Asia/Yakutsk", "Якутск (UTC+9)"),
    ("Asia/Vladivostok", "Владивосток (UTC+10)"),
    ("Asia/Magadan", "Магадан (UTC+11)"),
    ("Asia/Kamchatka", "Камчатка (UTC+12)"),
]


# --- Клавиатуры ---

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_ADD, BTN_ADD_TASK],
            [BTN_LIST, BTN_LIST_TASKS],
            [BTN_REMINDERS, BTN_TEST],
            [BTN_HELP],
        ],
        resize_keyboard=True,
        input_field_placeholder="Тапни кнопку, напиши или наговори...",
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

async def transcribe_voice(voice_file):
    if not voice_available():
        return None

    with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await voice_file.download_to_drive(tmp_path)

        if _openai_client:
            with open(tmp_path, "rb") as f:
                transcript = await _async_call(
                    _openai_client.audio.transcriptions.create,
                    model="whisper-1",
                    file=f,
                    language="ru",
                    prompt=(
                        "Пользователь диктует короткие заметки на русском для бота. "
                        "Возможные команды: добавь идею, запиши идею, запиши задачу, "
                        "напомни, купить молоко, в 9 часов вечера, по Омску."
                    ),
                )
            return (transcript.text or "").strip() or None

        text = await _async_call(_vosk_transcribe_file, tmp_path)
        return (text or "").strip() or None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _vosk_transcribe_file(ogg_path: str):
    model = _get_vosk_model()
    if model is None:
        return None

    import wave
    from vosk import KaldiRecognizer

    wav_path = ogg_path + ".wav"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", ogg_path,
                "-ar", "16000", "-ac", "1", "-f", "wav", wav_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            logger.warning("ffmpeg вернул код %s: %s", result.returncode, result.stderr.decode(errors="ignore")[:200])
            return None

        wf = wave.open(wav_path, "rb")
        rec = KaldiRecognizer(model, wf.getframerate())
        chunks = []
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            if rec.AcceptWaveform(data):
                piece = json.loads(rec.Result()).get("text", "")
                if piece:
                    chunks.append(piece)
        final = json.loads(rec.FinalResult()).get("text", "")
        if final:
            chunks.append(final)
        wf.close()
        return " ".join(chunks).strip() or None
    except FileNotFoundError:
        logger.error("ffmpeg не найден — Vosk не может конвертировать OGG. Установи ffmpeg на хост.")
        return None
    except Exception:
        logger.exception("Vosk транскрипция упала")
        return None
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


async def parse_idea_with_gpt(text: str):
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


TIMEZONE_ALIASES = {
    "омск": "Asia/Omsk",
    "омску": "Asia/Omsk",
    "омское": "Asia/Omsk",
    "москва": "Europe/Moscow",
    "москве": "Europe/Moscow",
    "мск": "Europe/Moscow",
    "самара": "Europe/Samara",
    "самаре": "Europe/Samara",
    "екатеринбург": "Asia/Yekaterinburg",
    "екатеринбургу": "Asia/Yekaterinburg",
    "новосибирск": "Asia/Novosibirsk",
    "новосибирску": "Asia/Novosibirsk",
    "иркутск": "Asia/Irkutsk",
    "иркутску": "Asia/Irkutsk",
    "владивосток": "Asia/Vladivostok",
    "владивостоку": "Asia/Vladivostok",
}

CAPTURE_INTENT_WORDS = (
    "задача", "задачу", "напомни", "напоминание", "поставь задачу",
    "сделай задачу", "запиши задачу", "добавь задачу", "поставь напоминание",
)
IDEA_INTENT_PATTERNS = (
    r"\b(добавь|запиши|сохрани|создай)\s+(мне\s+)?иде[яю]\b",
    r"^\s*идея\s*[:\-—]",
)
DATE_TIME_WORDS = (
    "сегодня", "завтра", "послезавтра", "вечером", "утром", "днем", "днём",
    "ночью", "по омску", "по москве",
)
ACTION_WORDS = (
    "купить", "сделать", "позвонить", "написать", "проверить", "отправить",
    "созвониться", "встретиться", "подготовить", "разобрать", "найти",
)


def _compact_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_capture_timezone(text: str, default_tz: str = "") -> str:
    low = (text or "").lower()
    for alias, tz in TIMEZONE_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            return tz
    return default_tz


def extract_capture_due_time(text: str) -> str:
    low = (text or "").lower().replace("ё", "е")

    m = re.search(r"\b(?:к|на|в)\s+([01]?\d|2[0-3])[:.](\d{2})\b", low)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"

    m = re.search(
        r"\b(?:к|на|в)?\s*([01]?\d|2[0-3])\s*(?:час(?:ов|а)?|ч)?\s*(утра|дня|вечера|ночи)\b",
        low,
    )
    if m:
        hour = int(m.group(1))
        part = m.group(2)
        if part in {"вечера", "дня"} and hour < 12:
            hour += 12
        if part == "ночи" and hour == 12:
            hour = 0
        return f"{hour:02d}:00"

    m = re.search(r"\b(?:к|на|в)\s+([01]?\d|2[0-3])\s*(?:час(?:ов|а)?|ч)?\b", low)
    if m:
        hour = int(m.group(1))
        if "вечер" in low and hour < 12:
            hour += 12
        return f"{hour:02d}:00"

    return ""


def classify_capture_text(text: str) -> dict:
    clean = _compact_spaces(text)
    low = clean.lower().replace("ё", "е")
    due_time = extract_capture_due_time(clean)
    timezone = extract_capture_timezone(clean)
    has_idea_intent = any(re.search(pattern, low) for pattern in IDEA_INTENT_PATTERNS)

    has_intent = any(word in low for word in CAPTURE_INTENT_WORDS)
    has_date_time = due_time or any(word in low for word in DATE_TIME_WORDS) or bool(re.search(r"\b[кнв]\s+\d", low))
    has_action = any(re.search(rf"\b{word}\b", low) for word in ACTION_WORDS)

    if has_idea_intent and not has_date_time:
        capture_type = "idea"
    elif has_intent or has_date_time:
        capture_type = "reminder" if "напом" in low else "task"
    elif has_action:
        capture_type = "task"
    else:
        capture_type = "idea"

    return {
        "type": capture_type,
        "title": generate_capture_title(clean, capture_type),
        "body": clean,
        "due_time": due_time,
        "timezone": timezone,
    }


def generate_capture_title(text: str, capture_type: str) -> str:
    title = _compact_spaces(text).strip(" .,!?:;")
    title = re.sub(r"^(привет|слушай|так|короче)[,\s]+", "", title, flags=re.IGNORECASE)
    title = re.sub(
        r"^(идея|задача|напоминание)\s*[:\-—]\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^(сделай|создай|поставь|добавь|запиши|сохрани)\s+(мне\s+)?(задачу|напоминание)\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^(добавь|запиши|сохрани|создай)\s+(мне\s+)?иде[яю]\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"^напомни(ть)?\s+(мне\s+)?", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^(нужно|надо|что нужно)\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(
        r"\b(на|к|в)\s+\d{1,2}([:.]\d{2})?\s*(час(?:ов|а)?|ч)?\s*(утра|дня|вечера|ночи)?\b",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\b(сегодня|завтра|послезавтра|вечером|утром|днем|днём|ночью|по\s+\w+)\b",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\bчто\s+нужно\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\bмолока\b", "молоко", title, flags=re.IGNORECASE)
    title = _compact_spaces(title).strip(" .,!?:;")

    if capture_type == "idea":
        title = re.sub(r"\bс\s+360-турами\b", "", title, flags=re.IGNORECASE)
        m = re.search(r"\bоффер\s+для\s+застройщиков\b", title, flags=re.IGNORECASE)
        if m:
            title = m.group(0)

    title = re.sub(r"\bпосле\s+запуска\b", "", title, flags=re.IGNORECASE)
    title = _compact_spaces(title).strip(" .,!?:;")

    words = title.split()
    if len(words) > 6:
        title = " ".join(words[:6])
    if not title:
        title = _compact_spaces(text).split(".")[0][:80].strip() or "Без названия"
    return title[:1].upper() + title[1:]


async def _async_call(fn, *args, **kwargs):
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# --- Главные команды ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    schedule_user_reminders(context.application, user_id)

    tz = db.get_user_timezone(user_id)
    reminders = db.get_user_reminders(user_id)
    times_txt = ", ".join(f"{h:02d}:{m:02d}" for _, h, m in reminders) or "—"
    text = (
        "👋 Привет! Я бот-задачник и идейник в одном.\n\n"
        f"💡 <b>Идеи</b> — то, что хочется обдумать. Каждый день в удобное время "
        "я буду напоминать о них списком.\n"
        f"✅ <b>Задачи</b> — то, что нужно сделать. Хранятся с галочками «сделано/не сделано».\n\n"
        f"🌍 Часовой пояс: <b>{html.escape(tz)}</b>\n"
        f"⏰ Напоминания: <b>{times_txt}</b>\n\n"
        "Используй кнопки внизу. Идею или задачу можно ввести текстом или голосом."
    )
    await update.message.reply_html(text, reply_markup=main_menu_keyboard())


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _openai_client:
        voice_status = "включён (OpenAI Whisper) — можно наговорить идею или задачу голосом"
    elif _vosk_available():
        voice_status = "включён (Vosk, оффлайн) — можно наговорить идею или задачу голосом"
    else:
        voice_status = "выключен — команда /voice покажет, как включить"
    text = (
        "<b>Как это работает</b>\n\n"
        "<b>Идеи</b> — то, что хочется обдумать и не забыть.\n"
        f"• <b>{BTN_ADD}</b> — ввести краткое название и подробное описание.\n"
        f"• <b>{BTN_LIST}</b> — посмотреть все идеи. У каждой «📖 Подробнее» и «🗑 Удалить».\n\n"
        "<b>Задачи</b> — то, что нужно сделать.\n"
        f"• <b>{BTN_ADD_TASK}</b> — наговорить или ввести задачу одной строкой.\n"
        f"• <b>{BTN_LIST_TASKS}</b> — список задач с галочками «сделано».\n\n"
        f"• <b>{BTN_REMINDERS}</b> — ежедневные напоминания (по идеям) и часовой пояс.\n"
        f"• <b>{BTN_TEST}</b> — отправить тестовое напоминание прямо сейчас.\n"
        f"• <b>/voice</b> — инструкция по голосовому вводу (сейчас {voice_status})."
    )
    await update.message.reply_html(text, reply_markup=main_menu_keyboard())


async def voice_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _openai_client:
        text = (
            "🎤 <b>Голосовой ввод включён (OpenAI Whisper).</b>\n\n"
            "Просто запиши голосовое прямо в чате:\n"
            "• Вне диалогов — создастся идея (GPT сам разобьёт на название и описание).\n"
            f"• В режиме «{BTN_ADD_TASK}» — текст голоса сохранится как задача.\n"
            f"• На шагах «{BTN_ADD}» (название/описание) — текст голоса подставится в шаг."
        )
    elif _vosk_available():
        text = (
            "🎤 <b>Голосовой ввод включён (Vosk, оффлайн, бесплатно).</b>\n\n"
            "Vosk работает прямо на сервере, без OpenAI. Качество ниже Whisper, "
            "но для коротких фраз вполне приемлемо.\n\n"
            f"• «{BTN_ADD_TASK}» — наговори задачу, она сохранится как есть.\n"
            f"• На шагах «{BTN_ADD}» — голос подставится в текущий шаг.\n"
            "• Вне диалогов голос создаст идею (название = первая фраза, описание = весь текст).\n\n"
            "Если хочешь лучшее качество — добавь <code>OPENAI_API_KEY</code> в Railway Variables, "
            "и бот автоматически переключится на Whisper."
        )
    else:
        text = (
            "🎤 <b>Голосовой ввод выключен.</b>\n\n"
            "Доступно два варианта:\n\n"
            "<b>1. OpenAI Whisper (рекомендуется, платно но дёшево):</b>\n"
            "• https://platform.openai.com → Billing → пополни $5.\n"
            "• API keys → Create new secret key.\n"
            "• Railway → Variables → добавь <code>OPENAI_API_KEY</code>.\n\n"
            "<b>2. Vosk (бесплатно, оффлайн, качество ниже):</b>\n"
            "• Установи пакет vosk (он уже в requirements.txt).\n"
            "• На сервере нужен ffmpeg (nixpacks.toml в репо уже его ставит).\n"
            "• Vosk сам скачает русскую модель (~45 МБ) при первом голосовом.\n\n"
            "Railway сам перезапустит бота — голос заработает."
        )
    await update.message.reply_html(text, reply_markup=main_menu_keyboard(), disable_web_page_preview=True)


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


# --- Тестовое напоминание + ежедневное ---

def _build_reminder_text(ideas) -> str:
    lines = [f"💡 {html.escape(b)}" for _, b, _ in ideas]
    return (
        f"👋 Привет! Напоминаю про твои идеи ({len(ideas)}):\n\n"
        + "\n".join(lines)
        + f"\n\nЗагляни в «{BTN_LIST}» — может, что-то захочешь сделать прямо сейчас."
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

    await update.message.reply_html(
        _build_reminder_text(ideas),
        reply_markup=main_menu_keyboard(),
    )


async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    ideas = db.get_user_ideas(user_id)
    if not ideas:
        return
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=_build_reminder_text(ideas),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Не удалось отправить напоминание пользователю %s", user_id)


def _user_tzinfo(user_id: int):
    tz_name = db.get_user_timezone(user_id)
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("Неизвестный часовой пояс %s у пользователя %s, fallback на UTC", tz_name, user_id)
        return ZoneInfo("UTC")


def schedule_user_reminders(application: Application, user_id: int):
    job_queue = application.job_queue
    prefix = f"{REMINDER_JOB_PREFIX}{user_id}:"
    for job in list(job_queue.jobs()):
        if job.name and job.name.startswith(prefix):
            job.schedule_removal()

    tzinfo = _user_tzinfo(user_id)
    for rid, hour, minute in db.get_user_reminders(user_id):
        job_queue.run_daily(
            send_daily_reminder,
            time=time(hour=hour, minute=minute, tzinfo=tzinfo),
            data={"user_id": user_id},
            name=f"{prefix}{rid}",
        )


# --- Диалог: добавить идею ---

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice_hint = " или наговори голосом 🎤" if voice_available() else ""
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
            "Слишком длинно (макс. 200 символов). Сократи, пожалуйста.",
            reply_markup=cancel_keyboard(),
        )
        return BRIEF

    context.user_data["brief"] = brief
    voice_hint = " или наговори голосом 🎤" if voice_available() else ""
    await update.message.reply_text(
        f"📖 Теперь подробное описание (что именно, зачем, как){voice_hint}.\n\n"
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
    schedule_user_reminders(context.application, user_id)

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


# --- Меню напоминаний ---

def _reminders_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for rid, h, m in db.get_user_reminders(user_id):
        rows.append([
            InlineKeyboardButton(f"🔔 {h:02d}:{m:02d}", callback_data=f"rem_noop:{rid}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"rem_del:{rid}"),
        ])
    rows.append([InlineKeyboardButton("➕ Добавить напоминание", callback_data="rem_add")])
    rows.append([InlineKeyboardButton("🌍 Сменить часовой пояс", callback_data="rem_tz")])
    return InlineKeyboardMarkup(rows)


def _reminders_text(user_id: int) -> str:
    tz = db.get_user_timezone(user_id)
    reminders = db.get_user_reminders(user_id)
    head = f"🌍 Часовой пояс: <b>{html.escape(tz)}</b>\n\n"
    if not reminders:
        return head + "У тебя пока нет напоминаний. Добавь первое кнопкой ниже."
    return (
        head
        + f"Напоминания ({len(reminders)}), время по этому поясу.\n"
        + "Чтобы удалить — нажми «🗑 Удалить» рядом со временем."
    )


async def reminders_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    await update.message.reply_html(
        _reminders_text(user_id),
        reply_markup=_reminders_keyboard(user_id),
    )


async def _refresh_reminders_menu(query, user_id: int):
    try:
        await query.edit_message_text(
            _reminders_text(user_id),
            parse_mode=ParseMode.HTML,
            reply_markup=_reminders_keyboard(user_id),
        )
    except Exception:
        # Если сообщение нельзя отредактировать — пришлём новое.
        await query.message.reply_html(
            _reminders_text(user_id),
            reply_markup=_reminders_keyboard(user_id),
        )


async def reminders_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tz = db.get_user_timezone(query.from_user.id)
    await query.message.reply_text(
        f"Введи время напоминания в формате HH:MM по времени «{tz}».\n"
        "Например: 12:00\n\n"
        f"Или нажми «{BTN_CANCEL}».",
        reply_markup=cancel_keyboard(),
    )
    return ADD_REMINDER_TIME


async def reminders_add_apply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        hh, mm = text.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Неверный формат. Введи HH:MM, например 12:00.",
            reply_markup=cancel_keyboard(),
        )
        return ADD_REMINDER_TIME

    user_id = update.effective_user.id
    db.upsert_user(user_id)
    created = db.add_reminder(user_id, h, m)
    schedule_user_reminders(context.application, user_id)

    if created:
        msg = f"✅ Напоминание на {h:02d}:{m:02d} добавлено."
    else:
        msg = f"ℹ️ Напоминание на {h:02d}:{m:02d} уже было — ничего не изменилось."

    await update.message.reply_text(msg, reply_markup=main_menu_keyboard())
    await update.message.reply_html(
        _reminders_text(user_id),
        reply_markup=_reminders_keyboard(user_id),
    )
    return ConversationHandler.END


# --- Задачи ---

def _task_row_parts(task) -> tuple[int, str, bool, str, str, str]:
    if len(task) >= 6:
        task_id, title, done, body, due_time, timezone = task[:6]
    else:
        task_id, title, done = task[:3]
        body, due_time, timezone = title, "", ""
    return task_id, title, bool(done), body, due_time, timezone


def task_message(title: str, done: bool, body: str = "", due_time: str = "", timezone: str = "") -> str:
    title = title or body or "Без названия"
    meta = ""
    if due_time:
        meta = f"\n⏰ {html.escape(due_time)}"
        if timezone:
            meta += f" {html.escape(timezone)}"
    if done:
        return f"✔️ <s>{html.escape(title)}</s>{meta}"
    return f"⬜ {html.escape(title)}{meta}"


def recognized_voice_message(text: str) -> str:
    text = _compact_spaces(text)
    if len(text) > 500:
        text = text[:500].rstrip() + "..."
    return f"\n\n<i>Распознано:</i> {html.escape(text)}" if text else ""


def task_keyboard(task_id: int, done: bool) -> InlineKeyboardMarkup:
    toggle_label = "↩️ Не сделано" if done else "✅ Сделано"
    toggle_action = "task_undone" if done else "task_done"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(toggle_label, callback_data=f"{toggle_action}:{task_id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"task_del:{task_id}"),
        ],
    ])


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = db.get_user_tasks(user_id)

    if not tasks:
        await update.message.reply_text(
            f"Задач пока нет. Нажми «{BTN_ADD_TASK}» и наговори или напиши задачу.",
            reply_markup=main_menu_keyboard(),
        )
        return

    open_count = sum(1 for task in tasks if not _task_row_parts(task)[2])
    done_count = len(tasks) - open_count
    await update.message.reply_text(
        f"📋 Задачи: {open_count} открыто, {done_count} сделано.",
        reply_markup=main_menu_keyboard(),
    )
    for task in tasks:
        tid, title, done, body, due_time, timezone = _task_row_parts(task)
        await update.message.reply_html(
            task_message(title, done, body, due_time, timezone),
            reply_markup=task_keyboard(tid, done),
        )


async def task_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice_hint = " или наговори голосом 🎤" if voice_available() else ""
    await update.message.reply_text(
        f"✅ Что нужно сделать? Напиши задачу одной строкой{voice_hint}.\n\n"
        f"Или нажми «{BTN_CANCEL}».",
        reply_markup=cancel_keyboard(),
    )
    return TASK_TEXT


async def _handle_task_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    text = (text or "").strip()
    if not text:
        await update.message.reply_text(
            "Пустая задача — так нельзя. Попробуй ещё раз.",
            reply_markup=cancel_keyboard(),
        )
        return TASK_TEXT
    if len(text) > 500:
        text = text[:500]

    capture = classify_capture_text(text)
    title = capture["title"]

    user_id = update.effective_user.id
    db.upsert_user(user_id)
    timezone = capture.get("timezone") or db.get_user_timezone(user_id)
    task_id = db.add_task(
        user_id,
        title,
        title=title,
        body=text,
        due_time=capture.get("due_time", ""),
        timezone=timezone,
    )

    await update.message.reply_html(
        f"✅ Задача создана\n\n{task_message(title, False, text, capture.get('due_time', ''), timezone)}",
        reply_markup=main_menu_keyboard(),
    )
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Сделано", callback_data=f"task_done:{task_id}")],
            [
                InlineKeyboardButton("➕ Ещё задача", callback_data="menu:add_task"),
                InlineKeyboardButton("📋 Все задачи", callback_data="menu:list_tasks"),
            ],
            [InlineKeyboardButton("🗑 Удалить эту", callback_data=f"task_del:{task_id}")],
        ]),
    )
    return ConversationHandler.END


async def task_add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_task_text(update, context, update.message.text)


async def task_add_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context)
    if text is None:
        return TASK_TEXT
    return await _handle_task_text(update, context, text)


# --- Голос вне диалога: классифицируем в идею / задачу / напоминание ---

async def voice_top_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not voice_available():
        await update.message.reply_text(
            "🎤 Голосовой ввод сейчас не настроен. Команда /voice покажет, как включить.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    text = await _transcribe_or_warn(update, context)
    if not text:
        return

    user_id = update.effective_user.id
    db.upsert_user(user_id)

    capture = classify_capture_text(text)
    capture_type = capture["type"]

    if capture_type in {"task", "reminder"}:
        title = capture["title"]
        due_time = capture.get("due_time", "")
        timezone = capture.get("timezone") or db.get_user_timezone(user_id)
        task_id = db.add_task(
            user_id,
            title,
            title=title,
            body=capture["body"],
            due_time=due_time,
            timezone=timezone,
        )
        response = "🔔 Напоминание создано" if capture_type == "reminder" else "✅ Задача создана"
        await update.message.reply_html(
            f"{response}\n\n{task_message(title, False, capture['body'], due_time, timezone)}"
            f"{recognized_voice_message(capture['body'])}",
            reply_markup=main_menu_keyboard(),
        )
        await update.message.reply_text(
            "Что дальше?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Сделано", callback_data=f"task_done:{task_id}")],
                [
                    InlineKeyboardButton("➕ Ещё задача", callback_data="menu:add_task"),
                    InlineKeyboardButton("📋 Все задачи", callback_data="menu:list_tasks"),
                ],
                [InlineKeyboardButton("🗑 Удалить эту", callback_data=f"task_del:{task_id}")],
            ]),
        )
        return

    brief = capture["title"]
    details = capture["body"]
    if _openai_client:
        parsed = await parse_idea_with_gpt(text)
        if parsed:
            brief, details = parsed

    if not brief:
        # Fallback (Vosk или GPT отвалился): берём первую фразу как название,
        # весь текст — как описание.
        first = text.split(".")[0].strip() or text.strip()
        brief = first[:120]
        details = text.strip()

    idea_id = db.add_idea(user_id, brief, details)
    schedule_user_reminders(context.application, user_id)

    await update.message.reply_html(
        f"✅ Идея сохранена\n\n{full_message(brief, details)}{recognized_voice_message(text)}",
        reply_markup=main_menu_keyboard(),
    )
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=post_save_keyboard(idea_id),
    )


async def _transcribe_or_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not voice_available():
        await update.message.reply_text(
            "🎤 Голосовой ввод сейчас не настроен. Напиши текстом, пожалуйста.",
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


# --- Inline-кнопки (общий обработчик) ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    user_id = query.from_user.id

    # Меню напоминаний
    if data.startswith("rem_del:"):
        rid = int(data.split(":", 1)[1])
        db.delete_reminder(rid, user_id)
        schedule_user_reminders(context.application, user_id)
        await _refresh_reminders_menu(query, user_id)
        return

    if data == "rem_tz":
        rows = [
            [InlineKeyboardButton(label, callback_data=f"rem_tz_set:{tz}")]
            for tz, label in PRESET_TIMEZONES
        ]
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="rem_back")])
        await query.edit_message_text(
            "Выбери часовой пояс:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("rem_tz_set:"):
        tz = data.split(":", 1)[1]
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            await query.answer("Неизвестный часовой пояс", show_alert=True)
            return
        db.upsert_user(user_id)
        db.set_user_timezone(user_id, tz)
        schedule_user_reminders(context.application, user_id)
        await _refresh_reminders_menu(query, user_id)
        return

    if data == "rem_back":
        await _refresh_reminders_menu(query, user_id)
        return

    if data == "rem_noop:0" or data.startswith("rem_noop:"):
        return

    # Меню после сохранения
    if data == "menu:list":
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

    if data == "menu:add":
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Нажми «{BTN_ADD}» внизу — и начнём вводить новую идею.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "menu:list_tasks":
        tasks = db.get_user_tasks(user_id)
        if not tasks:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Задач пока нет. Нажми «{BTN_ADD_TASK}».",
                reply_markup=main_menu_keyboard(),
            )
            return
        open_count = sum(1 for task in tasks if not _task_row_parts(task)[2])
        done_count = len(tasks) - open_count
        await context.bot.send_message(
            chat_id=user_id,
            text=f"📋 Задачи: {open_count} открыто, {done_count} сделано.",
            reply_markup=main_menu_keyboard(),
        )
        for task in tasks:
            tid, title, done, body, due_time, timezone = _task_row_parts(task)
            await context.bot.send_message(
                chat_id=user_id,
                text=task_message(title, done, body, due_time, timezone),
                parse_mode=ParseMode.HTML,
                reply_markup=task_keyboard(tid, done),
            )
        return

    if data == "menu:add_task":
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Нажми «{BTN_ADD_TASK}» внизу — и добавим новую задачу.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Задачи: toggle done / undone / delete
    if data.startswith("task_done:") or data.startswith("task_undone:") or data.startswith("task_del:"):
        action, tid_str = data.split(":", 1)
        try:
            tid = int(tid_str)
        except ValueError:
            return
        task = db.get_task(tid, user_id)
        if not task:
            await query.edit_message_text("⚠️ Задача не найдена или была удалена.")
            return
        _, title, _done, body, due_time, timezone = _task_row_parts(task)
        if action == "task_done":
            db.set_task_done(tid, user_id, True)
            await query.edit_message_text(
                task_message(title, True, body, due_time, timezone),
                parse_mode=ParseMode.HTML,
                reply_markup=task_keyboard(tid, True),
            )
        elif action == "task_undone":
            db.set_task_done(tid, user_id, False)
            await query.edit_message_text(
                task_message(title, False, body, due_time, timezone),
                parse_mode=ParseMode.HTML,
                reply_markup=task_keyboard(tid, False),
            )
        elif action == "task_del":
            db.delete_task(tid, user_id)
            await query.edit_message_text(
                f"🗑 Задача удалена:\n\n{task_message(title, False, body, due_time, timezone)}",
                parse_mode=ParseMode.HTML,
            )
        return

    # Идеи: expand/collapse/delete
    try:
        action, idea_id_str = data.split(":", 1)
        idea_id = int(idea_id_str)
    except (ValueError, AttributeError):
        return

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


# --- Прочее ---

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Update %s caused error", update, exc_info=context.error)


async def post_init(application: Application):
    db.init_db()
    for user_id in db.get_all_user_ids():
        schedule_user_reminders(application, user_id)
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
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(filters.Regex(f"^{BTN_ADD}$"), add_start),
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

    task_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addtask", task_add_start),
            MessageHandler(filters.Regex(f"^{BTN_ADD_TASK}$"), task_add_start),
        ],
        states={
            TASK_TEXT: [
                MessageHandler(filters.VOICE | filters.AUDIO, task_add_voice),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{BTN_CANCEL}$"),
                    task_add_text,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel),
        ],
        allow_reentry=True,
    )

    add_reminder_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(reminders_add_start, pattern="^rem_add$")],
        states={
            ADD_REMINDER_TIME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{BTN_CANCEL}$"),
                    reminders_add_apply,
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
    application.add_handler(CommandHandler("reminders", reminders_show))
    application.add_handler(CommandHandler("settime", reminders_show))
    application.add_handler(CommandHandler("voice", voice_instructions))
    application.add_handler(CommandHandler("tasks", list_tasks))

    application.add_handler(add_conv)
    application.add_handler(task_conv)
    application.add_handler(add_reminder_conv)

    # Кнопки главного меню (вне диалогов)
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_LIST}$"), list_ideas))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_LIST_TASKS}$"), list_tasks))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_TEST}$"), test_reminder))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_HELP}$"), show_help))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_REMINDERS}$"), reminders_show))

    # Голосовые вне диалогов — классифицируем в идею / задачу / напоминание.
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_top_level))

    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
