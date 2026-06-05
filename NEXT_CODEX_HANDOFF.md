# NEXT CODEX HANDOFF

## 2026-06-06 Robust Weeek Project Callback Release

### Session Goal

Fix the still-broken live Weeek project button flow after commit `1f09081` so selecting a Weeek project can never keep the Telegram bot stuck, while leaving menu, reminders, `.env`, and unrelated flows alone.

### Root Cause Found

- Fresh live logs for the reported 02:58-02:59 Asia/Omsk failure map to `2026-06-05 20:58-20:59 UTC` on the server.
- Exact last log line before the project-click path stalled for project `Личное` was:
  - `2026-06-05 20:59:04,731 - __main__ - INFO - weeek_columns_loading_started board_id='10'`
- The next line arrived only at `20:59:26,516 UTC`:
  - `weeek_columns_loading_failed board_id='10' error=Weeek API timed out`
- Then the error reply to Telegram also timed out:
  - `20:59:30,528 ... weeek_columns_error_message_failed board_id='10' error=Timed out`
- A later stale project click timed out in `query.answer(...)`, and `/start` then failed with `telegram.error.TimedOut` caused by `httpx.ConnectTimeout` while connecting through the configured HTTP proxy/TLS path.
- Conclusion: the project callback was waiting inside `ConversationHandler` for slow Weeek loading and Telegram sends. There is also a real server network/proxy problem to Telegram API; code can bound and recover from it, but cannot fully fix the server/proxy connectivity.

### What Changed

- In `bot.py`:
  - added `TELEGRAM_SHORT_TIMEOUT = 4` and `WEEEK_PROJECT_CALLBACK_TIMEOUT = 16`;
  - changed the active `weeek_project_callback` implementation so it:
    - logs `callback_id`, callback data, user id, matched handler, and current active flow;
    - acknowledges callback with short Telegram timeouts and continues safely if ACK fails/times out;
    - updates the selected project in `weeek_draft`;
    - schedules project next-step work in a background task;
    - immediately clears `_active_flow` and returns `ConversationHandler.END`, releasing the conversation;
  - moved slow post-project work into `_bounded_weeek_project_next_step(...)` with strict timeout;
  - added safe error reporting that clears `weeek_draft` and `_active_flow` even if the error message cannot be sent;
  - made `/start` clear transient flow state before replying;
  - made `/start` reply use short Telegram timeouts and catch `TelegramError`;
  - moved the public `/start` handler to group `-1` and added a silent `/start` fallback only to `weeek_conv`, so `/start` can end a stuck Weeek conversation after sending/attempting the recovery reply.
- In `tests/test_time_parsing.py`:
  - added `WeeekProjectCallbackTests.test_project_callback_schedules_next_step_and_releases_conversation`.

### Files Edited

- `C:\Users\gorbi\OneDrive\Документы\mira-task-bot\bot.py`
- `C:\Users\gorbi\OneDrive\Документы\mira-task-bot\tests\test_time_parsing.py`
- `C:\Users\gorbi\OneDrive\Документы\mira-task-bot\NEXT_CODEX_HANDOFF.md`

### Checks Run

- Local:
  - `C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m py_compile bot.py weeek_client.py tests\test_time_parsing.py`
  - `C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s tests`
- Server:
  - `cd /home/sanya/mira-task-bot && git pull --ff-only`
  - `.venv/bin/python -m py_compile bot.py weeek_client.py tests/test_time_parsing.py`
  - `.venv/bin/python -m unittest discover -s tests`
  - `kill 18811` to restart the user-owned process under systemd `Restart=always` because `sudo systemctl restart mira-task-bot.service` required an unavailable sudo password.
  - `systemctl status mira-task-bot.service --no-pager -l`
  - `journalctl -u mira-task-bot.service -n 100 --no-pager`

### Test Result

- Local `py_compile`: passed.
- Local unittest discovery: `Ran 44 tests ... OK`.
- Server `py_compile`: passed.
- Server unittest discovery: `Ran 44 tests ... OK`.

### Deployment

- Deployed to `/home/sanya/mira-task-bot`.
- Running commit after deploy: `6bf2d12`.
- Service restarted by terminating PID `18811`; systemd restarted it automatically.
- Post-restart status:
  - `Active: active (running) since Fri 2026-06-05 21:17:46 UTC`
  - new PID: `19749`
