#!/usr/bin/env python3
"""
Telegram Bot connected to opencode in Termux.
Rich Telegram formatting, fast replies, full media support.
"""

import asyncio
import base64
import fcntl
import json
import logging
import os
import random
import re
import ssl
import tempfile
import time
from pathlib import Path

# Fix SSL certificates for Termux
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())
except ImportError:
    pass

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import Conflict

# ── Config ────────────────────────────────────────────────────────────────────
def _load_dotenv():
    """Load KEY=VALUE lines from .env next to this script (no hardcoded secrets in code)."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

# The bot token is REQUIRED via the BOT_TOKEN environment variable.
# It is never hardcoded in this file. Run with:
#     BOT_TOKEN="xxxx:yyyy" python3 bot.py
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
# Some networks use MITM proxies that break cert verification. Set 1 to bypass.
INSECURE_SSL = os.environ.get("INSECURE_SSL", "1") == "1"
# Optional: route Telegram API through a proxy/tunnel base URL when your
# network blocks api.telegram.org (e.g. https://your-worker.workers.dev).
BASE_URL = os.environ.get("BOT_API_BASE_URL", "")
OPENCODE_DIR = os.environ.get("OPENCODE_DIR", os.path.expanduser("~"))
SESSION_FILE = Path(__file__).parent / "sessions.json"
AUTH_FILE = Path(__file__).parent / "users.json"
LOCK_FILE = Path(__file__).parent / "bot.lock"
PID_FILE = Path(__file__).parent / "bot.pid"
MAX_MSG = 4000
OPCODE_TIMEOUT = 90
MEDIA_DIR = Path(tempfile.mkdtemp(prefix="tgbot_media_"))

# ── Access Control ────────────────────────────────────────────────────────────
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "8937986952").split(",") if x.strip().isdigit()]
BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "sherlock@")

# ── Fun status messages (shown while waiting for a reply) ────────────────────
STATUS_WORDS = [
    "Cooking", "Thinking hard", "Doing quantum math", "Brewing coffee",
    "Warming up neurons", "Consulting the crystal ball", "Sharpening pencils",
    "Summoning knowledge", "Flipping switches", "Charging the AI core",
    "Reading the manual", "Asking the mothership",
]

# ── Task Management ───────────────────────────────────────────────────────────
TASK_FILE = Path(__file__).parent / "tasks.json"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("bot")


class TokenScrubFilter(logging.Filter):
    """Redact the bot token and other secrets from any log output."""

    def __init__(self):
        super().__init__()
        self._secrets = tuple(s for s in (BOT_TOKEN,) if s)

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        for s in self._secrets:
            msg = msg.replace(s, "<REDACTED>")
        record.msg = msg
        record.args = ()
        return True


logging.getLogger().addFilter(TokenScrubFilter())
# python-telegram-bot / httpx log full request URLs (which contain the token).
# Keep those quiet on top of the redaction filter.
for _name in ("httpx", "httpcore", "telegram", "urllib3"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def require_token():
    """Exit with a clear error if BOT_TOKEN is missing."""
    if not BOT_TOKEN:
        print("")
        print("❌ BOT_TOKEN is not set!")
        print("")
        print("   The token is required via the BOT_TOKEN environment variable.")
        print("   Get one from @BotFather: https://t.me/BotFather")
        print("")
        print("   Then run:")
        print("       BOT_TOKEN='123456:ABC...' python3 bot.py")
        print("   or add it to a `.env` file next to this script.")
        print("")
        raise SystemExit(1)


# ── Obfuscated Storage (sessions / auth / tasks) ──────────────────────────────
def _secret_key() -> bytes:
    """Key used for obfuscation. From BOT_SECRET_KEY or a local key file."""
    key = os.environ.get("BOT_SECRET_KEY", "").strip()
    if not key:
        kf = Path(__file__).parent / ".secret.key"
        try:
            if kf.exists():
                key = kf.read_text().strip()
            if not key:
                key = os.urandom(32).hex()
                kf.write_text(key)
                kf.chmod(0o600)
        except Exception:
            pass
    return (key or "insecure").encode()


def _xor(data: bytes, key: bytes) -> bytes:
    rep = key * (len(data) // len(key) + 1)
    return bytes(a ^ b for a, b in zip(data, rep))


def obfuscate(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(_xor(raw, _secret_key())).decode()


def deobfuscate(s: str):
    raw = base64.urlsafe_b64decode(s.encode())
    return json.loads(_xor(raw, _secret_key()).decode())


def load_stored(path: Path):
    """Read a store file. Supports obfuscated blobs and legacy plain JSON."""
    if not path.exists():
        return None
    try:
        txt = path.read_text().strip()
        if not txt:
            return None
        if txt.startswith(("{", "[")):
            return json.loads(txt)
        return deobfuscate(txt)
    except Exception:
        return None


def store_obj(path: Path, obj) -> bool:
    try:
        path.write_text(obfuscate(obj))
        return True
    except Exception as e:
        log.error(f"Failed to save {path.name}: {e}")
        return False


# ── Sessions ──────────────────────────────────────────────────────────────────
lock = asyncio.Lock()
sessions: dict[int, dict] = {}
tasks: dict[int, asyncio.Task] = {}
sem = asyncio.Semaphore(1)  # 1 concurrent opencode run — avoids slow/crashy parallel runs


def load_sessions():
    global sessions
    data = load_stored(SESSION_FILE)
    if isinstance(data, dict):
        try:
            sessions = {int(k): v for k, v in data.items()}
        except Exception:
            sessions = {k: v for k, v in data.items() if isinstance(k, int)}
    else:
        sessions = {}


def save_sessions():
    if store_obj(SESSION_FILE, {str(k): v for k, v in sessions.items()}):
        return
    # Last resort: keep a plain copy so sessions are never silently lost
    try:
        SESSION_FILE.write_text(json.dumps(sessions, indent=2))
    except Exception:
        pass


async def get_sid(uid):
    async with lock:
        return sessions.get(uid, {}).get("sid", "")


async def set_sid(uid, sid, title=""):
    async with lock:
        sessions[uid] = {"sid": sid, "title": title, "t": time.time()}
        save_sessions()


async def clear_sid(uid):
    async with lock:
        sessions.pop(uid, None)
        save_sessions()


# ── Auth / Access Control ─────────────────────────────────────────────────────
authorized: set[int] = set()


def load_auth():
    global authorized
    data = load_stored(AUTH_FILE)
    if isinstance(data, (list, set)):
        try:
            authorized = set(int(x) for x in data)
        except Exception:
            authorized = set()
    else:
        authorized = set()


def save_auth():
    if not store_obj(AUTH_FILE, sorted(authorized)):
        try:
            AUTH_FILE.write_text(json.dumps(sorted(authorized)))
        except Exception:
            pass


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_authorized(uid: int) -> bool:
    return is_admin(uid) or uid in authorized


def authorize(uid: int):
    authorized.add(uid)
    save_auth()


# ── Task Management ───────────────────────────────────────────────────────────
tasks_db: dict[int, list[dict]] = {}
tasks_lock = asyncio.Lock()


def load_tasks():
    global tasks_db
    data = load_stored(TASK_FILE)
    if isinstance(data, dict):
        try:
            tasks_db = {int(k): v for k, v in data.items()}
        except Exception:
            tasks_db = {}
    else:
        tasks_db = {}


def save_tasks():
    if store_obj(TASK_FILE, {str(k): v for k, v in tasks_db.items()}):
        return
    try:
        TASK_FILE.write_text(json.dumps(tasks_db, indent=2))
    except Exception:
        pass


async def add_todo(uid, text):
    async with tasks_lock:
        todos = tasks_db.setdefault(uid, [])
        nid = (max((t["id"] for t in todos), default=0)) + 1
        todos.append({"id": nid, "text": text, "done": False, "ts": time.time()})
        save_tasks()
        return nid


async def list_todos(uid):
    async with tasks_lock:
        return list(tasks_db.get(uid, []))


async def toggle_todo(uid, tid):
    async with tasks_lock:
        for t in tasks_db.get(uid, []):
            if t["id"] == tid:
                t["done"] = not t["done"]
                save_tasks()
                return t
        return None


async def remove_todo(uid, tid):
    async with tasks_lock:
        todos = tasks_db.get(uid, [])
        rm = [t for t in todos if t["id"] == tid]
        tasks_db[uid] = [t for t in todos if t["id"] != tid]
        save_tasks()
        return rm[0] if rm else None


async def clear_todos(uid):
    async with tasks_lock:
        n = len(tasks_db.get(uid, []))
        tasks_db[uid] = []
        save_tasks()
        return n


# ── Health / Alerting ─────────────────────────────────────────────────────────
ALERT_FILE = Path(__file__).parent / "alerts.log"
_error_streak = 0


def alert(message: str):
    """Log a loud, greppable ALERT signal (and persist it to alerts.log)."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] ALERT: {message}"
    log.critical("ALERT: %s", message)
    try:
        with open(ALERT_FILE, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ── Single Instance Lock ──────────────────────────────────────────────────────
def acquire_singleton():
    """Guarantee only ONE bot process polls Telegram.

    Holds an exclusive flock on bot.lock. A second instance fails to acquire it
    (LOCK_NB) and exits with a clear message instead of causing a getUpdates
    Conflict. The lock is released automatically when the process dies, so stale
    bot.pid entries are never a problem.
    """
    try:
        fh = open(LOCK_FILE, "a+")
    except Exception as e:
        log.error(f"Could not open lock file: {e}")
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    # We hold the lock — record our PID so tools/scripts can show it.
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass
    return fh


# ── OpenCode ──────────────────────────────────────────────────────────────────
# NOTE: --attach (warm server) does NOT return text in opencode 1.18.22,
# so we always run cold (reliable output).

async def run_oc(msg, uid, sid="", files=None, title=""):
    cmd = ["opencode", "run", "--format", "json", "--auto"]
    if sid:
        cmd += ["--session", sid]
    elif title:
        cmd += ["--title", title]
    for f in (files or []):
        if f:
            cmd += ["--file", f]
    cmd.append(msg)

    log.info(f"[{uid}] opencode run (sid={sid[:12] if sid else 'new'})...")
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=OPENCODE_DIR,
        )
        out, _ = await asyncio.wait_for(p.communicate(), timeout=OPCODE_TIMEOUT)
        resp, sid_out = parse_json(out.decode("utf-8", errors="replace"), sid)
        log.info(f"[{uid}] opencode done ({len(out)} bytes out, {len(resp)} chars text)")
        return resp, sid_out
    except asyncio.TimeoutError:
        try:
            p.kill()
        except Exception:
            pass
        log.error(f"[{uid}] opencode timed out after {OPCODE_TIMEOUT}s")
        return "Timed out. Try a shorter message.", ""
    except Exception as e:
        log.error(f"[{uid}] opencode error: {e}")
        return f"Error: {e}", ""


