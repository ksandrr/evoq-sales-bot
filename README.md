# evoq-sales-bot

Telegram-бот для быстрых идей, задач и напоминаний. Можно писать текстом или надиктовывать голосом: бот распознаёт речь, чистит лишние вводные, определяет тип записи и сохраняет результат в SQLite.

## Возможности

- Идеи: короткое название и подробное описание.
- Задачи: список дел с кнопками «сделано» и «удалить».
- Напоминания по задачам: дата, время и часовой пояс.
- Ежедневные напоминания по идеям.
- Голосовой ввод через OpenAI STT, с Vosk как бесплатным fallback.
- GPT-парсинг смысла: идея / задача / напоминание, нормальный title/body, дата и время.

## Голосовой режим

Рекомендованный режим — OpenAI. Он лучше понимает живую русскую речь, запинки и продуктовые слова вроде EVOQ, Vosk, OpenAI, Telegram, GitHub.

Переменные окружения:

- `OPENAI_API_KEY` — задаётся только на сервере или в окружении. Не коммитьте ключ в репозиторий.
- `OPENAI_BASE_URL` — custom endpoint провайдера. Если не задан, используется стандартный `https://api.openai.com/v1`.
- `OPENAI_TRANSCRIBE_MODEL` — модель распознавания речи. По умолчанию `gpt-4o-mini-transcribe`.
- `OPENAI_PARSE_MODEL` — модель парсинга смысла. По умолчанию `gpt-4o-mini`; для custom endpoint можно поставить `gpt-5.4-mini`.

Допустимые STT-модели:

- `gpt-4o-mini-transcribe` — дешевле, используется по умолчанию.
- `gpt-4o-transcribe` — дороже, обычно лучше качество.
- `whisper-1` — legacy/fallback.

Если `OPENAI_API_KEY` не задан или OpenAI transcription упал, бот пробует Vosk. Vosk бесплатный и работает оффлайн, но качество заметно ниже, особенно на хаотичной живой речи.

Команда `/voice` показывает текущий режим: OpenAI с выбранной моделью или Vosk.

## Безопасность секретов

- `.env` должен оставаться в `.gitignore`.
- Не добавляйте значение `OPENAI_API_KEY` в README, `.env.example`, код, коммиты или логи.
- На Linux/systemd задавайте ключ только через environment/secret manager.

## Локальный запуск

1. Создайте бота у [@BotFather](https://t.me/BotFather) и получите `BOT_TOKEN`.
2. Создайте `.env` локально:

   ```bash
   cp .env.example .env
   ```

3. Заполните `BOT_TOKEN` в `.env`. OpenAI-ключ лучше задавать через окружение вашей машины или секреты сервера.
4. Установите зависимости:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

5. Запустите:

   ```bash
   python bot.py
   ```

По умолчанию SQLite хранится в `ideas.db` рядом с `bot.py`. Для продакшена задайте `DB_PATH`.

## Linux server

Пример обновления существующего деплоя:

```bash
cd /path/to/evoq-sales-bot
git fetch origin
git checkout tembo/telegram-idea-bot-daily-reminders
git pull --ff-only origin tembo/telegram-idea-bot-daily-reminders
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Добавьте переменные окружения в тот механизм, которым запущен бот: systemd `EnvironmentFile`, Docker secrets/env, screen/tmux wrapper или панель хостинга. Не храните секреты в Git.

Минимальный пример для systemd `EnvironmentFile`:

```bash
BOT_TOKEN=...
DB_PATH=/var/lib/evoq-sales-bot/ideas.db
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://codex.sale/v1
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
OPENAI_PARSE_MODEL=gpt-5.4-mini
DEFAULT_TIMEZONE=Asia/Omsk
```

Модели меняются именно здесь:

- аудио/транскрибация: `OPENAI_TRANSCRIBE_MODEL`;
- смысловой GPT-парсинг задач/идей/напоминаний: `OPENAI_PARSE_MODEL`;
- провайдер/API endpoint: `OPENAI_BASE_URL`.

После изменения env обязательно перезапустите процесс бота.

Для systemd после изменения env:

```bash
sudo systemctl daemon-reload
sudo systemctl restart evoq-sales-bot
sudo journalctl -u evoq-sales-bot -n 100 -f
```

Если бот запущен в `screen`/`tmux`, остановите старый процесс и запустите `python bot.py` в активированном venv с нужными переменными окружения.

## Проверки

```bash
python -m unittest discover -s tests
python -m py_compile bot.py database.py
```

Ручные проверки в Telegram:

- «это не задача, просто мысль, надо сделать быстрый режим записи идей» → idea.
- «сделай задачу проверить логи Linux-сервера и понять какой движок используется» → task.
- «напомни сегодня вечером в десять проверить список задач» → reminder на 22:00.

## Структура

- `bot.py` — Telegram handlers, voice/STT, GPT/rules parsing, reminders.
- `database.py` — SQLite wrapper and compatible migrations.
- `requirements.txt` — Python dependencies.
- `.env.example` — безопасный пример локальной конфигурации без секретов.