- Fresh journal showed app startup completed:
  - `Bot started; reminders scheduled.`
  - `Application started`

### Commit

- Commit: `6bf2d12` (`Release Weeek project callbacks from conversation`)
- Branch pushed: `tembo/telegram-idea-bot-daily-reminders`

### .env Changes

- No `.env` variables were changed.
- No secrets were read or written into handoff.

### Open Issues

- Server network/proxy connectivity to Telegram API is still a real external risk. The code now bounds Telegram waits and clears state, but if `api.telegram.org` is unreachable through the configured proxy, Telegram replies can still fail visibly.
- Weeek API `list_columns(board_id='10')` timed out during the live failure. The new code prevents that from holding the callback conversation, but Weeek/API/network latency may still produce a user-facing failure.

### Next Check

- Run a live Telegram smoke test:
  - send a voice Weeek task that reaches project buttons;
  - click `Личное`;
  - confirm logs show `weeek_project_callback_received`, `weeek_project_callback_answer_*`, `weeek_project_callback_next_step_scheduled`, `weeek_project_callback_exited`;
  - immediately send `/start` and a new voice message while Weeek loading is still slow;
  - verify `/start` and new voice are accepted immediately, even if the background Weeek step later logs timeout/error cleanup.

## 2026-06-06 Telegram Callback Timeout Follow-Up

### Session Goal

Investigate the still-broken live Weeek project selection hang after deploy using the exact server logs from the real Telegram test, then apply the smallest safe fix.

### Root Cause Found

- The live server logs for the real click on project `Личное` showed that the last confirmed business step was:
  - `weeek_project_selected project_id='6' project_name='Личное'`
- After that, the process was not hanging inside Weeek API loading. It was timing out on Telegram API calls, specifically on:
  - `await query.answer()` inside `weeek_project_callback`
- The exact stack trace in `journalctl` showed:
  - `bot.py -> handle_callback -> weeek_project_callback -> await query.answer()`
  - failure type: `telegram.error.TimedOut`
- A later live voice right after the stuck click also failed on a Telegram send call (`reply_text("Распознаю голосовое...")`) with the same timeout family, so `/start` and new voice were not blocked by FSM state logic alone. Telegram API request timeouts were part of the real failure mode.

### What Changed

- In `bot.py`:
  - added precise logging around the active Weeek project-selection path:
    - callback received
    - callback answer started/finished/timed out/failed
    - project draft update started/finished
    - known project mapping found
    - boards loading started/finished/failed
    - columns loading started/finished/failed
    - parent tasks loading started/finished/failed
    - next Telegram message send started/finished
  - added a later overriding active version of the Weeek picker functions and `weeek_project_callback` without changing menu or overall architecture
  - changed Weeek project callback to use short Telegram timeouts for `query.answer(...)`
  - if `query.answer(...)` times out, the callback now logs it and continues instead of crashing immediately
  - if the next Telegram step or a Weeek-loading step fails, the bot now clears `weeek_draft` and clears the active flow before ending that flow
  - used short Telegram timeouts on the follow-up `reply_text(...)` calls inside the Weeek picker flow so one slow Telegram request does not keep the flow hanging as long

### Files Edited

- `C:\Users\gorbi\OneDrive\Документы\mira-task-bot\bot.py`
- `C:\Users\gorbi\OneDrive\Документы\mira-task-bot\NEXT_CODEX_HANDOFF.md`

### Checks Run

- Linux log inspection:
  - `journalctl -u mira-task-bot.service --since "2026-06-05 20:40:00" --until "2026-06-05 20:45:00" --no-pager`
- Local code checks:
  - `C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m py_compile bot.py weeek_client.py`
  - `C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_time_parsing`

### Test Result

- `py_compile`: passed
- `tests.test_time_parsing`: passed

### Deployment

- Deployed to the Linux server at `/home/sanya/mira-task-bot` with `git pull`.
- Server checks completed:
  - `.venv/bin/python -m py_compile bot.py weeek_client.py`
  - `.venv/bin/python -m unittest tests.test_time_parsing`