def parse_json(output, fallback=""):
    texts, sid = [], fallback
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            if not line.startswith("{"):
                texts.append(line)
            continue
        if ev.get("type") == "text":
            t = ev.get("part", {}).get("text", "")
            if t:
                texts.append(t)
        s = ev.get("sessionID", "")
        if s:
            sid = s
    resp = "\n".join(texts).strip()
    if not resp:
        for line in output.strip().split("\n"):
            l = line.strip()
            if l and not l.startswith("{"):
                texts.append(l)
        resp = "\n".join(texts).strip()
    return resp or "_No response._", sid


# ── MarkdownV2 Formatter ─────────────────────────────────────────────────────
# Telegram MarkdownV2 treats these as reserved: _ * [ ] ( ) ~ ` > # + - = | { } . !
# Every occurrence in text (outside code/links) MUST be backslash-escaped or the
# whole message fails to parse. The formatter below escapes plain text and only
# the *content* of inline styles; code blocks/link URLs are left verbatim.

_SPECIAL_RE = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')


def esc(text):
    """Escape text for Telegram MarkdownV2. Escapes every reserved char."""
    return _SPECIAL_RE.sub(r'\\\1', text)


def _iter_blocks(text):
    """Tokenize text into (kind, content). kind: text|code|bold|italic|strike|spoiler|link"""
    i, n = 0, len(text)
    doubles = {"**": "bold", "__": "bold", "~~": "strike", "||": "spoiler"}
    while i < n:
        ch = text[i]

        # Inline code `...` — content is verbatim, never escaped
        if ch == "`":
            j = text.find("`", i + 1)
            if j != -1:
                yield ("code", text[i + 1:j])
                i = j + 1
                continue
            yield ("text", ch)
            i += 1
            continue

        # Link [label](url) — label escaped, url verbatim
        if ch == "[":
            c = text.find("]", i + 1)
            if c != -1 and c + 1 < n and text[c + 1] == "(":
                u = text.find(")", c + 2)
                if u != -1:
                    yield ("link", (text[i + 1:c], text[c + 2:u]))
                    i = u + 1
                    continue
            yield ("text", ch)
            i += 1
            continue

        # Plain URL https://... → [url](url), keeps it clickable
        if text.startswith(("http://", "https://"), i):
            m = re.match(r'https?://[^\s)\]]+', text[i:])
            if m:
                url = m.group(0)
                yield ("link", (url, url))
                i += len(url)
                continue

        hit = False
        for marker, kind in doubles.items():
            if text.startswith(marker, i):
                j = text.find(marker, i + len(marker))
                if j != -1:
                    yield (kind, text[i + len(marker):j])
                    i = j + len(marker)
                else:
                    yield ("text", marker)
                    i += len(marker)
                hit = True
                break
        if hit:
            continue

        # Single _ or * → italic (only at word boundaries so snake_case stays text)
        if ch in ("_", "*"):
            before = text[i - 1] if i > 0 else ""
            if not before or before.isspace() or before in "([{":
                j = text.find(ch, i + 1)
                if j != -1:
                    after = text[j + 1] if j + 1 < n else ""
                    if not after or after.isspace() or after in ".,;:!?)]}~":
                        yield ("italic", text[i + 1:j])
                        i = j + 1
                        continue
            yield ("text", ch)
            i += 1
            continue

        yield ("text", ch)
        i += 1


