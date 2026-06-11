import os
import asyncio
import difflib
import json
import html
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, time
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
from weeek_client import WeeekApiError, WeeekClient

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Asia/Omsk")
OPENAI_PARSE_MODEL = os.getenv("OPENAI_PARSE_MODEL", "gpt-5.5")
OPENAI_STT_MODEL = (os.getenv("OPENAI_STT_MODEL") or "gpt-4o-transcribe").strip()
OPENAI_STT_ENABLED = (os.getenv("OPENAI_STT_ENABLED") or "true").strip().lower() in {"1", "true", "yes", "on"}
OPENAI_TIMEOUT_SECONDS = float((os.getenv("OPENAI_TIMEOUT_SECONDS") or "90").strip())
OPENAI_MAX_RETRIES = int((os.getenv("OPENAI_MAX_RETRIES") or "3").strip())
WEEEK_API_TOKEN = (os.getenv("WEEEK_API_TOKEN") or "").strip()
WEEEK_API_BASE_URL = (os.getenv("WEEEK_API_BASE_URL") or "https://api.weeek.net/public/v1").strip()
WEEEK_DEFAULT_WORKSPACE_ID = (os.getenv("WEEEK_DEFAULT_WORKSPACE_ID") or "").strip()


def _normalize_text(text: str) -> str:
    return _compact_spaces(text).lower().replace("ё", "е")


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

_last_openai_stt_status = "not_tested"
_last_openai_stt_error = ""
_last_capture_parse_status = "not_tested"


def _int_env(name: str, default: int) -> int:
    raw_value = (os.getenv(name) or "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("%s must be an integer; using default %s", name, default)
        return default


OPENAI_TIMEOUT_SECONDS = _int_env("OPENAI_TIMEOUT_SECONDS", 90)
OPENAI_MAX_RETRIES = _int_env("OPENAI_MAX_RETRIES", 3)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if BOT_TOKEN:
            message = message.replace(BOT_TOKEN, "<BOT_TOKEN_REDACTED>")
        message = re.sub(r"\bsk-(?:proj|live|test|svcacct|admin|org)-[A-Za-z0-9_-]+", "<OPENAI_KEY_REDACTED>", message)
        message = re.sub(r"\bsk-[A-Za-z0-9_-]{20,}\b", "<OPENAI_KEY_REDACTED>", message)
        if message != record.getMessage():
            record.msg = message
            record.args = ()
        return True


for handler in logging.getLogger().handlers:
    handler.addFilter(SecretRedactionFilter())


def _openai_client_kwargs() -> dict:
    return {
        "api_key": OPENAI_API_KEY,
        "timeout": OPENAI_TIMEOUT_SECONDS,
        "max_retries": OPENAI_MAX_RETRIES,
    }


# OpenAI client is used for text/chat parsing and the primary STT path.
_openai_client = None
_openai_stt_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI

        _openai_client = OpenAI(**_openai_client_kwargs())
        logger.info(
            "OpenAI client initialized: official_api=true, parse_model=%s, timeout=%s, max_retries=%s",
            OPENAI_PARSE_MODEL,
            OPENAI_TIMEOUT_SECONDS,
            OPENAI_MAX_RETRIES,
        )
        if OPENAI_STT_ENABLED and OPENAI_STT_MODEL:
            _openai_stt_client = _openai_client
            logger.info(
                "OpenAI STT client initialized: official_api=true, model=%s, timeout=%s, max_retries=%s",
                OPENAI_STT_MODEL,
                OPENAI_TIMEOUT_SECONDS,
                OPENAI_MAX_RETRIES,
            )
        elif OPENAI_STT_ENABLED:
            logger.info("OpenAI STT is enabled, but OPENAI_STT_MODEL is empty.")
        else:
            logger.info("OpenAI STT is disabled via OPENAI_STT_ENABLED=false.")
    except ImportError:
        logger.warning("OPENAI_API_KEY is set, but the openai package is not installed.")
else:
    logger.info("OPENAI_API_KEY is not set - GPT parsing is disabled and voice falls back to Vosk when available.")

VOSK_MODEL_URL = os.getenv(
    "VOSK_MODEL_URL",
    "https://huggingface.co/rhasspy/vosk-models/resolve/main/ru/vosk-model-small-ru-0.22.zip",
)
_vosk_model = None
_vosk_checked = False


def _voice_status_summary() -> str:
    if _openai_stt_available():
        return f"OpenAI STT: {OPENAI_STT_MODEL}"
    if _vosk_available() and shutil.which("ffmpeg"):
        return "Vosk fallback"
    return "disabled"


def _record_openai_stt_status(status: str, error_code: str = "") -> None:
    global _last_openai_stt_status, _last_openai_stt_error
    _last_openai_stt_status = status
    _last_openai_stt_error = error_code


def _record_capture_parse_status(status: str) -> None:
    global _last_capture_parse_status
    _last_capture_parse_status = status


def _voice_runtime_status_lines() -> list[str]:
    lines = [f"<b>Configured primary STT:</b> {html.escape(_voice_status_summary())}"]
    if _last_openai_stt_status == "ok":
        lines.append("<b>Last OpenAI STT result:</b> success")
    elif _last_openai_stt_status == "fallback":
        detail = html.escape(_last_openai_stt_error or "unknown_error")
        lines.append(f"<b>Last OpenAI STT result:</b> fallback to Vosk ({detail})")
    else:
        lines.append("<b>Last OpenAI STT result:</b> not tested since current startup")

    if _last_capture_parse_status == "gpt":
        lines.append("<b>Last GPT parse result:</b> success")
    elif _last_capture_parse_status == "rules":
        lines.append("<b>Last GPT parse result:</b> rules fallback")
    else:
        lines.append("<b>Last GPT parse result:</b> not tested since current startup")
    return lines


def _vosk_model_dir() -> str:
    explicit = os.getenv("VOSK_MODEL_PATH")
    if explicit:
        return explicit
    if os.path.isdir("/data"):
        return "/data/vosk-model"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vosk-model")


def _vosk_model_ready() -> bool:
    return os.path.isdir(_vosk_model_dir())


def _download_vosk_model(target_dir: str):
    logger.info("Скачиваем Vosk модель: %s", VOSK_MODEL_URL)
    work_dir = tempfile.mkdtemp(prefix="vosk-dl-")
    zip_path = os.path.join(work_dir, "model.zip")
    with urllib.request.urlopen(VOSK_MODEL_URL, timeout=60) as response:
        with open(zip_path, "wb") as f:
            shutil.copyfileobj(response, f)
    if os.path.getsize(zip_path) < 1_000_000:
        raise RuntimeError("Vosk model download is too small; likely a network/proxy error")
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
        logger.info("Пакет vosk не установлен — голосовой ввод недоступен.")
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


def _openai_stt_available() -> bool:
    return bool(_openai_stt_client and OPENAI_STT_ENABLED and OPENAI_STT_MODEL)


def voice_available() -> bool:
    if _openai_stt_available():
        return True
    return _vosk_available() and bool(shutil.which("ffmpeg"))


logger.info(
    "STT health primary=%s official_api=%s model=%s fallback_vosk=%s status=%s",
    bool(_openai_stt_available()),
    bool(_openai_stt_available()),
    OPENAI_STT_MODEL or "-",
    bool(_vosk_available()),
    _voice_status_summary(),
)


# Состояния диалогов
BRIEF, DETAILS = range(2)
ADD_REMINDER_TIME = 200
TASK_TEXT = 300
CLARIFY_REMINDER_TIME = 400
WEEEK_CAPTURE = 500
WEEEK_PROJECT = 510
WEEEK_BOARD = 520
WEEEK_PARENT = 525
WEEEK_COLUMN = 530
WEEEK_EDIT = 535
WEEEK_PREVIEW = 540

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
BTN_ADD_WEEEK_TASK = "🧩 Задача ВИК"
BTN_LIST_WEEEK_TASKS = "📂 Задачи ВИК"
LEGACY_MENU_BUTTONS = (
    BTN_ADD,
    BTN_LIST,
    BTN_ADD_TASK,
    BTN_LIST_TASKS,
    BTN_REMINDERS,
    BTN_TEST,
    BTN_HELP,
)
MENU_BUTTON_PATTERN = "^(" + "|".join(
    re.escape(label)
    for label in (
        *LEGACY_MENU_BUTTONS,
        BTN_ADD_WEEEK_TASK,
        BTN_LIST_WEEEK_TASKS,
        BTN_CANCEL,
    )
) + ")$"

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
            [BTN_LIST_WEEEK_TASKS],
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

WEEEK_TEXT_PATTERNS = (
    r"\b(?:добавь|создай|закинь|сохрани|запиши)\s+(?:мне\s+)?задач\w*\s+(?:в|во)\s+(?:вик|weeek)\b",
    r"\b(?:вик|weeek)\s+задач\w*\b",
    r"\bзадач\w*\s+(?:в|во)\s+(?:вик|weeek)\b",
)

WEEEK_COLUMN_HINTS = {
    "to_work": ("к работе", "to work", "на потом", "в очередь", "в план"),
    "in_work": ("в работе", "в работу", "делаю", "в процессе", "in work"),
}


WEEEK_SUBTASK_PATTERNS = (
    r"\b(?:добавь|создай|запиши)\s+подзадач\w*",
    r"\bподзадач\w*\s+(?:к|для)\s+задач\w*",
)

LOCAL_TASK_PATTERNS = (
    r"\b(?:добавь|создай|запиши)\s+(?:мне\s+)?задач\w*\b",
)

IDEA_PATTERNS = (
    r"\b(?:запиши|сохрани)\s+иде\w+\b",
    r"\bэто\s+идея\b",
)

REMINDER_PATTERNS = (
    r"\bнапомни\b",
    r"\bпоставь\s+напоминани\w*\b",
)

WEEEK_TARGETS = {
    "6": {
        "project_id": "6",
        "project_name": "Личное",
        "board_id": "10",
        "board_name": "Моя доска",
        "aliases": ("личное", "в личное", "личная", "личную", "личка"),
    },
    "5": {
        "project_id": "5",
        "project_name": "Vibecoding SANYA&EGOR",
        "board_id": "9",
        "board_name": "SaaS Deck - задачи",
        "aliases": ("vibe coding", "vibecoding", "вайб кодинг", "вибе кодинг", "в айкодинг", "в vibecoding"),
    },
}

WEEEK_COLUMN_NAMES = {
    "to_work": ("к работе", "to work"),
    "in_work": ("в работе", "in work"),
    "done": ("готово", "сделано"),
}


def weeek_available() -> bool:
    return bool(WEEEK_API_TOKEN and WEEEK_API_BASE_URL)


def get_weeek_client() -> WeeekClient:
    if not weeek_available():
        raise WeeekApiError("WEEEK API is not configured")
    return WeeekClient(
        api_token=WEEEK_API_TOKEN,
        base_url=WEEEK_API_BASE_URL,
        workspace_id=WEEEK_DEFAULT_WORKSPACE_ID,
    )


def is_weeek_request(text: str) -> bool:
    low = _compact_spaces(text).lower().replace("ё", "е")
    return any(re.search(pattern, low, flags=re.IGNORECASE) for pattern in WEEEK_TEXT_PATTERNS)


def strip_weeek_request_prefix(text: str) -> str:
    cleaned = normalize_capture_command_text(text)
    cleaned = re.sub(
        r"^(?:добавь|создай|закинь|сохрани|запиши)\s+(?:мне\s+)?задач\w*\s+(?:в|во)\s+(?:вик|weeek)\s*[:,-]?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:вик|weeek)\s+задач\w*\s*[:,-]?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return _compact_spaces(cleaned).strip(" .,!?:;")


def _weeek_project_route_variants(route: dict | None = None) -> list[str]:
    items = [route.get("project")] if route and route.get("project") else list(WEEEK_TARGETS.values())
    variants: list[str] = []
    for item in items:
        if not item:
            continue
        names = [item.get("project_name", "")] + list(item.get("aliases", ()))
        for name in names:
            candidate = _compact_spaces(str(name))
            candidate = re.sub(r"^(?:в|во)\s+", "", candidate, flags=re.IGNORECASE)
            if candidate and candidate not in variants:
                variants.append(candidate)
    return variants


def _weeek_column_route_variants(route: dict | None = None) -> list[str]:
    keys = [route.get("column_hint")] if route and route.get("column_hint") else list(WEEEK_COLUMN_HINTS)
    variants: list[str] = []
    for key in keys:
        if not key:
            continue
        for source in (WEEEK_COLUMN_HINTS.get(key, ()), WEEEK_COLUMN_NAMES.get(key, ())):
            for name in source:
                candidate = _compact_spaces(str(name))
                if candidate and candidate not in variants:
                    variants.append(candidate)
    return variants


def strip_weeek_routing_metadata(text: str, route: dict | None = None) -> str:
    cleaned = _compact_spaces(text)
    if not cleaned:
        return ""

    patterns: list[str] = []
    for variant in _weeek_project_route_variants(route):
        escaped = re.escape(variant)
        patterns.extend(
            [
                rf"\bв\s+проект\s+{escaped}\b",
                rf"\bв\s+раздел\s+{escaped}\b",
                rf"\bпроект\s+{escaped}\b",
                rf"\bраздел\s+{escaped}\b",
                rf"\bв\s+{escaped}\b",
                rf"\b(?:раздел|проект)\s*:\s*{escaped}\b",
            ]
        )
    for variant in _weeek_column_route_variants(route):
        escaped = re.escape(variant)
        patterns.extend(
            [
                rf"\b(?:и\s+)?постав(?:ь|ить)\s+статус\s+{escaped}\b",
                rf"\b(?:и\s+)?постав(?:ь|ить)\s+(?:это\s+)?в\s+колонку\s+{escaped}\b",
                rf"\b(?:и\s+)?в\s+колонку\s+{escaped}\b",
                rf"\b(?:и\s+)?добав(?:ь|ить)\s+в\s+{escaped}\b",
                rf"\b(?:и\s+)?статус\s+{escaped}\b",
                rf"\b(?:и\s+)?колонк[ауе]\s+{escaped}\b",
                rf"^\s*(?:и\s+)?{escaped}\b",
                rf"\b(?:статус|колонка)\s*:\s*{escaped}\b",
            ]
        )

    for pattern in patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"([,.;:!?])\s*(?:[,.;:!?]\s*)+", r"\1 ", cleaned)
    cleaned = re.sub(r"\b(?:и|а)\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:и|а)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = cleaned.strip(" ,.;:!?-")
    return cleaned


def sanitize_weeek_capture_content(capture: dict, route: dict | None = None, fallback_text: str = "") -> dict:
    sanitized = dict(capture)
    capture_type = sanitized.get("type") or "task"
    title = strip_weeek_routing_metadata(_compact_spaces(sanitized.get("title") or ""), route)
    body = strip_weeek_routing_metadata(_compact_spaces(sanitized.get("body") or fallback_text or ""), route)

    if not title:
        title = generate_capture_title(fallback_text or body or sanitized.get("transcript_clean") or "", capture_type)
    if not body:
        body = _compact_spaces(fallback_text or sanitized.get("body") or sanitized.get("transcript_clean") or "")

    sanitized["title"] = _compact_spaces(title)
    sanitized["body"] = _compact_spaces(body)
    return sanitized


def normalize_weeek_column_hint(text: str) -> str:
    low = _compact_spaces(text).lower().replace("ё", "е")
    for key, variants in WEEEK_COLUMN_HINTS.items():
        if any(variant in low for variant in variants):
            return key
    return ""


def detect_top_level_route(text: str) -> dict:
    normalized = _normalize_text(normalize_capture_command_text(text))
    project = match_weeek_target(normalized)
    target = ""
    if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in WEEEK_SUBTASK_PATTERNS):
        target = "weeek_subtask"
    elif is_weeek_request(normalized):
        target = "weeek_task"
    elif project and re.search(r"\b(?:добавь|создай|запиши)\b", normalized):
        target = "weeek_task"
    elif any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in REMINDER_PATTERNS):
        target = "reminder"
    elif any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in LOCAL_TASK_PATTERNS):
        target = "weeek_task"
    elif any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in IDEA_PATTERNS):
        target = "idea"
    return {
        "target": target,
        "project": project,
        "board_name_candidate": extract_board_candidate(normalized),
        "column_hint": normalize_weeek_column_hint(normalized),
        "parent_task_candidate": extract_parent_task_candidate(normalized),
    }


def match_weeek_target(text: str) -> dict | None:
    normalized = _normalize_text(text)
    for item in WEEEK_TARGETS.values():
        if any(alias in normalized for alias in item["aliases"]):
            return dict(item)
    return None


def extract_parent_task_candidate(text: str) -> str:
    normalized = _compact_spaces(text)
    quoted = re.search(r"[\"«](.+?)[\"»]", normalized)
    if quoted:
        return _compact_spaces(quoted.group(1))
    match = re.search(
        r"подзадач\w*\s+(?:к|для)\s+задач\w*\s+(.+?)(?:\s+(?:что|чтобы|нужно|надо|и)\b|[,:.]|$)",
        normalized,
        flags=re.IGNORECASE,
    )
    if match:
        return _compact_spaces(match.group(1)).strip(" .,!?:;")
    return ""


