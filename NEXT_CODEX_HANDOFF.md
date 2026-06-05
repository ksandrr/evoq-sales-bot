# NEXT CODEX HANDOFF

## 2026-06-06 Voice/Audio Flow Fix

### Session Goal

Fix the broken Telegram voice/audio flow in Mira Task Bot with the smallest possible patch so that voice messages no longer go silent.

### What Changed

- In `bot.py`:
  - added an immediate user-facing reply `Распознаю голосовое...` before transcription starts
  - wrapped the top-level voice flow in a defensive `try/except` so unexpected failures now return a visible error instead of going silent
  - kept the change scoped to voice/audio handling only

### Files Edited

- `C:\Users\gorbi\OneDrive\Документы\mira-task-bot\bot.py`
- `C:\Users\gorbi\OneDrive\Документы\mira-task-bot\NEXT_CODEX_HANDOFF.md`

### Checks Run

- `C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m py_compile bot.py`
- `C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_time_parsing`

### Test Result

- `py_compile`: passed
- `tests.test_time_parsing`: passed

### Deployment

- No Linux deploy was performed in this session.

### Commit

- No commit was created in this session.

### .env Changes

- No `.env` variables were changed in this session.

### Open Issues

- The patch is code-checked locally, but it still needs a live Telegram smoke test with a real voice or audio message.

### Next Check

- First verify that a real Telegram voice message now gets `Распознаю голосовое...`, then the Weeek draft or a clear error message.

## Session Goal

Finish the move to official OpenAI API usage with one `OPENAI_API_KEY`, rename the GitHub repo to `mira-task-bot`, rename the Linux deploy path and systemd service to `mira-task-bot`, deploy the updated code, and verify the service starts cleanly.

## What Changed

- In `bot.py`:
  - removed support for `OPENAI_BASE_URL`
  - removed support for `OPENAI_STT_BASE_URL`
  - removed support for `OPENAI_STT_API_KEY`
  - kept one official OpenAI client path based on `OpenAI(api_key=OPENAI_API_KEY)`
  - kept OpenAI STT as primary voice recognition and Vosk as fallback
  - removed custom-endpoint request branching and related diagnostics
  - updated user-facing wording toward `Mira` where applicable
- In `tests/test_time_parsing.py`:
  - removed custom-endpoint expectations
  - added official-only assertions
  - kept negative assertions to ensure removed env vars and `codex.sale` do not come back
- In `.env.example`:
  - switched to the single-key OpenAI contract
  - documented the final variable order requested by the user
- In `AGENTS.md`:
  - corrected the handoff path back to the current local workspace path because the Windows workspace folder itself was not renamed in this session

## Files Edited

- `H:\TGBots\evoq-sales-bot\bot.py`
- `H:\TGBots\evoq-sales-bot\tests\test_time_parsing.py`
- `H:\TGBots\evoq-sales-bot\.env.example`
- `H:\TGBots\evoq-sales-bot\AGENTS.md`
- `H:\TGBots\evoq-sales-bot\NEXT_CODEX_HANDOFF.md`

## Git Working Tree At Start

Recorded before mutation:

```text
 M .env.example
 D README.md
 M bot.py
 M tests/test_time_parsing.py
?? .kiloignore
?? AGENTS.md
?? NEXT_CODEX_HANDOFF.md
```

Notes:

- `README.md` was already deleted by the user and was not restored.
- `.kiloignore` was left untouched.

## Commands Run

### Local Windows

```powershell
git status --short
git remote -v
git rev-parse --short HEAD
git log -1 --pretty=%B
```

Used bundled Python runtime from Codex:

```powershell
& 'C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m py_compile bot.py weeek_client.py tests\test_time_parsing.py
& 'C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests
```

Static searches:

