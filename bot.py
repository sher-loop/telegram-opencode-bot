#!/usr/bin/env python3
"""
Telegram Bot connected to opencode in Termux.
Rich Telegram formatting, fast replies, full media support.
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import signal
import ssl

# POSIX-only; used for the single-instance file lock. Absent on Windows dev
# boxes (where the tests still need to import this module), so degrade gracefully.
try:
    import fcntl
except ImportError:
    fcntl = None
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

import httpx

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
# Optional: pin the opencode model used for text replies, e.g. "xai/grok-3"
# (Grok) or "xai/grok-beta". When empty, opencode uses its default model.
OC_MODEL = os.environ.get("OPENCODE_MODEL", "").strip()
SESSION_FILE = Path(__file__).parent / "sessions.json"
AUTH_FILE = Path(__file__).parent / "users.json"   # legacy store (migrated once)
CHATID_FILE = Path(__file__).parent / "userchatid.txt"   # authorized user chat IDs
ADMIN_CHAT_FILE = Path(__file__).parent / "adminchatid.txt"  # admin chat ID
LOCK_FILE = Path(__file__).parent / "bot.lock"
PID_FILE = Path(__file__).parent / "bot.pid"
MAX_MSG = 4000
MEDIA_DIR = Path(tempfile.mkdtemp(prefix="tgbot_media_"))

# ── Access Control ────────────────────────────────────────────────────────────
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "8937986952").split(",") if x.strip().isdigit()]

# ── Fun status messages (shown while waiting for a reply) ────────────────────
STATUS_WORDS = [
    "Cooking", "Thinking hard", "Doing quantum math", "Brewing coffee",
    "Warming up neurons", "Consulting the crystal ball", "Sharpening pencils",
    "Summoning knowledge", "Flipping switches", "Charging the AI core",
    "Reading the manual", "Asking the mothership",
]

# ── Task Management ───────────────────────────────────────────────────────────
TASK_FILE = Path(__file__).parent / "tasks.json"


# ── Speech-to-Text ────────────────────────────────────────────────────────────
# opencode has no audio support, so voice notes / audio / video are transcribed
# to text HERE and only the text is handed to opencode. Any Whisper-compatible
# endpoint works; the default is Groq (fast, accepts Telegram's .ogg directly).
def _env_num(name, default, cast=int):
    """Read a numeric env var, falling back to `default` on junk input."""
    try:
        return cast(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# ── OpenCode runtime ──────────────────────────────────────────────────────────
# opencode cold-starts a full agent on every message; on Termux that can take a
# while. The old hard 90s cutoff was too aggressive and produced frequent
# "timed out" replies (and it threw away answers that were seconds from arriving),
# so the default is higher and tunable via .env.
OPCODE_TIMEOUT = _env_num("OPCODE_TIMEOUT", 180)
# How many opencode runs may execute at once. 1 is safest on low-RAM Termux
# devices (parallel cold agents get slow/crashy); bump it only on a beefier host.
OC_CONCURRENCY = max(1, _env_num("OPENCODE_CONCURRENCY", 1))

STT_API_KEY = os.environ.get("STT_API_KEY", "").strip()
STT_URL = os.environ.get(
    "STT_URL", "https://api.groq.com/openai/v1/audio/transcriptions"
).strip()
STT_MODEL = os.environ.get("STT_MODEL", "whisper-large-v3").strip()
# Empty = auto-detect, which is what makes every language (and code-mixed
# speech like Thanglish) work. Pin it only to force one language.
STT_LANG = os.environ.get("STT_LANG", "").strip()
# Optional vocabulary/style hint passed to Whisper, e.g.
#   STT_PROMPT="Code-mixed Tamil and English (Thanglish) talk about programming."
STT_PROMPT = os.environ.get("STT_PROMPT", "").strip()
STT_TIMEOUT = _env_num("STT_TIMEOUT", 60)
STT_MAX_MB = _env_num("STT_MAX_MB", 24, float)  # Groq caps uploads at 25 MB
STT_ECHO = os.environ.get("STT_ECHO", "1") == "1"  # show what the bot heard

# Extensions the Whisper endpoints accept, and how to guess one per MIME type.
STT_FORMATS = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga",
               ".oga", ".ogg", ".opus", ".wav", ".webm"}
STT_EXT_BY_MIME = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
    "audio/m4a": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".m4a",
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/vorbis": ".ogg",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/webm": ".webm", "audio/flac": ".flac", "audio/x-flac": ".flac",
    "video/mp4": ".mp4", "video/quicktime": ".mp4", "video/webm": ".webm",
}

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("bot")


class TokenScrubFilter(logging.Filter):
    """Redact the bot token and other secrets from any log output."""

    def __init__(self):
        super().__init__()
        self._secrets = tuple(s for s in (BOT_TOKEN, STT_API_KEY) if s)

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


# A filter on a logger only sees records logged directly to that logger —
# records from child loggers ("bot", "httpx", ...) propagate straight to the
# root HANDLERS without passing the root logger's filters. Attach to the
# handlers instead, or nothing here is actually redacted.
_scrub = TokenScrubFilter()
for _h in logging.getLogger().handlers:
    _h.addFilter(_scrub)
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
sem = asyncio.Semaphore(OC_CONCURRENCY)  # concurrent opencode runs (OPENCODE_CONCURRENCY, default 1)


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
# Access is granted by the ADMIN (no passwords). Unauthorized users asking to
# use the bot trigger an approval request to the admin chat; approving adds the
# user's chat ID (and display name) to userchatid.txt.
# Format of userchatid.txt: one line per user -> "chat_id  display_name"
authorized: set[int] = set()
user_names: dict[int, str] = {}


def load_auth():
    global authorized, user_names
    authorized = set()
    user_names = {}
    if CHATID_FILE.exists():
        for line in CHATID_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if parts[0].lstrip("-").isdigit():
                cid = int(parts[0])
                authorized.add(cid)
                user_names[cid] = parts[1] if len(parts) > 1 else ""
        return
    # Migration: import from the legacy users.json store if present
    data = load_stored(AUTH_FILE)
    if isinstance(data, (list, set)):
        try:
            authorized = set(int(x) for x in data)
        except Exception:
            authorized = set()
        save_auth()


def save_auth():
    try:
        lines = []
        for cid in sorted(authorized):
            name = (user_names.get(cid) or "").strip()
            lines.append(f"{cid} {name}".rstrip() if name else str(cid))
        CHATID_FILE.write_text("\n".join(lines) + ("\n" if lines else ""))
    except Exception as e:
        log.error(f"Failed to save userchatid.txt: {e}")
        try:
            AUTH_FILE.write_text(json.dumps(sorted(authorized)))
        except Exception:
            pass


def load_admin_chat() -> int | None:
    """Admin chat ID from adminchatid.txt. Falls back to / creates the default."""
    if ADMIN_CHAT_FILE.exists():
        txt = ADMIN_CHAT_FILE.read_text().strip()
        if txt.lstrip("-").isdigit():
            return int(txt)
    chat = ADMIN_IDS[0] if ADMIN_IDS else None
    if chat is not None:
        try:
            ADMIN_CHAT_FILE.write_text(str(chat))
        except Exception:
            pass
    return chat


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_authorized(uid: int) -> bool:
    return is_admin(uid) or uid in authorized


def authorize(uid: int, name: str = ""):
    authorized.add(uid)
    if name:
        user_names[uid] = name
    save_auth()


def revoke(uid: int):
    authorized.discard(uid)
    user_names.pop(uid, None)
    save_auth()


async def fetch_user_name(ctx, cid) -> str:
    """Best-effort display name for a chat ID (first/last name, then @username)."""
    try:
        c = await ctx.bot.get_chat(cid)
        name = " ".join(p for p in (c.first_name, getattr(c, "last_name", None)) if p)
        name = name.strip()
        if name:
            return name
        return f"@{c.username}" if getattr(c, "username", None) else ""
    except Exception:
        return ""


# ── Access Requests (sent to the admin for approval) ─────────────────────────
REQUEST_COOLDOWN = 300  # seconds — don't spam the admin with duplicate requests
pending_requests: dict[int, tuple[float, str]] = {}


async def request_access(ctx, cid, uid, name):
    """Notify the admin that `cid` wants access, with Approve/Deny buttons."""
    admin_chat = load_admin_chat()
    if admin_chat is None:
        log.error("No admin chat configured (set ADMIN_IDS or adminchatid.txt)")
        return False
    now = time.time()
    if now - pending_requests.get(cid, (0, ""))[0] < REQUEST_COOLDOWN:
        return True  # already requested recently — don't notify again
    pending_requests[cid] = (now, name)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"access:{cid}"),
         InlineKeyboardButton("🚫 Deny", callback_data=f"deny:{cid}")],
    ])
    text = (
        "🔔 *Access Request*\n\n"
        f"User: `{esc(name)}`\n"
        f"User ID: `{uid}`\n"
        f"Chat ID: `{cid}`\n\n"
        "Press a button below, or use `/approve <id>` / `/deny <id>`\\."
    )
    try:
        await ctx.bot.send_message(
            admin_chat, text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb,
        )
        return True
    except Exception as e:
        log.error(f"Failed to notify admin about access request: {e}")
        return False


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
    if fcntl is None:
        # No flock available (e.g. Windows dev box). Skip the guarantee — the
        # single-instance protection only matters on the Termux/Linux host.
        log.warning("fcntl unavailable; single-instance lock disabled on this platform")
    else:
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


# ── Speech-to-Text ────────────────────────────────────────────────────────────
def audio_ext(obj, default=".mp3") -> str:
    """Pick a file extension the speech endpoint understands.

    Telegram gives us `file_name` and/or `mime_type`; Whisper decides how to
    decode from the extension, so an .mp3 named blob that is really .m4a fails.
    """
    name = getattr(obj, "file_name", None) or ""
    ext = os.path.splitext(name)[1].lower()
    if ext in STT_FORMATS:
        return ext
    mime = (getattr(obj, "mime_type", None) or "").lower().split(";")[0].strip()
    return STT_EXT_BY_MIME.get(mime, default)


async def transcribe(path) -> tuple[str, str]:
    """Turn one audio/video file into text. Returns (transcript, error_reason).

    opencode cannot read audio at all, so this runs first and only the text is
    forwarded. `STT_LANG` is empty by default => the model auto-detects, which
    is what lets any language (including code-mixed speech) come through.
    """
    if not path or not os.path.exists(path):
        return "", "the audio could not be downloaded"
    if not STT_API_KEY:
        return "", "speech-to-text is not set up yet (set STT_API_KEY)"

    size = os.path.getsize(path)
    if size == 0:
        return "", "the audio file is empty"
    if size > STT_MAX_MB * 1024 * 1024:
        return "", f"the audio is too big ({size / 1048576:.1f} MB, limit {STT_MAX_MB:g} MB)"

    form = {"model": STT_MODEL, "response_format": "verbose_json"}
    if STT_LANG:
        form["language"] = STT_LANG
    if STT_PROMPT:
        form["prompt"] = STT_PROMPT

    try:
        with open(path, "rb") as fh:
            blob = fh.read()
        async with httpx.AsyncClient(timeout=STT_TIMEOUT, verify=not INSECURE_SSL) as c:
            r = await c.post(
                STT_URL,
                headers={"Authorization": f"Bearer {STT_API_KEY}"},
                files={"file": (os.path.basename(path), blob, "application/octet-stream")},
                data=form,
            )
        if r.status_code != 200:
            log.error(f"STT HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code in (401, 403):
                return "", "the speech service rejected the API key"
            if r.status_code == 429:
                return "", "the speech service is rate limited — try again in a moment"
            return "", f"the speech service returned HTTP {r.status_code}"
        body = r.json()
    except Exception as e:
        log.error(f"STT failed: {type(e).__name__}: {e}")
        return "", "the speech service could not be reached"

    txt = (body.get("text") or "").strip() if isinstance(body, dict) else ""
    if not txt:
        return "", "no speech was detected in that audio"
    lang = (body.get("language") or "").strip() or "auto"
    log.info(f"STT ok: {len(txt)} chars, lang={lang}")
    return txt, ""


# ── OpenCode ──────────────────────────────────────────────────────────────────
# NOTE: --attach (warm server) does NOT return text in opencode 1.18.22,
# so we always run cold (reliable output).

def _kill_tree(p):
    """Kill an opencode run *and every child it spawned* (Node workers, etc.).

    A plain p.kill() only signals the direct child; the node subprocesses it
    started keep running, hog CPU/RAM on a small Termux device and slow the very
    next reply. Because we launch opencode with start_new_session=True it leads
    its own process group, so on POSIX we signal the whole group in one shot."""
    if p is None or p.returncode is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        else:
            p.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.kill()
        except Exception:
            pass


async def run_oc(msg, uid, sid="", files=None, title=""):
    cmd = ["opencode", "run", "--format", "json", "--auto"]
    if OC_MODEL:
        cmd += ["--model", OC_MODEL]
    if sid:
        cmd += ["--session", sid]
    elif title:
        cmd += ["--title", title]
    for f in (files or []):
        if f:
            cmd += ["--file", f]
    cmd.append(msg)

    log.info(f"[{uid}] opencode run (sid={sid[:12] if sid else 'new'})...")
    p = None
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=OPENCODE_DIR,
            start_new_session=True,  # own process group, so a timeout kills the whole tree
        )
        out, _ = await asyncio.wait_for(p.communicate(), timeout=OPCODE_TIMEOUT)
        resp, sid_out = parse_json(out.decode("utf-8", errors="replace"), sid)
        log.info(f"[{uid}] opencode done ({len(out)} bytes out, {len(resp)} chars text)")
        return resp, sid_out
    except asyncio.TimeoutError:
        _kill_tree(p)
        # Salvage anything opencode already streamed before we gave up — a slow
        # run is often only seconds from done, and showing that answer beats a
        # bare "timed out". Best effort: the process is dead, so this drains fast.
        salvaged = b""
        try:
            salvaged, _ = await asyncio.wait_for(p.communicate(), timeout=5)
        except Exception:
            pass
        resp, sid_out = parse_json(salvaged.decode("utf-8", errors="replace"), sid)
        if resp and resp != "_No response._":
            log.warning(f"[{uid}] opencode hit {OPCODE_TIMEOUT}s; salvaged {len(resp)} chars")
            return resp, sid_out
        log.error(f"[{uid}] opencode timed out after {OPCODE_TIMEOUT}s (no text)")
        return "That took too long. Try again, or send a shorter message.", ""
    except Exception as e:
        _kill_tree(p)
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
async def deny(u: Update, ctx: ContextTypes.DEFAULT_TYPE = None):
    """Unauthorized user → request approval from the admin, then reply."""
    msg = u.effective_message
    if not msg:
        return
    user = u.effective_user
    name = user.full_name or user.first_name or str(user.id)
    sent = False
    if ctx is not None:
        sent = await request_access(ctx, msg.chat.id, user.id, name)
    if sent:
        await msg.reply_text(
            "🔒 *Access Request Sent\\!*\n\n"
            "The *admin* has been notified\\.\n"
            "Wait for approval to use the bot\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await msg.reply_text(
            "🔒 *Restricted Access*\n\n"
            "This bot is *private\\.*\n"
            "Ask the **admin** to give you access\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def cmd_chatid(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_authorized(uid):
        await deny(u, ctx)
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
            f"This bot is *private*\\.\n"
            f"An *access request* will be sent to the **admin**\\.\n"
            f"Wait for approval to start using it\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await request_access(
            ctx, u.effective_chat.id, uid,
            u.effective_user.full_name or u.effective_user.first_name or str(uid),
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
        await deny(u, ctx)
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
        await deny(u, ctx)
        return
    await clear_sid(uid)
    await u.message.reply_text(
        "✅ *Session cleared\\!* Fresh start\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_status(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_authorized(uid):
        await deny(u, ctx)
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
        await deny(u, ctx)
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


async def cmd_approve(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_admin(uid):
        await deny(u, ctx)
        return
    args = ctx.args
    if not args or not args[0].strip().lstrip("-").isdigit():
        await u.message.reply_text("Usage: `/approve <chat_id>`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    target = int(args[0].strip())
    name = await fetch_user_name(ctx, target)
    authorize(target, name)
    shown = esc(name) if name else f"`{target}`"
    await u.message.reply_text(
        f"✅ *Access granted* for {shown}\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    try:
        await ctx.bot.send_message(
            target,
            "✅ *Access granted\\!* You can now use the bot\\. Send a message to start\\.",
        )
    except Exception:
        pass


async def cmd_deny(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_admin(uid):
        await deny(u, ctx)
        return
    args = ctx.args
    if not args or not args[0].strip().lstrip("-").isdigit():
        await u.message.reply_text("Usage: `/deny <chat_id>`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    target = int(args[0].strip())
    revoke(target)
    await u.message.reply_text(f"🚫 *Access denied* for `{target}`\\.", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        await ctx.bot.send_message(target, "🚫 *Access denied*\\.. Ask the admin again\\.")
    except Exception:
        pass


async def on_callback(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    if not q:
        return
    data = q.data or ""
    if not data.startswith(("access:", "deny:")):
        await q.answer()
        return
    if not is_admin(u.effective_user.id):
        await q.answer("Only the admin can approve or deny.", show_alert=True)
        return
    try:
        target = int(data.split(":", 1)[1])
    except ValueError:
        await q.answer("Invalid chat ID.", show_alert=True)
        return
    if data.startswith("access:"):
        name = pending_requests.get(target, ("", ""))[1] or ""
        if not name:
            name = await fetch_user_name(ctx, target)
        authorize(target, name)
        shown = esc(name) if name else f"`{target}`"
        await q.edit_message_text(f"✅ *Access granted\\:* {shown}")
        try:
            await ctx.bot.send_message(
                target,
                "✅ *Access granted\\!* You can now use the bot\\. Send a message to start\\.",
            )
        except Exception:
            pass
    else:
        revoke(target)
        await q.edit_message_text("🚫 *Access denied\\..*")
        try:
            await ctx.bot.send_message(target, "🚫 *Access denied\\..* Ask the admin again\\.")
        except Exception:
            pass
    await q.answer()


async def cmd_revoke(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_admin(uid):
        await deny(u, ctx)
        return
    args = ctx.args
    if args and args[0].strip().isdigit():
        target = int(args[0])
        revoke(target)
        await u.message.reply_text(f"🔓 Access revoked for `{target}`\\.", parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await u.message.reply_text(
            "Usage: /revoke <user\\_id>\n\nExample: `/revoke 123456789`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _user_list(u: Update):
    """Show every authorized user with name + chat ID."""
    if authorized:
        lines = []
        for cid in sorted(authorized):
            name = (user_names.get(cid) or "").strip()
            nm = esc(name) if name else "*unknown*"
            lines.append(f"`{cid}` \\- {nm}")
        await u.message.reply_text(
            f"👥 *Authorized Users* \\({len(authorized)}\\):\n\n" + "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await u.message.reply_text("👥 *No authorized users yet\\.*", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_users(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if not is_admin(uid):
        await deny(u, ctx)
        return
    await _user_list(u)


async def cmd_user(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel: `/user` (list), `/user add <chat_id> [name]`, `/user remove <chat_id>`."""
    uid = u.effective_user.id
    if not is_admin(uid):
        await deny(u, ctx)
        return
    args = ctx.args
    action = args[0].lower() if args else "list"

    if action in ("list", "ls", "show"):
        await _user_list(u)
        return

    if action in ("add", "a", "+"):
        if len(args) < 2 or not args[1].strip().lstrip("-").isdigit():
            await u.message.reply_text(
                "Usage: `/user add <chat_id> [name]`", parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        target = int(args[1].strip())
        name = " ".join(args[2:]).strip()
        if not name:
            name = await fetch_user_name(ctx, target)
        authorize(target, name)
        shown = esc(name) if name else f"`{target}`"
        await u.message.reply_text(
            f"➕ Added {shown} \\(`{target}`\\)\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        try:
            await ctx.bot.send_message(
                target,
                "✅ *Access granted\\!* You can now use the bot\\. Send a message to start\\.",
            )
        except Exception:
            pass
        return

    if action in ("remove", "rm", "del", "delete", "-"):
        if len(args) < 2 or not args[1].strip().lstrip("-").isdigit():
            await u.message.reply_text(
                "Usage: `/user remove <chat_id>`", parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        target = int(args[1].strip())
        revoke(target)
        await u.message.reply_text(
            f"➖ Removed `{target}`\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        try:
            await ctx.bot.send_message(target, "🔒 *Access revoked*\\.. Ask the admin again\\.")
        except Exception:
            pass
        return

    await u.message.reply_text(
        "Usage:\n"
        "  /user \\- list users\n"
        "  `/user add <chat\\_id> [name]` \\- add user\n"
        "  `/user remove <chat\\_id>` \\- remove user",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_msg(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _error_streak
    msg = u.message or u.edited_message
    if not msg:
        return

    uid = msg.from_user.id
    cid = msg.chat.id

    # ── Access gate: unauthorized users must be approved by the admin ──
    if not is_authorized(uid):
        await deny(u, ctx)
        return

    # Prevent concurrent requests per user
    if uid in tasks and not tasks[uid].done():
        await msg.reply_text("⏳ Wait for previous answer\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Show we picked the message up before the (possibly slow) downloads
    await ctx.bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING)

    # Extract content
    text = msg.text or msg.caption or ""
    files = []        # handed to opencode as --file
    audio = []        # transcribed here instead — opencode cannot read audio

    if msg.document:
        files.append(await dl(ctx.bot, msg.document, f"doc_{msg.message_id}"))
    if msg.photo:
        # file_size is optional on PhotoSize; `or 0` keeps max() from crashing
        p = max(msg.photo, key=lambda x: x.file_size or 0)
        files.append(await dl(ctx.bot, p, f"photo_{msg.message_id}.jpg"))
    if msg.audio:
        aext = audio_ext(msg.audio, ".mp3")
        audio.append(await dl(ctx.bot, msg.audio, f"audio_{msg.message_id}{aext}"))
    if msg.voice:
        audio.append(await dl(ctx.bot, msg.voice, f"voice_{msg.message_id}.ogg"))
    if msg.video:
        audio.append(await dl(ctx.bot, msg.video, f"video_{msg.message_id}.mp4"))
    if msg.video_note:
        audio.append(await dl(ctx.bot, msg.video_note, f"vn_{msg.message_id}.mp4"))
    if msg.sticker:
        ext = "webm" if msg.sticker.is_video else "webp"
        if not msg.sticker.is_animated:
            files.append(await dl(ctx.bot, msg.sticker, f"stk_{msg.message_id}.{ext}"))

    # ── Voice / audio / video → text ──
    # The language is auto-detected, so speak any language — or mix two.
    if audio:
        heard, why = [], ""
        for a in audio:
            one, err = await transcribe(a)
            if one:
                heard.append(one)
            elif err:
                why = err
        for a in audio:                       # the audio itself is never kept
            try:
                if a and os.path.exists(a):
                    os.remove(a)
            except Exception:
                pass

        spoken = "\n".join(heard).strip()
        if spoken:
            text = f"{text}\n\n{spoken}".strip() if text else spoken
            if STT_ECHO:
                preview = spoken if len(spoken) <= 700 else spoken[:700] + "…"
                try:
                    await msg.reply_text(
                        f"🎤 _{esc(preview)}_", parse_mode=ParseMode.MARKDOWN_V2
                    )
                except Exception:
                    pass
        else:
            try:
                await msg.reply_text(
                    f"🎤 Couldn't turn that into text — {esc(why or 'unknown error')}\\.",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception:
                pass
            if not text and not files:
                return

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
        BotCommand("approve", "Approve access (admin)"),
        BotCommand("deny", "Deny access (admin)"),
        BotCommand("task", "Manage your tasks"),
        BotCommand("new", "New conversation"),
        BotCommand("status", "Session info"),
        BotCommand("help", "Help"),
        BotCommand("users", "List authorized users (admin)"),
        BotCommand("user", "User panel: add / remove / list (admin)"),
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
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("user", cmd_user))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CallbackQueryHandler(on_callback))
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
    load_admin_chat()  # create adminchatid.txt on first run

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