import asyncio
import os
import sys
import types
import unittest


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return _Dummy()

    def __call__(self, *args, **kwargs):
        return _Dummy()

    def __or__(self, other):
        return self

    def __and__(self, other):
        return self

    def __invert__(self):
        return self


dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", dotenv)

httpx = types.ModuleType("httpx")
httpx.AsyncClient = _Dummy
sys.modules.setdefault("httpx", httpx)

telegram = types.ModuleType("telegram")
telegram.InlineKeyboardButton = _Dummy
telegram.InlineKeyboardMarkup = _Dummy
telegram.ReplyKeyboardMarkup = _Dummy
telegram.Update = _Dummy
sys.modules.setdefault("telegram", telegram)

telegram_constants = types.ModuleType("telegram.constants")
telegram_constants.ChatAction = _Dummy()
telegram_constants.ParseMode = _Dummy()
sys.modules.setdefault("telegram.constants", telegram_constants)

telegram_ext = types.ModuleType("telegram.ext")
for name in (
    "Application",
    "CallbackQueryHandler",
    "CommandHandler",
    "ConversationHandler",
    "MessageHandler",
):
    setattr(telegram_ext, name, _Dummy)
telegram_ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=_Dummy)
telegram_ext.ConversationHandler.END = -1
telegram_ext.filters = _Dummy()
sys.modules.setdefault("telegram.ext", telegram_ext)

import bot
import weeek_client


extract_capture_due_time = bot.extract_capture_due_time


class ExtractCaptureDueTimeTests(unittest.TestCase):
    def test_evening_ten_is_22(self):
        self.assertEqual(extract_capture_due_time("вечером в десять"), "22:00")
        self.assertEqual(extract_capture_due_time("сегодня вечером в десять"), "22:00")

    def test_morning_ten_is_10(self):
        self.assertEqual(extract_capture_due_time("завтра утром часов в десять"), "10:00")

    def test_afternoon_three_is_15(self):
        self.assertEqual(extract_capture_due_time("послезавтра где-то в три дня"), "15:00")

    def test_night_three_is_03(self):
        self.assertEqual(extract_capture_due_time("в три ночи"), "03:00")

    def test_midnight_and_noon_twelve(self):
        self.assertEqual(extract_capture_due_time("в двенадцать ночи"), "00:00")
        self.assertEqual(extract_capture_due_time("в двенадцать дня"), "12:00")


class CaptureTitleFallbackTests(unittest.TestCase):
    def test_noisy_dentist_task_gets_human_title(self):
        capture = bot.classify_capture_text(
            "Бот, привет, смотри, мне, короче, нужно записать задачу "
            "на завтра в 11 утра, что мне нужно сходить к стоматологу."
        )

        self.assertEqual(capture["type"], "task")
        self.assertEqual(capture["title"], "Сходить к стоматологу")
        self.assertEqual(capture["due_time"], "11:00")

    def test_broken_noisy_title_does_not_keep_filler(self):
        capture = bot.classify_capture_text("Смотри, мне, короче, записать на, сходить")

        self.assertEqual(capture["type"], "task")
        self.assertEqual(capture["title"], "Сходить")
        self.assertNotIn(",", capture["title"])

    def test_future_iphone_idea_gets_meaningful_title(self):
        capture = bot.classify_capture_text(
            "Чат, запиши идею на... просто запиши идею, "
            "что нужно купить iPhone в будущем. Запиши эту идею."
        )

        self.assertEqual(capture["type"], "idea")
        self.assertEqual(capture["title"], "Идея покупки iPhone в будущем")

    def test_future_iphone_idea_normalizes_russian_word(self):
        capture = bot.classify_capture_text(
            "Бот, запиши идею насчёт того, что нужно купить айфон в будущем"
        )

        self.assertEqual(capture["type"], "idea")
        self.assertEqual(capture["title"], "Идея покупки iPhone в будущем")


class _FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=self.content))]
        )


