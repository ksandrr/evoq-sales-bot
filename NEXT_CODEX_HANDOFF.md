# NEXT CODEX HANDOFF

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
