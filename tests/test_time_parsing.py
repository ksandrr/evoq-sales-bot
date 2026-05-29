import unittest
import asyncio
import os
import sys
import types


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
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=self.content)
                )
            ]
        )


class GptJsonParsingTests(unittest.TestCase):
    def _parse_with_fake_client(self, content, base_url):
        completions = _FakeCompletions(content)
        old_client = bot._openai_client
        old_base_url = bot.OPENAI_BASE_URL
        bot._openai_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )
        bot.OPENAI_BASE_URL = base_url
        try:
            result = asyncio.run(bot.parse_capture_with_gpt("сходить к стоматологу", "Asia/Omsk"))
        finally:
            bot._openai_client = old_client
            bot.OPENAI_BASE_URL = old_base_url
        return result, completions.kwargs

    def test_custom_endpoint_does_not_send_response_format(self):
        result, kwargs = self._parse_with_fake_client(
            '{"type":"task","title":"Сходить к стоматологу","body":"Сходить к стоматологу",'
            '"due_date":"","due_time":"","timezone":"","confidence":0.9,'
            '"time_confidence":1,"needs_time_clarification":false,"clarification_reason":""}',
            "https://custom.example/v1",
        )

        self.assertEqual(result["title"], "Сходить к стоматологу")
        self.assertNotIn("response_format", kwargs)

    def test_text_wrapped_json_is_parsed(self):
        result, _ = self._parse_with_fake_client(
            'Готово:\n{"type":"task","title":"Сходить к стоматологу","body":"Сходить к стоматологу",'
            '"due_date":"","due_time":"","timezone":"","confidence":0.9,'
            '"time_confidence":1,"needs_time_clarification":false,"clarification_reason":""}',
            "https://custom.example/v1",
        )

        self.assertEqual(result["source"], "gpt")
        self.assertEqual(result["title"], "Сходить к стоматологу")

    def test_bad_json_returns_none(self):
        result, _ = self._parse_with_fake_client("это не json", "https://custom.example/v1")

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


class VoiceVoskOnlyTests(unittest.TestCase):
    def test_custom_endpoint_client_disables_sdk_retries(self):
        self.assertEqual(
            bot._openai_client_kwargs("test-key", "https://custom.example/v1")["max_retries"],
            0,
        )
        self.assertNotIn(
            "max_retries",
            bot._openai_client_kwargs("test-key", "https://api.openai.com/v1"),
        )

    def test_gpt_54_custom_request_omits_json_mode_and_temperature(self):
        old_base_url = bot.OPENAI_BASE_URL
        bot.OPENAI_BASE_URL = "https://custom.example/v1"
        try:
            kwargs = bot._chat_json_request_kwargs(
                "gpt-5.4-mini",
                [{"role": "user", "content": "ping"}],
                0.1,
            )
        finally:
            bot.OPENAI_BASE_URL = old_base_url

        self.assertNotIn("response_format", kwargs)
        self.assertNotIn("temperature", kwargs)

    def test_gpt_55_custom_request_omits_json_mode_and_temperature(self):
        old_base_url = bot.OPENAI_BASE_URL
        bot.OPENAI_BASE_URL = "https://custom.example/v1"
        try:
            kwargs = bot._chat_json_request_kwargs(
                "gpt-5.5",
                [{"role": "user", "content": "ping"}],
                0.1,
            )
        finally:
            bot.OPENAI_BASE_URL = old_base_url

        self.assertNotIn("response_format", kwargs)
        self.assertNotIn("temperature", kwargs)

    def test_voice_available_requires_vosk_and_ffmpeg(self):
        old_vosk_available = bot._vosk_available
        old_which = bot.shutil.which
        try:
            bot._vosk_available = lambda: True
            bot.shutil.which = lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
            self.assertTrue(bot.voice_available())

            bot.shutil.which = lambda _name: None
            self.assertFalse(bot.voice_available())

            bot._vosk_available = lambda: False
            bot.shutil.which = lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
            self.assertFalse(bot.voice_available())
        finally:
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

    def test_transcribe_voice_uses_vosk_even_when_openai_client_exists(self):
        class _FakeVoiceFile:
            async def download_to_drive(self, path):
                with open(path, "wb") as f:
                    f.write(b"fake audio")

        class _FakeTranscriptions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                raise AssertionError("OpenAI STT must not be called")

        transcriptions = _FakeTranscriptions()
        old_client = bot._openai_client
        old_voice_available = bot.voice_available
        old_get_vosk_model = bot._get_vosk_model
        old_vosk_transcribe_file = bot._vosk_transcribe_file
        bot._openai_client = types.SimpleNamespace(
            audio=types.SimpleNamespace(transcriptions=transcriptions)
        )
        bot.voice_available = lambda: True
        bot._get_vosk_model = lambda: object()
        bot._vosk_transcribe_file = lambda _path: "распознанный текст"
        try:
            result = asyncio.run(bot.transcribe_voice(_FakeVoiceFile()))
        finally:
            bot._openai_client = old_client
            bot.voice_available = old_voice_available
            bot._get_vosk_model = old_get_vosk_model
            bot._vosk_transcribe_file = old_vosk_transcribe_file

        self.assertEqual(transcriptions.calls, 0)
        self.assertEqual(result["text"], "распознанный текст")
        self.assertEqual(result["engine"], "vosk")


if __name__ == "__main__":
    unittest.main()