class _FakeReplyMessage:
    def __init__(self):
        self.reply_text_calls = []
        self.reply_html_calls = []

    async def reply_text(self, text, reply_markup=None):
        self.reply_text_calls.append({"text": text, "reply_markup": reply_markup})

    async def reply_html(self, text, reply_markup=None):
        self.reply_html_calls.append({"text": text, "reply_markup": reply_markup})


class _FakeContext:
    def __init__(self):
        self.user_data = {}


class _FakeUpdate:
    def __init__(self, user_id=123):
        self.effective_user = types.SimpleNamespace(id=user_id)
        self.message = _FakeReplyMessage()


class _FakeAppContext(_FakeContext):
    def __init__(self):
        super().__init__()
        self.application = object()


class GptJsonParsingTests(unittest.TestCase):
    def _parse_with_fake_client(self, content):
        completions = _FakeCompletions(content)
        old_client = bot._openai_client
        bot._openai_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )
        try:
            result = asyncio.run(bot.parse_capture_with_gpt("Сходить к стоматологу", "Asia/Omsk"))
        finally:
            bot._openai_client = old_client
        return result, completions.kwargs

    def test_official_request_uses_json_response_format(self):
        result, kwargs = self._parse_with_fake_client(
            '{"type":"task","title":"Сходить к стоматологу","body":"Сходить к стоматологу","due_date":"","due_time":"","timezone":"","confidence":0.9,"time_confidence":1,"needs_time_clarification":false,"clarification_reason":""}'
        )

        self.assertEqual(result["title"], "Сходить к стоматологу")
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertNotIn("temperature", kwargs)

    def test_text_wrapped_json_is_parsed(self):
        result, _ = self._parse_with_fake_client(
            'Готово:\n{"type":"task","title":"Сходить к стоматологу","body":"Сходить к стоматологу","due_date":"","due_time":"","timezone":"","confidence":0.9,"time_confidence":1,"needs_time_clarification":false,"clarification_reason":""}'
        )

        self.assertEqual(result["source"], "gpt")
        self.assertEqual(result["title"], "Сходить к стоматологу")

    def test_bad_json_returns_none(self):
        result, _ = self._parse_with_fake_client("это не json")
        self.assertIsNone(result)


class DisplayDescriptionTests(unittest.TestCase):
    def test_gpt_description_is_shown_without_raw_recognition_label(self):
        message = bot.gpt_description_message(
            {
                "source": "gpt",
                "title": "Написать Екатерине Михайловне",
                "body": "Нужно сегодня в 12:00 написать Екатерине Михайловне.",
            }
        )

        self.assertIn("Описание:", message)
        self.assertIn("Екатерине Михайловне", message)
        self.assertNotIn("Распознано:", message)

    def test_rules_description_is_hidden(self):
        message = bot.gpt_description_message(
            {
                "source": "rules",
                "title": "Написать Екатерине Михайловне",
                "body": "вот привет запиши задачу по поводу того что нужно...",
            }
        )

        self.assertEqual(message, "")