```powershell
rg -n --hidden -S "OPENAI_BASE_URL|OPENAI_STT_BASE_URL|OPENAI_STT_API_KEY|codex.sale" bot.py tests .env.example AGENTS.md NEXT_CODEX_HANDOFF.md
git remote set-url origin https://github.com/ksandrr/mira-task-bot.git
git remote -v
git add bot.py tests/test_time_parsing.py .env.example AGENTS.md
git commit -m "Switch to official OpenAI API and prepare Mira rename"
git push origin tembo/telegram-idea-bot-daily-reminders
```

### GitHub rename

The GitHub repository rename was completed through the GitHub API using the already configured local credentials:

- old repo: `ksandrr/evoq-sales-bot`
- new repo: `ksandrr/mira-task-bot`

After rename, local `origin` was updated and verified:

```text
origin  https://github.com/ksandrr/mira-task-bot.git (fetch)
origin  https://github.com/ksandrr/mira-task-bot.git (push)
```

### Linux server

Repo remote update and deploy sync:

```bash
cd /home/sanya/evoq-sales-bot
git remote set-url origin https://github.com/ksandrr/mira-task-bot.git
git fetch origin
git pull --ff-only origin tembo/telegram-idea-bot-daily-reminders
```

Server-side validation before/after rename:

```bash
cd /home/sanya/mira-task-bot && .venv/bin/python -m py_compile bot.py weeek_client.py tests/test_time_parsing.py
cd /home/sanya/mira-task-bot && .venv/bin/python -m unittest discover -s tests
systemctl is-active mira-task-bot.service
journalctl -u mira-task-bot.service -n 100 --no-pager
```

## Test Results

Local bundled Python:

- `py_compile`: passed
- `unittest discover -s tests`: passed
- result: `Ran 30 tests ... OK`

Server Python inside `.venv`:

- `py_compile`: passed
- `unittest discover -s tests`: passed
- result: `Ran 30 tests ... OK`

## Linux Deploy Status

Yes, Linux rename and deploy were completed.

### Final Linux state

- deploy path: `/home/sanya/mira-task-bot`
- service: `mira-task-bot.service`
- service status after restart: `active`
- journal check after restart: completed

### systemd backup

Backups were created before the rename under:

- `/home/sanya/mira-task-bot-backup-20260603-175054`

Backup contents include:

- previous unit copy
- previous proxy drop-in copy
- rollback command file

Rollback command file:

- `/home/sanya/mira-task-bot-backup-20260603-175054/ROLLBACK_COMMANDS.txt`

### Final unit shape

The final unit runs:

- `WorkingDirectory=/home/sanya/mira-task-bot`
- `EnvironmentFile=/home/sanya/mira-task-bot/.env`
- `ExecStart=/home/sanya/mira-task-bot/.venv/bin/python /home/sanya/mira-task-bot/bot.py`

## Commit And Push

- deployed commit: `1ef9c71`
- commit message: `Switch to official OpenAI API and prepare Mira rename`
- pushed branch: `tembo/telegram-idea-bot-daily-reminders`

## Server .env Changes

- No secret values are recorded here.
- In this session, the server `.env` was not rewritten by Codex.
- Verified shape only: server config already matched the single-key OpenAI contract before final restart.

## Final Supported OpenAI Env Contract

Supported:

- `OPENAI_API_KEY`
- `OPENAI_PARSE_MODEL`
- `OPENAI_STT_MODEL`
- `OPENAI_STT_ENABLED`

Removed from supported contract:

- `OPENAI_BASE_URL`
- `OPENAI_STT_BASE_URL`
- `OPENAI_STT_API_KEY`

Voice behavior:

- primary STT: official OpenAI STT
- fallback STT: Vosk

## Remaining Issues / Follow-Up

1. The local Windows workspace folder is still `H:\TGBots\evoq-sales-bot`.
2. The repo and Linux service were renamed, but the live local workspace folder rename was intentionally skipped to avoid breaking the current Codex session.
3. `README.md` is still deleted locally because that deletion predates this session and was preserved.
4. Manual Telegram smoke checks were not run in this session:
   - text parsing flow
   - voice flow with `Название`, `Описание`, `Транскрипция`
   - Weeek task creation
   - Weeek subtask creation via `parentId`