- Restarted `mira-task-bot.service`.
- Post-restart status: `active (running)`.

### Commit

- Commit: `1f09081` (`Trace and bound Weeek project callback timeouts`)
- Branch pushed: `tembo/telegram-idea-bot-daily-reminders`

### .env Changes

- No `.env` variables were changed in this session.

### Open Issues

- A fresh live Telegram smoke test is still needed after deploy because the real issue was a Telegram callback/send timeout, not a purely local code-path exception.

### Next Check

- First check the fresh server logs right after one click on `Личное` and confirm the new log chain:
  - `weeek_project_callback_received`
  - `weeek_project_callback_answer_started`
  - either `weeek_project_callback_answer_finished` or `weeek_project_callback_answer_timed_out`
  - then one of:
    - `weeek_columns_loading_started`
    - `weeek_boards_loading_started`
    - `weeek_parent_tasks_loading_started`
  - and finally either a successful `...message_send_finished` or a clear logged failure with state reset

## 2026-06-06 Weeek Project Selection Freeze Fix

### Session Goal

Fix the hang in the voice -> Weeek project selection flow with a minimal patch so the bot does not get stuck after choosing a project.

### What Changed

- In `bot.py`:
  - wrapped Weeek project/board/column/parent loading steps with `WeeekApiError` handling
  - if Weeek project/board/task loading fails, the bot now sends a visible error, clears the Weeek draft, clears the active flow, and returns to a safe state
- In `weeek_client.py`:
  - changed Weeek HTTP timeout handling to `httpx.Timeout(20.0, connect=10.0)`
  - converted network/timeout failures from raw `httpx` exceptions into `WeeekApiError`, so callback handlers can recover cleanly instead of crashing the conversation

### Files Edited

- `C:\Users\gorbi\OneDrive\Документы\mira-task-bot\bot.py`
- `C:\Users\gorbi\OneDrive\Документы\mira-task-bot\weeek_client.py`
- `C:\Users\gorbi\OneDrive\Документы\mira-task-bot\NEXT_CODEX_HANDOFF.md`

### Checks Run

- `C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m py_compile bot.py weeek_client.py`
- `C:\Users\gorbi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest tests.test_time_parsing`
- `ssh ... journalctl -u mira-task-bot.service -n 200 --no-pager`

### Test Result

- `py_compile`: passed
- `tests.test_time_parsing`: passed
- Linux logs: showed uncaught `httpx.ConnectTimeout` in the bot process; this matched the failure mode where an upstream request could break the Weeek flow without clearing the user state

### Deployment

- Deployed to the Linux server at `/home/sanya/mira-task-bot` with `git pull`.
- Server checks completed:
  - `.venv/bin/python -m py_compile bot.py weeek_client.py`
  - `.venv/bin/python -m unittest tests.test_time_parsing`
- Restarted `mira-task-bot.service`.
- Post-restart status: `active (running)`.
- Fresh service logs after restart showed normal startup:
  - `Bot started; reminders scheduled.`
  - `Application started.`

### Commit

- Commit: `72155a6` (`Handle Weeek callback timeout failures`)
- Branch pushed: `tembo/telegram-idea-bot-daily-reminders`

### .env Changes

- No `.env` variables were changed in this session.

### Open Issues

- A real Telegram smoke test is still needed after deploy:
  - send voice
  - choose a Weeek project
  - verify the bot either continues to the next picker or shows a Weeek error without getting stuck

### Next Check

- First verify on the live bot that a failed Weeek API call after project selection now returns a user-facing error and that `/start` plus a new voice message still work immediately afterward.

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

- Deployed to the Linux server at `/home/sanya/mira-task-bot` with `git pull`.
- Restarted `mira-task-bot.service` with `sudo systemctl restart`.
- Post-restart status was `active (running)`.
- Recent logs show normal startup: `Bot started; reminders scheduled.` and `Application started.`

### Commit

- Commit: `eaf5c1e` (`Fix voice audio flow fallback`)
- Branch pushed: `tembo/telegram-idea-bot-daily-reminders`

### .env Changes

- No `.env` variables were changed in this session.

### Open Issues

- The patch is deployed and the service is running, but it still needs a live Telegram smoke test with a real voice or audio message.

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