class VoiceAndWeeekTests(unittest.TestCase):
    def test_openai_client_uses_single_official_key(self):
        self.assertFalse(hasattr(bot, "OPENAI_BASE_URL"))
        self.assertFalse(hasattr(bot, "OPENAI_STT_BASE_URL"))
        self.assertFalse(hasattr(bot, "OPENAI_STT_API_KEY"))

    def test_openai_client_kwargs_include_timeout_and_retries(self):
        kwargs = bot._openai_client_kwargs()

        self.assertEqual(kwargs["api_key"], bot.OPENAI_API_KEY)
        self.assertEqual(kwargs["timeout"], bot.OPENAI_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["max_retries"], bot.OPENAI_MAX_RETRIES)

    def test_official_request_kwargs_include_json_mode_without_temperature(self):
        kwargs = bot._chat_json_request_kwargs(
            "gpt-5.5",
            [{"role": "user", "content": "ping"}],
            0.1,
        )

        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("base_url", kwargs)

    def test_voice_available_accepts_openai_stt_without_vosk(self):
        old_stt_available = bot._openai_stt_available
        old_vosk_available = bot._vosk_available
        old_which = bot.shutil.which
        try:
            bot._openai_stt_available = lambda: True
            bot._vosk_available = lambda: False
            bot.shutil.which = lambda _name: None
            self.assertTrue(bot.voice_available())
        finally:
            bot._openai_stt_available = old_stt_available
            bot._vosk_available = old_vosk_available
            bot.shutil.which = old_which

    def test_voice_available_requires_vosk_and_ffmpeg_without_openai_stt(self):
        old_stt_available = bot._openai_stt_available
        old_vosk_available = bot._vosk_available
        old_which = bot.shutil.which
        try:
            bot._openai_stt_available = lambda: False
            bot._vosk_available = lambda: True
            bot.shutil.which = lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
            self.assertTrue(bot.voice_available())

            bot.shutil.which = lambda _name: None
            self.assertFalse(bot.voice_available())

            bot._vosk_available = lambda: False
            bot.shutil.which = lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
            self.assertFalse(bot.voice_available())
        finally:
            bot._openai_stt_available = old_stt_available
            bot._vosk_available = old_vosk_available
            bot.shutil.which = old_which

    def test_default_vosk_model_dir_is_project_data(self):
        old_path = os.environ.pop("VOSK_MODEL_PATH", None)
        old_isdir = bot.os.path.isdir
        try:
            bot.os.path.isdir = lambda path: False if path == "/data" else old_isdir(path)
            expected = os.path.join(os.path.dirname(os.path.abspath(bot.__file__)), "data", "vosk-model")
            self.assertEqual(bot._vosk_model_dir(), expected)
        finally:
            if old_path is not None:
                os.environ["VOSK_MODEL_PATH"] = old_path
            bot.os.path.isdir = old_isdir

    def test_transcribe_voice_prefers_openai_stt(self):
        class _FakeVoiceFile:
            async def download_to_drive(self, path):
                with open(path, "wb") as f:
                    f.write(b"fake audio")

        class _FakeSttTranscriptions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                return types.SimpleNamespace(text="openai transcript")

        transcriptions = _FakeSttTranscriptions()
        old_stt_client = bot._openai_stt_client
        old_stt_available = bot._openai_stt_available
        old_vosk_available = bot._vosk_available
        old_convert = bot._convert_audio_for_openai_stt
        bot._openai_stt_client = types.SimpleNamespace(
            audio=types.SimpleNamespace(transcriptions=transcriptions)
        )
        bot._openai_stt_available = lambda: True
        bot._vosk_available = lambda: False
        bot._convert_audio_for_openai_stt = lambda path: path
        try:
            result = asyncio.run(bot.transcribe_voice(_FakeVoiceFile()))
        finally:
            bot._openai_stt_client = old_stt_client
            bot._openai_stt_available = old_stt_available
            bot._vosk_available = old_vosk_available
            bot._convert_audio_for_openai_stt = old_convert

        self.assertEqual(transcriptions.calls, 1)
        self.assertEqual(result["text"], "openai transcript")
        self.assertEqual(result["engine"], bot._openai_stt_engine_label())

    def test_voice_status_summary_does_not_reference_custom_endpoint(self):
        old_stt_available = bot._openai_stt_available
        try:
            bot._openai_stt_available = lambda: True
            summary = bot._voice_status_summary()
        finally:
            bot._openai_stt_available = old_stt_available

        self.assertIn("OpenAI STT", summary)
        self.assertNotIn("codex.sale", summary)
        self.assertNotIn("OPENAI_BASE_URL", summary)
        self.assertNotIn("OPENAI_STT_BASE_URL", summary)
        self.assertNotIn("OPENAI_STT_API_KEY", summary)

    def test_transcribe_voice_falls_back_to_vosk_after_openai_failure(self):
        class _FakeVoiceFile:
            async def download_to_drive(self, path):
                with open(path, "wb") as f:
                    f.write(b"fake audio")

        class _BrokenSttTranscriptions:
            def create(self, **kwargs):
                raise RuntimeError("upstream temporarily unavailable")

        old_stt_client = bot._openai_stt_client
        old_stt_available = bot._openai_stt_available
        old_vosk_available = bot._vosk_available
        old_which = bot.shutil.which
        old_get_vosk_model = bot._get_vosk_model
        old_vosk_transcribe_file = bot._vosk_transcribe_file
        old_convert = bot._convert_audio_for_openai_stt
        bot._openai_stt_client = types.SimpleNamespace(
            audio=types.SimpleNamespace(transcriptions=_BrokenSttTranscriptions())
        )
        bot._openai_stt_available = lambda: True
        bot._vosk_available = lambda: True
        bot.shutil.which = lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
        bot._get_vosk_model = lambda: object()
        bot._vosk_transcribe_file = lambda _path: "vosk transcript"
        bot._convert_audio_for_openai_stt = lambda path: path
        try:
            result = asyncio.run(bot.transcribe_voice(_FakeVoiceFile()))
        finally:
            bot._openai_stt_client = old_stt_client
            bot._openai_stt_available = old_stt_available
            bot._vosk_available = old_vosk_available
            bot.shutil.which = old_which
            bot._get_vosk_model = old_get_vosk_model
            bot._vosk_transcribe_file = old_vosk_transcribe_file
            bot._convert_audio_for_openai_stt = old_convert

        self.assertEqual(result["text"], "vosk transcript")
        self.assertEqual(result["engine"], "vosk")

    def test_weeek_request_detection_and_prefix_strip(self):
        text = "Бот, добавь задачу в ВИК: написать Кате по смете"
        self.assertTrue(bot.is_weeek_request(text))
        self.assertEqual(bot.strip_weeek_request_prefix(text), "написать Кате по смете")

    def test_weeek_column_hint_normalization(self):
        self.assertEqual(bot.normalize_weeek_column_hint("поставь это к работе"), "to_work")
        self.assertEqual(bot.normalize_weeek_column_hint("эту задачу давай в работу"), "in_work")

    def test_weeek_title_adds_see_description_suffix(self):
        title = bot.build_weeek_task_title(
            {
                "title": "Подготовить оффер",
                "body": "Подготовить оффер для застройщиков и отдельно описать условия запуска.",
            }
        )
        self.assertEqual(title, "Подготовить оффер — см. описание")


    def test_weeek_done_column_is_filtered_from_picker(self):
        columns = [
            {"id": "1", "name": "К работе", "raw": {}},
            {"id": "2", "name": "В работе", "raw": {}},
            {"id": "3", "name": "Готово", "raw": {}},
        ]

        filtered = bot._sort_weeek_columns(columns, "")

        self.assertEqual([item["name"] for item in filtered], ["В работе", "К работе"])


