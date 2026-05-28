import unittest
import asyncio
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


class SttSafetyTests(unittest.TestCase):
    def test_custom_endpoint_forces_expensive_transcribe_model_to_mini(self):
        self.assertEqual(
            bot._effective_openai_transcribe_model("gpt-4o-transcribe", "https://custom.example/v1"),
            ("gpt-4o-mini-transcribe", True),
        )
        self.assertEqual(
            bot._effective_openai_transcribe_model("gpt-4o-transcribe", "https://api.openai.com/v1"),
            ("gpt-4o-transcribe", False),
        )

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

    def test_rate_limit_returns_error_without_second_transcription_call(self):
        class _FakeVoiceFile:
            async def download_to_drive(self, path):
                with open(path, "wb") as f:
                    f.write(b"fake audio")

        class _FakeTranscriptions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                raise RuntimeError("upstream_rate_limited")

        transcriptions = _FakeTranscriptions()
        old_client = bot._openai_client
        old_convert = bot._convert_audio_for_openai
        old_vosk_ready = bot._vosk_model_ready
        old_disabled = bot._openai_transcription_disabled
        old_reason = bot._openai_transcription_disabled_reason
        bot._openai_client = types.SimpleNamespace(
            audio=types.SimpleNamespace(transcriptions=transcriptions)
        )
        bot._convert_audio_for_openai = lambda _path: None
        bot._vosk_model_ready = lambda: False
        bot._openai_transcription_disabled = False
        bot._openai_transcription_disabled_reason = ""
        try:
            result = asyncio.run(bot.transcribe_voice(_FakeVoiceFile()))
        finally:
            bot._openai_client = old_client
            bot._convert_audio_for_openai = old_convert
            bot._vosk_model_ready = old_vosk_ready
            bot._openai_transcription_disabled = old_disabled
            bot._openai_transcription_disabled_reason = old_reason

        self.assertEqual(transcriptions.calls, 1)
        self.assertEqual(result["error"], "openai_rate_limited")
        self.assertEqual(result["engine"], f"openai:{bot.OPENAI_TRANSCRIBE_MODEL}")


if __name__ == "__main__":
    unittest.main()