def extract_board_candidate(text: str) -> str:
    normalized = _compact_spaces(text)
    patterns = (
        r"(?:в|во)\s+доск(?:у|е)\s+[\"«]?(.+?)[\"»]?(?:\s+(?:в|во)\s+проект\b|\s+(?:к|в)\s+работе\b|[,.!?:;]|$)",
        r"доск(?:а|у|е)\s*[:\-]\s*[\"«]?(.+?)[\"»]?(?:\s+(?:в|во)\s+проект\b|\s+(?:к|в)\s+работе\b|[,.!?:;]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return _compact_spaces(match.group(1)).strip(" .,!?:;")
    return ""


def _match_weeek_board(boards: list[dict], candidate: str) -> tuple[dict | None, str]:
    if not candidate:
        return None, ""
    normalized_candidate = _normalize_text(candidate)
    exact = [board for board in boards if _compact_spaces(board.get("name") or "") == candidate]
    if len(exact) == 1:
        return exact[0], "exact"

    normalized = [board for board in boards if _normalize_text(board.get("name") or "") == normalized_candidate]
    if len(normalized) == 1:
        return normalized[0], "normalized"

    contains = [
        board for board in boards
        if normalized_candidate in _normalize_text(board.get("name") or "")
        or _normalize_text(board.get("name") or "") in normalized_candidate
    ]
    if len(contains) == 1:
        return contains[0], "fuzzy"

    scored = []
    for board in boards:
        ratio = difflib.SequenceMatcher(None, normalized_candidate, _normalize_text(board.get("name") or "")).ratio()
        if ratio >= 0.72:
            scored.append((ratio, board))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and (len(scored) == 1 or scored[0][0] >= scored[1][0] + 0.12):
        return scored[0][1], "fuzzy"
    return None, ""


def _column_matches_hint(name: str, hint: str) -> bool:
    normalized = _normalize_text(name)
    return any(token in normalized for token in WEEEK_COLUMN_NAMES.get(hint, ()))


def _match_parent_task(tasks: list[dict], candidate: str) -> tuple[dict | None, str]:
    if not candidate:
        return None, ""
    normalized_candidate = _normalize_text(candidate)
    exact = [task for task in tasks if _compact_spaces(task.get("name") or "") == candidate]
    if len(exact) == 1:
        return exact[0], "exact"

    normalized = [task for task in tasks if _normalize_text(task.get("name") or "") == normalized_candidate]
    if len(normalized) == 1:
        return normalized[0], "normalized"

    contains = [
        task for task in tasks
        if normalized_candidate in _normalize_text(task.get("name") or "")
        or _normalize_text(task.get("name") or "") in normalized_candidate
    ]
    if len(contains) == 1:
        return contains[0], "fuzzy"

    scored = []
    for task in tasks:
        ratio = difflib.SequenceMatcher(None, normalized_candidate, _normalize_text(task.get("name") or "")).ratio()
        if ratio >= 0.72:
            scored.append((ratio, task))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and (len(scored) == 1 or scored[0][0] >= scored[1][0] + 0.12):
        return scored[0][1], "fuzzy"
    return None, ""


def build_weeek_task_title(capture: dict) -> str:
    title = _compact_spaces(capture.get("title") or "")
    body = _compact_spaces(capture.get("body") or "")
    if not title:
        title = "Новая задача"
    if body and body.casefold() != title.casefold() and "см. описание" not in title.lower():
        title = f"{title} — см. описание"
    return title[:120]


def _weeek_edit_field_label(field: str) -> str:
    if field == "title":
        return "название"
    if field == "body":
        return "описание"
    return "поле"


def parse_weeek_edit_request(text: str) -> dict | None:
    normalized = _compact_spaces(text).strip()
    if not normalized:
        return None

    patterns = (
        (r"^(?:мира[,:\s-]*)?(?:пожалуйста\s+)?(?:отредактируй|редактируй|измени|поменяй|обнови)\s+(?:тему|название|заголовок)\s*(?:на|вот на)?\s+(.+)$", "title"),
        (r"^(?:мира[,:\s-]*)?(?:пожалуйста\s+)?(?:отредактируй|редактируй|измени|поменяй|обнови)\s+описани[ея]\s*(?:на|вот на)?\s+(.+)$", "body"),
    )
    for pattern, field in patterns:
        match = re.match(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        value = _compact_spaces(match.group(1)).strip(" .,!?:;\"'«»")
        if value:
            return {"field": field, "value": value}
    return None


def _apply_weeek_draft_edit(draft: dict, field: str, value: str) -> bool:
    capture = draft.get("capture") or {}
    clean_value = _compact_spaces(value).strip()
    if field not in {"title", "body"} or not clean_value:
        return False
    capture[field] = clean_value
    draft["capture"] = capture
    draft.pop("edit_field", None)
    return True


def _has_editable_weeek_draft(context: ContextTypes.DEFAULT_TYPE) -> bool:
    draft = _get_weeek_draft(context)
    capture = draft.get("capture") or {}
    return bool(draft.get("project_id") and draft.get("board_id") and draft.get("column_id") and capture)


def weeek_preview_message(draft: dict) -> str:
    capture = draft.get("capture") or {}
    project = draft.get("project_name") or "—"
    board = draft.get("board_name") or "—"
    column = draft.get("column_name") or "—"
    title = build_weeek_task_title(capture)
    body = _compact_spaces(capture.get("body") or "")
    due_date = capture.get("due_date") or ""
    due_time = capture.get("due_time") or ""
    timezone = capture.get("timezone") or ""

    parts = [
        "🧩 <b>Черновик задачи для Weeek</b>",
        "",
        f"<b>Название:</b> {html.escape(title)}",
        f"<b>Проект:</b> {html.escape(project)}",
        f"<b>Доска:</b> {html.escape(board)}",
        f"<b>Колонка:</b> {html.escape(column)}",
    ]
    if due_date or due_time:
        parts.append(f"<b>Срок:</b> {html.escape((due_date + ' ' + due_time).strip())}")
    if timezone:
        parts.append(f"<b>Часовой пояс:</b> {html.escape(timezone)}")
    if body:
        parts.extend(["", f"<b>Описание:</b> {html.escape(body)}"])
    return "\n".join(parts)


def weeek_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Создать в Weeek", callback_data="weeek_create")],
            [InlineKeyboardButton("🔁 Выбрать колонку заново", callback_data="weeek_repick_column")],
            [InlineKeyboardButton("✖️ Отмена", callback_data="weeek_cancel")],
        ]
    )


def weeek_preview_message_ex(draft: dict) -> str:
    capture = draft.get("capture") or {}
    base = weeek_preview_message(draft)
    extra_parts = []
    parent_task = draft.get("parent_task_name") or ""
    transcript = _compact_spaces(capture.get("transcript_clean") or capture.get("transcript") or "")
    if parent_task and "Р РѕРґРёС‚РµР»СЊСЃРєР°СЏ Р·Р°РґР°С‡Р°:" not in base:
        extra_parts.append(f"<b>Р РѕРґРёС‚РµР»СЊСЃРєР°СЏ Р·Р°РґР°С‡Р°:</b> {html.escape(parent_task)}")
    if transcript and "РўСЂР°РЅСЃРєСЂРёРїС†РёСЏ:" not in base:
        extra_parts.append(f"<b>РўСЂР°РЅСЃРєСЂРёРїС†РёСЏ:</b> {html.escape(transcript)}")
    if not extra_parts:
        return base
    return base + "\n\n" + "\n".join(extra_parts)


def weeek_preview_keyboard_ex(draft: dict | None = None) -> InlineKeyboardMarkup:
    draft = draft or {}
    rows = [[InlineKeyboardButton("вњ… РЎРѕР·РґР°С‚СЊ РІ Weeek", callback_data="weeek_create")]]
    if draft.get("target") == "weeek_subtask":
        rows.append([InlineKeyboardButton("рџ”Ѓ Р’С‹Р±СЂР°С‚СЊ СЂРѕРґРёС‚РµР»СЏ Р·Р°РЅРѕРІРѕ", callback_data="weeek_repick_parent")])
    rows.append([InlineKeyboardButton("рџ”Ѓ Р’С‹Р±СЂР°С‚СЊ РєРѕР»РѕРЅРєСѓ Р·Р°РЅРѕРІРѕ", callback_data="weeek_repick_column")])
    rows.append([InlineKeyboardButton("вњ–пёЏ РћС‚РјРµРЅР°", callback_data="weeek_cancel")])
    return InlineKeyboardMarkup(rows)