class WeeekFlowResilienceTests(unittest.TestCase):
    def test_weeek_project_picker_replies_on_api_error(self):
        async def _raise_projects():
            raise weeek_client.WeeekApiError("Request timed out")

        message = _FakeReplyMessage()
        context = _FakeContext()
        context.user_data["weeek_draft"] = {"capture": {"title": "Buy milk"}}
        context.user_data["_active_flow"] = "weeek_capture"
        old_get_client = bot.get_weeek_client
        bot.get_weeek_client = lambda: types.SimpleNamespace(list_projects=_raise_projects)
        try:
            result = asyncio.run(bot._send_weeek_project_picker(message, context))
        finally:
            bot.get_weeek_client = old_get_client

        self.assertEqual(result, bot.ConversationHandler.END)
        self.assertEqual(len(message.reply_text_calls), 1)
        self.assertIn("Не удалось связаться с Weeek", message.reply_text_calls[0]["text"])
        self.assertNotIn("weeek_draft", context.user_data)
        self.assertNotIn("_active_flow", context.user_data)


class WeeekBrowserOverviewTests(unittest.TestCase):
    def test_format_overview_groups_tasks_by_status(self):
        tasks = [
            {"id": "101", "name": "Write offer", "raw": {"boardColumnId": "1", "number": 101}},
            {"id": "102", "name": "Call client", "raw": {"boardColumnId": "2", "number": 102}},
            {"id": "103", "name": "Archive notes", "raw": {"boardColumnId": "3", "number": 103}},
        ]
        columns_by_id = {
            "1": {"id": "1", "name": "К работе", "raw": {}},
            "2": {"id": "2", "name": "В работе", "raw": {}},
            "3": {"id": "3", "name": "Готово", "raw": {}},
        }

        message = bot._format_weeek_tasks_overview("Личное", tasks, columns_by_id)

        self.assertIn("Личное", message)
        self.assertIn("К работе", message)
        self.assertIn("В работе", message)
        self.assertIn("Готово", message)
        self.assertIn("#101 Write offer", message)
        self.assertLess(message.index("К работе"), message.index("В работе"))
        self.assertLess(message.index("В работе"), message.index("Готово"))


