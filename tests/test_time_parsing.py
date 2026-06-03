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


if __name__ == "__main__":
    unittest.main()
