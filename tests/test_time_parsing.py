import asyncio
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


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


class WeeekProjectCallbackTests(unittest.IsolatedAsyncioTestCase):
    class FakeTask:
        def __init__(self, coro=None):
            self.coro = coro
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True
            if self.coro:
                self.coro.close()

    async def test_project_callback_schedules_next_step_and_releases_conversation(self):
        scheduled = []

        class FakeApplication:
            def create_task(self, coro):
                task = WeeekProjectCallbackTests.FakeTask(coro)
                scheduled.append(task)
                return task

        class FakeQuery:
            id = "cb-1"
            data = "weeek_project:draft1:6"
            from_user = types.SimpleNamespace(id=123)
            message = types.SimpleNamespace(message_id=456)

            async def answer(self, text=None, **kwargs):
                return None

        update = types.SimpleNamespace(callback_query=FakeQuery())
        context = types.SimpleNamespace(
            user_data={
                "_active_flow": "weeek_capture",
                "weeek_draft": {
                    "draft_id": "draft1",
                    "projects": [{"id": "6", "name": "Personal"}],
                    "target": "weeek_task",
                },
            },
            application=FakeApplication(),
        )

        result = await bot.weeek_project_callback(update, context)

        self.assertEqual(result, bot.ConversationHandler.END)
        self.assertIsNone(context.user_data.get("_active_flow"))
        self.assertEqual(context.user_data["weeek_draft"]["project_id"], "6")
        self.assertEqual(len(scheduled), 1)
        scheduled[0].cancel()

    async def test_start_clears_active_weeek_state(self):
        pending = self.FakeTask()

        class FakeMessage:
            async def reply_html(self, *args, **kwargs):
                return None

        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=123),
            message=FakeMessage(),
        )
        context = types.SimpleNamespace(
            user_data={
                "_active_flow": "weeek_capture",
                "weeek_draft": {"draft_id": "draft1"},
                "_weeek_background_task": pending,
                "_weeek_background_draft_id": "draft1",
            },
            application=types.SimpleNamespace(),
        )

        with (
            patch.object(bot.db, "upsert_user"),
            patch.object(bot, "schedule_user_reminders"),
            patch.object(bot, "effective_user_timezone", return_value="Asia/Omsk"),
            patch.object(bot.db, "get_user_reminders", return_value=[]),
        ):
            await bot.start(update, context)

        self.assertNotIn("weeek_draft", context.user_data)
        self.assertNotIn("_active_flow", context.user_data)
        self.assertNotIn("_weeek_background_task", context.user_data)
        self.assertTrue(pending.cancelled)

    async def test_stale_project_callback_does_not_mutate_current_draft(self):
        class FakeQuery:
            id = "cb-stale-project"
            data = "weeek_project:old123:6"
            from_user = types.SimpleNamespace(id=123)
            message = types.SimpleNamespace(message_id=456)

            def __init__(self):
                self.answers = []

            async def answer(self, text=None, **kwargs):
                self.answers.append(text)

        query = FakeQuery()
        update = types.SimpleNamespace(callback_query=query)
        current_draft = {
            "draft_id": "new456",
            "projects": [{"id": "7", "name": "Current"}],
        }
        context = types.SimpleNamespace(
            user_data={"_active_flow": "weeek_capture", "weeek_draft": current_draft},
            application=types.SimpleNamespace(),
        )

        result = await bot.weeek_project_callback(update, context)

        self.assertEqual(result, bot.ConversationHandler.END)
        self.assertIs(context.user_data["weeek_draft"], current_draft)
        self.assertNotIn("project_id", current_draft)
        self.assertIn("Этот черновик уже устарел, начни заново.", query.answers)

    async def test_stale_cancel_does_not_clear_current_draft(self):
        class FakeQuery:
            id = "cb-stale-cancel"
            data = "weeek_cancel:old123"
            from_user = types.SimpleNamespace(id=123)
            message = types.SimpleNamespace(message_id=456)

            async def answer(self, text=None, **kwargs):
                return None

        current_draft = {"draft_id": "new456", "capture": {"title": "Current"}}
        context = types.SimpleNamespace(
            user_data={"_active_flow": "weeek_preview", "weeek_draft": current_draft},
            application=types.SimpleNamespace(),
        )

        result = await bot.weeek_preview_callback(
            types.SimpleNamespace(callback_query=FakeQuery()),
            context,
        )

        self.assertEqual(result, bot.ConversationHandler.END)
        self.assertIs(context.user_data["weeek_draft"], current_draft)
        self.assertEqual(context.user_data["_active_flow"], "weeek_preview")

    async def test_new_voice_clears_previous_unfinished_weeek_draft(self):
        pending = self.FakeTask()
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(),
        )
        context = types.SimpleNamespace(
            user_data={
                "_active_flow": "weeek_capture",
                "weeek_draft": {"draft_id": "draft1"},
                "_weeek_background_task": pending,
                "_weeek_background_draft_id": "draft1",
            },
            application=types.SimpleNamespace(),
        )

        with (
            patch.object(bot, "voice_available", return_value=True),
            patch.object(bot, "_transcribe_or_warn", new=AsyncMock(return_value=None)),
        ):
            result = await bot.voice_top_level(update, context)

        self.assertEqual(result, bot.ConversationHandler.END)
        self.assertNotIn("weeek_draft", context.user_data)
        self.assertNotIn("_active_flow", context.user_data)
        self.assertTrue(pending.cancelled)

    async def test_background_task_exits_when_draft_id_is_stale(self):
        context = types.SimpleNamespace(
            user_data={
                "weeek_draft": {
                    "draft_id": "new456",
                    "project_id": "6",
                    "project_name": "Current",
                }
            }
        )

        with patch.object(bot, "_send_weeek_column_picker", new=AsyncMock()) as picker:
            await bot._run_weeek_project_next_step(
                types.SimpleNamespace(),
                context,
                "cb-stale-background",
                "old123",
            )

        picker.assert_not_awaited()


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


