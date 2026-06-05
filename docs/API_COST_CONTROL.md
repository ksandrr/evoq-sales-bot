# API Cost Control

This bot uses OpenAI in two separate places:

- Chat completions for capture parsing: idea, task, reminder.
- Speech-to-text for voice messages.

Change cost settings in `.env`, a systemd `EnvironmentFile`, Docker env, or the process environment used to start the bot. After changing env values, restart the bot.

## Important Env Values

```bash
OPENAI_PARSE_ENABLED=true
OPENAI_PARSE_STRATEGY=gpt_first
OPENAI_PARSE_MODEL=gpt-5.4-mini
OPENAI_STT_ENABLED=true
OPENAI_STT_MODEL=gpt-4o-mini-transcribe
```

- `OPENAI_PARSE_ENABLED=false` disables all OpenAI chat completion calls. Voice STT is controlled separately.
- `OPENAI_PARSE_STRATEGY=gpt_first` keeps the safer current behavior: GPT first, local rules as fallback.
- `OPENAI_PARSE_STRATEGY=rules_first` uses local rules first and calls GPT only for unclear captures.
- `OPENAI_STT_ENABLED=false` disables OpenAI STT. Vosk can still work as fallback when installed with ffmpeg.

## Safe Mode After Deploy

```bash
OPENAI_PARSE_ENABLED=true
OPENAI_PARSE_STRATEGY=gpt_first
OPENAI_PARSE_MODEL=gpt-5.4-mini
OPENAI_STT_ENABLED=true
OPENAI_STT_MODEL=gpt-4o-mini-transcribe
```

## Cheaper Test Mode

```bash
OPENAI_PARSE_ENABLED=true
OPENAI_PARSE_STRATEGY=rules_first
OPENAI_PARSE_MODEL=gpt-5.4-mini
OPENAI_STT_ENABLED=true
OPENAI_STT_MODEL=gpt-4o-mini-transcribe
```

## Fast Rollback

If `rules_first` works poorly, change only:

```bash
OPENAI_PARSE_STRATEGY=gpt_first
```

Then restart the bot.

## Log Checks

For systemd:

```bash
sudo systemctl daemon-reload
sudo systemctl restart evoq-sales-bot
sudo journalctl -u evoq-sales-bot -n 100 -f
```

Check startup config:

```bash
sudo journalctl -u evoq-sales-bot -n 100 --no-pager | grep cost_config
```

Check OpenAI usage:

```bash
sudo journalctl -u evoq-sales-bot -n 300 --no-pager | grep openai_usage
```

Expected chat usage log shape:

```text
openai_usage feature=capture_parse model=gpt-5.4-mini input_tokens=123 output_tokens=45 total_tokens=168
```

Expected STT usage log shape:

```text
openai_usage feature=openai_stt model=gpt-4o-mini-transcribe duration=12 file_size=34567
```

You can also send `/cost` in Telegram to see the current non-secret cost mode.
