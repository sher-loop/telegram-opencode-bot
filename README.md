# Telegram OpenCode Bot

AI-powered Telegram bot connected to opencode running in Termux.

## Features

- **Rich Telegram Formatting** - bold, italic, mono, spoiler, code blocks, blockquotes, links
- **Fast Replies** - typing indicator + instant response
- **Session Memory** - bot remembers your conversation history
- **Full Media Support** - text, files, photos, audio, video, voice, documents, stickers
- **Voice → Text** - voice notes, audio files, videos and video notes are
  transcribed to text *before* they reach opencode (opencode itself has no
  speech-to-text). The language is auto-detected, so you can speak any
  language — or mix two, like Thanglish
- **Markdown → Telegram** - AI responses automatically converted to Telegram style
- **Multi-user** - supports multiple users at once
- **HTML Fallback** - if MarkdownV2 fails, auto-switches to HTML
- **Config via Environment Variables** - bot token is never hardcoded in code
- **Obfuscated Storage** - sessions/tasks/users are stored obfuscated, not plain JSON
- **Health Monitoring** - crashes and repeated API errors are logged to `alerts.log`

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/new` | Start a new conversation |
| `/status` | Check session info |
| `/help` | Show help |
| `/approve <chat_id>` | Grant access (admin only) |
| `/deny <chat_id>` | Deny access (admin only) |
| `/users` | List authorized users with names + chat IDs (admin only) |
| `/user` | User panel: `/user`, `/user add <chat_id> [name]`, `/user remove <chat_id>` (admin only) |
| `/revoke <chat_id>` | Revoke access (admin only) |
| `/task ...` | Manage your tasks |

## Access Control (admin approval — no password)

Access is granted by the **admin** — there is no password.

- When an unauthorized user tries to use the bot, it sends an **access request**
  to the admin chat with **Approve / Deny** buttons.
- Approved users are stored in `userchatid.txt`; the admin chat ID lives in
  `adminchatid.txt` (auto-created from `ADMIN_IDS` on first run).
- The admin can also approve/deny manually: `/approve <chat_id>` or
  `/deny <chat_id>`.
- `/users` (or `/user`) lists every approved user with their **name + chat ID**.
- Manage approved users from the `/user` panel:
  - `/user` — list all users
  - `/user add <chat_id> [name]` — add a user (name auto-fetched if omitted)
  - `/user remove <chat_id>` — remove a user

`userchatid.txt` stores one user per line as `chat_id  display_name`.

## Setup

### 1. Install Requirements

```bash
pip install python-telegram-bot
```

### 2. Make Sure opencode is Installed

```bash
opencode --version
```

If not installed, install it first.

### 3. Configure Bot Token

The token is **required** via the `BOT_TOKEN` environment variable. It is never
hardcoded in `bot.py` — the bot exits with a clear error if it's missing.

**Option A — `.env` file (recommended for Termux):**

```bash
cd ~/telegram-opencode-bot
echo 'BOT_TOKEN="your_token_here"' > .env
chmod 600 .env
```

**Option B — export before running:**

```bash
export BOT_TOKEN="your_token_here"
```

Get your token from [@BotFather](https://t.me/BotFather) on Telegram.

Other optional variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `BOT_TOKEN` | *(required)* | Telegram bot token |
| `BOT_SECRET_KEY` | auto-generated | Key used to obfuscate stored data |
| `ADMIN_IDS` | `8937986952` | Comma-separated admin user IDs |
| `OPENCODE_DIR` | `$HOME` | Working directory for opencode |
| `OPENCODE_MODEL` | *(empty = opencode default)* | Pin the chat model for answers, e.g. `xai/grok-3` (Grok) |
| `INSECURE_SSL` | `1` | Bypass SSL verification (MITM networks) |
| `BOT_API_BASE_URL` | *(empty)* | Optional proxy/tunnel base URL |
| `STT_API_KEY` | *(empty)* | Speech-to-text key — **without it, voice messages don't work** |
| `STT_URL` | Groq transcriptions endpoint | Any Whisper-compatible endpoint |
| `STT_MODEL` | `whisper-large-v3` | Use `whisper-large-v3-turbo` for ~2x speed, slightly lower accuracy |
| `STT_LANG` | *(empty = auto)* | Force one language (`ta`, `en`, …). Leave empty for all languages |
| `STT_PROMPT` | *(empty)* | Vocabulary/style hint for the transcriber |
| `STT_ECHO` | `1` | Reply with `🎤 <what I heard>` so you can check it |
| `STT_TIMEOUT` | `60` | Seconds to wait for the transcription |
| `STT_MAX_MB` | `24` | Skip audio larger than this (Groq caps uploads at 25 MB) |

### 4. Enable Voice Messages (optional)

opencode cannot read audio at all. Sending it a `.ogg` gets you a useless
answer. So the bot transcribes the audio itself and sends **only the text** to
opencode:

```
voice note (.ogg) → speech-to-text → "add a login page" → opencode → reply
```

Get a free API key from [console.groq.com](https://console.groq.com/keys), then:

```bash
cd ~/telegram-opencode-bot
echo 'STT_API_KEY="gsk_your_key_here"' >> .env
```

Restart the bot. Now voice notes, audio files, videos and video notes all work.
The bot replies with `🎤 <what it heard>` first, so you can tell whether it
misheard you (turn that off with `STT_ECHO=0`).

**Languages.** `STT_LANG` is empty by default, which means the language is
auto-detected — speak Tamil, English, Hindi, anything. Code-mixed speech
(Thanglish) works too; Whisper transcribes it in whichever script it detects,
and opencode understands both. If it keeps guessing wrong, nudge it:

```bash
echo 'STT_PROMPT="Code-mixed Tamil and English (Thanglish) talk about programming."' >> .env
```

Or pin a single language with `STT_LANG="ta"` if you only ever speak one.

> Your audio is uploaded to whatever `STT_URL` points at (Groq by default).
> If that is not acceptable, point `STT_URL` at a local Whisper server instead —
> the bot only needs an OpenAI-compatible `/audio/transcriptions` endpoint.

### 5. Use Grok as the Chat Model (optional)

Text answers go through `opencode`. To make the bot use **Grok (xAI)** instead of
opencode's default model:

```bash
# 1. Add your xAI key to opencode
opencode auth login          # select "xAI" → "Manually enter API Key"
# 2. Pin the model for the bot
echo 'OPENCODE_MODEL="xai/grok-3"' >> ~/telegram-opencode-bot/.env
# 3. Restart
cd ~/telegram-opencode-bot && bash run_bg.sh stop && bash run_bg.sh start
```

Get the key at [console.x.ai](https://console.x.ai/). Any Grok id works, e.g.
`xai/grok-3`, `xai/grok-3-mini`, `xai/grok-beta`. Leave `OPENCODE_MODEL` empty
to keep opencode's default model. Sanity-check a key directly with:

```bash
opencode run --model xai/grok-3 --format json --auto "ping"
```

### 6. Run the Bot

**Foreground (test mode):**
```bash
cd ~/telegram-opencode-bot
python3 bot.py
```

**Background (production):**
```bash
cd ~/telegram-opencode-bot
setsid python3 bot.py </dev/null > bot.log 2>&1 &
```

## Manage the Bot

```bash
# Check if running
pgrep -f bot.py