class WeeekColumnSelectionTests(unittest.TestCase):
    def test_send_weeek_column_picker_auto_selects_to_work(self):
        async def _list_columns(_board_id):
            return [
                types.SimpleNamespace(id="2", name="В работе", raw={}),
                types.SimpleNamespace(id="1", name="К работе", raw={}),
                types.SimpleNamespace(id="3", name="Готово", raw={}),
            ]

        old_get_client = bot.get_weeek_client
        old_show_preview = bot._show_weeek_preview
        message = _FakeReplyMessage()
        context = _FakeContext()
        context.user_data["weeek_draft"] = {"board_id": "10", "column_hint": ""}
        preview_calls = []

        async def _fake_show_preview(_message, _context):
            preview_calls.append(True)
            return bot.WEEEK_PREVIEW

        bot.get_weeek_client = lambda: types.SimpleNamespace(list_columns=_list_columns)
        bot._show_weeek_preview = _fake_show_preview
        try:
            result = asyncio.run(bot._send_weeek_column_picker(message, context))
        finally:
            bot.get_weeek_client = old_get_client
            bot._show_weeek_preview = old_show_preview

        draft = context.user_data["weeek_draft"]
        self.assertEqual(result, bot.WEEEK_PREVIEW)
        self.assertEqual(draft["column_id"], "1")
        self.assertEqual(draft["column_name"], "К работе")
        self.assertEqual(len(preview_calls), 1)
        self.assertEqual(message.reply_text_calls, [])