## What Next Codex Should Check First

1. Confirm current local git status and decide whether to commit the final handoff/doc updates from this session.
2. Run one live Telegram smoke test for text.
3. Run one live Telegram smoke test for voice and confirm the `Название` / `Описание` / `Транскрипция` block.
4. Run one Weeek task creation test and one subtask creation test.
5. Decide separately whether the Windows workspace folder should also be renamed from `evoq-sales-bot` to `mira-task-bot`.

## Follow-Up On 2026-06-04

- The user manually updated absolute paths in the server `.env` from the old Linux path to the new one.
- No secret values were copied into this handoff.
- After the `.env` path fix, the service was restarted on the server:

```bash
sudo systemctl restart mira-task-bot.service
systemctl is-active mira-task-bot.service
journalctl -u mira-task-bot.service -n 100 --no-pager
```

- Result:
  - `mira-task-bot.service` is `active`
  - logs show `OpenAI client initialized: official_api=true`
  - logs show `OpenAI STT client initialized: official_api=true`
  - logs show `STT health primary=True ... fallback_vosk=True`
  - bot startup completed successfully

## Follow-Up On 2026-06-04: Voice / Weeek Fix

### Goal

Fix the broken voice -> Weeek flow reported by the user:

- OpenAI STT was failing on Telegram `.oga`
- GPT parsing for `gpt-5.5` was failing because `temperature` was sent
- Weeek preview buttons/messages had mojibake
- active draft states could intercept a new voice command instead of switching into a new Weeek flow
- logs were not detailed enough to reconstruct the full scenario from journal

### Code Changes

- Reworked the primary STT path so OpenAI STT now transcodes Telegram voice to `.wav` through `ffmpeg` before sending it to the official API.
- Kept Vosk as the fallback path after OpenAI STT failure.
- Removed `temperature` from official JSON parsing request kwargs used for GPT parsing.
- Added detailed structured runtime logs for:
  - `voice_downloaded`
  - `voice_converted_for_openai`
  - `openai_stt_request_started`
  - `openai_stt_request_succeeded`
  - `vosk_fallback_started`
  - `vosk_fallback_succeeded`
  - `voice_route_detected`
  - `voice_route_override_previous_state`
  - `capture_gpt_parse_started`
  - `capture_gpt_parse_succeeded`
  - `capture_gpt_parse_failed`
  - `weeek_capture_started`
  - `weeek_projects_loaded`
  - `weeek_boards_loaded`
  - `weeek_columns_loaded`
  - `weeek_columns_filtered`
  - `weeek_column_auto_selected`
  - `weeek_preview_rendered`
  - `weeek_task_create_started`
  - `weeek_task_create_succeeded`
  - `weeek_task_create_failed`
- Added voice override behavior: if a user is inside another unfinished flow but clearly says a new Weeek command by voice, the bot resets the transient draft state and starts a fresh Weeek flow.
- Filtered Weeek column selection so `Готово` is not shown as a selectable target column for creating a new task.
- Replaced the broken Weeek preview UI strings with clean Russian text and a single canonical preview path.
- Reworked Weeek callback routing so top-level voice/text Weeek starts do not depend on the previous conversation state tracking.

### Files Edited In This Follow-Up

- `H:\TGBots\evoq-sales-bot\bot.py`
- `H:\TGBots\evoq-sales-bot\tests\test_time_parsing.py`

### Commands Run

Local:

```powershell
& 'C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m py_compile bot.py weeek_client.py tests\test_time_parsing.py
& 'C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests
git commit -m "Fix voice Weeek flow and OpenAI STT handling"
git push origin tembo/telegram-idea-bot-daily-reminders
```

Linux:

```bash
cd /home/sanya/mira-task-bot
git fetch origin
git pull --ff-only origin tembo/telegram-idea-bot-daily-reminders
.venv/bin/python -m py_compile bot.py weeek_client.py tests/test_time_parsing.py
.venv/bin/python -m unittest discover -s tests
sudo systemctl restart mira-task-bot.service
systemctl is-active mira-task-bot.service
journalctl -u mira-task-bot.service -n 40 --no-pager
```