def _md_inline(text):
    parts = []
    for kind, content in _iter_blocks(text):
        if kind == "text":
            parts.append(esc(content))
        elif kind == "code":
            parts.append(f"`{content}`")
        elif kind == "bold":
            parts.append(f"*{esc(content)}*" if content else "")
        elif kind == "italic":
            parts.append(f"_{esc(content)}_" if content else "")
        elif kind == "strike":
            parts.append(f"~{esc(content)}~" if content else "")
        elif kind == "spoiler":
            parts.append(f"||{esc(content)}||" if content else "")
        elif kind == "link":
            label, url = content
            parts.append(f"[{esc(label)}]({url})")
    return "".join(parts)


def format_md(text):
    """Convert AI markdown output to Telegram MarkdownV2.

    Handles: bold, italic, code blocks, inline code, headers,
    lists, blockquotes, spoilers, links, horizontal rules.
    """
    text = text.strip()
    if not text:
        return f"_{esc('No response.')}_", ParseMode.MARKDOWN_V2

    lines = text.split("\n")
    out = []
    in_code = False
    code_lang = ""
    code_lines = []

    for line in lines:
        stripped = line.strip()

        # ── Code block start/end (content kept verbatim)
        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                code_lang = stripped[3:].strip()
                code_lines = []
            else:
                body = "\n".join(code_lines)
                out.append(f"```{code_lang}\n{body}```" if code_lang else f"```\n{body}```")
                in_code = False
                code_lang = ""
            continue

        if in_code:
            code_lines.append(line)
            continue

        # ── Horizontal rule
        if re.match(r'^[\-\*_]{3,}\s*$', stripped):
            out.append("━━━━━━━━━━━━━━")
            continue

        # ── Blockquote
        if stripped.startswith(">"):
            q = stripped.lstrip(">").strip()
            out.append(f"> {_md_inline(q)}")
            continue

        # ── Headers: ### Header → *Header*
        hm = re.match(r'^(#{1,6})\s+(.+)', stripped)
        if hm:
            out.append(f"\n*{_md_inline(hm.group(2).strip())}*\n")
            continue

        # ── Numbered lists: 1. item → 1\. item
        nm = re.match(r'^(\s*)(\d+)\.\s+(.+)', line)
        if nm:
            out.append(f"{nm.group(1)}{nm.group(2)}\\. {_md_inline(nm.group(3))}")
            continue

        # ── Bullet lists: - item / * item → • item
        bm = re.match(r'^(\s*)[-*]\s+(.+)', line)
        if bm:
            out.append(f"{bm.group(1)}• {_md_inline(bm.group(2))}")
            continue

        # ── Plain line
        out.append(_md_inline(line))

    # Code block never closed
    if in_code and code_lines:
        body = "\n".join(code_lines)
        out.append(f"```\n{body}```")

    result = "\n".join(out)
    result = re.sub(r'\r', '', result)
    result = re.sub(r'\n{4,}', '\n\n\n', result)

    return result, ParseMode.MARKDOWN_V2