class RouteAndSubtaskTests(unittest.TestCase):
    def test_transcription_message_shows_fallback_label(self):
        old_stt_available = bot._openai_stt_available
        try:
            bot._openai_stt_available = lambda: True
            message = bot.transcription_message("сходить к Комарову", "vosk")
        finally:
            bot._openai_stt_available = old_stt_available

        self.assertIn("fallback", message)
        self.assertIn("Комарову", message)

    def test_detect_top_level_route_weeek_task(self):
        route = bot.detect_top_level_route("добавь задачу в ВИК в личное к работе написать Кате")
        self.assertEqual(route["target"], "weeek_task")
        self.assertEqual(route["project"]["project_id"], "6")
        self.assertEqual(route["column_hint"], "to_work")

    def test_detect_top_level_route_weeek_subtask(self):
        route = bot.detect_top_level_route('в Vibecoding добавь подзадачу к задаче "Разобраться, как парсить аудиторию"')
        self.assertEqual(route["target"], "weeek_subtask")
        self.assertEqual(route["project"]["project_id"], "5")
        self.assertIn("разобраться", route["parent_task_candidate"].lower())

    def test_detect_top_level_route_plain_task_defaults_to_weeek(self):
        route = bot.detect_top_level_route("добавь задачу завтра в 10 написать Кате")
        self.assertEqual(route["target"], "weeek_task")

    def test_weeek_routing_metadata_does_not_leak_into_task_content(self):
        raw_text = "Мира, запиши задачу в личное, что нужно написать Мише завтра в 15:30 и поставь статус к работе"
        route = bot.detect_top_level_route(raw_text)
        content_text = bot.strip_weeek_routing_metadata(bot.strip_weeek_request_prefix(raw_text), route)
        capture = bot.classify_capture_text(content_text)
        capture = bot.sanitize_weeek_capture_content(capture, route, fallback_text=content_text)
        polluted_capture = bot.sanitize_weeek_capture_content(
            {
                "type": "task",
                "title": "Написать Мише",
                "body": "Нужно написать Мише. Раздел: личное, статус: к работе.",
            },
            route,
            fallback_text=content_text,
        )

        self.assertEqual(route["project"]["project_name"], "Личное")
        self.assertEqual(route["column_hint"], "to_work")
        self.assertEqual(capture["title"], "Написать Мише")
        self.assertNotIn("личное", capture["body"].lower())
        self.assertNotIn("к работе", capture["body"].lower())
        self.assertNotIn("статус", capture["body"].lower())
        self.assertEqual(polluted_capture["body"], "Нужно написать Мише")

    def test_plain_phrase_keeps_useful_content(self):
        raw_text = "написать Мише завтра в 15:30"
        route = bot.detect_top_level_route(raw_text)
        content_text = bot.strip_weeek_routing_metadata(raw_text, route)
        capture = bot.classify_capture_text(content_text)
        capture = bot.sanitize_weeek_capture_content(capture, route, fallback_text=content_text)

        self.assertEqual(content_text, raw_text)
        self.assertIn("Мише", capture["title"])
        self.assertIn("Мише", capture["body"])

    def test_match_parent_task_exact_and_fuzzy(self):
        tasks = [
            {"id": "1", "name": "Разобраться, как парсить аудиторию"},
            {"id": "2", "name": "Добавить план быстрого теста"},
        ]
        exact, source_exact = bot._match_parent_task(tasks, "Разобраться, как парсить аудиторию")
        fuzzy, source_fuzzy = bot._match_parent_task(tasks, "разобраться как парсить аудиторию")
        self.assertEqual(exact["id"], "1")
        self.assertEqual(source_exact, "exact")
        self.assertEqual(fuzzy["id"], "1")
        self.assertIn(source_fuzzy, {"normalized", "fuzzy"})


class WeeekOnlyUxTests(unittest.TestCase):
    def test_start_text_mentions_only_weeek(self):
        update = _FakeUpdate()
        context = _FakeAppContext()
        old_upsert_user = bot.db.upsert_user
        old_schedule = bot.schedule_user_reminders
        old_timezone = bot.effective_user_timezone
        try:
            bot.db.upsert_user = lambda _user_id: None
            bot.schedule_user_reminders = lambda _app, _user_id: None
            bot.effective_user_timezone = lambda _user_id: "Asia/Omsk"
            asyncio.run(bot.start(update, context))
        finally:
            bot.db.upsert_user = old_upsert_user
            bot.schedule_user_reminders = old_schedule
            bot.effective_user_timezone = old_timezone

        self.assertEqual(len(update.message.reply_html_calls), 1)
        text = update.message.reply_html_calls[0]["text"]
        self.assertIn("Weeek", text)
        self.assertIn(bot.BTN_LIST_WEEEK_TASKS, text)
        self.assertNotIn("Идеи", text)
        self.assertNotIn("Напоминания", text)

    def test_help_text_describes_weeek_only_flow(self):
        update = _FakeUpdate()
        context = _FakeContext()
        old_voice_available = bot.voice_available
        try:
            bot.voice_available = lambda: True
            asyncio.run(bot.show_help(update, context))
        finally:
            bot.voice_available = old_voice_available

        self.assertEqual(len(update.message.reply_html_calls), 1)
        text = update.message.reply_html_calls[0]["text"]
        self.assertIn(bot.BTN_LIST_WEEEK_TASKS, text)
        self.assertIn("К работе", text)
        self.assertNotIn("Мои идеи", text)
        self.assertNotIn("Мои задачи", text)

    def test_legacy_command_disabled_redirects_to_weeek(self):
        update = _FakeUpdate()
        context = _FakeContext()

        asyncio.run(bot.vik_only_command_disabled(update, context))

        self.assertEqual(len(update.message.reply_text_calls), 1)
        text = update.message.reply_text_calls[0]["text"]
        self.assertIn("Weeek", text)
        self.assertIn("Задачи ВИК", text)