### Test Results

- Local `py_compile`: passed
- Local `unittest`: passed (`31 tests`, `OK`)
- Server `py_compile`: passed
- Server `unittest`: passed (`31 tests`, `OK`)

### Deploy Status

- Deploy to Linux completed successfully.
- Service restart completed successfully.
- `systemctl is-active mira-task-bot.service` returned `active`.
- Fresh startup logs after restart show:
  - `OpenAI client initialized: official_api=true`
  - `OpenAI STT client initialized: official_api=true`
  - `STT health primary=True official_api=True model=gpt-4o-transcribe fallback_vosk=True`
  - `Bot started; reminders scheduled.`
  - `Application started`

### Commit

- deployed commit: `88aebae`
- commit message: `Fix voice Weeek flow and OpenAI STT handling`

### Server .env Changes

- No secret values were written to this file.
- In this follow-up Codex did not rewrite server `.env`.
- The user had already updated the Linux absolute paths in `.env` before this deploy.

### Remaining Risks / Manual Checks

1. The journal now confirms the old bad behavior happened before this fix:
   - `Unsupported file format oga`
   - `temperature does not support 0.1`
   - `voice_top_level returned state 530`
2. After deploy, startup is clean, but the exact user scenario still needs a real Telegram smoke test with live voice input.
3. First manual checks to run:
   - voice from empty chat: `Мира, запиши задачу в ВИК в личное купить молока завтра в 22:00`
   - voice while another draft is active: must override into a new Weeek flow
   - manual button flow through `Задача ВИК`
   - confirm `Готово` is no longer offered in the column picker
## Follow-Up On 2026-06-04: Weeek dueDateTime UTC format fix

### Symptom

- Weeek task creation still failed after the previous due-field fix.
- The live error was:
  - `422 {"success":false,"errors":{"dueDateTime":["The due date time does not match the format Y-m-d\\TH:i:s\\Z."]}}`

### Root Cause

- The previous fix removed conflicting due fields, but `weeek_client.py` still serialized combined due datetime as local `YYYY-MM-DDTHH:MM`.
- Weeek expects `dueDateTime` in strict UTC form with seconds and a trailing `Z`.
- The bot also was not passing the parsed task timezone into the Weeek client, so the client could not convert local captured time into UTC correctly.

### Fix

- In `weeek_client.py`:
  - added `_format_due_datetime(...)`
  - convert local `due_date + due_time + timezone_name` into UTC
  - serialize as `%Y-%m-%dT%H:%M:%SZ`
- In `bot.py`:
  - pass `capture["timezone"]` into `create_task(...)` and `create_subtask(...)`
  - fallback to `effective_user_timezone(...)` if capture timezone is empty
- In `tests/test_time_parsing.py`:
  - changed the Weeek payload assertion from local `2026-06-05T22:00`
  - new expected value for `Asia/Omsk 2026-06-05 22:00` is `2026-06-05T16:00:00Z`

### Validation

Validated on the Linux server:

```bash
cd /home/sanya/mira-task-bot && .venv/bin/python -m py_compile bot.py weeek_client.py tests/test_time_parsing.py
cd /home/sanya/mira-task-bot && .venv/bin/python -m unittest discover -s tests
sudo systemctl restart mira-task-bot.service
systemctl is-active mira-task-bot.service
```

Results:

- `py_compile`: passed
- `unittest`: passed (`32 tests`, `OK`)
- service status after restart: `active`

### Important Deployment Note

- The code fix is already live on the Linux server after restart.
- Server-side `git commit` failed because `user.name` / `user.email` are not configured there.
- Server-side `git push` failed because the server itself does not have GitHub HTTPS credentials configured.
- The safe fallback is to push from a local authenticated clone and then run `git pull` on the server.