def fallback_format(text):
    """If MarkdownV2 fails, send as HTML."""
    t = text.strip()
    # Convert code blocks
    t = re.sub(r'```(\w*)\n(.*?)```', lambda m: f'<pre><code class="language-{m.group(1)}">{esc_html(m.group(2))}</code></pre>', t, flags=re.DOTALL)
    t = re.sub(r'`([^`]+)`', lambda m: f'<code>{esc_html(m.group(1))}</code>', t)
    # Bold
    t = re.sub(r'\*\*(.+?)\*\*', lambda m: f'<b>{esc_html(m.group(1))}</b>', t)
    t = re.sub(r'__(.+?)__', lambda m: f'<b>{esc_html(m.group(1))}</b>', t)
    # Italic
    t = re.sub(r'\*(.+?)\*', lambda m: f'<i>{esc_html(m.group(1))}</i>', t)
    t = re.sub(r'_(.+?)_', lambda m: f'<i>{esc_html(m.group(1))}</i>', t)
    # Strikethrough
    t = re.sub(r'~~(.+?)~~', lambda m: f'<s>{esc_html(m.group(1))}</s>', t)
    # Spoiler
    t = re.sub(r'\|\|(.+?)\|\|', lambda m: f'<span class="tg-spoiler">{esc_html(m.group(1))}</span>', t)
    # Blockquote
    t = re.sub(r'^>\s*(.+)$', lambda m: f'<blockquote>{esc_html(m.group(1))}</blockquote>', t, flags=re.MULTILINE)
    # Headers
    t = re.sub(r'^#{1,6}\s+(.+)$', lambda m: f'<b>{esc_html(m.group(1))}</b>', t, flags=re.MULTILINE)
    # Links
    t = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', lambda m: f'<a href="{m.group(2)}">{esc_html(m.group(1))}</a>', t)
    # Lists
    t = re.sub(r'^\s*[-*]\s+', '• ', t, flags=re.MULTILINE)
    return t, ParseMode.HTML