# Stop
kill $(pgrep -f bot.py)

# View live logs
tail -f ~/telegram-opencode-bot/bot.log

# Restart
kill $(pgrep -f bot.py)
cd ~/telegram-opencode-bot && setsid python3 bot.py </dev/null > bot.log 2>&1 &
```

## Auto-Start on Boot

Add this line to `~/.bashrc`:

```bash
cd ~/telegram-opencode-bot && setsid python3 bot.py </dev/null > bot.log 2>&1 &
```

## Run the Tests

```bash
pip install pytest
cd ~/telegram-opencode-bot
python3 -m pytest tests/ -v
```

Unit tests cover the MarkdownV2 formatter (`esc`, `format_md`, `fallback_format`)
and the JSON response parser (`parse_json`), including edge cases for special
characters, code blocks, links, and unmatched markers.

## File Structure

```
telegram-opencode-bot/
├── bot.py           # Main bot script
├── start.sh         # Run in foreground (checks network first)
├── run_bg.sh        # Background management (start/stop/status/log)
├── check_network.py # Check if Telegram API is reachable
├── .env             # BOT_TOKEN etc. (not hardcoded in code)
├── .secret.key      # Auto-created obfuscation key (chmod 600)
├── bot.lock         # Single-instance file lock (auto-created)
├── bot.pid          # PID of the running instance (auto-created)
├── adminchatid.txt  # Admin chat ID for access requests (auto-created)
├── userchatid.txt   # Authorized user chat IDs (auto-created)
├── sessions.json    # Auto-created per-user sessions (obfuscated)
├── alerts.log       # Crash / repeated-error ALERT signals
├── tests/           # pytest unit tests
└── README.md        # This file
```

## Telegram Formatting Examples

The bot converts AI responses to rich Telegram style:

- **Bold** → `*text*`
- *Italic* → `_text_`
- `Mono` → `` `text` ``
- ~~Strike~~ → `~text~`
- \|\|Spoiler\|\| → `\|\|text\|\|`
- > Quote → `> text`
- Code blocks → ` ```language\ncode\n``` `
- Lists → `• item`
- Links → `[text](url)`

## Troubleshooting

**Bot not responding:**
- Check if running: `pgrep -f bot.py`
- Check logs: `tail -20 ~/telegram-opencode-bot/bot.log`

**Network blocks Telegram (Sophos firewall / VPN required):**
- Some networks (corporate WiFi, certain ISPs) block `api.telegram.org`
- Error log shows: `SSL: CERTIFICATE_VERIFY_FAILED` or `403 Blocked site`
- Fix: switch to mobile data, enable a VPN, or use another network
- Test connectivity first: `python3 check_network.py`
- The bot auto-bypasses SSL cert issues on MITM networks

**Conflict: terminated by other getUpdates request**
- Only **ONE** bot instance may run. All instances call `getUpdates` on the same
  token, and Telegram rejects all but the first.
- If you see this error, kill all instances with `bash run_bg.sh stop`, then
  start once with `bash run_bg.sh start`.
- Do **NOT** run `python3 bot.py` manually while the bot is managed by
  `run_bg.sh`.
- `bot.py` refuses to start a second instance (file-lock on `bot.lock`), and
  `run_bg.sh stop` kills **all** `python.*bot.py` processes so no stray instance
  survives a restart.

**Voice messages reply "speech-to-text is not set up yet":**
- `STT_API_KEY` is missing. Add it to `.env` (see *Enable Voice Messages*) and restart.

**Voice messages are transcribed in the wrong language / wrong script:**
- Set `STT_PROMPT` to a hint about how you speak, or pin `STT_LANG` to one language.
- `whisper-large-v3` is more accurate on non-English than `whisper-large-v3-turbo`.

**"the speech service rejected the API key" / "returned HTTP 4xx":**
- Check the key at [console.groq.com](https://console.groq.com/keys); look in
  `bot.log` for the exact response body.

**opencode errors:**
- Make sure opencode is installed: `opencode --version`
- Check opencode config: `opencode providers`

**Permission errors:**
- Make scripts executable: `chmod +x start.sh run_bg.sh check_network.py`

## How It Works

1. User sends message to Telegram bot
2. Bot downloads any attached files
3. Any audio (voice note, audio file, video, video note) is transcribed to text
   and the audio is deleted — opencode never sees it
4. Runs `opencode run --format json --auto` with the message
5. Parses JSON response stream
6. Converts markdown to Telegram MarkdownV2
7. Sends formatted reply back to user
8. Session is saved for conversation continuity
