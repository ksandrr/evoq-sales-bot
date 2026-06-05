# evoq-sales-bot

## OpenAI Cost Controls

OpenAI STT is enabled by default through `gpt-4o-mini-transcribe`. Local Vosk remains a fallback when ffmpeg and the Vosk model are available.

Semantic parsing is enabled by default through `gpt-5.4-mini`.

- `OPENAI_PARSE_STRATEGY=gpt_first` is the safe default: GPT parses first, local rules are fallback.
- `OPENAI_PARSE_STRATEGY=rules_first` is the cheaper test mode: local rules parse first, GPT is used only when rules are not confident.
- `OPENAI_PARSE_ENABLED=false` fully disables OpenAI chat completion calls for semantic parsing. OpenAI STT is controlled separately by `OPENAI_STT_ENABLED`.

Use `/cost` in Telegram to see the current non-secret cost mode. Detailed rollout and rollback notes are in `docs/API_COST_CONTROL.md`.

Telegram-бот для быстрых идей, задач и напоминаний. Можно писать текстом или надиктовывать голосом: бот распознаёт речь, чистит лишние вводные, определяет тип записи и сохраняет результат в SQLite.

## Возможности

- Идеи: короткое название и подробное описание.
- Задачи: список дел с кнопками «сделано» и «удалить».
- Напоминания по задачам: дата, время и часовой пояс.
- Ежедневные напоминания по идеям.
- Голосовой ввод через Vosk: локально, оффлайн, без OpenAI STT и токенов.
- GPT-парсинг смысла: идея / задача / напоминание, нормальный title/body, дата и время.

## Голосовой режим

Голос распознаётся только локально через Vosk. OpenAI/custom endpoint используется только для GPT-парсинга уже распознанного текста.

Переменные окружения:

- `OPENAI_API_KEY` — задаётся только на сервере или в окружении. Не коммитьте ключ в репозиторий.
- `OPENAI_BASE_URL` — custom endpoint провайдера. Если не задан, используется стандартный `https://api.openai.com/v1`.
- `OPENAI_PARSE_MODEL` — модель парсинга смысла. По умолчанию `gpt-5.5`; для custom endpoint можно менять на другую доступную GPT-модель.
- `VOSK_MODEL_PATH` — опциональный путь к заранее скачанной Vosk-модели.
- `VOSK_MODEL_URL` — опциональная ссылка на модель для автоскачивания.

STT-движок:

- `Vosk` — единственный движок голосового распознавания в текущей версии.

Vosk бесплатный и работает оффлайн, но качество заметно ниже современных Whisper-моделей, особенно на хаотичной живой речи.

Команда `/voice` показывает текущий режим Vosk.

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
OPENAI_PARSE_MODEL=gpt-5.5
DEFAULT_TIMEZONE=Asia/Omsk
```

GPT-парсинг меняется именно здесь:

- смысловой GPT-парсинг задач/идей/напоминаний: `OPENAI_PARSE_MODEL`;
- провайдер/API endpoint: `OPENAI_BASE_URL`.

Аудио-модель OpenAI больше не используется. Для голоса нужен Vosk и ffmpeg.

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
