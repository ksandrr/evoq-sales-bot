import unittest
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

from bot import extract_capture_due_time


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


if __name__ == "__main__":
    unittest.main()