class ParseCostModeTests(unittest.TestCase):
    def setUp(self):
        self.old_strategy = bot.OPENAI_PARSE_STRATEGY
        self.old_enabled = bot.OPENAI_PARSE_ENABLED
        self.old_client = bot._openai_client
        self.old_parse_capture = bot.parse_capture_with_gpt
        self.old_parse_idea = bot.parse_idea_with_gpt
        self.old_effective_tz = bot.effective_user_timezone
        bot.effective_user_timezone = lambda _user_id: "Asia/Omsk"

    def tearDown(self):
        bot.OPENAI_PARSE_STRATEGY = self.old_strategy
        bot.OPENAI_PARSE_ENABLED = self.old_enabled
        bot._openai_client = self.old_client
        bot.parse_capture_with_gpt = self.old_parse_capture
        bot.parse_idea_with_gpt = self.old_parse_idea
        bot.effective_user_timezone = self.old_effective_tz

    def _update(self):
        return types.SimpleNamespace(effective_user=types.SimpleNamespace(id=123))

    def test_gpt_first_keeps_old_order(self):
        calls = {"count": 0}

        async def fake_parse(text, default_timezone, feature="capture_parse"):
            calls["count"] += 1
            return {
                "type": "task",
                "title": "GPT title",
                "body": "GPT body",
                "due_date": "",
                "due_time": "",
                "timezone": "",
                "confidence": 0.9,
                "time_confidence": 1.0,
                "needs_time_clarification": False,
                "source": "gpt",
            }

        bot.OPENAI_PARSE_STRATEGY = "gpt_first"
        bot.OPENAI_PARSE_ENABLED = True
        bot._openai_client = object()
        bot.parse_capture_with_gpt = fake_parse

        capture = asyncio.run(bot.classify_capture(self._update(), "добавь задачу завтра в 10 написать Кате"))

        self.assertEqual(calls["count"], 1)
        self.assertEqual(capture["source"], "gpt")

    def test_rules_first_simple_task_skips_gpt(self):
        calls = {"count": 0}

        async def fake_parse(*args, **kwargs):
            calls["count"] += 1
            return None

        bot.OPENAI_PARSE_STRATEGY = "rules_first"
        bot.OPENAI_PARSE_ENABLED = True
        bot._openai_client = object()
        bot.parse_capture_with_gpt = fake_parse

        capture = asyncio.run(bot.classify_capture(self._update(), "добавь задачу завтра в 10 написать Кате"))

        self.assertEqual(calls["count"], 0)
        self.assertEqual(capture["source"], "rules")

    def test_rules_first_simple_idea_skips_gpt(self):
        calls = {"count": 0}

        async def fake_parse(*args, **kwargs):
            calls["count"] += 1
            return None

        bot.OPENAI_PARSE_STRATEGY = "rules_first"
        bot.OPENAI_PARSE_ENABLED = True
        bot._openai_client = object()
        bot.parse_capture_with_gpt = fake_parse

        capture = asyncio.run(bot.classify_capture(self._update(), "запиши идею про Telegram-бота для ЛПР"))

        self.assertEqual(calls["count"], 0)
        self.assertEqual(capture["type"], "idea")

    def test_rules_first_simple_reminder_with_time_skips_gpt(self):
        calls = {"count": 0}

        async def fake_parse(*args, **kwargs):
            calls["count"] += 1
            return None

        bot.OPENAI_PARSE_STRATEGY = "rules_first"
        bot.OPENAI_PARSE_ENABLED = True
        bot._openai_client = object()
        bot.parse_capture_with_gpt = fake_parse

        capture = asyncio.run(bot.classify_capture(self._update(), "напомни сегодня вечером проверить задачи"))

        self.assertEqual(calls["count"], 0)
        self.assertEqual(capture["type"], "reminder")

    def test_rules_first_ambiguous_reminder_may_call_gpt(self):
        calls = {"count": 0}

        async def fake_parse(*args, **kwargs):
            calls["count"] += 1
            return None

        bot.OPENAI_PARSE_STRATEGY = "rules_first"
        bot.OPENAI_PARSE_ENABLED = True
        bot._openai_client = object()
        bot.parse_capture_with_gpt = fake_parse

        asyncio.run(bot.classify_capture(self._update(), "напомни проверить задачи"))

        self.assertEqual(calls["count"], 1)

    def test_parse_disabled_never_calls_gpt(self):
        calls = {"capture": 0, "idea": 0}

        async def fake_capture(*args, **kwargs):
            calls["capture"] += 1
            return None

        async def fake_idea(*args, **kwargs):
            calls["idea"] += 1
            return ("x", "y")

        bot.OPENAI_PARSE_STRATEGY = "gpt_first"
        bot.OPENAI_PARSE_ENABLED = False
        bot._openai_client = object()
        bot.parse_capture_with_gpt = fake_capture
        bot.parse_idea_with_gpt = fake_idea

        capture = asyncio.run(bot.classify_capture(self._update(), "добавь задачу написать Кате"))

        self.assertEqual(calls, {"capture": 0, "idea": 0})
        self.assertEqual(capture["source"], "rules")

    def test_default_env_values_are_safe(self):
        self.assertEqual(bot.OPENAI_PARSE_MODEL, "gpt-5.4-mini")
        self.assertEqual(bot.OPENAI_PARSE_STRATEGY, "gpt_first")
        self.assertTrue(bot.OPENAI_PARSE_ENABLED)
        self.assertTrue(bot.OPENAI_STT_ENABLED)
        self.assertEqual(bot.OPENAI_STT_MODEL, "gpt-4o-mini-transcribe")

    def test_cost_message_hides_secrets(self):
        message = bot.cost_settings_message()

        self.assertIn("OPENAI_PARSE_ENABLED", message)
        self.assertIn("OPENAI_PARSE_STRATEGY", message)
        self.assertIn("voice mode", message)
        self.assertNotIn("BOT_TOKEN", message)
        self.assertNotIn("OPENAI_API_KEY", message)
        self.assertNotIn("WEEEK_API_TOKEN", message)


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

    def test_detect_top_level_route_local_task(self):
        route = bot.detect_top_level_route("добавь задачу завтра в 10 написать Кате")
        self.assertEqual(route["target"], "local_task")

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


if __name__ == "__main__":
    unittest.main()