class WeeekClientTransportTests(unittest.TestCase):
    def test_request_bypasses_env_proxy_and_wraps_network_error(self):
        calls = {}

        class _Boom(Exception):
            pass

        class _FakeAsyncClient:
            def __init__(self, **kwargs):
                calls["kwargs"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def request(self, *args, **kwargs):
                raise _Boom("proxy timeout")

        old_httpx = weeek_client.httpx
        weeek_client.httpx = types.SimpleNamespace(AsyncClient=_FakeAsyncClient, HTTPError=_Boom)
        client = weeek_client.WeeekClient(api_token="token", base_url="https://api.weeek.net/public/v1")
        try:
            with self.assertRaises(weeek_client.WeeekApiError) as ctx:
                asyncio.run(client._request("GET", "/tm/projects"))
        finally:
            weeek_client.httpx = old_httpx

        self.assertEqual(calls["kwargs"]["timeout"], 30.0)
        self.assertFalse(calls["kwargs"]["trust_env"])
        self.assertIn("Weeek request failed", str(ctx.exception))


class WeeekPayloadTests(unittest.TestCase):
    def test_due_datetime_is_sent_without_conflicting_due_fields(self):
        client = weeek_client.WeeekClient(api_token="token", base_url="https://api.weeek.net/public/v1")

        payload = client._task_payload(
            title="Купить молоко",
            description="Нужно купить молоко.",
            board_id="10",
            column_id="20",
            project_id="6",
            due_date="2026-06-05",
            due_time="22:00",
            timezone_name="Asia/Omsk",
        )

        self.assertEqual(payload["dueDateTime"], "2026-06-05T16:00:00Z")
        self.assertNotIn("dueDate", payload)
        self.assertNotIn("dueTime", payload)
        self.assertNotIn("day", payload)

    def test_extract_task_display_id_uses_created_task_id(self):
        client = weeek_client.WeeekClient(api_token="token", base_url="https://api.weeek.net/public/v1")
        response = {
            "success": True,
            "task": {
                "id": 74,
                "title": "[CODEX PROBE] Inspect Weeek create response",
                "projectId": 6,
                "boardId": 10,
                "boardColumnId": 29,
            },
        }

        self.assertEqual(client.extract_task_display_id(response), "74")


class TaskReminderSchedulingTests(unittest.TestCase):
    def test_store_weeek_task_reminder_shadow_skips_undated_tasks(self):
        old_add_task = bot.db.add_task
        try:
            bot.db.add_task = lambda *args, **kwargs: self.fail("add_task should not be called")
            result = bot._store_weeek_task_reminder_shadow(
                1,
                {"title": "Купить молоко", "body": "Нужно купить молоко", "due_date": "", "due_time": "", "timezone": "Asia/Omsk"},
                {"project_name": "Личное"},
                "89",
            )
        finally:
            bot.db.add_task = old_add_task

        self.assertIsNone(result)

    def test_store_weeek_task_reminder_shadow_saves_due_task(self):
        calls = {}
        old_add_task = bot.db.add_task
        old_tz = bot.effective_user_timezone
        try:
            bot.db.add_task = lambda *args, **kwargs: calls.update({"args": args, "kwargs": kwargs}) or 55
            bot.effective_user_timezone = lambda _user_id: "Asia/Omsk"
            result = bot._store_weeek_task_reminder_shadow(
                1,
                {
                    "title": "Купить молоко",
                    "body": "Нужно купить молоко",
                    "due_date": "2026-06-08",
                    "due_time": "15:00",
                    "timezone": "Asia/Omsk",
                },
                {"project_name": "Личное"},
                "89",
            )
        finally:
            bot.db.add_task = old_add_task
            bot.effective_user_timezone = old_tz

        self.assertEqual(result, 55)
        self.assertEqual(calls["args"][0], 1)
        self.assertIn("[Weeek #89]", calls["args"][1])
        self.assertEqual(calls["kwargs"]["due_date"], "2026-06-08")
        self.assertEqual(calls["kwargs"]["due_time"], "15:00")
        self.assertIn("Проект Weeek: Личное", calls["kwargs"]["body"])

    def test_check_task_reminders_v2_sends_midday_reminder_for_date_only_task(self):
        sent = []
        marked = []
        old_get_due_tasks = bot.db.get_due_tasks
        old_mark = bot.db.mark_task_reminder_sent
        old_datetime = bot.datetime
        old_timezone = bot.effective_user_timezone
        try:
            bot.db.get_due_tasks = lambda: [
                (1, 7, "Купить молоко", 0, "Нужно купить молоко", "2026-06-08", "", "Asia/Omsk", "", "", "")
            ]
            bot.db.mark_task_reminder_sent = lambda task_id, user_id, kind: marked.append((task_id, user_id, kind))

            class _FixedDateTime(bot.datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 6, 8, 12, 0, tzinfo=tz)

            bot.datetime = _FixedDateTime
            bot.effective_user_timezone = lambda _user_id: "Asia/Omsk"
            fake_context = types.SimpleNamespace(
                bot=types.SimpleNamespace(
                    send_message=lambda **kwargs: sent.append(kwargs) or asyncio.sleep(0)
                )
            )
            asyncio.run(bot.check_task_reminders_v2(fake_context))
        finally:
            bot.db.get_due_tasks = old_get_due_tasks
            bot.db.mark_task_reminder_sent = old_mark
            bot.datetime = old_datetime
            bot.effective_user_timezone = old_timezone

        self.assertEqual(len(sent), 1)
        self.assertIn("Сегодня в 12:00", sent[0]["text"])
        self.assertEqual(marked, [(1, 7, "day")])

    def test_check_task_reminders_v2_sends_both_pre_deadline_reminders(self):
        sent = []
        marked = []
        old_get_due_tasks = bot.db.get_due_tasks
        old_mark = bot.db.mark_task_reminder_sent
        old_datetime = bot.datetime
        old_timezone = bot.effective_user_timezone
        try:
            bot.db.get_due_tasks = lambda: [
                (2, 7, "Созвон", 0, "Созвон с клиентом", "2026-06-08", "15:00", "Asia/Omsk", "", "", "")
            ]
            bot.db.mark_task_reminder_sent = lambda task_id, user_id, kind: marked.append((task_id, user_id, kind))

            class _FixedDateTime(bot.datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 6, 8, 14, 46, tzinfo=tz)

            bot.datetime = _FixedDateTime
            bot.effective_user_timezone = lambda _user_id: "Asia/Omsk"
            fake_context = types.SimpleNamespace(
                bot=types.SimpleNamespace(
                    send_message=lambda **kwargs: sent.append(kwargs) or asyncio.sleep(0)
                )
            )
            asyncio.run(bot.check_task_reminders_v2(fake_context))
        finally:
            bot.db.get_due_tasks = old_get_due_tasks
            bot.db.mark_task_reminder_sent = old_mark
            bot.datetime = old_datetime
            bot.effective_user_timezone = old_timezone

        self.assertEqual(len(sent), 2)
        self.assertIn("за 30 минут", sent[0]["text"])
        self.assertIn("за 15 минут", sent[1]["text"])
        self.assertEqual(marked, [(2, 7, "pre30"), (2, 7, "pre15")])


if __name__ == "__main__":
    unittest.main()