def esc_html(text):
    """Escape HTML entities."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def split_msg(text, mx=MAX_MSG):
    if len(text) <= mx:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > mx:
            if cur:
                chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [text[:mx]]


# ── Handlers ──────────────────────────────────────────────────────────────────
async def deny(u: Update):
    await u.message.reply_text(
        "🔒 *Restricted Access*\n\n"
        "This bot is *private*\\.\n"
        "Send the **password** to continue\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_chatid(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_authorized(uid):
        await deny(u)
        return
    cid = u.effective_chat.id
    await u.message.reply_text(
        f"🆔 *Chat ID*\n\n"
        f"Chat: `{cid}`\n"
        f"User: `{uid}`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    n = esc(u.effective_user.first_name)
    if not is_authorized(uid):
        await u.message.reply_text(
            f"🔒 *Welcome {n}\\!*\n\n"
            f"This bot is *private* and requires a **password**\\.\n"
            f"Send the password as a message to unlock\\.\n\n"
            f"🔑 *Hint\\:* type the secret password here",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    await u.message.reply_text(
        f"👋 *Welcome {n}\\!*\n\n"
        f"🤖 *OpenCode Bot* connected to AI in Termux\n\n"
        f"💬 Send any *message* for AI response\n"
        f"📎 Send *files*, *photos*, *audio*, *video*\n\n"
        f"⚡ Commands:\n"
        f"  /new \\- New conversation\n"
        f"  /status \\- Session info\n"
        f"  /help \\- Help",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_help(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_authorized(uid):
        await deny(u)
        return
    await u.message.reply_text(
        "📖 *How it works:*\n\n"
        "• Type a *message* → get AI answer\n"
        "• *Reply* to add context\n"
        "• Send *files* for analysis\n"
        "• */new* \\= fresh start\n"
        "• */status* \\= session info\n\n"
        "🔧 *Supports:*\n"
        "Text, code, files, photos, audio, video, voice\\.\n\n"
        "Everything goes through *opencode*\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_new(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_authorized(uid):
        await deny(u)
        return
    await clear_sid(uid)
    await u.message.reply_text(
        "✅ *Session cleared\\!* Fresh start\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_status(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_authorized(uid):
        await deny(u)
        return
    async with lock:
        s = sessions.get(uid)
    if s:
        sid = s.get("sid", "N/A")[:24]
        ct = time.strftime("%H:%M:%S", time.localtime(s.get("t", 0)))
        await u.message.reply_text(
            f"📊 *Session Info*\n\n"
            f"ID: `{sid}`\\.\\.\\.\n"
            f"Last: {ct}\n"
            f"Status: `active`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await u.message.reply_text(
            "📊 *No session yet*\\.\nSend a message to start\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ── Task handlers ─────────────────────────────────────────────────────────────
async def cmd_task(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_authorized(uid):
        await deny(u)
        return
    args = ctx.args

    if not args or args[0].lower() in ("list", "ls"):
        await _task_list(u, uid)
        return

    action = args[0].lower()

    if action in ("add", "a", "+"):
        text = " ".join(args[1:]).strip()
        if not text:
            await u.message.reply_text("Usage: `/task add <text>`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        nid = await add_todo(uid, text)
        await u.message.reply_text(
            f"✅ Task #{nid} added\\.\n`{esc(text)}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if action in ("done", "d", "complete", "check", "x"):
        if len(args) < 2 or not args[1].isdigit():
            await u.message.reply_text("Usage: `/task done <id>`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        t = await toggle_todo(uid, int(args[1]))
        if t:
            mark = "✅ *completed\\!*" if t["done"] else "↩️ *reopened*"
            await u.message.reply_text(
                f"Task #{t['id']} {mark}\n`{esc(t['text'])}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await u.message.reply_text("❌ Task not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if action in ("rm", "del", "delete", "remove"):
        if len(args) < 2 or not args[1].isdigit():
            await u.message.reply_text("Usage: `/task rm <id>`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        t = await remove_todo(uid, int(args[1]))
        if t:
            await u.message.reply_text(
                f"🗑️ Removed task #{t['id']}\\.\n`{esc(t['text'])}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await u.message.reply_text("❌ Task not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if action in ("clear", "clr", "reset"):
        n = await clear_todos(uid)
        await u.message.reply_text(f"🧹 Cleared all {n} tasks\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    await _task_list(u, uid)


async def _task_list(u: Update, uid: int):
    todos = await list_todos(uid)
    if not todos:
        await u.message.reply_text(
            "📋 *No tasks yet\\.*\nAdd one: `/task add <text>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    lines = []
    for t in todos:
        mark = "✅" if t["done"] else "⬜"
        done = " **~DONE~**" if t["done"] else ""
        lines.append(f"{mark} `#{t['id']}` {esc(t['text'])}{done}")
    header = f"📋 *Your Tasks* \\({len([t for t in todos if not t['done']])} open\\):\n\n"
    await u.message.reply_text(
        header + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_login(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if is_authorized(uid):
        await u.message.reply_text("✅ *Already authorized\\!*", parse_mode=ParseMode.MARKDOWN_V2)
        return
    pw = ctx.args[0] if ctx.args else ""
    if pw == BOT_PASSWORD:
        authorize(uid)
        await u.message.reply_text(
            "✅ *Access granted\\!* Welcome to OpenCode Bot\\.\n"
            "Send a message to start\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await u.message.reply_text("❌ *Wrong password*\\. Try again\\.", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_revoke(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_admin(uid):
        await deny(u)
        return
    args = ctx.args
    if args and args[0].strip().isdigit():
        target = int(args[0])
        authorized.discard(target)
        save_auth()
        await u.message.reply_text(f"🔓 Access revoked for `{target}`\\.", parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await u.message.reply_text(
            "Usage: /revoke <user\\_id>\n\nExample: `/revoke 123456789`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def cmd_users(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_admin(uid):
        await deny(u)
        return
    users = sorted(authorized)
    if users:
        lines = "\n".join(f"`{x}`" for x in users)
        await u.message.reply_text(
            f"👥 *Authorized Users*\n\n{lines}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await u.message.reply_text("👥 *No authorized users yet\\.*", parse_mode=ParseMode.MARKDOWN_V2)


async def handle_msg(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _error_streak
    msg = u.message or u.edited_message
    if not msg:
        return

    uid = msg.from_user.id
    cid = msg.chat.id

    # ── Access gate: admin allowed, others need password ──
    if not is_authorized(uid):
        guess = (msg.text or "").strip()
        if guess and guess == BOT_PASSWORD:
            authorize(uid)
            await msg.reply_text(
                "✅ *Access granted\\!*\n"
                "You can now use the bot\\. Send a message to start\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        await deny(u)
        return

    # Prevent concurrent requests per user
    if uid in tasks and not tasks[uid].done():
        await msg.reply_text("⏳ Wait for previous answer\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Extract content
    text = msg.text or msg.caption or ""
    files = []

    if msg.document:
        files.append(await dl(ctx.bot, msg.document, f"doc_{msg.message_id}"))
    if msg.photo:
        p = max(msg.photo, key=lambda x: x.file_size)
        files.append(await dl(ctx.bot, p, f"photo_{msg.message_id}.jpg"))
    if msg.audio:
        files.append(await dl(ctx.bot, msg.audio, f"audio_{msg.message_id}.mp3"))
    if msg.voice:
        files.append(await dl(ctx.bot, msg.voice, f"voice_{msg.message_id}.ogg"))
    if msg.video:
        files.append(await dl(ctx.bot, msg.video, f"video_{msg.message_id}.mp4"))
    if msg.video_note:
        files.append(await dl(ctx.bot, msg.video_note, f"vn_{msg.message_id}.mp4"))
    if msg.sticker:
        ext = "webm" if msg.sticker.is_video else "webp"
        if not msg.sticker.is_animated:
            files.append(await dl(ctx.bot, msg.sticker, f"stk_{msg.message_id}.{ext}"))

    if not text and not files:
        await msg.reply_text("🤔 Send a message or file\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Reply context
    if msg.reply_to_message:
        r = msg.reply_to_message
        if r.text:
            text = f'Context: "{r.text[:500]}"\n\n{text}' if text else f'About this: "{r.text[:500]}"'
        elif r.caption:
            text = f'About: "{r.caption[:500]}"\n\n{text}' if text else f'About: "{r.caption[:500]}"'

    if not text and files:
        text = "Analyze this file."

    if files and not text.startswith("Analyze"):
        names = [os.path.basename(f) for f in files if f]
        if names:
            text = f"{text}\n\nFiles: {', '.join(names)}"

    # Send "typing..." immediately
    await ctx.bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING)

    # Random reaction emoji for instant feedback
    try:
        await msg.set_reaction(random.choice(["⚡", "🤔", "🔥", "✨", "💭", "👀", "🚀", "🧠", "💯", "🔍"]))
    except Exception:
        pass

    async def process():
        ack = None
        anim_task = None
        stop_anim = None
        sent = False
        try:
            async with sem:
                # Funny random status bubble — animates with "..." while we wait
                stop_anim = asyncio.Event()
                start_word = random.choice(STATUS_WORDS)
                ack = await msg.reply_text(f"{start_word}...")
                anim_task = asyncio.create_task(animate_status(ack, stop_anim))
                log.info(f"[{uid}] Processing: {text[:80]}")

                await ctx.bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING)

                sid = await get_sid(uid)
                title = f"tg_{uid}" if not sid else ""
                resp, new_sid = await run_oc(text, uid, sid, files, title)

                # Retry once fresh if the model returned nothing useful
                if not resp or resp == "_No response._":
                    log.warning(f"[{uid}] Empty response, retrying fresh...")
                    resp, new_sid = await run_oc(text, uid, "", files, title)

                log.info(f"[{uid}] Got response ({len(resp)} chars), session={new_sid[:12] or 'none'}")
                if new_sid and new_sid != sid:
                    await set_sid(uid, new_sid, title)

                if anim_task:
                    stop_anim.set()
                    await asyncio.gather(anim_task, return_exceptions=True)

                await ctx.bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING)

                # Remove the animation bubble, then show a clean answer
                try:
                    if ack:
                        await ack.delete()
                        ack = None
                except Exception:
                    pass

                for chunk in split_msg(resp):
                    ok = await send_answer(ctx.bot, cid, chunk)
                    sent = sent or ok

                if sent:
                    _error_streak = 0

                if not resp or resp.strip() == "":
                    log.error(f"[{uid}] Answer still empty after retry!")
                    alert(f"Empty answer for user {uid}")
                    await ctx.bot.send_message(cid, "⚠️ Couldn't generate a response. Try again.")

        except Exception as e:
            log.error(f"[{uid}] Error: {e}", exc_info=True)
            try:
                if ack:
                    await ack.edit_text(f"❌ Error: {str(e)[:500]}")
                else:
                    await ctx.bot.send_message(cid, f"❌ Error: {str(e)[:500]}")
            except Exception:
                pass
        finally:
            tasks.pop(uid, None)
            if stop_anim:
                stop_anim.set()
            if anim_task:
                await asyncio.gather(anim_task, return_exceptions=True)
            for f in files:
                try:
                    if f and os.path.exists(f):
                        os.remove(f)
                except Exception:
                    pass

    tasks[uid] = asyncio.create_task(process())


async def dl(bot, obj, name):
    try:
        f = await bot.get_file(obj.file_id)
        path = str(MEDIA_DIR / name)
        await f.download_to_drive(path)
        return path
    except Exception as e:
        log.error(f"Download failed: {e}")
        return None


async def animate_status(msg, stop_event: asyncio.Event):
    """Animate the status bubble — cycling dots + fresh random words until stopped."""
    dots = ["", ".", "..", "..."]
    base = random.choice(STATUS_WORDS)
    i = 0
    while not stop_event.is_set():
        try:
            await msg.edit_text(base + dots[i], disable_web_page_preview=True)
        except Exception:
            pass
        i = (i + 1) % len(dots)
        if i == 0:
            base = random.choice(STATUS_WORDS)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.2)
        except asyncio.TimeoutError:
            continue


async def send_answer(bot, chat_id, text):
    """Send one answer chunk. Try MarkdownV2 → HTML → plain. Returns success."""
    try:
        formatted, pm = format_md(text)
        await bot.send_message(
            chat_id=chat_id, text=formatted, parse_mode=pm,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e1:
        log.warning(f"Markdown send failed: {e1}")
        try:
            h_text, h_pm = fallback_format(text)
            await bot.send_message(
                chat_id=chat_id, text=h_text, parse_mode=h_pm,
                disable_web_page_preview=True,
            )
            return True
        except Exception as e2:
            log.warning(f"HTML send failed: {e2}")
            try:
                await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
                return True
            except Exception as e3:
                log.error(f"Plain send failed: {e3}")
                return False


async def on_error(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _error_streak
    _error_streak += 1
    err = f"{type(ctx.error).__name__}: {str(ctx.error)[:200]}"
    log.error(f"Error: {err}", exc_info=ctx.error)
    if _error_streak >= 3:
        alert(f"{_error_streak} consecutive errors — latest: {err}")
        _error_streak = 0
    if u and u.effective_message:
        try:
            await u.effective_message.reply_text("❌ Error\\. Use /new to reset\\.", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            pass


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Start bot"),
        BotCommand("login", "Unlock bot with password"),
        BotCommand("task", "Manage your tasks"),
        BotCommand("new", "New conversation"),
        BotCommand("status", "Session info"),
        BotCommand("help", "Help"),
        BotCommand("users", "List authorized users (admin)"),
        BotCommand("revoke", "Revoke access (admin)"),
    ])


def build_app():
    require_token()
    builder = Application.builder().token(BOT_TOKEN).post_init(post_init)

    if INSECURE_SSL:
        from telegram.request import HTTPXRequest
        builder.request(HTTPXRequest(httpx_kwargs={"verify": False, "follow_redirects": True}))
    elif BASE_URL:
        builder.base_url(BASE_URL)

    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_msg))
    app.add_handler(MessageHandler(filters.PHOTO, handle_msg))
    app.add_handler(MessageHandler(filters.AUDIO, handle_msg))
    app.add_handler(MessageHandler(filters.VIDEO, handle_msg))
    app.add_handler(MessageHandler(filters.VOICE, handle_msg))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_msg))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_msg))
    app.add_error_handler(on_error)
    return app


def main():
    require_token()

    # ── Enforce a single instance BEFORE touching Telegram ──
    lock_fh = acquire_singleton()
    if lock_fh is None:
        print("")
        print("❌ Another bot instance is already running.")
        print("")
        print("   Only ONE bot instance may run.")
        print("   Kill all instances and start once:")
        print("       bash run_bg.sh stop")
        print("       bash run_bg.sh start")
        print("   Do NOT run 'python3 bot.py' manually while it is managed by run_bg.sh.")
        print("")
        log.error("Duplicate instance refused (lock held by another process)")
        raise SystemExit(1)

    load_sessions()
    load_auth()
    load_tasks()

    while True:
        try:
            app = build_app()
            log.info("Bot starting...")
            app.run_polling(drop_pending_updates=True)
            log.info("Bot stopped. Exiting.")
            break
        except KeyboardInterrupt:
            log.info("Interrupted by user. Exiting.")
            break
        except SystemExit:
            raise
        except Conflict as e:
            # Another instance grabbed getUpdates — do NOT retry-loop forever.
            log.error(f"Telegram Conflict (duplicate instance?): {e}")
            alert(f"getUpdates conflict — another bot instance may be running: {e}")
            print("\n❌ Conflict: this bot token is being polled by another instance.")
            print("   Kill all instances and start once:")
            print("       bash run_bg.sh stop")
            print("       bash run_bg.sh start\n")
            break
        except Exception as e:
            log.error(f"Bot crashed: {e}. Restarting in 5s...")
            alert(f"Bot crashed and is restarting: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()