def _is_upstream_bad_request(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", "") or "").lower()
    text = str(exc).lower()
    return status_code == 400 or "bad_request" in code or "upstream_bad_request" in text


def _openai_stt_engine_label() -> str:
    return f"openai:{OPENAI_STT_MODEL or 'stt'}"


def _openai_stt_error_code(exc: Exception) -> str:
    text = str(exc).lower()
    status_code = getattr(exc, "status_code", None)
    if status_code == 429 or "rate_limit" in text or "too many requests" in text:
        return "openai_stt_rate_limited"
    if "unsupported_country_region_territory" in text or "unsupported region" in text:
        return "openai_stt_unsupported_region"
    if status_code == 400 or "bad_request" in text:
        return "openai_stt_bad_request"
    if (status_code and status_code >= 500) or "upstream_unavailable" in text or "server_error" in text:
        return "openai_stt_server_error"
    if status_code == 401 or status_code == 403:
        return "openai_stt_auth"
    return "openai_stt_failed"


def _openai_transcribe_file(audio_path: str) -> str:
    with open(audio_path, "rb") as audio_file:
        response = _openai_stt_client.audio.transcriptions.create(
            model=OPENAI_STT_MODEL,
            file=audio_file,
        )
    return _compact_spaces(getattr(response, "text", "") or "")


async def transcribe_voice(voice_file):
    if not voice_available():
        if not _vosk_available() and not _openai_stt_available():
            return {"text": "", "engine": "", "error": "voice_unavailable"}
        if not shutil.which("ffmpeg"):
            return {"text": "", "engine": "vosk", "error": "ffmpeg_missing"}
        return {"text": "", "engine": "vosk", "error": "vosk_unavailable"}

    with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await voice_file.download_to_drive(tmp_path)
        if _openai_stt_available():
            try:
                text = await _async_call(_openai_transcribe_file, tmp_path)
                if text:
                    logger.info("voice transcription engine=%s", _openai_stt_engine_label())
                    return {"text": text, "engine": _openai_stt_engine_label()}
            except Exception as exc:
                openai_error = _openai_stt_error_code(exc)
                logger.warning(
                    "primary_stt_failed code=%s model=%s official_api=true fallback=vosk error=%s",
                    openai_error,
                    OPENAI_STT_MODEL,
                    exc,
                )
            else:
                openai_error = "openai_stt_empty"
        else:
            openai_error = ""

        if not _vosk_available():
            return {"text": "", "engine": _openai_stt_engine_label(), "error": openai_error or "vosk_unavailable"}
        if not shutil.which("ffmpeg"):
            return {"text": "", "engine": "vosk", "error": openai_error or "ffmpeg_missing"}

        model = await _async_call(_get_vosk_model)
        if model is None:
            return {"text": "", "engine": "vosk", "error": openai_error or "vosk_model_unavailable"}
        text = await _async_call(_vosk_transcribe_file, tmp_path)
        text = (text or "").strip()
        if text:
            logger.info("voice transcription engine=vosk")
            return {"text": text, "engine": "vosk"}
        return {"text": "", "engine": "vosk", "error": openai_error or "vosk_empty"}
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



def _chat_json_request_kwargs(model: str, messages: list[dict], temperature: float) -> dict:
    return {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }


def _extract_json_object(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return None

    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        try:
            data = json.loads(fence.group(1).strip())
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None



async def parse_idea_with_gpt(text: str):
    if not _openai_client:
        return None

    system = (
        "Ты ассистент, который из произвольного сообщения пользователя извлекает идею. "
        "Верни JSON с двумя полями: brief (короткое название идеи, до 100 символов, "
        "одна строка, без точки в конце) и details (краткое описание идеи в 1-2 предложениях, "
        "можно слегка причесать). Если в сообщении только название — придумай адекватное "
        "описание-расширение. Если только описание — придумай короткое название по смыслу. "
        "Отвечай на русском."
    )
    system += (
        " Дополнительно верни поля: "
        "target: local_task | weeek_task | weeek_subtask | idea | reminder; "
        "project_name_candidate: строка или пустая строка; "
        "board_name_candidate: строка или пустая строка; "
        "column_hint: to_work | in_work | done | пустая строка; "
        "parent_task_candidate: строка или пустая строка; "
        "transcript_clean: очищенная транскрипция без обращений к боту и без мусорных вводных слов. "
        "Если в тексте явно просят добавить задачу в ВИК/Weeek — target=weeek_task. "
        "Если явно просят подзадачу к существующей задаче — target=weeek_subtask. "
        "Если просят просто добавить задачу без ВИК/Weeek — target=local_task. "
        "Если встречается Личное — project_name_candidate=Личное. "
        "Если встречается Vibe Coding, Vibecoding, вайб кодинг — project_name_candidate=Vibecoding SANYA&EGOR. "
        "Примеры: "
        "1) 'добавь задачу в ВИК в личное к работе написать Кате по смете' => target=weeek_task, project_name_candidate=Личное, column_hint=to_work. "
        "2) 'добавь подзадачу к задаче Разобраться как парсить аудиторию и назови ее Собрать примеры' => target=weeek_subtask, parent_task_candidate='Разобраться как парсить аудиторию'. "
        "3) 'добавь задачу завтра в 10 написать Кате' => target=local_task."
    )
    system += (
        " Дополнительно верни поля: "
        "target: local_task | weeek_task | weeek_subtask | idea | reminder; "
        "project_name_candidate: строка или пустая строка; "
        "board_name_candidate: строка или пустая строка; "
        "column_hint: to_work | in_work | done | пустая строка; "
        "parent_task_candidate: строка или пустая строка; "
        "transcript_clean: очищенная транскрипция без обращений к боту и без мусорных вводных слов. "
        "Если в тексте явно просят добавить задачу в ВИК/Weeek — target=weeek_task. "
        "Если явно просят подзадачу к существующей задаче — target=weeek_subtask. "
        "Если просят просто добавить задачу без ВИК/Weeek — target=local_task. "
        "Если встречается Личное — project_name_candidate=Личное. "
        "Если встречается Vibe Coding, Vibecoding, вайб кодинг — project_name_candidate=Vibecoding SANYA&EGOR. "
        "Примеры: "
        "1) 'добавь задачу в ВИК в личное к работе написать Кате по смете' => target=weeek_task, project_name_candidate=Личное, column_hint=to_work. "
        "2) 'добавь подзадачу к задаче Разобраться как парсить аудиторию и назови ее Собрать примеры' => target=weeek_subtask, parent_task_candidate='Разобраться как парсить аудиторию'. "
        "3) 'добавь задачу завтра в 10 написать Кате' => target=local_task."
    )
    request_kwargs = _chat_json_request_kwargs(
        OPENAI_PARSE_MODEL,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        0.3,
    )
    try:
        response = await _async_call(_openai_client.chat.completions.create, **request_kwargs)
        raw = response.choices[0].message.content
        data = _extract_json_object(raw)
        if not data:
            return None
        brief = (data.get("brief") or "").strip()
        details = (data.get("details") or "").strip()
        if not brief or not details:
            return None
        return brief[:200], details
    except Exception as exc:
        if _is_upstream_bad_request(exc):
            logger.warning(
                "GPT idea parsing upstream bad request model=%s request_keys=%s error=%s",
                OPENAI_PARSE_MODEL,
                sorted(request_kwargs),
                exc,
            )
            return None
        logger.exception("GPT парсинг идеи упал")
        return None


async def parse_capture_with_gpt(text: str, default_timezone: str):
    if not _openai_client:
        return None

    now = _today_in_timezone(default_timezone)
    system = (
        "Ты понимаешь хаотичную русскую речь пользователя для Telegram-бота идей, задач и напоминаний. "
        "Пользователь может идти по улице, запинаться и говорить мусорные вводные: бот, слушай, короче, так, ну, типа, привет. "
        "Удали этот шум и извлеки смысл как человек. Верни только JSON без markdown с полями: "
        "type: idea | task | reminder; "
        "title: нормальное короткое название 2-7 слов, не обрывок распознанной речи, без обращений, команд, даты и времени; "
        "body: краткое очищенное описание по смыслу в 1-2 предложениях, без сырой расшифровки и команд пользователя; "
        "due_date: YYYY-MM-DD или пустая строка; "
        "due_time: HH:MM в 24-часовом формате или пустая строка; "
        "timezone: IANA timezone или пустая строка; "
        "confidence: число 0.0-1.0; "
        "time_confidence: число 0.0-1.0; "
        "needs_time_clarification: true или false; "
        "clarification_reason: короткая причина или пустая строка. "
        "Если пользователь говорит 'это не задача, просто мысль' — type=idea. "
        "Если говорит 'запиши идею', 'мысль', 'надо бы подумать' без конкретного действия — type=idea. "
        "Если говорит 'сделай задачу', 'надо проверить', 'нужно сделать' — type=task. "
        "Если говорит 'напомни', 'поставь напоминание' или явно указывает дату/время для уведомления — type=reminder. "
        "Если есть дата/время, вычисли due_date/due_time; 'вечером в десять' значит 22:00, не 10:00. "
        "Если сказано 'сегодня/завтра/послезавтра', вычисли due_date. "
        "Если есть время, но нет даты, выбери ближайшую будущую дату в указанном timezone. "
        "Если timezone не указан, используй default_timezone. "
        "Если для reminder нет точной даты или времени, либо время неоднозначное, выставь needs_time_clarification=true "
        "и time_confidence ниже 0.75. "
        "Служебные фразы маршрутизации вроде 'в личное', 'в проект личное', 'раздел личное', "
        "'поставь статус к работе', 'в колонку к работе', 'статус к работе' используй только для project_name_candidate, board_name_candidate и column_hint, "
        "но не включай их в title и body. "
        "Слова Mira, Weeek, Vosk, OpenAI, Telegram, GitHub сохраняй корректно в title/body. "
        f"Сегодня: {now.date().isoformat()}. Текущее время: {now.strftime('%H:%M')}. "
        f"default_timezone: {default_timezone}."
    )
    request_kwargs = _chat_json_request_kwargs(
        OPENAI_PARSE_MODEL,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        0.1,
    )
    try:
        response = await _async_call(_openai_client.chat.completions.create, **request_kwargs)
        raw = response.choices[0].message.content
        data = _extract_json_object(raw)
        if not data:
            return None
        capture_type = (data.get("type") or "").strip().lower()
        if capture_type not in {"idea", "task", "reminder"}:
            return None

        title = _compact_spaces(data.get("title") or "")
        body = _compact_spaces(data.get("body") or text)
        due_date = _compact_spaces(data.get("due_date") or "")
        due_time = _compact_spaces(data.get("due_time") or "")
        timezone = _compact_spaces(data.get("timezone") or "")
        confidence = _coerce_confidence(data.get("confidence"), 0.8)
        time_confidence = _coerce_confidence(data.get("time_confidence"), 0.0 if capture_type == "reminder" else 1.0)
        needs_time_clarification = bool(data.get("needs_time_clarification", False))
        clarification_reason = _compact_spaces(data.get("clarification_reason") or "")
        target = _compact_spaces(data.get("target") or "")
        project_name_candidate = _compact_spaces(data.get("project_name_candidate") or "")
        board_name_candidate = _compact_spaces(data.get("board_name_candidate") or "")
        column_hint = _compact_spaces(data.get("column_hint") or "")
        parent_task_candidate = _compact_spaces(data.get("parent_task_candidate") or "")
        transcript_clean = _compact_spaces(data.get("transcript_clean") or text)

        if not title:
            return None
        if due_time and not re.match(r"^\d{2}:\d{2}$", due_time):
            due_time = extract_capture_due_time(due_time)
        rule_due_time = extract_capture_due_time(text)
        if rule_due_time and (not due_time or _has_explicit_daypart(text)):
            due_time = rule_due_time
            time_confidence = max(time_confidence, _rule_time_confidence(text, rule_due_time))
        if due_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", due_date):
            due_date = ""
        if timezone:
            try:
                ZoneInfo(timezone)
            except ZoneInfoNotFoundError:
                timezone = extract_capture_timezone(timezone)

        return {
            "type": capture_type,
            "title": title[:120],
            "body": body or text,
            "due_date": due_date,
            "due_time": due_time,
            "timezone": timezone,
            "confidence": confidence,
            "time_confidence": time_confidence,
            "needs_time_clarification": needs_time_clarification,
            "clarification_reason": clarification_reason,
            "target": target,
            "project_name_candidate": project_name_candidate,
            "board_name_candidate": board_name_candidate,
            "column_hint": column_hint,
            "parent_task_candidate": parent_task_candidate,
            "transcript_clean": transcript_clean,
            "transcript": text,
            "source": "gpt",
        }
    except Exception as exc:
        if _is_upstream_bad_request(exc):
            logger.warning(
                "GPT capture parsing upstream bad request model=%s request_keys=%s error=%s",
                OPENAI_PARSE_MODEL,
                sorted(request_kwargs),
                exc,
            )
            return None
        logger.exception("GPT capture parsing failed")
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
RUSSIAN_HOURS = {
    "ноль": 0,
    "час": 1,
    "один": 1,
    "два": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
}
RUSSIAN_NUMBER_WORDS = {
    "ноль": 0,
    "час": 1,
    "один": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
}

CAPTURE_INTENT_WORDS = (
    "задача", "задачу", "напомни", "напоминание", "поставь задачу",
    "сделай задачу", "запиши задачу", "добавь задачу", "поставь напоминание",
)
IDEA_INTENT_PATTERNS = (
    r"\b(это\s+)?не\s+задача\b.*\b(мысль|иде[яю])\b",
    r"\b(добавь|запиши|сохрани|создай)\s+(мне\s+)?иде[яю]\b",
    r"\b(запиши|сохрани)\s+(мысль|заметку)\b",
    r"\bнадо\s+бы\s+подумать\b",
    r"\b(точнее|вернее|нет)\s*,?\s*иде[яю]\b",
    r"\b(иде[яю]|мысль)\b",
    r"^\s*идея\s*[:\-—]",
)
DATE_TIME_WORDS = (
    "сегодня", "завтра", "послезавтра", "вечером", "утром", "днем", "днём",
    "ночью", "по омску", "по москве",
)
ACTION_WORDS = (
    "купить", "сделать", "позвонить", "написать", "проверить", "отправить",
    "созвониться", "встретиться", "подготовить", "разобрать", "найти", "понять",
    "сходить", "записаться", "записать",
)
TITLE_WORD_NORMALIZATIONS = {
    "молока": "молоко",
    "хлеба": "хлеб",
    "яиц": "яйца",
    "газпрома": "Газпрома",
    "арине": "Арине",
    "айфон": "iPhone",
    "айфона": "iPhone",
    "iphone": "iPhone",
}


def _compact_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _coerce_confidence(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _has_explicit_daypart(text: str) -> bool:
    low = (text or "").lower().replace("ё", "е")
    return bool(re.search(r"\b(утра|утром|дня|днем|вечера|вечером|ночи|ночью)\b", low))


def _rule_time_confidence(text: str, due_time: str = "") -> float:
    low = (text or "").lower().replace("ё", "е")
    if re.search(r"\b\d{1,2}[:.]\d{2}\b", low):
        return 0.95
    if _has_explicit_daypart(low):
        return 0.9
    if due_time:
        return 0.6
    return 0.0


def _today_in_timezone(tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Asia/Omsk")
    return datetime.now(tz)


def _default_timezone() -> str:
    try:
        ZoneInfo(DEFAULT_TIMEZONE)
        return DEFAULT_TIMEZONE
    except ZoneInfoNotFoundError:
        return "Asia/Omsk"


def effective_user_timezone(user_id: int) -> str:
    tz = db.get_user_timezone(user_id)
    if not tz or tz == "UTC":
        return _default_timezone()
    return tz


def _strip_leading_capture_noise(text: str) -> str:
    noise = r"(?:привет|слушай|смотри|так|короче|ну|типа|бот|чат|ботик|ассистент|мне)"
    text = _compact_spaces(text).strip(" .,!?:;")
    for _ in range(12):
        cleaned = re.sub(rf"^{noise}\b\s*,?\s*", "", text, flags=re.IGNORECASE)
        cleaned = _compact_spaces(cleaned).strip(" .,!?:;")
        if cleaned == text:
            break
        text = cleaned
    return text


def normalize_capture_command_text(text: str) -> str:
    text = _strip_leading_capture_noise(text)
    text = re.sub(r"\b(пожалуйста|плиз|ну|типа)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(точнее|вернее)\s*,?\s*(задачу|иде[яю])\b", r" \2 ", text, flags=re.IGNORECASE)
    return _compact_spaces(text).strip(" .,!?:;")


def _strip_capture_noise(text: str) -> str:
    text = _compact_spaces(text)
    text = _strip_leading_capture_noise(text)
    text = re.sub(r"^(записать|запиши)\s+(на|к|в)\s*,?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(что\s+но|что\s+ну|что)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(мне\s+нужно|нужно|надо)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(на|к|в)?\s*\d{1,2}([:.]\d{2})?\s*(час(?:ов|а)?|ч)?\s*(утра|дня|вечера|ночи)?\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    hour_words = "|".join(RUSSIAN_HOURS)
    text = re.sub(
        rf"\b(на|к|в)?\s*({hour_words})\s*(час(?:ов|а)?|ч)?\s*(утра|дня|вечера|ночи)?\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    number_word = "|".join(sorted(RUSSIAN_NUMBER_WORDS, key=len, reverse=True))
    text = re.sub(
        rf"\b(на|к|в)\s+(?:{number_word})(?:\s+(?:{number_word})){{0,3}}\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(сегодня|завтра|послезавтра|вечером|утром|днем|днём|ночью|по\s+\w+)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b(задачу|задача|напоминание|иде[яю])\b", " ", text, flags=re.IGNORECASE)
    return _compact_spaces(text).strip(" .,!?:;")


def _parse_ru_number_words(words: list[str]) -> int | None:
    if not words:
        return None
    total = 0
    for word in words:
        value = RUSSIAN_NUMBER_WORDS.get(word)
        if value is None:
            return None
        total += value
    return total


def _infer_daypart(text: str) -> str:
    low = (text or "").lower().replace("ё", "е")
    if re.search(r"\b(вечера|вечером)\b", low):
        return "вечера"
    if re.search(r"\b(дня|днем)\b", low):
        return "дня"
    if re.search(r"\b(утра|утром)\b", low):
        return "утра"
    if re.search(r"\b(ночи|ночью)\b", low):
        return "ночи"
    return ""


def _apply_daypart_to_hour(hour: int, part: str) -> int:
    if part == "вечера" and hour < 12:
        return hour + 12
    if part == "дня" and 1 <= hour < 12:
        return hour + 12
    if part == "ночи" and hour == 12:
        return 0
    return hour


def _title_from_action_phrase(text: str) -> str:
    low = text.lower().replace("ё", "е")
    matches = []
    for word in ACTION_WORDS:
        m = re.search(rf"\b{word}\b", low)
        if m:
            matches.append((m.start(), m.end()))
    if not matches:
        return text
    start, _ = min(matches)
    return text[start:]


def _strip_trailing_idea_commands(text: str) -> str:
    text = _compact_spaces(text)
    text = re.sub(
        r"\b(запиши|сохрани|добавь|создай)\s+(эту\s+|такую\s+)?иде[яю]\b\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bпросто\s+(запиши|сохрани)\b\.?", "", text, flags=re.IGNORECASE)
    return _compact_spaces(text).strip(" .,!?:;")


def _extract_idea_subject(text: str) -> str:
    text = _strip_trailing_idea_commands(normalize_capture_command_text(text))
    patterns = (
        r"\b(?:насч[её]т|на\s+тему|про|о)\s+(?:того\s*,?\s*)?что\s+(?:мне\s+)?(?:нужно|надо)\s+(.+)",
        r"\b(?:о\s+том\s+)?что\s+(?:мне\s+)?(?:нужно|надо)\s+(.+)",
        r"\bиде[яю]\s+(?:насч[её]т|на\s+тему|про|о)\s+(?:того\s*,?\s*)?что\s+(?:мне\s+)?(?:нужно|надо)\s+(.+)",
        r"\bиде[яю]\s+(?:на\s+)?(.+)",
    )
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
        if matches:
            return _strip_trailing_idea_commands(matches[-1].group(1))
    return text


def _idea_title_from_subject(subject: str) -> str:
    subject = _strip_capture_noise(_strip_trailing_idea_commands(subject))
    subject = _normalize_title_words(subject)
    subject = _compact_spaces(subject).strip(" .,!?:;")
    if not subject:
        return ""

    m = re.match(r"^купить\s+(.+)$", subject, flags=re.IGNORECASE)
    if m:
        item = _compact_spaces(m.group(1)).strip(" .,!?:;")
        item = _normalize_title_words(item)
        return _compact_spaces(f"Идея покупки {item}").strip(" .,!?:;")

    return subject


def _normalize_title_words(title: str) -> str:
    parts = re.findall(r"\w+|[^\w\s]", title, flags=re.UNICODE)
    normalized = []
    for part in parts:
        low = part.lower()
        normalized.append(TITLE_WORD_NORMALIZATIONS.get(low, part))
    title = " ".join(normalized)
    title = re.sub(r"\s+([,.;:!?])", r"\1", title)
    title = re.sub(r"\bмолоко\s+хлеб\s+и\s+яйца\b", "молоко, хлеб и яйца", title, flags=re.IGNORECASE)
    title = re.sub(r"\bмолоко\s+хлеб\b", "молоко и хлеб", title, flags=re.IGNORECASE)
    return _compact_spaces(title)


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
        hour = _apply_daypart_to_hour(hour, part)
        return f"{hour:02d}:00"

    hour_words = "|".join(RUSSIAN_HOURS)
    m = re.search(
        rf"\b(?:к|на|в)?\s*({hour_words})\s*(?:час(?:ов|а)?|ч)?\s*(утра|дня|вечера|ночи)\b",
        low,
    )
    if m:
        hour = RUSSIAN_HOURS[m.group(1)]
        part = m.group(2)
        hour = _apply_daypart_to_hour(hour, part)
        return f"{hour:02d}:00"

    number_word = "|".join(sorted(RUSSIAN_NUMBER_WORDS, key=len, reverse=True))
    for m in re.finditer(
        rf"\b(?:к|на|в)\s+((?:{number_word})(?:\s+(?:{number_word})){{0,3}})(?:\s+(утра|дня|вечера|ночи))?\b",
        low,
    ):
        words = m.group(1).split()
        part = m.group(2) or _infer_daypart(low)
        for split_at in range(len(words), 0, -1):
            hour = _parse_ru_number_words(words[:split_at])
            minute = _parse_ru_number_words(words[split_at:]) if split_at < len(words) else 0
            if hour is None or minute is None:
                continue
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                hour = _apply_daypart_to_hour(hour, part)
                return f"{hour:02d}:{minute:02d}"

    m = re.search(r"\b(?:к|на|в)\s+([01]?\d|2[0-3])\s*(?:час(?:ов|а)?|ч)?\b", low)
    if m:
        hour = int(m.group(1))
        hour = _apply_daypart_to_hour(hour, _infer_daypart(low))
        return f"{hour:02d}:00"

    return ""


def extract_capture_due_date(text: str, timezone: str) -> str:
    low = (text or "").lower().replace("ё", "е")
    now = _today_in_timezone(timezone or _default_timezone())
    if "послезавтра" in low:
        return (now + timedelta(days=2)).date().isoformat()
    if "завтра" in low:
        return (now + timedelta(days=1)).date().isoformat()
    if "сегодня" in low:
        return now.date().isoformat()
    return ""


def resolve_capture_due_date(text: str, timezone: str, due_time: str) -> str:
    due_date = extract_capture_due_date(text, timezone)
    if due_date or not due_time:
        return due_date

    now = _today_in_timezone(timezone or _default_timezone())
    try:
        hour, minute = [int(x) for x in due_time.split(":", 1)]
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return ""
    if candidate + timedelta(minutes=1) <= now:
        candidate = candidate + timedelta(days=1)
    return candidate.date().isoformat()


def classify_capture_text(text: str) -> dict:
    clean = _compact_spaces(text)
    normalized = normalize_capture_command_text(clean)
    low = normalized.lower().replace("ё", "е")
    route = detect_top_level_route(normalized)
    due_time = extract_capture_due_time(normalized)
    timezone = extract_capture_timezone(normalized)
    has_idea_intent = any(re.search(pattern, low) for pattern in IDEA_INTENT_PATTERNS)

    has_intent = any(word in low for word in CAPTURE_INTENT_WORDS)
    has_date_time = due_time or any(word in low for word in DATE_TIME_WORDS) or bool(re.search(r"\b[кнв]\s+\d", low))
    has_action = any(re.search(rf"\b{word}\b", low) for word in ACTION_WORDS)

    if re.search(r"\b(это\s+)?не\s+задача\b.*\b(мысль|иде[яю])\b", low):
        capture_type = "idea"
    elif has_idea_intent and not has_date_time:
        capture_type = "idea"
    elif has_intent or has_date_time:
        capture_type = "reminder" if "напом" in low else "task"
    elif has_action:
        capture_type = "task"
    else:
        capture_type = "idea"

    time_confidence = _rule_time_confidence(normalized, due_time)
    needs_time_clarification = (
        capture_type == "reminder"
        and (not due_time or time_confidence < 0.75)
    )

    return {
        "type": capture_type,
        "title": generate_capture_title(normalized, capture_type),
        "body": clean,
        "due_date": "",
        "due_time": due_time,
        "due_date_hint": "послезавтра" if "послезавтра" in low else "завтра" if "завтра" in low else "сегодня" if "сегодня" in low else "",
        "timezone": timezone,
        "confidence": 0.75,
        "time_confidence": time_confidence,
        "needs_time_clarification": needs_time_clarification,
        "clarification_reason": "не хватает точного времени" if needs_time_clarification else "",
        "target": route.get("target") or "",
        "project_name_candidate": (route.get("project") or {}).get("project_name", ""),
        "board_name_candidate": route.get("board_name_candidate", ""),
        "column_hint": route.get("column_hint", ""),
        "parent_task_candidate": route.get("parent_task_candidate", ""),
        "transcript_clean": clean,
        "transcript": clean,
        "source": "rules",
    }


async def classify_capture(update: Update, text: str, forced_type: str | None = None) -> dict:
    user_id = update.effective_user.id
    default_timezone = effective_user_timezone(user_id)
    parsed = await parse_capture_with_gpt(text, default_timezone)
    if parsed:
        capture = parsed
    else:
        capture = classify_capture_text(text)

    if forced_type:
        capture["type"] = forced_type

    timezone = capture.get("timezone") or default_timezone
    due_time = capture.get("due_time", "")
    due_date = capture.get("due_date", "")
    if not due_time:
        rule_due_time = extract_capture_due_time(text)
        if rule_due_time:
            due_time = rule_due_time
            capture["time_confidence"] = max(
                _coerce_confidence(capture.get("time_confidence"), 0.0),
                _rule_time_confidence(text, due_time),
            )
    if not due_date:
        due_date = extract_capture_due_date(capture.get("body") or text, timezone)
    if due_time and not due_date:
        due_date = resolve_capture_due_date(capture.get("body") or text, timezone, due_time)

    capture["timezone"] = timezone
    capture["due_date"] = due_date
    capture["due_time"] = due_time
    capture["body"] = capture.get("body") or text
    capture["title"] = _compact_spaces(capture.get("title") or generate_capture_title(text, capture["type"]))
    capture["target"] = capture.get("target") or ""
    capture["project_name_candidate"] = capture.get("project_name_candidate") or ""
    capture["board_name_candidate"] = capture.get("board_name_candidate") or extract_board_candidate(text)
    capture["column_hint"] = capture.get("column_hint") or normalize_weeek_column_hint(text)
    capture["parent_task_candidate"] = capture.get("parent_task_candidate") or extract_parent_task_candidate(text)
    capture["transcript_clean"] = _compact_spaces(capture.get("transcript_clean") or text)
    capture["transcript"] = _compact_spaces(capture.get("transcript") or text)
    capture["confidence"] = _coerce_confidence(capture.get("confidence"), 0.75)
    capture["time_confidence"] = _coerce_confidence(capture.get("time_confidence"), _rule_time_confidence(text, due_time))
    capture["needs_time_clarification"] = bool(capture.get("needs_time_clarification", False))
    if capture["type"] == "reminder":
        missing = []
        if not due_date:
            missing.append("даты")
        if not due_time:
            missing.append("времени")
        if missing:
            capture["needs_time_clarification"] = True
            capture["clarification_reason"] = "не хватает " + " и ".join(missing)
        elif capture["time_confidence"] < 0.75:
            capture["needs_time_clarification"] = True
            capture["clarification_reason"] = capture.get("clarification_reason") or "время распознано неоднозначно"
    else:
        capture["needs_time_clarification"] = False
    return capture


def generate_capture_title(text: str, capture_type: str) -> str:
    title = normalize_capture_command_text(text)
    if capture_type == "idea":
        idea_title = _idea_title_from_subject(_extract_idea_subject(text))
        if idea_title:
            return idea_title[:1].upper() + idea_title[1:]
    m = re.search(r"\b(?:о\s+том\s+)?что\s+(?:мне\s+)?(?:нужно|надо)\s+(.+)", title, flags=re.IGNORECASE)
    if m:
        title = m.group(1)
    if capture_type == "idea":
        title = re.sub(
            r"^.*\bиде[яю]\b\s*(о\s+том\s*)?(что\s*)?",
            "",
            title,
            flags=re.IGNORECASE,
        )
    title = _title_from_action_phrase(title)
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
        r"^(записать|запиши)\s+(мне\s+)?(задачу|напоминание)\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^(сделай|создай|поставь|добавь|запиши|сохрани)\s+(на\s+)?(сегодня|завтра|послезавтра)?\s*(задачу|напоминание)\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^(сделай|создай|поставь|добавь|запиши|сохрани)\s+(на\s+)?(сегодня|завтра|послезавтра)?\s*(?:в\s+\d{1,2}([:.]\d{2})?\s*(час(?:ов|а)?|ч)?\s*(утра|дня|вечера|ночи)?\s*)?(задачу|напоминание)\s+",
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
    title = re.sub(
        r"^(сделай|создай|поставь|добавь|запиши|сохрани)\s+иде[яю]\s+(о\s+том\s+)?(что\s+)?",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"^напомни(ть)?\s+(мне\s+)?", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^(нужно|надо|что нужно)\s+", "", title, flags=re.IGNORECASE)
    title = _strip_capture_noise(title)
    title = re.sub(
        r"\b(на|к|в)\s+\d{1,2}([:.]\d{2})?\s*(час(?:ов|а)?|ч)?\s*(утра|дня|вечера|ночи)?\b",
        "",
        title,
        flags=re.IGNORECASE,
    )
    hour_words = "|".join(RUSSIAN_HOURS)
    title = re.sub(
        rf"\b(на|к|в)\s+({hour_words})\s*(час(?:ов|а)?|ч)?\s*(утра|дня|вечера|ночи)?\b",
        "",
        title,
        flags=re.IGNORECASE,
    )
    number_word = "|".join(sorted(RUSSIAN_NUMBER_WORDS, key=len, reverse=True))
    title = re.sub(
        rf"\b(на|к|в)\s+(?:{number_word})(?:\s+(?:{number_word})){{0,3}}\b",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\b(задачу|задача|напоминание|иде[яю])\b",
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
    title = re.sub(r"\b(о\s+том\s+)?что\s+(мне\s+)?нужно\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\bмне\s+нужно\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^(записать|запиши)\s+(на|к|в)\s*,?\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(на|к|в)\s*$", "", title, flags=re.IGNORECASE)
    title = _normalize_title_words(title)
    title = _compact_spaces(title).strip(" .,!?:;")

    if capture_type == "idea":
        title = re.sub(r"\bс\s+360-турами\b", "", title, flags=re.IGNORECASE)
        m = re.search(r"\bоффер\s+для\s+застройщиков\b", title, flags=re.IGNORECASE)
        if m:
            title = m.group(0)

    title = re.sub(r"\bпосле\s+запуска\b", "", title, flags=re.IGNORECASE)
    title = _compact_spaces(title).strip(" .,!?:;")

    words = title.split()
    if len(words) > 6 and " и " not in f" {title.lower()} ":
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

    tz = effective_user_timezone(user_id)
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
    if voice_available():
        voice_status = "включён — сначала OpenAI STT, при сбое Vosk fallback"
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
    if voice_available():
        text = (
            "🎤 <b>Голосовой ввод включён.</b>\n\n"
            f"🧠 Парсинг смысла: <b>{html.escape(OPENAI_PARSE_MODEL)}</b>.\n\n"
            "Mira сначала пробует OpenAI STT через официальный API, а при сбое переключается на локальный Vosk. "
            "Качество ниже Whisper, но для коротких фраз вполне приемлемо.\n\n"
            "Просто запиши голосовое прямо в чате:\n"
            "• Вне диалогов — бот поймёт, это идея, задача или напоминание.\n"
            f"• В режиме «{BTN_ADD_TASK}» — текст голоса сохранится как задача.\n"
            f"• На шагах «{BTN_ADD}» — голос подставится в текущий шаг.\n"
        )
    else:
        text = (
            "🎤 <b>Голосовой ввод выключен.</b>\n\n"
            "Для голосового ввода нужен OpenAI API key, а локальный Vosk и ffmpeg остаются fallback-вариантом:\n\n"
            "• Установи зависимости из <code>requirements.txt</code>.\n"
            "• На сервере нужен ffmpeg: <code>sudo apt install ffmpeg</code>.\n"
            "• Vosk сам скачает русскую модель (~45 МБ) при первом голосовом.\n\n"
            "После установки перезапусти сервис бота."
        )
    text += "\n\n" + "\n".join(_voice_runtime_status_lines())
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


async def _build_weeek_daily_digest() -> str:
    if not weeek_available():
        return "Интеграция с Weeek не настроена."

    client = get_weeek_client()
    projects = await client.list_projects()
    if not projects:
        return "В Weeek пока нет доступных проектов."

    blocks: list[str] = []
    for project in projects:
        try:
            boards = await client.list_boards(project.id)
            tasks = await client.list_tasks(project_id=project.id)
        except WeeekApiError as exc:
            _log_event("weeek_daily_digest_project_failed", project_id=project.id, error=str(exc))
            continue

        columns_by_id: dict[str, dict] = {}
        for board in boards:
            try:
                columns = await client.list_columns(board.id)
            except WeeekApiError:
                continue
            for column in columns:
                columns_by_id[str(column.id)] = {"id": str(column.id), "name": column.name, "raw": column.raw}

        mapped_tasks = [{"id": option.id, "name": option.name, "raw": option.raw} for option in tasks]
        blocks.append(_format_weeek_tasks_overview(project.name, mapped_tasks, columns_by_id))

    if not blocks:
        return "Не удалось собрать ежедневную сводку по Weeek."

    return "👋 Напоминаю про задачи в Weeek:\n\n" + "\n\n".join(blocks)


async def test_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await _build_weeek_daily_digest()
    except WeeekApiError as exc:
        _log_event("weeek_daily_digest_manual_failed", error=str(exc))
        await update.message.reply_text(
            "Не удалось получить ежедневную сводку из Weeek. Попробуй ещё раз чуть позже.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await update.message.reply_html(text, reply_markup=main_menu_keyboard())


async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    try:
        text = await _build_weeek_daily_digest()
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Не удалось отправить напоминание пользователю %s", user_id)


def _user_tzinfo(user_id: int):
    tz_name = effective_user_timezone(user_id)
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("Неизвестный часовой пояс %s у пользователя %s, fallback на Asia/Omsk", tz_name, user_id)
        return ZoneInfo("Asia/Omsk")


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


async def check_task_reminders(context: ContextTypes.DEFAULT_TYPE):
    for task in db.get_due_tasks():
        task_id, user_id, title, _done, body, due_date, due_time, timezone = task
        timezone = timezone or effective_user_timezone(user_id)
        try:
            tz = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            timezone = _default_timezone()
            tz = ZoneInfo(timezone)

        try:
            due_at = datetime.fromisoformat(f"{due_date}T{due_time}:00").replace(tzinfo=tz)
        except ValueError:
            logger.warning("Некорректный срок задачи id=%s: %s %s", task_id, due_date, due_time)
            continue

        if datetime.now(tz) < due_at:
            continue

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=task_reminder_message(title, body, due_date, due_time, timezone),
                parse_mode=ParseMode.HTML,
                reply_markup=task_keyboard(task_id, False),
            )
            db.mark_task_notified(task_id, user_id)
        except Exception:
            logger.exception("Не удалось отправить напоминание по задаче %s пользователю %s", task_id, user_id)


async def check_task_reminders_v2(context: ContextTypes.DEFAULT_TYPE):
    for task in db.get_due_tasks():
        (
            task_id,
            user_id,
            title,
            _done,
            body,
            due_date,
            due_time,
            timezone,
            reminder_day_sent_at,
            reminder_30_sent_at,
            reminder_15_sent_at,
        ) = task
        timezone = timezone or effective_user_timezone(user_id)
        try:
            tz = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            timezone = _default_timezone()
            tz = ZoneInfo(timezone)

        try:
            due_day = date.fromisoformat(due_date)
        except ValueError:
            logger.warning("Некорректная дата задачи id=%s: %s", task_id, due_date)
            continue

        now = datetime.now(tz)
        due_reminders = []
        if due_time:
            try:
                due_at = datetime.fromisoformat(f"{due_date}T{due_time}:00").replace(tzinfo=tz)
            except ValueError:
                logger.warning("Некорректный срок задачи id=%s: %s %s", task_id, due_date, due_time)
                continue
            if not reminder_30_sent_at and now >= due_at - timedelta(minutes=30):
                due_reminders.append("pre30")
            if not reminder_15_sent_at and now >= due_at - timedelta(minutes=15):
                due_reminders.append("pre15")
        else:
            midday_at = datetime.combine(due_day, time(hour=12, minute=0), tzinfo=tz)
            if not reminder_day_sent_at and now >= midday_at:
                due_reminders.append("day")

        for reminder_kind in due_reminders:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=task_reminder_message_for_kind(title, body, due_date, due_time, timezone, reminder_kind),
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_menu_keyboard(),
                )
                db.mark_task_reminder_sent(task_id, user_id, reminder_kind)
            except Exception:
                logger.exception("Не удалось отправить напоминание %s по задаче %s пользователю %s", reminder_kind, task_id, user_id)


def schedule_task_reminder_checker(application: Application):
    for job in list(application.job_queue.jobs()):
        if job.name == "check_task_reminders":
            job.schedule_removal()
    application.job_queue.run_repeating(
        check_task_reminders_v2,
        interval=60,
        first=10,
        name="check_task_reminders",
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
    text = await _transcribe_or_warn(update, context, failure_reply_markup=main_menu_keyboard())
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
    tz = effective_user_timezone(user_id)
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
    tz = effective_user_timezone(query.from_user.id)
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

def _task_row_parts(task) -> tuple[int, str, bool, str, str, str, str]:
    if len(task) >= 7:
        task_id, title, done, body, due_date, due_time, timezone = task[:7]
    else:
        task_id, title, done = task[:3]
        body, due_date, due_time, timezone = title, "", "", ""
    return task_id, title, bool(done), body, due_date, due_time, timezone


def task_message(title: str, done: bool, body: str = "", due_date: str = "", due_time: str = "", timezone: str = "") -> str:
    title = title or body or "Без названия"
    meta = ""
    if due_date or due_time:
        date_part = f"{html.escape(due_date)}" if due_date else ""
        time_part = f" {html.escape(due_time)}" if due_time else ""
        meta = f"\n⏰ {(date_part + time_part).strip()}"
        if timezone:
            meta += f" {html.escape(format_timezone_label(timezone))}"
    if done:
        return f"✔️ <s>{html.escape(title)}</s>{meta}"
    return f"⬜ {html.escape(title)}{meta}"


def task_reminder_message(title: str, body: str, due_date: str, due_time: str, timezone: str) -> str:
    return (
        "🔔 <b>Напоминание по задаче</b>\n\n"
        f"{task_message(title, False, body, due_date, due_time, timezone)}"
    )


def task_reminder_message_for_kind(
    title: str,
    body: str,
    due_date: str,
    due_time: str,
    timezone: str,
    reminder_kind: str,
) -> str:
    if reminder_kind == "day":
        lead_text = "Сегодня в 12:00 напоминаю про задачу на этот день."
    elif reminder_kind == "pre30":
        lead_text = "Напоминаю за 30 минут до задачи."
    elif reminder_kind == "pre15":
        lead_text = "Напоминаю за 15 минут до задачи."
    else:
        lead_text = "Напоминаю по задаче."
    return (
        "🔔 <b>Напоминание по задаче</b>\n\n"
        f"{html.escape(lead_text)}\n\n"
        f"{task_message(title, False, body, due_date, due_time, timezone)}"
    )


def format_timezone_label(timezone: str) -> str:
    labels = {
        "Asia/Omsk": "Asia/Omsk (UTC+6)",
        "Europe/Moscow": "Europe/Moscow (UTC+3)",
        "UTC": "UTC",
    }
    return labels.get(timezone, timezone)


def gpt_description_message(capture: dict) -> str:
    if capture.get("source") != "gpt":
        return ""
    body = _compact_spaces(capture.get("body") or "")
    title = _compact_spaces(capture.get("title") or "")
    if not body or body.casefold() == title.casefold():
        return ""
    if len(body) > 600:
        body = body[:600].rstrip() + "..."
    return f"\n\n<b>Описание:</b> {html.escape(body)}"


def transcription_message(transcript: str, speech_engine: str = "") -> str:
    transcript = _compact_spaces(transcript or "")
    if not transcript:
        return ""
    label = "Транскрипция"
    if speech_engine == "vosk" and _openai_stt_available():
        label = "Транскрипция (fallback: Vosk)"
    elif speech_engine:
        label = f"Транскрипция ({speech_engine})"
    if len(transcript) > 800:
        transcript = transcript[:800].rstrip() + "..."
    return f"\n\n<b>{html.escape(label)}:</b> {html.escape(transcript)}"


def transcription_error_text(error: str) -> str:
    if error == "voice_unavailable":
        return (
            "Сейчас голосовой ввод недоступен: не настроены ни OpenAI STT, ни локальный Vosk. "
            "Текстовые идеи, задачи и напоминания продолжают работать."
        )
    if error == "openai_stt_rate_limited":
        return (
            "OpenAI STT сейчас упёрся в rate limit, а локальный fallback недоступен. "
            "Попробуй чуть позже или напиши задачу текстом."
        )
    if error == "openai_stt_auth":
        return (
            "OpenAI STT сейчас не прошёл авторизацию, а локальный fallback недоступен. "
            "Проверь OPENAI_API_KEY или переключись на текст."
        )
    if error in {"openai_stt_failed", "openai_stt_empty"}:
        return (
            "OpenAI STT сейчас не смог разобрать голос, а локальный fallback недоступен. "
            "Попробуй ещё раз или отправь текст."
        )
    if error == "vosk_unavailable":
        return (
            "Сейчас голос не распознаётся: на сервере не установлен Vosk. "
            "Текстовые идеи, задачи и напоминания работают."
        )
    if error == "ffmpeg_missing":
        return (
            "Сейчас голос не распознаётся: на сервере не установлен ffmpeg. "
            "Поставь ffmpeg и перезапусти бота."
        )
    if error == "vosk_model_unavailable":
        return (
            "Сейчас голос не распознаётся: Vosk-модель не загрузилась. "
            "Проверь доступ сервера к скачиванию модели или задай VOSK_MODEL_PATH."
        )
    if error == "vosk_empty":
        return (
            "Vosk не смог разобрать голосовое. Попробуй сказать короче и чётче "
            "или введи текстом."
        )
    if error == "transcription_timeout":
        return (
            "Голосовое не удалось распознать за разумное время. "
            "Текстовые идеи, задачи и напоминания сейчас работают."
        )
    return "Не удалось распознать голосовое. Попробуй текстом, пожалуйста."


def task_keyboard(task_id: int, done: bool) -> InlineKeyboardMarkup:
    toggle_label = "↩️ Не сделано" if done else "✅ Сделано"
    toggle_action = "task_undone" if done else "task_done"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(toggle_label, callback_data=f"{toggle_action}:{task_id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"task_del:{task_id}"),
        ],
    ])


def post_task_save_keyboard(task_id: int, include_add: bool = True) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("✅ Сделано", callback_data=f"task_done:{task_id}")]]
    if include_add:
        rows.append([
            InlineKeyboardButton("➕ Ещё задача", callback_data="menu:add_task"),
            InlineKeyboardButton("📋 Все задачи", callback_data="menu:list_tasks"),
        ])
    else:
        rows.append([InlineKeyboardButton("📋 Все задачи", callback_data="menu:list_tasks")])
    rows.append([InlineKeyboardButton("🗑 Удалить эту", callback_data=f"task_del:{task_id}")])
    return InlineKeyboardMarkup(rows)


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
        tid, title, done, body, due_date, due_time, timezone = _task_row_parts(task)
        await update.message.reply_html(
            task_message(title, done, body, due_date, due_time, timezone),
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


async def _handle_task_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, transcript: str = "", speech_engine: str = ""):
    text = (text or "").strip()
    if not text:
        await update.message.reply_text(
            "Пустая задача — так нельзя. Попробуй ещё раз.",
            reply_markup=cancel_keyboard(),
        )
        return TASK_TEXT
    if len(text) > 500:
        text = text[:500]

    user_id = update.effective_user.id
    db.upsert_user(user_id)
    capture = await classify_capture(update, text, forced_type="task")
    capture["speech_engine"] = speech_engine
    capture["transcript"] = transcript or text
    capture["transcript_clean"] = capture.get("transcript_clean") or transcript or text
    title = capture["title"]
    timezone = capture.get("timezone") or effective_user_timezone(user_id)
    due_date = capture.get("due_date", "")
    task_id = db.add_task(
        user_id,
        title,
        title=title,
        body=capture.get("body") or text,
        due_date=due_date,
        due_time=capture.get("due_time", ""),
        timezone=timezone,
    )

    await update.message.reply_html(
        f"✅ Задача создана\n\n{task_message(title, False, capture.get('body') or text, due_date, capture.get('due_time', ''), timezone)}"
        f"{gpt_description_message(capture)}",
        reply_markup=main_menu_keyboard(),
    )
    extra_details = transcription_message(transcript or text, speech_engine).strip()
    if extra_details:
        await update.message.reply_html(extra_details, reply_markup=main_menu_keyboard())
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=post_task_save_keyboard(task_id),
    )
    return ConversationHandler.END


async def task_add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_task_text(update, context, update.message.text)


async def task_add_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context)
    if text is None:
        return TASK_TEXT
    speech_engine = context.user_data.pop("_last_speech_engine", "")
    return await _handle_task_text(update, context, text, transcript=text, speech_engine=speech_engine)


# --- Голос вне диалога: классифицируем в идею / задачу / напоминание ---

def _weeek_options_keyboard(options: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(option["name"][:60], callback_data=f"{prefix}:{option['id']}")]
        for option in options[:20]
    ]
    rows.append([InlineKeyboardButton("✖️ Отмена", callback_data="weeek_cancel")])
    return InlineKeyboardMarkup(rows)


def _get_weeek_draft(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("weeek_draft", {})


def _clear_weeek_draft(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("weeek_draft", None)


def _sort_weeek_columns(columns: list[dict], hint: str) -> list[dict]:
    if not hint:
        return columns

    def score(column: dict) -> tuple[int, str]:
        name = (column.get("name") or "").lower().replace("ё", "е")
        raw = column.get("raw") or {}
        slug = str(raw.get("slug") or raw.get("key") or raw.get("code") or raw.get("status") or "").lower()
        if hint in name or hint == slug:
            return (0, name)
        if hint == "to_work" and any(token in name or token == slug for token in ("к работе", "to work", "todo", "backlog", "queue")):
            return (0, name)
        if hint == "in_work" and any(token in name or token == slug for token in ("в работе", "in work", "progress", "doing")):
            return (0, name)
        return (1, name)

    return sorted(columns, key=score)


async def _send_weeek_project_picker(message, context: ContextTypes.DEFAULT_TYPE):
    client = get_weeek_client()
    try:
        projects = await client.list_projects()
    except WeeekApiError as exc:
        return await _fail_weeek_flow(
            message,
            context,
            "Не удалось связаться с Weeek и получить список проектов. Попробуй ещё раз чуть позже.",
            "weeek_projects_load_failed",
            exc,
        )
    if not projects:
        await message.reply_text(
            "Не смог найти проекты в Weeek. Проверь WEEEK_API_TOKEN, WEEEK_API_BASE_URL и доступы у токена.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        return ConversationHandler.END

    draft = _get_weeek_draft(context)
    draft["projects"] = [{"id": option.id, "name": option.name, "raw": option.raw} for option in projects]
    await message.reply_text(
        "Выбери проект для задачи в Weeek:",
        reply_markup=_weeek_options_keyboard(draft["projects"], "weeek_project"),
    )
    return WEEEK_PROJECT


async def _send_weeek_board_picker(message, context: ContextTypes.DEFAULT_TYPE):
    draft = _get_weeek_draft(context)
    project_id = draft.get("project_id")
    client = get_weeek_client()
    try:
        boards = await client.list_boards(project_id)
    except WeeekApiError as exc:
        return await _fail_weeek_flow(
            message,
            context,
            "Не удалось связаться с Weeek и получить список досок. Попробуй ещё раз чуть позже.",
            "weeek_boards_load_failed",
            exc,
        )
    if not boards:
        await message.reply_text(
            "Для выбранного проекта не нашёл доски Weeek. Проверь структуру проекта в Weeek.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        return ConversationHandler.END

    draft["boards"] = [{"id": option.id, "name": option.name, "raw": option.raw} for option in boards]
    await message.reply_text(
        "Теперь выбери доску:",
        reply_markup=_weeek_options_keyboard(draft["boards"], "weeek_board"),
    )
    return WEEEK_BOARD


async def _send_weeek_column_picker(message, context: ContextTypes.DEFAULT_TYPE):
    draft = _get_weeek_draft(context)
    board_id = draft.get("board_id")
    client = get_weeek_client()
    try:
        columns = await client.list_columns(board_id)
    except WeeekApiError as exc:
        return await _fail_weeek_flow(
            message,
            context,
            "Не удалось связаться с Weeek и получить список колонок. Попробуй ещё раз чуть позже.",
            "weeek_columns_load_failed",
            exc,
        )
    if not columns:
        await message.reply_text(
            "Не смог получить колонки этой доски Weeek. Возможно, API вернул непривычный формат.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        return ConversationHandler.END

    mapped = [{"id": option.id, "name": option.name, "raw": option.raw} for option in columns]
    draft["columns"] = _sort_weeek_columns(mapped, draft.get("column_hint", ""))
    hint = draft.get("column_hint", "")
    if hint:
        matched = [column for column in draft["columns"] if _column_matches_hint(column.get("name", ""), hint)]
        if len(matched) == 1:
            draft["column_id"] = matched[0]["id"]
            draft["column_name"] = matched[0]["name"]
            return await _show_weeek_preview(message, context)
    await message.reply_text(
        "И последним шагом выбери колонку:",
        reply_markup=_weeek_options_keyboard(draft["columns"], "weeek_column"),
    )
    return WEEEK_COLUMN


async def _send_weeek_parent_picker(message, context: ContextTypes.DEFAULT_TYPE):
    draft = _get_weeek_draft(context)
    client = get_weeek_client()
    try:
        tasks = await client.list_tasks(draft.get("project_id", ""), draft.get("board_id", ""))
    except WeeekApiError as exc:
        return await _fail_weeek_flow(
            message,
            context,
            "Не удалось связаться с Weeek и получить список задач. Попробуй ещё раз чуть позже.",
            "weeek_parent_tasks_load_failed",
            exc,
        )
    if not tasks:
        await message.reply_text(
            "Не смог получить список задач в этой доске Weeek.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        return ConversationHandler.END

    mapped = [{"id": option.id, "name": option.name, "raw": option.raw} for option in tasks]
    draft["parent_tasks"] = mapped
    candidate = draft.get("parent_task_candidate") or ""
    parent_task, match_source = _match_parent_task(mapped, candidate)
    if parent_task:
        draft["parent_task_id"] = parent_task["id"]
        draft["parent_task_name"] = parent_task["name"]
        draft["parent_task_match_source"] = match_source
        logger.info("weeek_subtask parent_task_match_source=%s parent_task=%r", match_source, parent_task["name"])
        return await _send_weeek_column_picker(message, context)

    if candidate:
        await message.reply_text(
            "Не смог однозначно найти родительскую задачу. Выбери её вручную:",
            reply_markup=_weeek_options_keyboard(mapped, "weeek_parent"),
        )
    else:
        await message.reply_text(
            "Выбери родительскую задачу для подзадачи:",
            reply_markup=_weeek_options_keyboard(mapped, "weeek_parent"),
        )
    return WEEEK_PARENT


async def _show_weeek_preview(message, context: ContextTypes.DEFAULT_TYPE):
    draft = _get_weeek_draft(context)
    await message.reply_html(
        weeek_preview_message_ex(draft),
        reply_markup=weeek_preview_keyboard_ex(draft),
    )
    return WEEEK_PREVIEW


async def weeek_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not weeek_available():
        await update.message.reply_text(
            "Weeek пока не настроен. Добавь WEEEK_API_TOKEN и при необходимости WEEEK_API_BASE_URL / WEEEK_DEFAULT_WORKSPACE_ID, потом перезапусти бота.",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    _clear_weeek_draft(context)
    voice_hint = " или наговори голосом 🎤" if voice_available() else ""
    await update.message.reply_text(
        "🧩 Давай создадим задачу в Weeek.\n\n"
        "Пришли одним сообщением, что нужно сделать,"
        f"{voice_hint}.\n\n"
        "Можно писать и так: «бот, добавь задачу в ВИК: ...»",
        reply_markup=cancel_keyboard(),
    )
    return WEEEK_CAPTURE


async def _start_weeek_capture(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
    if not weeek_available():
        await update.message.reply_text(
            "Поймал запрос на задачу в Weeek, но интеграция Weeek сейчас не настроена в запущенном сервисе. "
            "Проверь WEEEK_API_TOKEN/WEEEK_API_BASE_URL и перезапусти бота.",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END
    clean_text = strip_weeek_request_prefix(raw_text)
    if clean_text == _compact_spaces(raw_text).strip(" .,!?:;"):
        clean_text = re.sub(r"^(?:бот[, ]+)?(?:добавь|создай|запиши)\s+", "", clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(r"^(?:задачу|подзадачу)\s+", "", clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(r"^(?:в\s+)?(?:weeek|вик)\s+", "", clean_text, flags=re.IGNORECASE)
    route = detect_top_level_route(raw_text)
    content_text = strip_weeek_routing_metadata(clean_text, route)
    if not content_text:
        await update.message.reply_text(
            "Поймал запрос на Weeek, но сама задача пустая. Напиши одной строкой, что нужно сделать.",
            reply_markup=cancel_keyboard(),
        )
        return WEEEK_CAPTURE
    capture = await classify_capture(update, content_text, forced_type="task")
    capture = sanitize_weeek_capture_content(capture, route, fallback_text=content_text)
    speech_engine = context.user_data.get("_last_speech_engine", "")
    capture["speech_engine"] = speech_engine
    capture["transcript"] = raw_text
    capture["transcript_clean"] = capture.get("transcript_clean") or content_text
    draft = _get_weeek_draft(context)
    draft.clear()
    draft.update(
        {
            "raw_text": raw_text,
            "clean_text": content_text,
            "capture": capture,
            "target": route.get("target") or capture.get("target") or "weeek_task",
            "column_hint": route.get("column_hint") or capture.get("column_hint") or normalize_weeek_column_hint(raw_text),
            "parent_task_candidate": route.get("parent_task_candidate") or capture.get("parent_task_candidate") or "",
        }
    )
    if not draft["capture"].get("transcript_clean"):
        draft["capture"]["transcript_clean"] = content_text
    if not draft["capture"].get("transcript"):
        draft["capture"]["transcript"] = raw_text
    auto_project = route.get("project") or match_weeek_target(capture.get("project_name_candidate") or "")
    if auto_project:
        draft.update(auto_project)
        if draft.get("target") == "weeek_subtask":
            return await _send_weeek_parent_picker(update.message, context)
        return await _send_weeek_column_picker(update.message, context)
    return await _send_weeek_project_picker(update.message, context)


async def weeek_capture_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Сообщение пустое. Попробуй ещё раз.", reply_markup=cancel_keyboard())
        return WEEEK_CAPTURE
    return await _start_weeek_capture(update, context, text)


async def weeek_capture_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context, failure_reply_markup=cancel_keyboard())
    if not text:
        return WEEEK_CAPTURE
    return await _start_weeek_capture(update, context, text)


async def weeek_project_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _get_weeek_draft(context)
    project_id = (query.data or "").split(":", 1)[1]
    project = next((item for item in draft.get("projects", []) if item["id"] == project_id), None)
    if not project:
        await query.answer("Проект уже устарел, выбери ещё раз.", show_alert=True)
        return await _send_weeek_project_picker(query.message, context)
    draft["project_id"] = project["id"]
    draft["project_name"] = project["name"]
    known_project = WEEEK_TARGETS.get(str(project["id"]))
    if _should_auto_select_project_board(known_project):
        draft["board_id"] = known_project["board_id"]
        draft["board_name"] = known_project["board_name"]
        if draft.get("target") == "weeek_subtask":
            return await _send_weeek_parent_picker(query.message, context)
        return await _send_weeek_column_picker(query.message, context)
    return await _send_weeek_board_picker(query.message, context)


async def weeek_board_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _get_weeek_draft(context)
    board_id = (query.data or "").split(":", 1)[1]
    board = next((item for item in draft.get("boards", []) if item["id"] == board_id), None)
    if not board:
        await query.answer("Доска уже устарела, выбери ещё раз.", show_alert=True)
        return await _send_weeek_board_picker(query.message, context)
    draft["board_id"] = board["id"]
    draft["board_name"] = board["name"]
    if draft.get("target") == "weeek_subtask":
        return await _send_weeek_parent_picker(query.message, context)
    return await _send_weeek_column_picker(query.message, context)


async def weeek_column_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _get_weeek_draft(context)
    column_id = (query.data or "").split(":", 1)[1]
    column = next((item for item in draft.get("columns", []) if item["id"] == column_id), None)
    if not column:
        await query.answer("Колонка уже устарела, выбери ещё раз.", show_alert=True)
        return await _send_weeek_column_picker(query.message, context)
    draft["column_id"] = column["id"]
    draft["column_name"] = column["name"]
    return await _show_weeek_preview(query.message, context)


async def weeek_parent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _get_weeek_draft(context)
    parent_id = (query.data or "").split(":", 1)[1]
    parent = next((item for item in draft.get("parent_tasks", []) if item["id"] == parent_id), None)
    if not parent:
        await query.answer("Р—Р°РґР°С‡Р° СѓР¶Рµ СѓСЃС‚Р°СЂРµР»Р°. Р’С‹Р±РµСЂРё РµС‰С‘ СЂР°Р·.", show_alert=True)
        return await _send_weeek_parent_picker(query.message, context)
    draft["parent_task_id"] = parent["id"]
    draft["parent_task_name"] = parent["name"]
    draft["parent_task_match_source"] = "manual"
    logger.info("weeek_subtask parent_task_match_source=manual parent_task=%r", parent["name"])
    return await _send_weeek_column_picker(query.message, context)


async def weeek_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    _log_event("weeek_preview_callback", data=data, active_flow=context.user_data.get("_active_flow", ""))

    if data == "weeek_cancel":
        _clear_weeek_draft(context)
        await query.message.reply_text("Ок, отменил создание задачи в Weeek.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    if data == "weeek_repick_parent":
        return await _send_weeek_parent_picker(query.message, context)

    if data == "weeek_edit":
        await query.answer("Выбираем, что менять в черновике.")
        return await _show_weeek_edit_menu(query.message, context)

    if data != "weeek_create":
        return WEEEK_PREVIEW

    draft = _get_weeek_draft(context)
    capture = draft.get("capture") or {}
    client = get_weeek_client()
    try:
        if draft.get("target") == "weeek_subtask":
            response = await client.create_subtask(
                title=build_weeek_task_title(capture),
                description=_compact_spaces(capture.get("body") or ""),
                project_id=draft.get("project_id", ""),
                board_id=draft.get("board_id", ""),
                column_id=draft.get("column_id", ""),
                parent_task_id=draft.get("parent_task_id", ""),
                due_date=capture.get("due_date", ""),
                due_time=capture.get("due_time", ""),
                timezone_name=capture.get("timezone") or effective_user_timezone(query.from_user.id),
            )
        else:
            response = await client.create_task(
                title=build_weeek_task_title(capture),
                description=_compact_spaces(capture.get("body") or ""),
                project_id=draft.get("project_id", ""),
                board_id=draft.get("board_id", ""),
                column_id=draft.get("column_id", ""),
                due_date=capture.get("due_date", ""),
                due_time=capture.get("due_time", ""),
                timezone_name=capture.get("timezone") or effective_user_timezone(query.from_user.id),
            )
    except WeeekApiError as exc:
        await query.message.reply_text(
            f"Не удалось создать задачу в Weeek: {exc}",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        return ConversationHandler.END

    title = build_weeek_task_title(capture)
    task_id = client.extract_task_display_id(response)
    await query.message.reply_html(
        "✅ <b>Задача отправлена в Weeek</b>\n\n"
        f"<b>ID:</b> {html.escape(str(task_id))}\n"
        f"<b>Название:</b> {html.escape(title)}\n"
        f"<b>Проект:</b> {html.escape(draft.get('project_name', '—'))}\n"
        f"<b>Доска:</b> {html.escape(draft.get('board_name', '—'))}\n"
        f"<b>Колонка:</b> {html.escape(draft.get('column_name', '—'))}",
        reply_markup=main_menu_keyboard(),
    )
    transcription = transcription_message(capture.get("transcript_clean") or capture.get("transcript") or "", capture.get("speech_engine", ""))
    if transcription:
        await query.message.reply_html(transcription.lstrip(), reply_markup=main_menu_keyboard())
    _clear_weeek_draft(context)
    return ConversationHandler.END


def _needs_reminder_time_clarification(capture: dict) -> bool:
    return (
        capture.get("type") == "reminder"
        and (
            not capture.get("due_date")
            or not capture.get("due_time")
            or _coerce_confidence(capture.get("time_confidence"), 0.0) < 0.75
            or bool(capture.get("needs_time_clarification"))
        )
    )


def _clarify_time_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Сохранить без времени", callback_data="clarify_save_without_time")],
        [InlineKeyboardButton("Отмена", callback_data="clarify_cancel")],
    ])


async def _ask_reminder_time_clarification(update: Update, context: ContextTypes.DEFAULT_TYPE, with_buttons: bool = False):
    reply_markup = _clarify_time_keyboard() if with_buttons else cancel_keyboard()
    await update.message.reply_text(
        "Не понял, на какое время поставить напоминание.\n"
        "Запиши коротко только дату и время, например: «сегодня в 22:00» или «завтра в 10 утра».",
        reply_markup=reply_markup,
    )


def _log_voice_capture(raw_text: str, capture: dict, speech_engine: str):
    logger.info(
        "voice_capture raw_transcript=%r type=%s title=%r body=%r due_date=%s due_time=%s timezone=%s "
        "source=%s speech_engine=%s confidence=%.2f time_confidence=%.2f needs_time_clarification=%s",
        raw_text,
        capture.get("type", ""),
        capture.get("title", ""),
        capture.get("body", ""),
        capture.get("due_date", ""),
        capture.get("due_time", ""),
        capture.get("timezone", ""),
        capture.get("source", ""),
        speech_engine,
        _coerce_confidence(capture.get("confidence"), 0.0),
        _coerce_confidence(capture.get("time_confidence"), 0.0),
        bool(capture.get("needs_time_clarification")),
    )


def _create_task_from_capture(user_id: int, capture: dict) -> int:
    title = capture["title"]
    return db.add_task(
        user_id,
        title,
        title=title,
        body=capture.get("body") or title,
        due_date=capture.get("due_date", ""),
        due_time=capture.get("due_time", ""),
        timezone=capture.get("timezone", ""),
    )


def _store_weeek_task_reminder_shadow(user_id: int, capture: dict, draft: dict, weeek_task_id: str = "") -> int | None:
    due_date = capture.get("due_date", "")
    if not due_date:
        return None
    title = build_weeek_task_title(capture)
    project_name = _compact_spaces(draft.get("project_name") or "")
    body = _compact_spaces(capture.get("body") or title)
    if project_name and project_name.casefold() not in body.casefold():
        body = f"{body}\n\nПроект Weeek: {project_name}"
    reminder_text = title
    if weeek_task_id:
        reminder_text = f"[Weeek #{weeek_task_id}] {title}"
    return db.add_task(
        user_id,
        reminder_text,
        title=title,
        body=body,
        due_date=due_date,
        due_time=capture.get("due_time", ""),
        timezone=capture.get("timezone") or effective_user_timezone(user_id),
    )


async def _send_created_task(update: Update, capture: dict, task_id: int, recognized_text: str = "", reminder: bool = False):
    title = capture["title"]
    due_date = capture.get("due_date", "")
    due_time = capture.get("due_time", "")
    timezone = capture.get("timezone", "")
    response = "🔔 Напоминание создано" if reminder else "✅ Задача создана"
    await update.message.reply_html(
        f"{response}\n\n{task_message(title, False, capture.get('body') or title, due_date, due_time, timezone)}"
        f"{gpt_description_message(capture)}"
        f"{transcription_message(recognized_text or capture.get('transcript_clean') or capture.get('transcript') or '', capture.get('speech_engine', ''))}",
        reply_markup=main_menu_keyboard(),
    )
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=post_task_save_keyboard(task_id, include_add=not reminder),
    )


async def _extract_clarified_due(update: Update, text: str, pending: dict) -> dict:
    user_id = update.effective_user.id
    default_timezone = effective_user_timezone(user_id)
    parsed = await parse_capture_with_gpt(text, default_timezone)
    timezone = (parsed or {}).get("timezone") or pending.get("timezone") or default_timezone
    due_time = (parsed or {}).get("due_time") or extract_capture_due_time(text)
    due_date = (parsed or {}).get("due_date") or extract_capture_due_date(text, timezone)
    if due_time and not due_date:
        due_date = resolve_capture_due_date(text, timezone, due_time)

    time_confidence = _coerce_confidence(
        (parsed or {}).get("time_confidence"),
        _rule_time_confidence(text, due_time),
    )
    needs_clarification = not due_date or not due_time or time_confidence < 0.75 or bool(
        (parsed or {}).get("needs_time_clarification", False)
    )
    return {
        "due_date": due_date,
        "due_time": due_time,
        "timezone": timezone,
        "time_confidence": time_confidence,
        "needs_time_clarification": needs_clarification,
        "source": (parsed or {}).get("source", "rules"),
    }


async def _finish_pending_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE, due: dict | None = None, without_time: bool = False):
    message = update.message or (update.callback_query.message if update.callback_query else None)
    pending = context.user_data.get("pending_reminder_capture")
    if not pending:
        if message:
            await message.reply_text("Не нашёл черновик напоминания. Попробуй создать его заново.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    user_id = update.effective_user.id
    db.upsert_user(user_id)
    capture = dict(pending)
    if due:
        capture.update({
            "due_date": due.get("due_date", ""),
            "due_time": due.get("due_time", ""),
            "timezone": due.get("timezone") or capture.get("timezone") or effective_user_timezone(user_id),
            "time_confidence": due.get("time_confidence", 1.0),
            "needs_time_clarification": False,
        })
    elif without_time:
        capture.update({"due_date": "", "due_time": "", "needs_time_clarification": False})

    task_id = _create_task_from_capture(user_id, capture)
    context.user_data.pop("pending_reminder_capture", None)
    context.user_data.pop("pending_reminder_attempts", None)

    if without_time:
        await message.reply_html(
            f"✅ Сохранено без времени\n\n{task_message(capture['title'], False, capture.get('body') or capture['title'])}",
            reply_markup=main_menu_keyboard(),
        )
        await message.reply_text(
            "Что дальше?",
            reply_markup=post_task_save_keyboard(task_id, include_add=False),
        )
    else:
        await _send_created_task(update, capture, task_id, reminder=True)
    return ConversationHandler.END


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
        return ConversationHandler.END

    route = detect_top_level_route(text)
    if route.get("target") in {"weeek_task", "weeek_subtask"}:
        return await _start_weeek_capture(update, context, text)

    user_id = update.effective_user.id
    db.upsert_user(user_id)

    forced_type = "task" if route.get("target") == "local_task" else "reminder" if route.get("target") == "reminder" else None
    capture = await classify_capture(update, text, forced_type=forced_type)
    capture_type = capture["type"]
    speech_engine = context.user_data.pop("_last_speech_engine", "")
    capture["speech_engine"] = speech_engine
    capture["transcript"] = text
    capture["transcript_clean"] = capture.get("transcript_clean") or text
    _log_voice_capture(text, capture, speech_engine)

    if _needs_reminder_time_clarification(capture):
        context.user_data["pending_reminder_capture"] = capture
        context.user_data["pending_reminder_attempts"] = 0
        await _ask_reminder_time_clarification(update, context)
        return CLARIFY_REMINDER_TIME

    if capture_type in {"task", "reminder"}:
        task_id = _create_task_from_capture(user_id, capture)
        await _send_created_task(update, capture, task_id, recognized_text=text, reminder=capture_type == "reminder")
        return ConversationHandler.END

    brief = capture["title"]
    details = capture["body"]
    if _openai_client and capture.get("source") != "gpt":
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
        f"✅ Идея сохранена\n\n{full_message(brief, details)}",
        reply_markup=main_menu_keyboard(),
    )
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=post_save_keyboard(idea_id),
    )
    extra_details = f"{gpt_description_message(capture)}{transcription_message(text, speech_engine)}".strip()
    if extra_details:
        await update.message.reply_html(extra_details, reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def text_top_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return ConversationHandler.END

    route = detect_top_level_route(text)
    if route.get("target") in {"weeek_task", "weeek_subtask"}:
        return await _start_weeek_capture(update, context, text)

    user_id = update.effective_user.id
    db.upsert_user(user_id)

    forced_type = "task" if route.get("target") == "local_task" else "reminder" if route.get("target") == "reminder" else None
    capture = await classify_capture(update, text, forced_type=forced_type)
    capture_type = capture["type"]
    logger.info(
        "text_capture raw_text=%r type=%s title=%r body=%r due_date=%s due_time=%s timezone=%s source=%s confidence=%.2f time_confidence=%.2f needs_time_clarification=%s",
        text,
        capture.get("type", ""),
        capture.get("title", ""),
        capture.get("body", ""),
        capture.get("due_date", ""),
        capture.get("due_time", ""),
        capture.get("timezone", ""),
        capture.get("source", ""),
        _coerce_confidence(capture.get("confidence"), 0.0),
        _coerce_confidence(capture.get("time_confidence"), 0.0),
        bool(capture.get("needs_time_clarification")),
    )

    if capture_type == "idea":
        return await vik_only_disabled(update, context)

    if _needs_reminder_time_clarification(capture):
        context.user_data["pending_reminder_capture"] = capture
        context.user_data["pending_reminder_attempts"] = 0
        await _ask_reminder_time_clarification(update, context)
        return CLARIFY_REMINDER_TIME

    if capture_type == "task":
        return await _start_weeek_capture(update, context, text, return_state=False)

    if capture_type == "reminder":
        task_id = _create_task_from_capture(user_id, capture)
        await _send_created_task(update, capture, task_id, reminder=capture_type == "reminder")
        return ConversationHandler.END

    brief = capture["title"]
    details = capture["body"]
    if _openai_client and capture.get("source") != "gpt":
        parsed = await parse_idea_with_gpt(text)
        if parsed:
            brief, details = parsed

    if not brief:
        first = text.split(".")[0].strip() or text
        brief = first[:120]
        details = text

    idea_id = db.add_idea(user_id, brief, details)
    schedule_user_reminders(context.application, user_id)

    await update.message.reply_html(
        f"✅ Идея сохранена\n\n{full_message(brief, details)}",
        reply_markup=main_menu_keyboard(),
    )
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=post_save_keyboard(idea_id),
    )
    return ConversationHandler.END


async def _transcribe_or_warn(update: Update, context: ContextTypes.DEFAULT_TYPE, failure_reply_markup=None):
    failure_reply_markup = failure_reply_markup or cancel_keyboard()
    if not voice_available():
        await update.message.reply_text(
            "🎤 Голосовой ввод сейчас не настроен. Напиши текстом, пожалуйста.",
            reply_markup=failure_reply_markup,
        )
        return None

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    voice = update.message.voice or update.message.audio
    if not voice:
        return None

    try:
        file = await voice.get_file()
        result = await asyncio.wait_for(transcribe_voice(file), timeout=45)
    except asyncio.TimeoutError:
        logger.exception("Voice transcription timed out")
        await update.message.reply_text(
            transcription_error_text("transcription_timeout"),
            reply_markup=failure_reply_markup,
        )
        return None
    except Exception:
        logger.exception("Не удалось распознать голосовое")
        await update.message.reply_text(
            "Не удалось распознать голосовое. Попробуй ещё раз или введи текстом.",
            reply_markup=failure_reply_markup,
        )
        return None

    if isinstance(result, dict):
        text = result.get("text")
        speech_engine = result.get("engine", "")
        error = result.get("error", "")
    else:
        text = result
        speech_engine = ""
        error = ""
    context.user_data["_last_speech_engine"] = speech_engine

    if not text:
        message = transcription_error_text(error) if error else "Голос распознался как пустой. Попробуй ещё раз или введи текстом."
        await update.message.reply_text(
            message,
            reply_markup=failure_reply_markup,
        )
        return None

    logger.info("voice_transcribed raw_transcript=%r speech_engine=%s", text, speech_engine)
    return text


async def _handle_clarified_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    text = (text or "").strip()
    if not text:
        await _ask_reminder_time_clarification(update, context)
        return CLARIFY_REMINDER_TIME

    pending = context.user_data.get("pending_reminder_capture")
    if not pending:
        await update.message.reply_text("Черновик напоминания потерялся. Попробуй создать его заново.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    due = await _extract_clarified_due(update, text, pending)
    logger.info(
        "reminder_time_clarification raw_text=%r due_date=%s due_time=%s timezone=%s source=%s time_confidence=%.2f needs_time_clarification=%s",
        text,
        due.get("due_date", ""),
        due.get("due_time", ""),
        due.get("timezone", ""),
        due.get("source", ""),
        due.get("time_confidence", 0.0),
        due.get("needs_time_clarification", False),
    )
    if not due["needs_time_clarification"]:
        return await _finish_pending_reminder(update, context, due=due)

    attempts = int(context.user_data.get("pending_reminder_attempts", 0)) + 1
    context.user_data["pending_reminder_attempts"] = attempts
    await _ask_reminder_time_clarification(update, context, with_buttons=attempts >= 1)
    return CLARIFY_REMINDER_TIME


async def clarify_reminder_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_clarified_time_text(update, context, update.message.text)


async def clarify_reminder_time_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context)
    if text is None:
        return CLARIFY_REMINDER_TIME
    speech_engine = context.user_data.pop("_last_speech_engine", "")
    logger.info("reminder_time_clarification_voice raw_transcript=%r speech_engine=%s", text, speech_engine)
    return await _handle_clarified_time_text(update, context, text)


async def clarify_reminder_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "clarify_cancel":
        context.user_data.pop("pending_reminder_capture", None)
        context.user_data.pop("pending_reminder_attempts", None)
        await query.message.reply_text("❌ Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    if query.data == "clarify_save_without_time":
        return await _finish_pending_reminder(update, context, without_time=True)
    return CLARIFY_REMINDER_TIME


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
            tid, title, done, body, due_date, due_time, timezone = _task_row_parts(task)
            await context.bot.send_message(
                chat_id=user_id,
                text=task_message(title, done, body, due_date, due_time, timezone),
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
        _, title, _done, body, due_date, due_time, timezone = _task_row_parts(task)
        if action == "task_done":
            db.set_task_done(tid, user_id, True)
            await query.edit_message_text(
                task_message(title, True, body, due_date, due_time, timezone),
                parse_mode=ParseMode.HTML,
                reply_markup=task_keyboard(tid, True),
            )
        elif action == "task_undone":
            db.set_task_done(tid, user_id, False)
            await query.edit_message_text(
                task_message(title, False, body, due_date, due_time, timezone),
                parse_mode=ParseMode.HTML,
                reply_markup=task_keyboard(tid, False),
            )
        elif action == "task_del":
            db.delete_task(tid, user_id)
            await query.edit_message_text(
                f"🗑 Задача удалена:\n\n{task_message(title, False, body, due_date, due_time, timezone)}",
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
    schedule_task_reminder_checker(application)
    logger.info("Bot started; reminders scheduled.")


def _log_event(event: str, **fields):
    parts = []
    for key, value in fields.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            parts.append(f"{key}={value!r}")
        else:
            parts.append(f"{key}={value}")
    logger.info("%s %s", event, " ".join(parts).strip())


def _voice_message_meta(update: Update) -> dict:
    message = update.message or (update.callback_query.message if update.callback_query else None)
    voice = (message.voice if message else None) or (message.audio if message else None)
    return {
        "chat_id": getattr(update.effective_chat, "id", ""),
        "user_id": getattr(update.effective_user, "id", ""),
        "message_id": getattr(message, "message_id", ""),
        "file_id": getattr(voice, "file_id", ""),
        "duration": getattr(voice, "duration", ""),
        "file_size": getattr(voice, "file_size", ""),
    }


def _set_active_flow(context: ContextTypes.DEFAULT_TYPE, flow: str):
    context.user_data["_active_flow"] = flow


def _clear_active_flow(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("_active_flow", None)


def _reset_transient_flow_state(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "brief",
        "weeek_draft",
        "pending_reminder_capture",
        "pending_reminder_attempts",
        "_active_flow",
    ):
        context.user_data.pop(key, None)


def _convert_audio_for_openai_stt(audio_path: str) -> str:
    wav_path = audio_path + ".wav"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", audio_path,
                "-ar", "16000", "-ac", "1", "-f", "wav", wav_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg_missing") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="ignore")[:300]
        raise RuntimeError(f"ffmpeg_convert_failed:{stderr}")
    _log_event("voice_converted_for_openai", source_path=audio_path, converted_path=wav_path)
    return wav_path


def _chat_json_request_kwargs(model: str, messages: list[dict], temperature: float) -> dict:
    return {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }


def _openai_transcribe_file(audio_path: str) -> str:
    converted_path = _convert_audio_for_openai_stt(audio_path)
    try:
        with open(converted_path, "rb") as audio_file:
            response = _openai_stt_client.audio.transcriptions.create(
                model=OPENAI_STT_MODEL,
                file=audio_file,
            )
        return _compact_spaces(getattr(response, "text", "") or "")
    finally:
        try:
            os.unlink(converted_path)
        except OSError:
            pass


async def transcribe_voice(voice_file, update: Update | None = None):
    if not voice_available():
        if not _vosk_available() and not _openai_stt_available():
            return {"text": "", "engine": "", "error": "voice_unavailable"}
        if not shutil.which("ffmpeg"):
            return {"text": "", "engine": "vosk", "error": "ffmpeg_missing"}
        return {"text": "", "engine": "vosk", "error": "vosk_unavailable"}

    meta = _voice_message_meta(update) if update else {}
    with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await voice_file.download_to_drive(tmp_path)
        _log_event("voice_downloaded", path=tmp_path, **meta)
        if _openai_stt_available():
            try:
                _log_event("openai_stt_request_started", model=OPENAI_STT_MODEL, **meta)
                text = await _async_call(_openai_transcribe_file, tmp_path)
                if text:
                    _record_openai_stt_status("ok")
                    _log_event("openai_stt_request_succeeded", model=OPENAI_STT_MODEL, transcript=text, **meta)
                    logger.info("voice transcription engine=%s", _openai_stt_engine_label())
                    return {"text": text, "engine": _openai_stt_engine_label()}
            except Exception as exc:
                openai_error = _openai_stt_error_code(exc)
                _record_openai_stt_status("fallback", openai_error)
                logger.warning(
                    "primary_stt_failed code=%s model=%s official_api=true fallback=vosk error=%s",
                    openai_error,
                    OPENAI_STT_MODEL,
                    exc,
                )
            else:
                openai_error = "openai_stt_empty"
                _record_openai_stt_status("fallback", openai_error)
        else:
            openai_error = ""

        if not _vosk_available():
            return {"text": "", "engine": _openai_stt_engine_label(), "error": openai_error or "vosk_unavailable"}
        if not shutil.which("ffmpeg"):
            return {"text": "", "engine": "vosk", "error": openai_error or "ffmpeg_missing"}

        _log_event("vosk_fallback_started", reason=openai_error or "primary_unavailable", **meta)
        model = await _async_call(_get_vosk_model)
        if model is None:
            return {"text": "", "engine": "vosk", "error": openai_error or "vosk_model_unavailable"}
        text = await _async_call(_vosk_transcribe_file, tmp_path)
        text = (text or "").strip()
        if text:
            _log_event("vosk_fallback_succeeded", transcript=text, **meta)
            logger.info("voice transcription engine=vosk")
            return {"text": text, "engine": "vosk"}
        return {"text": "", "engine": "vosk", "error": openai_error or "vosk_empty"}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def build_weeek_task_title(capture: dict) -> str:
    title = _compact_spaces(capture.get("title") or "")
    body = _compact_spaces(capture.get("body") or "")
    if not title:
        title = "Новая задача"
    if body and body.casefold() != title.casefold() and "см. описание" not in title.lower():
        title = f"{title} — см. описание"
    return title[:120]


def weeek_preview_message(draft: dict) -> str:
    capture = draft.get("capture") or {}
    project = draft.get("project_name") or "—"
    board = draft.get("board_name") or "—"
    column = draft.get("column_name") or "—"
    title = build_weeek_task_title(capture)
    body = _compact_spaces(capture.get("body") or "")
    due_date = capture.get("due_date") or ""
    due_time = capture.get("due_time") or ""
    timezone = capture.get("timezone") or ""
    parent_task = draft.get("parent_task_name") or ""
    transcript = _compact_spaces(capture.get("transcript_clean") or capture.get("transcript") or "")

    parts = [
        "🧩 <b>Черновик задачи для Weeek</b>",
        "",
        f"<b>Название:</b> {html.escape(title)}",
        f"<b>Проект:</b> {html.escape(project)}",
        f"<b>Доска:</b> {html.escape(board)}",
        f"<b>Колонка:</b> {html.escape(column)}",
    ]
    if parent_task:
        parts.append(f"<b>Родительская задача:</b> {html.escape(parent_task)}")
    if due_date or due_time:
        parts.append(f"<b>Срок:</b> {html.escape((due_date + ' ' + due_time).strip())}")
    if timezone:
        parts.append(f"<b>Часовой пояс:</b> {html.escape(timezone)}")
    if body:
        parts.extend(["", f"<b>Описание:</b> {html.escape(body)}"])
    if transcript:
        parts.extend(["", f"<b>Транскрипция:</b> {html.escape(transcript)}"])
    return "\n".join(parts)


def weeek_preview_keyboard(draft: dict | None = None) -> InlineKeyboardMarkup:
    draft = draft or {}
    rows = [[InlineKeyboardButton("✅ Создать в Weeek", callback_data="weeek_create")]]
    if draft.get("target") == "weeek_subtask":
        rows.append([InlineKeyboardButton("🔁 Выбрать родителя заново", callback_data="weeek_repick_parent")])
    rows.append([InlineKeyboardButton("🎨 Редактировать", callback_data="weeek_edit")])
    rows.append([InlineKeyboardButton("✖️ Отмена", callback_data="weeek_cancel")])
    return InlineKeyboardMarkup(rows)


def weeek_preview_message_ex(draft: dict) -> str:
    return weeek_preview_message(draft)


def weeek_preview_keyboard_ex(draft: dict | None = None) -> InlineKeyboardMarkup:
    return weeek_preview_keyboard(draft)


def weeek_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Менять название", callback_data="weeek_edit_field:title")],
            [InlineKeyboardButton("Менять описание", callback_data="weeek_edit_field:body")],
            [InlineKeyboardButton("До черновика", callback_data="weeek_edit_back")],
            [InlineKeyboardButton("✖️ Отмена", callback_data="weeek_cancel")],
        ]
    )


def _is_done_weeek_column(column: dict) -> bool:
    name = _normalize_text(column.get("name") or "")
    raw = column.get("raw") or {}
    slug = _normalize_text(str(raw.get("slug") or raw.get("key") or raw.get("code") or raw.get("status") or ""))
    return any(token in name or token == slug for token in ("готово", "сделано", "done", "complete", "completed"))


def _sort_weeek_columns(columns: list[dict], hint: str) -> list[dict]:
    filtered = [column for column in columns if not _is_done_weeek_column(column)]
    if not filtered:
        filtered = list(columns)

    def score(column: dict) -> tuple[int, str]:
        name = _normalize_text(column.get("name") or "")
        raw = column.get("raw") or {}
        slug = _normalize_text(str(raw.get("slug") or raw.get("key") or raw.get("code") or raw.get("status") or ""))
        if hint and (hint in name or hint == slug):
            return (0, name)
        if hint == "to_work" and any(token in name or token == slug for token in ("к работе", "to work", "todo", "backlog", "queue")):
            return (0, name)
        if hint == "in_work" and any(token in name or token == slug for token in ("в работе", "in work", "progress", "doing")):
            return (0, name)
        return (1, name)

    return sorted(filtered, key=score)


async def _fail_weeek_flow(message, context: ContextTypes.DEFAULT_TYPE, user_text: str, log_code: str, exc: Exception):
    _log_event(log_code, error=str(exc))
    await message.reply_text(user_text, reply_markup=main_menu_keyboard())
    _clear_weeek_draft(context)
    _clear_active_flow(context)
    return ConversationHandler.END


async def weeek_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "weeek_edit_back":
        return await _show_weeek_preview(query.message, context)
    if data == "weeek_cancel":
        return await weeek_preview_callback(update, context)
    if not data.startswith("weeek_edit_field:"):
        return WEEEK_EDIT
    field = data.split(":", 1)[1]
    if field not in {"title", "body"}:
        return WEEEK_EDIT
    return await _prompt_weeek_edit_field(query.message, context, field)


def _weeek_browser_keyboard(options: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(item["name"], callback_data=f"{prefix}:{item['id']}")] for item in options]
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data="weeek_list_close")])
    return InlineKeyboardMarkup(rows)


def _weeek_browser_nav_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔙 К проектам", callback_data="weeek_list_back")],
            [InlineKeyboardButton("✖️ Закрыть", callback_data="weeek_list_close")],
        ]
    )


def _extract_weeek_task_board_id(task: dict) -> str:
    return str(task.get("boardId") or task.get("board", {}).get("id") or "")


def _extract_weeek_task_column_id(task: dict) -> str:
    return str(
        task.get("boardColumnId")
        or task.get("columnId")
        or task.get("statusId")
        or task.get("boardColumn", {}).get("id")
        or task.get("column", {}).get("id")
        or task.get("status", {}).get("id")
        or ""
    )


def _extract_weeek_task_column_name(task: dict, columns_by_id: dict[str, dict]) -> str:
    column_id = _extract_weeek_task_column_id(task)
    if column_id and column_id in columns_by_id:
        return columns_by_id[column_id].get("name", "")
    for key in ("boardColumn", "column", "status"):
        value = task.get(key)
        if isinstance(value, dict):
            name = _compact_spaces(str(value.get("name") or value.get("title") or ""))
            if name:
                return name
    return ""


def _weeek_status_rank(status_name: str) -> tuple[int, str]:
    normalized = _normalize_text(status_name)
    if any(token in normalized for token in WEEEK_COLUMN_NAMES.get("to_work", ())):
        return (0, normalized)
    if any(token in normalized for token in WEEEK_COLUMN_NAMES.get("in_work", ())):
        return (1, normalized)
    if any(token in normalized for token in WEEEK_COLUMN_NAMES.get("done", ())):
        return (2, normalized)
    if not normalized:
        return (4, "")
    return (3, normalized)


def _format_weeek_tasks_overview(project_name: str, tasks: list[dict], columns_by_id: dict[str, dict]) -> str:
    grouped: dict[str, list[str]] = {}
    for task in tasks:
        raw = task.get("raw") or {}
        status_name = _extract_weeek_task_column_name(raw, columns_by_id) or "Без статуса"
        title = _compact_spaces(task.get("name") or "Без названия")
        display_id = (
            raw.get("number")
            or raw.get("taskNumber")
            or raw.get("seqNumber")
            or raw.get("displayId")
            or task.get("id")
            or "?"
        )
        grouped.setdefault(status_name, []).append(f"• #{display_id} {title}")

    lines = [f"📂 <b>{html.escape(project_name)}</b>"]
    if not grouped:
        lines.extend(["", "Задач в этом проекте не нашёл."])
        return "\n".join(lines)

    for status_name in sorted(grouped, key=lambda item: _weeek_status_rank(item)):
        lines.extend(["", f"<b>{html.escape(status_name)}</b>"])
        lines.extend(grouped[status_name])
    return "\n".join(lines)


async def _load_weeek_project_options() -> list[dict]:
    client = get_weeek_client()
    projects = await client.list_projects()
    return [{"id": option.id, "name": option.name, "raw": option.raw} for option in projects]


async def weeek_list_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not weeek_available():
        await update.message.reply_text(
            "Интеграция с Weeek пока не настроена. Проверь WEEEK_API_TOKEN.",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    try:
        projects = await _load_weeek_project_options()
    except WeeekApiError as exc:
        _log_event("weeek_list_projects_load_failed", error=str(exc))
        await update.message.reply_text(
            "Не удалось получить список проектов из Weeek. Попробуй ещё раз чуть позже.",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    if not projects:
        await update.message.reply_text(
            "Не нашёл доступные проекты в Weeek.",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    context.user_data["weeek_list_projects"] = projects
    await update.message.reply_text(
        "Выбери проект:",
        reply_markup=_weeek_browser_keyboard(projects, "weeek_list_project"),
    )
    return ConversationHandler.END


async def weeek_list_project_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    projects = context.user_data.get("weeek_list_projects") or []
    project_id = (query.data or "").split(":", 1)[1]
    project = next((item for item in projects if item["id"] == project_id), None)
    if not project:
        try:
            projects = await _load_weeek_project_options()
        except WeeekApiError as exc:
            _log_event("weeek_list_projects_reload_failed", error=str(exc))
            await query.message.reply_text(
                "Не удалось заново получить список проектов из Weeek. Попробуй ещё раз чуть позже.",
                reply_markup=main_menu_keyboard(),
            )
            return ConversationHandler.END
        context.user_data["weeek_list_projects"] = projects
        project = next((item for item in projects if item["id"] == project_id), None)
        if not project:
            await query.message.reply_text("Проект устарел. Нажми «Задачи ВИК» ещё раз.", reply_markup=main_menu_keyboard())
            return ConversationHandler.END

    client = get_weeek_client()
    try:
        boards = await client.list_boards(project_id)
        tasks = await client.list_tasks(project_id=project_id)
    except WeeekApiError as exc:
        _log_event("weeek_list_project_tasks_failed", project_id=project_id, error=str(exc))
        await query.message.reply_text(
            "Не удалось получить задачи этого проекта из Weeek. Попробуй ещё раз чуть позже.",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    columns_by_id: dict[str, dict] = {}
    for board in boards:
        try:
            columns = await client.list_columns(board.id)
        except WeeekApiError:
            continue
        for column in columns:
            columns_by_id[str(column.id)] = {"id": str(column.id), "name": column.name, "raw": column.raw}

    mapped_tasks = [{"id": option.id, "name": option.name, "raw": option.raw} for option in tasks]
    overview = _format_weeek_tasks_overview(project["name"], mapped_tasks, columns_by_id)
    await query.message.reply_html(overview, reply_markup=_weeek_browser_nav_keyboard())
    return ConversationHandler.END


async def weeek_list_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "weeek_list_close":
        await query.message.reply_text("Ок.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    projects = context.user_data.get("weeek_list_projects") or []
    if not projects:
        try:
            projects = await _load_weeek_project_options()
        except WeeekApiError as exc:
            _log_event("weeek_list_projects_back_failed", error=str(exc))
            await query.message.reply_text(
                "Не удалось получить список проектов из Weeek. Попробуй ещё раз чуть позже.",
                reply_markup=main_menu_keyboard(),
            )
            return ConversationHandler.END
        context.user_data["weeek_list_projects"] = projects

    await query.message.reply_text(
        "Выбери проект:",
        reply_markup=_weeek_browser_keyboard(projects, "weeek_list_project"),
    )
    return ConversationHandler.END


async def legacy_menu_disabled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Сейчас в главном меню доступен только раздел «Задачи ВИК».",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def vik_only_disabled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Сейчас бот работает только как задачник ВИК. Скажи или напиши, какую задачу нужно добавить в ВИК.",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def _send_weeek_project_picker(message, context: ContextTypes.DEFAULT_TYPE):
    client = get_weeek_client()
    try:
        projects = await client.list_projects()
    except WeeekApiError as exc:
        return await _fail_weeek_flow(
            message,
            context,
            "Не удалось связаться с Weeek и получить список проектов. Попробуй ещё раз чуть позже.",
            "weeek_projects_load_failed",
            exc,
        )
    if not projects:
        await message.reply_text(
            "Не смог найти проекты в Weeek. Проверь WEEEK_API_TOKEN, WEEEK_API_BASE_URL и доступы у токена.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        _clear_active_flow(context)
        return ConversationHandler.END

    draft = _get_weeek_draft(context)
    draft["projects"] = [{"id": option.id, "name": option.name, "raw": option.raw} for option in projects]
    _log_event("weeek_projects_loaded", count=len(draft["projects"]))
    await message.reply_text(
        "Выбери проект для задачи в Weeek:",
        reply_markup=_weeek_options_keyboard(draft["projects"], "weeek_project"),
    )
    return WEEEK_PROJECT


async def _send_weeek_board_picker(message, context: ContextTypes.DEFAULT_TYPE):
    draft = _get_weeek_draft(context)
    project_id = draft.get("project_id")
    client = get_weeek_client()
    try:
        boards = await client.list_boards(project_id)
    except WeeekApiError as exc:
        return await _fail_weeek_flow(
            message,
            context,
            "Не удалось связаться с Weeek и получить список досок. Попробуй ещё раз чуть позже.",
            "weeek_boards_load_failed",
            exc,
        )
    if not boards:
        await message.reply_text(
            "Для выбранного проекта не нашёл доски Weeek. Проверь структуру проекта в Weeek.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        _clear_active_flow(context)
        return ConversationHandler.END

    draft["boards"] = [{"id": option.id, "name": option.name, "raw": option.raw} for option in boards]
    _log_event("weeek_boards_loaded", count=len(draft["boards"]), project_id=project_id)
    candidate = draft.get("board_name_candidate") or ""
    matched_board, match_source = _match_weeek_board(draft["boards"], candidate)
    if matched_board:
        draft["board_id"] = matched_board["id"]
        draft["board_name"] = matched_board["name"]
        _log_event(
            "weeek_board_auto_selected",
            board_id=matched_board["id"],
            board_name=matched_board["name"],
            match_source=match_source,
        )
        if draft.get("target") == "weeek_subtask":
            return await _send_weeek_parent_picker(message, context)
        return await _send_weeek_column_picker(message, context)

    await message.reply_text(
        "Теперь выбери доску:",
        reply_markup=_weeek_options_keyboard(draft["boards"], "weeek_board"),
    )
    return WEEEK_BOARD


async def _send_weeek_column_picker(message, context: ContextTypes.DEFAULT_TYPE):
    draft = _get_weeek_draft(context)
    board_id = draft.get("board_id")
    client = get_weeek_client()
    try:
        columns = await client.list_columns(board_id)
    except WeeekApiError as exc:
        return await _fail_weeek_flow(
            message,
            context,
            "Не удалось связаться с Weeek и получить список колонок. Попробуй ещё раз чуть позже.",
            "weeek_columns_load_failed",
            exc,
        )
    if not columns:
        await message.reply_text(
            "Не смог получить колонки этой доски Weeek. Возможно, API вернул непривычный формат.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        _clear_active_flow(context)
        return ConversationHandler.END

    mapped = [{"id": option.id, "name": option.name, "raw": option.raw} for option in columns]
    _log_event("weeek_columns_loaded", board_id=board_id, columns=[item["name"] for item in mapped])
    filtered = _sort_weeek_columns(mapped, draft.get("column_hint", ""))
    draft["columns"] = filtered
    _log_event("weeek_columns_filtered", board_id=board_id, columns=[item["name"] for item in filtered])

    preferred = None
    to_work = [column for column in filtered if _column_matches_hint(column.get("name", ""), "to_work")]
    if to_work:
        preferred = to_work[0]
    elif filtered:
        preferred = filtered[0]

    if not preferred:
        await message.reply_text(
            "Не смог выбрать колонку для новой задачи в Weeek.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        _clear_active_flow(context)
        return ConversationHandler.END

    draft["column_id"] = preferred["id"]
    draft["column_name"] = preferred["name"]
    _log_event(
        "weeek_column_auto_selected",
        board_id=board_id,
        column_id=preferred["id"],
        column_name=preferred["name"],
        hint="to_work",
    )
    return await _show_weeek_preview(message, context)


async def _send_weeek_parent_picker(message, context: ContextTypes.DEFAULT_TYPE):
    draft = _get_weeek_draft(context)
    client = get_weeek_client()
    try:
        tasks = await client.list_tasks(draft.get("project_id", ""), draft.get("board_id", ""))
    except WeeekApiError as exc:
        return await _fail_weeek_flow(
            message,
            context,
            "Не удалось связаться с Weeek и получить список задач. Попробуй ещё раз чуть позже.",
            "weeek_parent_tasks_load_failed",
            exc,
        )
    if not tasks:
        await message.reply_text(
            "Не смог получить список задач в этой доске Weeek.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        _clear_active_flow(context)
        return ConversationHandler.END

    mapped = [{"id": option.id, "name": option.name, "raw": option.raw} for option in tasks]
    draft["parent_tasks"] = mapped
    candidate = draft.get("parent_task_candidate") or ""
    parent_task, match_source = _match_parent_task(mapped, candidate)
    if parent_task:
        draft["parent_task_id"] = parent_task["id"]
        draft["parent_task_name"] = parent_task["name"]
        draft["parent_task_match_source"] = match_source
        logger.info("weeek_subtask parent_task_match_source=%s parent_task=%r", match_source, parent_task["name"])
        return await _send_weeek_column_picker(message, context)

    if candidate:
        await message.reply_text(
            "Не смог точно подобрать родительскую задачу. Выбери её вручную:",
            reply_markup=_weeek_options_keyboard(mapped, "weeek_parent"),
        )
    else:
        await message.reply_text(
            "Выбери родительскую задачу:",
            reply_markup=_weeek_options_keyboard(mapped, "weeek_parent"),
        )
    return WEEEK_PARENT


async def _show_weeek_preview(message, context: ContextTypes.DEFAULT_TYPE):
    draft = _get_weeek_draft(context)
    _set_active_flow(context, "weeek_preview")
    draft.pop("edit_field", None)
    _log_event(
        "weeek_preview_rendered",
        project_id=draft.get("project_id", ""),
        board_id=draft.get("board_id", ""),
        column_id=draft.get("column_id", ""),
        target=draft.get("target", ""),
        title=(draft.get("capture") or {}).get("title", ""),
    )
    await message.reply_html(
        weeek_preview_message(draft),
        reply_markup=weeek_preview_keyboard(draft),
    )
    return WEEEK_PREVIEW


async def _show_weeek_edit_menu(message, context: ContextTypes.DEFAULT_TYPE):
    draft = _get_weeek_draft(context)
    if not draft:
        await message.reply_text("Черновик задачи Weeek потерян. Начни заново.", reply_markup=main_menu_keyboard())
        _clear_active_flow(context)
        return ConversationHandler.END
    _set_active_flow(context, "weeek_edit")
    draft.pop("edit_field", None)
    _log_event(
        "weeek_edit_menu_opened",
        project_id=draft.get("project_id", ""),
        board_id=draft.get("board_id", ""),
        column_id=draft.get("column_id", ""),
    )
    await message.reply_text(
        "Что именно поправить в черновике? Можно нажать кнопку ниже или сразу написать/надиктовать: "
        "отредактируй описание ... или отредактируй название ...",
        reply_markup=weeek_edit_keyboard(),
    )
    return WEEEK_EDIT


async def _prompt_weeek_edit_field(message, context: ContextTypes.DEFAULT_TYPE, field: str):
    draft = _get_weeek_draft(context)
    if not draft:
        await message.reply_text("Черновик задачи Weeek потерян. Начни заново.", reply_markup=main_menu_keyboard())
        _clear_active_flow(context)
        return ConversationHandler.END
    draft["edit_field"] = field
    _set_active_flow(context, "weeek_edit")
    await message.reply_text(
        f"Пришли новое {_weeek_edit_field_label(field)} текстом или голосом.",
        reply_markup=weeek_edit_keyboard(),
    )
    return WEEEK_EDIT


async def _apply_weeek_edit_from_text(message, context: ContextTypes.DEFAULT_TYPE, text: str):
    draft = _get_weeek_draft(context)
    if not draft:
        await message.reply_text("Черновик задачи Weeek потерян. Начни заново.", reply_markup=main_menu_keyboard())
        _clear_active_flow(context)
        return ConversationHandler.END

    parsed = parse_weeek_edit_request(text)
    field = ""
    value = ""
    if parsed:
        field = parsed["field"]
        value = parsed["value"]
    else:
        field = draft.get("edit_field") or ""
        value = text

    if not field:
        await message.reply_text(
            "Не понял, что именно менять. Выбери поле кнопкой ниже или напиши: отредактируй описание ...",
            reply_markup=weeek_edit_keyboard(),
        )
        return WEEEK_EDIT

    if not _apply_weeek_draft_edit(draft, field, value):
        await message.reply_text(
            f"Не смог обновить {_weeek_edit_field_label(field)}. Пришли непустой текст ещё раз.",
            reply_markup=weeek_edit_keyboard(),
        )
        return WEEEK_EDIT

    await message.reply_html(
        f"Обновил поле <b>{html.escape(_weeek_edit_field_label(field))}</b>.",
        reply_markup=weeek_preview_keyboard(draft),
    )
    return await _show_weeek_preview(message, context)


async def _maybe_handle_weeek_live_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, source: str):
    if not _has_editable_weeek_draft(context):
        return None
    draft = _get_weeek_draft(context)
    active_flow = context.user_data.get("_active_flow", "")
    parsed = parse_weeek_edit_request(text)
    if not parsed and not draft.get("edit_field") and active_flow not in {"weeek_preview", "weeek_edit"}:
        return None
    _log_event(
        "weeek_live_edit_intercepted",
        source=source,
        active_flow=active_flow,
        has_edit_field=bool(draft.get("edit_field")),
        parsed_field=(parsed or {}).get("field", ""),
    )
    return await _apply_weeek_edit_from_text(update.message, context, text)


async def _start_weeek_capture(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str, return_state: bool = False):
    if not weeek_available():
        await update.message.reply_text(
            "Интеграция с Weeek пока не настроена. Проверь WEEEK_API_TOKEN.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_active_flow(context)
        return ConversationHandler.END

    clean_text = strip_weeek_request_prefix(raw_text)
    if not clean_text:
        clean_text = normalize_capture_command_text(raw_text)
        clean_text = re.sub(r"^(?:в\s+)?(?:weeek|вик)\s+", "", clean_text, flags=re.IGNORECASE)
        clean_text = _compact_spaces(clean_text).strip(" .,!?:;")
    if not clean_text:
        await update.message.reply_text(
            "Напиши или наговори, что именно нужно сделать в Weeek.",
            reply_markup=cancel_keyboard(),
        )
        return WEEEK_CAPTURE if return_state else ConversationHandler.END

    route = detect_top_level_route(raw_text)
    content_text = strip_weeek_routing_metadata(clean_text, route)
    if not content_text:
        await update.message.reply_text(
            "Поймал запрос на Weeek, но сама задача пустая. Напиши одной строкой, что нужно сделать.",
            reply_markup=cancel_keyboard(),
        )
        return WEEEK_CAPTURE if return_state else ConversationHandler.END

    capture = await classify_capture(update, content_text, forced_type="task")
    capture = sanitize_weeek_capture_content(capture, route, fallback_text=content_text)
    capture["transcript"] = raw_text
    capture["transcript_clean"] = capture.get("transcript_clean") or content_text
    capture["speech_engine"] = context.user_data.get("_last_speech_engine", "")

    draft = _get_weeek_draft(context)
    draft.clear()
    draft.update(
        {
            "capture": capture,
            "target": route.get("target") or capture.get("target") or "weeek_task",
            "board_name_candidate": route.get("board_name_candidate") or capture.get("board_name_candidate") or "",
            "column_hint": route.get("column_hint") or capture.get("column_hint") or normalize_weeek_column_hint(raw_text),
            "parent_task_candidate": route.get("parent_task_candidate") or capture.get("parent_task_candidate") or "",
        }
    )
    auto_project = route.get("project") or match_weeek_target(capture.get("project_name_candidate") or "")
    if auto_project:
        draft.update(auto_project)

    _set_active_flow(context, "weeek_capture")
    _log_event(
        "weeek_capture_started",
        transcript=raw_text,
        transcript_clean=content_text,
        target=draft.get("target", ""),
        project_candidate=(auto_project or {}).get("project_name", ""),
        column_hint=draft.get("column_hint", ""),
        speech_engine=capture.get("speech_engine", ""),
    )

    if auto_project:
        next_state = await _send_weeek_board_picker(update.message, context)
    else:
        next_state = await _send_weeek_project_picker(update.message, context)
    return next_state if return_state else ConversationHandler.END


async def _transcribe_or_warn(update: Update, context: ContextTypes.DEFAULT_TYPE, failure_reply_markup=None):
    failure_reply_markup = failure_reply_markup or cancel_keyboard()
    if not voice_available():
        await update.message.reply_text(
            "🎤 Голосовой ввод сейчас не настроен. Напиши текстом, пожалуйста.",
            reply_markup=failure_reply_markup,
        )
        return None

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    voice = update.message.voice or update.message.audio
    if not voice:
        return None

    try:
        file = await voice.get_file()
        result = await asyncio.wait_for(transcribe_voice(file, update=update), timeout=45)
    except asyncio.TimeoutError:
        logger.exception("Voice transcription timed out")
        await update.message.reply_text(
            transcription_error_text("transcription_timeout"),
            reply_markup=failure_reply_markup,
        )
        return None
    except Exception:
        logger.exception("Не удалось распознать голосовое")
        await update.message.reply_text(
            "Не удалось распознать голосовое. Попробуй ещё раз или введи текстом.",
            reply_markup=failure_reply_markup,
        )
        return None

    if isinstance(result, dict):
        text = result.get("text")
        speech_engine = result.get("engine", "")
        error = result.get("error", "")
    else:
        text = result
        speech_engine = ""
        error = ""
    context.user_data["_last_speech_engine"] = speech_engine

    if not text:
        message = transcription_error_text(error) if error else "Голос распознался как пустой. Попробуй ещё раз или введи текстом."
        await update.message.reply_text(message, reply_markup=failure_reply_markup)
        return None

    logger.info("voice_transcribed raw_transcript=%r speech_engine=%s", text, speech_engine)
    return text


async def _maybe_override_with_weeek_voice(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, previous_state: str):
    route = detect_top_level_route(text)
    _log_event(
        "voice_route_detected",
        previous_state=previous_state,
        active_flow=context.user_data.get("_active_flow", ""),
        new_target=route.get("target", ""),
        project=((route.get("project") or {}).get("project_name") or ""),
        column_hint=route.get("column_hint", ""),
    )
    if route.get("target") not in {"weeek_task", "weeek_subtask"}:
        return None
    _log_event(
        "voice_route_override_previous_state",
        previous_state=previous_state,
        new_target=route.get("target", ""),
        transcript=text,
    )
    _reset_transient_flow_state(context)
    return await _start_weeek_capture(update, context, text, return_state=False)


async def add_brief_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context, failure_reply_markup=main_menu_keyboard())
    if text is None:
        return BRIEF
    override = await _maybe_override_with_weeek_voice(update, context, text, "idea_brief")
    if override is not None:
        return override
    return await _handle_brief_text(update, context, text)


async def add_details_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context)
    if text is None:
        return DETAILS
    override = await _maybe_override_with_weeek_voice(update, context, text, "idea_details")
    if override is not None:
        return override
    return await _handle_details_text(update, context, text)


async def task_add_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context)
    if text is None:
        return TASK_TEXT
    override = await _maybe_override_with_weeek_voice(update, context, text, "task_text")
    if override is not None:
        return override
    speech_engine = context.user_data.pop("_last_speech_engine", "")
    return await _handle_task_text(update, context, text, transcript=text, speech_engine=speech_engine)


async def weeek_capture_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context, failure_reply_markup=cancel_keyboard())
    if not text:
        return WEEEK_CAPTURE
    return await _start_weeek_capture(update, context, text, return_state=True)


async def weeek_preview_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return WEEEK_PREVIEW
    parsed = parse_weeek_edit_request(text)
    if not parsed:
        await update.message.reply_text(
            "Сейчас открыт черновик Weeek. Нажми «Редактировать» или напиши команду вроде: отредактируй описание ...",
            reply_markup=weeek_preview_keyboard(_get_weeek_draft(context)),
        )
        return WEEEK_PREVIEW
    return await _apply_weeek_edit_from_text(update.message, context, text)


async def weeek_preview_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context, failure_reply_markup=weeek_preview_keyboard(_get_weeek_draft(context)))
    if not text:
        return WEEEK_PREVIEW
    return await _apply_weeek_edit_from_text(update.message, context, text)


async def weeek_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return WEEEK_EDIT
    return await _apply_weeek_edit_from_text(update.message, context, text)


async def weeek_edit_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context, failure_reply_markup=weeek_edit_keyboard())
    if not text:
        return WEEEK_EDIT
    return await _apply_weeek_edit_from_text(update.message, context, text)


async def clarify_reminder_time_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _transcribe_or_warn(update, context)
    if text is None:
        return CLARIFY_REMINDER_TIME
    override = await _maybe_override_with_weeek_voice(update, context, text, "clarify_reminder_time")
    if override is not None:
        return override
    logger.info("reminder_time_clarification_voice raw_transcript=%r speech_engine=%s", text, context.user_data.get("_last_speech_engine", ""))
    return await _handle_clarified_time_text(update, context, text)


async def classify_capture(update: Update, text: str, forced_type: str | None = None) -> dict:
    user_id = update.effective_user.id
    default_timezone = effective_user_timezone(user_id)
    _log_event("capture_gpt_parse_started", model=OPENAI_PARSE_MODEL, forced_type=forced_type or "", transcript=text)
    parsed = await parse_capture_with_gpt(text, default_timezone)
    if parsed:
        _record_capture_parse_status("gpt")
        capture = parsed
        _log_event(
            "capture_gpt_parse_succeeded",
            model=OPENAI_PARSE_MODEL,
            target=capture.get("target", ""),
            project_candidate=capture.get("project_name_candidate", ""),
            column_hint=capture.get("column_hint", ""),
            source=capture.get("source", ""),
        )
    else:
        _record_capture_parse_status("rules")
        capture = classify_capture_text(text)
        _log_event(
            "capture_gpt_parse_failed",
            model=OPENAI_PARSE_MODEL,
            target=capture.get("target", ""),
            project_candidate=capture.get("project_name_candidate", ""),
            column_hint=capture.get("column_hint", ""),
            source="rules",
        )

    if forced_type:
        capture["type"] = forced_type

    timezone = capture.get("timezone") or default_timezone
    due_time = capture.get("due_time", "")
    due_date = capture.get("due_date", "")
    if not due_time:
        rule_due_time = extract_capture_due_time(text)
        if rule_due_time:
            due_time = rule_due_time
    if due_time and not due_date:
        due_date = resolve_capture_due_date(text, timezone, due_time)
    if due_date and not due_time and capture["type"] == "reminder":
        capture["needs_time_clarification"] = True
        capture["clarification_reason"] = capture.get("clarification_reason") or "не хватает точного времени"
    capture["due_time"] = due_time
    capture["due_date"] = due_date
    capture["timezone"] = timezone
    return capture


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
        return ConversationHandler.END

    edit_result = await _maybe_handle_weeek_live_edit(update, context, text, "voice_top_level")
    if edit_result is not None:
        return edit_result

    _clear_active_flow(context)

    route = detect_top_level_route(text)
    _log_event(
        "voice_route_detected",
        previous_state="top_level",
        active_flow=context.user_data.get("_active_flow", ""),
        new_target=route.get("target", ""),
        project=((route.get("project") or {}).get("project_name") or ""),
        column_hint=route.get("column_hint", ""),
    )
    if route.get("target") in {"weeek_task", "weeek_subtask"}:
        return await _start_weeek_capture(update, context, text, return_state=False)

    user_id = update.effective_user.id
    db.upsert_user(user_id)
    forced_type = "task" if route.get("target") == "local_task" else "reminder" if route.get("target") == "reminder" else None
    capture = await classify_capture(update, text, forced_type=forced_type)
    capture_type = capture["type"]
    speech_engine = context.user_data.pop("_last_speech_engine", "")
    capture["speech_engine"] = speech_engine
    capture["transcript"] = text
    capture["transcript_clean"] = capture.get("transcript_clean") or text
    _log_voice_capture(text, capture, speech_engine)

    if capture_type == "idea":
        _clear_active_flow(context)
        return await vik_only_disabled(update, context)

    if _needs_reminder_time_clarification(capture):
        context.user_data["pending_reminder_capture"] = capture
        context.user_data["pending_reminder_attempts"] = 0
        _set_active_flow(context, "clarify_reminder_time")
        await _ask_reminder_time_clarification(update, context)
        return CLARIFY_REMINDER_TIME

    if capture_type == "task":
        return await _start_weeek_capture(update, context, text, return_state=False)

    if capture_type == "reminder":
        task_id = _create_task_from_capture(user_id, capture)
        await _send_created_task(update, capture, task_id, recognized_text=text, reminder=capture_type == "reminder")
        _clear_active_flow(context)
        return ConversationHandler.END

    brief = capture["title"]
    details = capture["body"]
    if _openai_client and capture.get("source") != "gpt":
        parsed = await parse_idea_with_gpt(text)
        if parsed:
            brief, details = parsed

    if not brief:
        first = text.split(".")[0].strip() or text.strip()
        brief = first[:120]
        details = text.strip()

    idea_id = db.add_idea(user_id, brief, details)
    schedule_user_reminders(context.application, user_id)
    await update.message.reply_html(
        f"✅ Идея сохранена!\n\n{brief_message(brief)}",
        reply_markup=main_menu_keyboard(),
    )
    if details and details.casefold() != brief.casefold():
        await update.message.reply_html(f"<b>Описание:</b> {html.escape(details)}", reply_markup=main_menu_keyboard())
    transcription = transcription_message(text, speech_engine)
    if transcription:
        await update.message.reply_html(transcription.lstrip(), reply_markup=main_menu_keyboard())
    await update.message.reply_text("Что дальше?", reply_markup=post_save_keyboard(idea_id))
    _clear_active_flow(context)
    return ConversationHandler.END


async def weeek_project_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _get_weeek_draft(context)
    if not draft:
        await query.answer("Черновик задачи Weeek потерян. Начни заново.", show_alert=True)
        return ConversationHandler.END
    project_id = (query.data or "").split(":", 1)[1]
    project = next((item for item in draft.get("projects", []) if item["id"] == project_id), None)
    if not project:
        await query.answer("Проект устарел, выбери ещё раз.", show_alert=True)
        return await _send_weeek_project_picker(query.message, context)
    draft["project_id"] = project["id"]
    draft["project_name"] = project["name"]
    for key in ("board_id", "board_name", "boards", "columns", "column_id", "column_name", "parent_tasks", "parent_task_id", "parent_task_name"):
        draft.pop(key, None)
    _log_event("weeek_project_selected", project_id=project["id"], project_name=project["name"])
    return await _send_weeek_board_picker(query.message, context)


async def weeek_board_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _get_weeek_draft(context)
    if not draft:
        await query.answer("Черновик задачи Weeek потерян. Начни заново.", show_alert=True)
        return ConversationHandler.END
    board_id = (query.data or "").split(":", 1)[1]
    board = next((item for item in draft.get("boards", []) if item["id"] == board_id), None)
    if not board:
        await query.answer("Доска устарела, выбери ещё раз.", show_alert=True)
        return await _send_weeek_board_picker(query.message, context)
    draft["board_id"] = board["id"]
    draft["board_name"] = board["name"]
    _log_event("weeek_board_selected", board_id=board["id"], board_name=board["name"])
    if draft.get("target") == "weeek_subtask":
        return await _send_weeek_parent_picker(query.message, context)
    return await _send_weeek_column_picker(query.message, context)


async def weeek_column_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _get_weeek_draft(context)
    if not draft:
        await query.answer("Черновик задачи Weeek потерян. Начни заново.", show_alert=True)
        return ConversationHandler.END
    column_id = (query.data or "").split(":", 1)[1]
    column = next((item for item in draft.get("columns", []) if item["id"] == column_id), None)
    if not column:
        await query.answer("Колонка устарела, выбери ещё раз.", show_alert=True)
        return await _send_weeek_column_picker(query.message, context)
    draft["column_id"] = column["id"]
    draft["column_name"] = column["name"]
    _log_event("weeek_column_selected", column_id=column["id"], column_name=column["name"])
    return await _show_weeek_preview(query.message, context)


async def weeek_parent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _get_weeek_draft(context)
    if not draft:
        await query.answer("Черновик задачи Weeek потерян. Начни заново.", show_alert=True)
        return ConversationHandler.END
    parent_id = (query.data or "").split(":", 1)[1]
    parent = next((item for item in draft.get("parent_tasks", []) if item["id"] == parent_id), None)
    if not parent:
        await query.answer("Родительская задача устарела. Выбери ещё раз.", show_alert=True)
        return await _send_weeek_parent_picker(query.message, context)
    draft["parent_task_id"] = parent["id"]
    draft["parent_task_name"] = parent["name"]
    draft["parent_task_match_source"] = "manual"
    _log_event("weeek_parent_selected", parent_task_id=parent["id"], parent_task_name=parent["name"])
    return await _send_weeek_column_picker(query.message, context)


async def weeek_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "weeek_cancel":
        _clear_weeek_draft(context)
        _clear_active_flow(context)
        await query.message.reply_text("Ок, отменил создание задачи в Weeek.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    if data == "weeek_repick_column":
        return await _send_weeek_column_picker(query.message, context)

    if data == "weeek_repick_parent":
        return await _send_weeek_parent_picker(query.message, context)

    if data != "weeek_create":
        return WEEEK_PREVIEW

    draft = _get_weeek_draft(context)
    if not draft:
        await query.answer("Черновик задачи Weeek потерян. Начни заново.", show_alert=True)
        return ConversationHandler.END
    capture = draft.get("capture") or {}
    client = get_weeek_client()
    _log_event(
        "weeek_task_create_started",
        target=draft.get("target", ""),
        project_id=draft.get("project_id", ""),
        board_id=draft.get("board_id", ""),
        column_id=draft.get("column_id", ""),
        title=build_weeek_task_title(capture),
    )
    try:
        if draft.get("target") == "weeek_subtask":
            response = await client.create_subtask(
                title=build_weeek_task_title(capture),
                description=_compact_spaces(capture.get("body") or ""),
                project_id=draft.get("project_id", ""),
                board_id=draft.get("board_id", ""),
                column_id=draft.get("column_id", ""),
                parent_task_id=draft.get("parent_task_id", ""),
                due_date=capture.get("due_date", ""),
                due_time=capture.get("due_time", ""),
                timezone_name=capture.get("timezone") or effective_user_timezone(query.from_user.id),
            )
        else:
            response = await client.create_task(
                title=build_weeek_task_title(capture),
                description=_compact_spaces(capture.get("body") or ""),
                project_id=draft.get("project_id", ""),
                board_id=draft.get("board_id", ""),
                column_id=draft.get("column_id", ""),
                due_date=capture.get("due_date", ""),
                due_time=capture.get("due_time", ""),
                timezone_name=capture.get("timezone") or effective_user_timezone(query.from_user.id),
            )
    except WeeekApiError as exc:
        _log_event("weeek_task_create_failed", error=str(exc))
        await query.message.reply_text(
            f"Не удалось создать задачу в Weeek: {exc}",
            reply_markup=main_menu_keyboard(),
        )
        _clear_weeek_draft(context)
        _clear_active_flow(context)
        return ConversationHandler.END

    title = build_weeek_task_title(capture)
    task_id = client.extract_task_display_id(response)
    reminder_shadow_id = _store_weeek_task_reminder_shadow(query.from_user.id, capture, draft, task_id)
    _log_event("weeek_task_create_succeeded", task_id=task_id, title=title)
    await query.message.reply_html(
        "✅ <b>Задача отправлена в Weeek</b>\n\n"
        f"<b>ID:</b> {html.escape(str(task_id))}\n"
        f"<b>Название:</b> {html.escape(title)}\n"
        f"<b>Проект:</b> {html.escape(draft.get('project_name', '—'))}\n"
        f"<b>Доска:</b> {html.escape(draft.get('board_name', '—'))}\n"
        f"<b>Колонка:</b> {html.escape(draft.get('column_name', '—'))}"
        + (
            f"\n<b>Напоминания:</b> в день задачи в 12:00 ({html.escape(format_timezone_label(capture.get('timezone') or effective_user_timezone(query.from_user.id)))})"
            if reminder_shadow_id and not capture.get("due_time")
            else f"\n<b>Напоминания:</b> за 30 и 15 минут до срока ({html.escape(format_timezone_label(capture.get('timezone') or effective_user_timezone(query.from_user.id)))})"
            if reminder_shadow_id and capture.get("due_time")
            else ""
        ),
        reply_markup=main_menu_keyboard(),
    )
    _clear_weeek_draft(context)
    _clear_active_flow(context)
    return ConversationHandler.END


async def text_top_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return ConversationHandler.END

    edit_result = await _maybe_handle_weeek_live_edit(update, context, text, "text_top_level")
    if edit_result is not None:
        return edit_result

    route = detect_top_level_route(text)
    if route.get("target") in {"weeek_task", "weeek_subtask"}:
        return await _start_weeek_capture(update, context, text, return_state=False)

    user_id = update.effective_user.id
    db.upsert_user(user_id)

    forced_type = "task" if route.get("target") == "local_task" else "reminder" if route.get("target") == "reminder" else None
    capture = await classify_capture(update, text, forced_type=forced_type)
    capture_type = capture["type"]
    logger.info(
        "text_capture raw_text=%r type=%s title=%r body=%r due_date=%s due_time=%s timezone=%s source=%s confidence=%.2f time_confidence=%.2f needs_time_clarification=%s",
        text,
        capture.get("type", ""),
        capture.get("title", ""),
        capture.get("body", ""),
        capture.get("due_date", ""),
        capture.get("due_time", ""),
        capture.get("timezone", ""),
        capture.get("source", ""),
        _coerce_confidence(capture.get("confidence"), 0.0),
        _coerce_confidence(capture.get("time_confidence"), 0.0),
        bool(capture.get("needs_time_clarification")),
    )

    if _needs_reminder_time_clarification(capture):
        context.user_data["pending_reminder_capture"] = capture
        context.user_data["pending_reminder_attempts"] = 0
        _set_active_flow(context, "clarify_reminder_time")
        await _ask_reminder_time_clarification(update, context)
        return CLARIFY_REMINDER_TIME

    if capture_type in {"task", "reminder"}:
        task_id = _create_task_from_capture(user_id, capture)
        await _send_created_task(update, capture, task_id, reminder=capture_type == "reminder")
        return ConversationHandler.END

    brief = capture["title"]
    details = capture["body"]
    if _openai_client and capture.get("source") != "gpt":
        parsed = await parse_idea_with_gpt(text)
        if parsed:
            brief, details = parsed

    if not brief:
        first = text.split(".")[0].strip() or text.strip()
        brief = first[:120]
        details = text.strip()

    idea_id = db.add_idea(user_id, brief, details)
    schedule_user_reminders(context.application, user_id)
    await update.message.reply_html(
        f"✅ Идея сохранена!\n\n{brief_message(brief)}",
        reply_markup=main_menu_keyboard(),
    )
    if details and details.casefold() != brief.casefold():
        await update.message.reply_html(f"<b>Описание:</b> {html.escape(details)}", reply_markup=main_menu_keyboard())
    await update.message.reply_text("Что дальше?", reply_markup=post_save_keyboard(idea_id))
    return ConversationHandler.END


_legacy_handle_callback = handle_callback


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    if data.startswith("weeek_list_project:"):
        return await weeek_list_project_callback(update, context)
    if data in {"weeek_list_back", "weeek_list_close"}:
        return await weeek_list_nav_callback(update, context)
    if data.startswith("weeek_project:"):
        return await weeek_project_callback(update, context)
    if data.startswith("weeek_board:"):
        return await weeek_board_callback(update, context)
    if data.startswith("weeek_parent:"):
        return await weeek_parent_callback(update, context)
    if data.startswith("weeek_column:"):
        return await weeek_column_callback(update, context)
    if data == "weeek_edit_back" or data.startswith("weeek_edit_field:"):
        return await weeek_edit_callback(update, context)
    if data.startswith("weeek_"):
        return await weeek_preview_callback(update, context)
    return await _legacy_handle_callback(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    schedule_user_reminders(context.application, user_id)

    tz = effective_user_timezone(user_id)
    text = (
        "👋 <b>Привет!</b> Сейчас Mira работает только с задачами в Weeek.\n\n"
        f"🌍 Часовой пояс: <b>{html.escape(tz)}</b>\n\n"
        f"Внизу оставлена одна кнопка: <b>{html.escape(BTN_LIST_WEEEK_TASKS)}</b>.\n"
        "Нажми её, чтобы выбрать проект и посмотреть список задач.\n\n"
        "Можно также просто написать или наговорить, какую задачу нужно добавить в Weeek."
    )
    await update.message.reply_html(text, reply_markup=main_menu_keyboard())


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if voice_available():
        voice_status = "включён — сначала OpenAI STT, при сбое Vosk fallback"
    else:
        voice_status = "выключен — команда /voice покажет, как включить"
    text = (
        "<b>Как это работает сейчас</b>\n\n"
        f"• <b>{html.escape(BTN_LIST_WEEEK_TASKS)}</b> — показывает проекты из Weeek. После выбора проекта бот покажет задачи по статусам.\n"
        "• Если просто написать или надиктовать задачу, Mira оформит её в Weeek.\n"
        "• Проект бот спросит, а колонку выберет автоматически: по умолчанию «К работе».\n"
        f"• <b>/voice</b> — показать текущий голосовой тракт (сейчас {voice_status})."
    )
    await update.message.reply_html(text, reply_markup=main_menu_keyboard())


async def voice_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if voice_available():
        text = (
            "🎤 <b>Голосовой ввод включён.</b>\n\n"
            f"🧠 Парсинг смысла: <b>{html.escape(OPENAI_PARSE_MODEL)}</b>.\n\n"
            "Mira сначала пробует OpenAI STT через официальный API, а при сбое переключается на локальный Vosk. "
            "Качество ниже Whisper, но для коротких фраз вполне приемлемо.\n\n"
            "Просто запиши голосовое прямо в чате:\n"
            "• Диктуй обычным языком, какую задачу нужно добавить в Weeek.\n"
            "• Если в задаче нет явного проекта, Mira спросит, куда её отправить.\n"
            "• После выбора проекта задача уйдёт в Weeek в колонку «К работе».\n"
        )
    else:
        text = (
            "🎤 <b>Голосовой ввод выключен.</b>\n\n"
            "Для голосового ввода нужен OpenAI API key, а локальный Vosk и ffmpeg остаются fallback-вариантом:\n\n"
            "• Установи зависимости из <code>requirements.txt</code>.\n"
            "• На сервере нужен ffmpeg: <code>sudo apt install ffmpeg</code>.\n"
            "• Vosk сам скачает русскую модель (~45 МБ) при первом голосовом.\n\n"
            "После установки перезапусти сервис бота."
        )
    text += "\n\n" + "\n".join(_voice_runtime_status_lines())
    await update.message.reply_html(text, reply_markup=main_menu_keyboard(), disable_web_page_preview=True)


async def vik_only_command_disabled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Сейчас Mira работает только как задачник Weeek. Напиши задачу голосом или текстом, либо нажми «Задачи ВИК».",
        reply_markup=main_menu_keyboard(),
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is not set. Copy .env.example to .env and fill it in.")

    db.init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
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

    weeek_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addweeek", weeek_add_start),
        ],
        states={
            WEEEK_CAPTURE: [
                MessageHandler(filters.VOICE | filters.AUDIO, weeek_capture_voice),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{BTN_CANCEL}$"),
                    weeek_capture_text,
                ),
            ],
            WEEEK_PROJECT: [
                CallbackQueryHandler(weeek_project_callback, pattern="^weeek_project:"),
                CallbackQueryHandler(weeek_preview_callback, pattern="^weeek_cancel$"),
            ],
            WEEEK_BOARD: [
                CallbackQueryHandler(weeek_board_callback, pattern="^weeek_board:"),
                CallbackQueryHandler(weeek_preview_callback, pattern="^weeek_cancel$"),
            ],
            WEEEK_PARENT: [
                CallbackQueryHandler(weeek_parent_callback, pattern="^weeek_parent:"),
                CallbackQueryHandler(weeek_preview_callback, pattern="^weeek_cancel$"),
            ],
            WEEEK_COLUMN: [
                CallbackQueryHandler(weeek_column_callback, pattern="^weeek_column:"),
                CallbackQueryHandler(weeek_preview_callback, pattern="^weeek_cancel$"),
            ],
            WEEEK_PREVIEW: [
                CallbackQueryHandler(weeek_preview_callback, pattern="^weeek_(create|repick_column|repick_parent|edit|cancel)$"),
                MessageHandler(filters.VOICE | filters.AUDIO, weeek_preview_voice),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{BTN_CANCEL}$"),
                    weeek_preview_text,
                ),
            ],
            WEEEK_EDIT: [
                CallbackQueryHandler(weeek_edit_callback, pattern="^weeek_edit_(field:.+|back)$"),
                CallbackQueryHandler(weeek_preview_callback, pattern="^weeek_cancel$"),
                MessageHandler(filters.VOICE | filters.AUDIO, weeek_edit_voice),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{BTN_CANCEL}$"),
                    weeek_edit_text,
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

    capture_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.VOICE | filters.AUDIO, voice_top_level),
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & ~filters.Regex(MENU_BUTTON_PATTERN),
                text_top_level,
            ),
        ],
        states={
            CLARIFY_REMINDER_TIME: [
                CallbackQueryHandler(clarify_reminder_time_callback, pattern="^clarify_(save_without_time|cancel)$"),
                MessageHandler(filters.VOICE | filters.AUDIO, clarify_reminder_time_voice),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{BTN_CANCEL}$"),
                    clarify_reminder_time_text,
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
    application.add_handler(CommandHandler("list", vik_only_command_disabled))
    application.add_handler(CommandHandler("test", vik_only_command_disabled))
    application.add_handler(CommandHandler("reminders", vik_only_command_disabled))
    application.add_handler(CommandHandler("settime", vik_only_command_disabled))
    application.add_handler(CommandHandler("voice", voice_instructions))
    application.add_handler(CommandHandler("tasks", vik_only_command_disabled))

    application.add_handler(add_conv)
    application.add_handler(task_conv)
    application.add_handler(weeek_conv)
    application.add_handler(add_reminder_conv)
    application.add_handler(capture_conv)

    # Кнопки главного меню (вне диалогов)
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_LIST_WEEEK_TASKS)}$"), weeek_list_start))
    application.add_handler(
        MessageHandler(
            filters.Regex("^(" + "|".join(re.escape(label) for label in LEGACY_MENU_BUTTONS + (BTN_ADD_WEEEK_TASK,)) + ")$"),
            legacy_menu_disabled,
        )
    )

    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
