# mira-task-bot

Telegram-бот для создания задач и подзадач в Weeek. Любой обычный текст или голосовое сообщение запускает один сценарий: разбор задачи, выбор проекта и колонки, предпросмотр и подтверждение.

## Возможности

- Создание обычных задач Weeek из текста и голоса.
- Создание подзадач с поиском родительской задачи.
- Ручной выбор проекта, доски/колонки и подтверждение перед созданием.
- OpenAI STT с локальным Vosk как резервным распознавателем.
- GPT-парсинг названия, описания, даты, проекта и колонки.
- Защита от устаревших Telegram-кнопок через `draft_id`.

## Голосовой режим

Голос сначала распознаётся через OpenAI STT. При ошибке используется локальный Vosk, если он установлен вместе с ffmpeg и моделью.

Переменные окружения:

- `OPENAI_API_KEY` — задаётся только на сервере или в окружении. Не коммитьте ключ в репозиторий.
- `OPENAI_PARSE_MODEL` — модель парсинга смысла. По умолчанию `gpt-5.4-mini`.
- `OPENAI_STT_MODEL` — модель распознавания голоса. По умолчанию `gpt-4o-mini-transcribe`.
- `OPENAI_STT_ENABLED` — включает OpenAI STT.
- `VOSK_MODEL_PATH` — опциональный путь к заранее скачанной Vosk-модели.
- `VOSK_MODEL_URL` — опциональная ссылка на модель для автоскачивания.
- `WEEEK_API_TOKEN` — токен Weeek.
- `WEEEK_API_BASE_URL` — адрес публичного Weeek API.
- `WEEEK_DEFAULT_WORKSPACE_ID` — workspace Weeek, если он нужен токену.

STT-движок:

- `gpt-4o-mini-transcribe` — основной.
- `Vosk` — резервный локальный.

Команда `/voice` показывает активные модели GPT и STT.

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
OPENAI_PARSE_MODEL=gpt-5.4-mini
OPENAI_STT_ENABLED=true
OPENAI_STT_MODEL=gpt-4o-mini-transcribe
WEEEK_API_TOKEN=...
WEEEK_API_BASE_URL=https://api.weeek.net/public/v1
DEFAULT_TIMEZONE=Asia/Omsk
```

Локальные идеи, задачи и напоминания больше не доступны через интерфейс и не планируют уведомления. Старые записи SQLite сохраняются без миграции или удаления.

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
