"""Unit tests for the opencode Telegram bot.

Run with:
    cd ~/telegram-opencode-bot
    python3 -m pytest tests/ -v
"""

import logging

import pytest

import bot


# ── esc() ────────────────────────────────────────────────────────────────────
def test_esc_escapes_all_reserved_chars():
    reserved = r"_*[]()~`>#+-=|{}.!"
    out = bot.esc(reserved)
    for ch in reserved:
        # "\-" should always be produced for "-"
        assert f"\\{ch}" in out


def test_esc_plain_text_unchanged():
    assert bot.esc("hello world 123") == "hello world 123"


def test_esc_backslash():
    assert bot.esc("a\\b") == "a\\\\b"


# ── format_md() ──────────────────────────────────────────────────────────────
def test_plain_special_chars_escaped():
    md, pm = bot.format_md("Version 2.0 is great!")
    assert md == "Version 2\\.0 is great\\!"
    assert pm == bot.ParseMode.MARKDOWN_V2


def test_bold():
    md, _ = bot.format_md("**bold** text")
    assert md == "*bold* text"


def test_bold_escaping_inside():
    md, _ = bot.format_md("**bold!** here")
    assert md == "*bold\\!* here"


def test_italic_single_asterisk():
    md, _ = bot.format_md("*italic* text")
    assert md == "_italic_ text"


def test_underscore_italic_not_snake_case():
    md, _ = bot.format_md("use snake_case here")
    assert md == "use snake\\_case here"


def test_inline_code_not_escaped():
    md, _ = bot.format_md("use `print(1!)` now")
    assert md == "use `print(1!)` now"


def test_strikethrough_and_spoiler():
    md, _ = bot.format_md("~~gone~~ and ||hidden||")
    assert md == "~gone~ and ||hidden||"


def test_link():
    md, _ = bot.format_md("[docs](https://example.com)")
    assert md == "[docs](https://example.com)"


def test_link_label_escaped_url_verbatim():
    md, _ = bot.format_md("[a! b](https://example.com/x_y)")
    assert md == "[a\\! b](https://example.com/x_y)"


def test_plain_url_becomes_clickable_link():
    md, _ = bot.format_md("See https://example.com/docs for info")
    assert "[https://example\\.com/docs](https://example.com/docs)" in md


def test_header():
    md, _ = bot.format_md("# Big Header")
    assert md == "\n*Big Header*\n"


def test_bullet_and_numbered():
    md, _ = bot.format_md("- item\n1. first")
    assert md == "• item\n1\\. first"


def test_code_block_not_escaped():
    src = "```python\nprint('hi!')\n```"
    md, _ = bot.format_md(src)
    assert "print('hi!')" in md
    assert "\\!" not in md


def test_unterminated_code_block():
    src = "```python\nprint(1)"
    md, _ = bot.format_md(src)
    assert "print(1)" in md


def test_unmatched_marker_is_escaped():
    md, _ = bot.format_md("a * lone star here")
    assert "\\*" in md


def test_empty_returns_no_response():
    md, _ = bot.format_md("")
    assert md == "_No response\\._"


def test_no_unescaped_specials_outside_code_or_links():
    sample = (
        "Numbers 1.5 and 3! fixed - done + tasks = 2 | pipe {x} [ok]\n"
        "**bold** _it_ ~~strike~~ `x!`\n"
        "> quote!\n"
        "```\n{raw: '!.-'}\n```"
    )
    md, _ = bot.format_md(sample)


# ── fallback_format() ────────────────────────────────────────────────────────
def test_fallback_bold():
    t, pm = bot.fallback_format("**bold** x")
    assert pm == bot.ParseMode.HTML
    assert "<b>bold</b>" in t


def test_fallback_html_escapes_entities():
    t, _ = bot.fallback_format("`1 < 2 & 3`")
    assert "&lt;" in t and "&amp;" in t


# ── parse_json() ─────────────────────────────────────────────────────────────
def test_parse_json_text_events():
    stream = (
        '{"type":"text","part":{"text":"Hello "}}\n'
        '{"type":"text","part":{"text":"world"}}\n'
        '{"type":"done","sessionID":"abc123"}\n'
    )
    resp, sid = bot.parse_json(stream)
    assert resp == "Hello \nworld"
    assert sid == "abc123"


def test_parse_json_keeps_non_json_lines():
    stream = "not json\n{\"type\":\"text\",\"part\":{\"text\":\"ok\"}}\n"
    resp, sid = bot.parse_json(stream)
    assert resp == "not json\nok"


def test_parse_json_no_text_events():
    stream = '{"type":"done","sessionID":"xyz"}\n'
    resp, sid = bot.parse_json(stream)
    assert resp == "_No response._"
    assert sid == "xyz"


def test_parse_json_fallback_session():
    resp, sid = bot.parse_json("", "SID_OLD")
    assert resp == "_No response._"
    assert sid == "SID_OLD"


def test_parse_json_empty_stream():
    resp, sid = bot.parse_json("  \n\n  ", "prev")
    assert resp == "_No response._"
    assert sid == "prev"


def test_parse_json_code_in_text():
    resp, _ = bot.parse_json('{"type":"text","part":{"text":"a `b` c"}}')
    assert resp == "a `b` c"


# ── split_msg() ──────────────────────────────────────────────────────────────
def test_split_msg_small():
    assert bot.split_msg("short") == ["short"]


def test_split_msg_chunks():
    chunks = bot.split_msg("\n".join(f"line {i}" for i in range(1000)), mx=100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 + 1 for c in chunks)
    assert "line 0" in chunks[0]


# ── Single instance lock ─────────────────────────────────────────────────────
@pytest.mark.skipif(
    bot.fcntl is None,
    reason="single-instance lock needs fcntl (POSIX only); guarantee holds on the Termux host",
)
def test_singleton_lock_excludes_second(tmp_path, monkeypatch):
    # Isolate: a live bot may already hold bot.lock
    monkeypatch.setattr(bot, "LOCK_FILE", tmp_path / "bot.lock")
    monkeypatch.setattr(bot, "PID_FILE", tmp_path / "bot.pid")
    fh1 = bot.acquire_singleton()
    assert fh1 is not None, "first instance should acquire the lock"
    fh2 = bot.acquire_singleton()
    assert fh2 is None, "second instance must not acquire the lock"
    fh1.close()
    fh3 = bot.acquire_singleton()
    assert fh3 is not None, "lock must be released when the process dies/ends"
    fh3.close()


# ── Access files (adminchatid.txt / userchatid.txt) ──────────────────────────
def test_auth_loads_from_userchatid(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CHATID_FILE", tmp_path / "userchatid.txt")
    monkeypatch.setattr(bot, "AUTH_FILE", tmp_path / "users.json")
    (tmp_path / "userchatid.txt").write_text("111\n222\n")
    bot.load_auth()
    assert bot.authorized == {111, 222}


def test_auth_saves_to_userchatid(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CHATID_FILE", tmp_path / "userchatid.txt")
    monkeypatch.setattr(bot, "AUTH_FILE", tmp_path / "users.json")
    bot.authorized = {3, 1}
    bot.user_names = {}
    bot.save_auth()
    assert (tmp_path / "userchatid.txt").read_text().strip() == "1\n3"


def test_auth_loads_names_from_userchatid(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CHATID_FILE", tmp_path / "userchatid.txt")
    monkeypatch.setattr(bot, "AUTH_FILE", tmp_path / "users.json")
    (tmp_path / "userchatid.txt").write_text("111 John\n222\n")
    bot.load_auth()
    assert bot.authorized == {111, 222}
    assert bot.user_names.get(111) == "John"
    assert bot.user_names.get(222) == ""


def test_auth_saves_names(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CHATID_FILE", tmp_path / "userchatid.txt")
    monkeypatch.setattr(bot, "AUTH_FILE", tmp_path / "users.json")
    bot.authorized = {1, 3}
    bot.user_names = {1: "Alice", 3: "Bob Jones"}
    bot.save_auth()
    lines = (tmp_path / "userchatid.txt").read_text().strip().split("\n")
    assert "1 Alice" in lines and "3 Bob Jones" in lines


def test_revoke_clears_name(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CHATID_FILE", tmp_path / "userchatid.txt")
    bot.authorized = {5}
    bot.user_names = {5: "Ed"}
    bot.revoke(5)
    assert bot.authorized == set()
    assert 5 not in bot.user_names


def test_admin_chat_created_from_admin_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "ADMIN_CHAT_FILE", tmp_path / "adminchatid.txt")
    monkeypatch.setattr(bot, "ADMIN_IDS", [42])
    assert bot.load_admin_chat() == 42
    assert (tmp_path / "adminchatid.txt").read_text().strip() == "42"


# ── Speech-to-Text ───────────────────────────────────────────────────────────
class _Obj:
    """Stand-in for telegram.Audio / telegram.Voice."""

    def __init__(self, file_name=None, mime_type=None):
        self.file_name = file_name
        self.mime_type = mime_type


def test_audio_ext_from_file_name():
    assert bot.audio_ext(_Obj(file_name="song.M4A")) == ".m4a"


def test_audio_ext_from_mime_when_name_useless():
    assert bot.audio_ext(_Obj(file_name="track.bin", mime_type="audio/ogg")) == ".ogg"


def test_audio_ext_mime_with_parameters():
    assert bot.audio_ext(_Obj(mime_type="audio/webm; codecs=opus")) == ".webm"


def test_audio_ext_falls_back_to_default():
    assert bot.audio_ext(_Obj(), default=".mp4") == ".mp4"


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_transcribe_without_key_is_reported(tmp_path, monkeypatch):
    f = tmp_path / "voice.ogg"
    f.write_bytes(b"fake audio")
    monkeypatch.setattr(bot, "STT_API_KEY", "")
    txt, err = _run(bot.transcribe(str(f)))
    assert txt == ""
    assert "STT_API_KEY" in err


def test_transcribe_missing_file(monkeypatch):
    monkeypatch.setattr(bot, "STT_API_KEY", "k")
    txt, err = _run(bot.transcribe("/nope/does_not_exist.ogg"))
    assert txt == ""
    assert err


def test_transcribe_rejects_oversized_audio(tmp_path, monkeypatch):
    f = tmp_path / "big.ogg"
    f.write_bytes(b"x" * 2048)
    monkeypatch.setattr(bot, "STT_API_KEY", "k")
    monkeypatch.setattr(bot, "STT_MAX_MB", 0.001)  # 1 KB
    txt, err = _run(bot.transcribe(str(f)))
    assert txt == ""
    assert "too big" in err


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = "error body"

    def json(self):
        return self._payload


def _fake_httpx(payload, sent, status=200):
    class _FakeClient:
        def __init__(self, **kw):
            sent["client_kwargs"] = kw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, files=None, data=None):
            sent["url"] = url
            sent["headers"] = headers
            sent["files"] = files
            sent["data"] = data
            return _FakeResponse(payload, status)

    class _FakeModule:
        AsyncClient = _FakeClient

    return _FakeModule


def test_transcribe_happy_path(tmp_path, monkeypatch):
    f = tmp_path / "voice.ogg"
    f.write_bytes(b"fake audio")
    sent = {}
    monkeypatch.setattr(bot, "STT_API_KEY", "secret-key")
    monkeypatch.setattr(bot, "STT_MODEL", "whisper-large-v3")
    monkeypatch.setattr(bot, "STT_LANG", "")
    monkeypatch.setattr(bot, "STT_PROMPT", "")
    monkeypatch.setattr(
        bot, "httpx", _fake_httpx({"text": " add a login page ", "language": "en"}, sent)
    )
    txt, err = _run(bot.transcribe(str(f)))
    assert txt == "add a login page"
    assert err == ""
    assert sent["headers"]["Authorization"] == "Bearer secret-key"
    assert sent["data"]["model"] == "whisper-large-v3"
    # Empty STT_LANG must NOT be sent — that is what enables auto-detection
    assert "language" not in sent["data"]


def test_transcribe_keeps_non_ascii_transcript(tmp_path, monkeypatch):
    f = tmp_path / "voice.ogg"
    f.write_bytes(b"fake audio")
    tamil = "வணக்கம் login page add பண்ணு"
    monkeypatch.setattr(bot, "STT_API_KEY", "k")
    monkeypatch.setattr(bot, "httpx", _fake_httpx({"text": tamil, "language": "ta"}, {}))
    txt, err = _run(bot.transcribe(str(f)))
    assert txt == tamil
    assert err == ""


def test_transcribe_sends_language_and_prompt_when_set(tmp_path, monkeypatch):
    f = tmp_path / "voice.ogg"
    f.write_bytes(b"fake audio")
    sent = {}
    monkeypatch.setattr(bot, "STT_API_KEY", "k")
    monkeypatch.setattr(bot, "STT_LANG", "ta")
    monkeypatch.setattr(bot, "STT_PROMPT", "Thanglish talk about code.")
    monkeypatch.setattr(bot, "httpx", _fake_httpx({"text": "ok"}, sent))
    _run(bot.transcribe(str(f)))
    assert sent["data"]["language"] == "ta"
    assert sent["data"]["prompt"] == "Thanglish talk about code."


def test_transcribe_bad_key_reported(tmp_path, monkeypatch):
    f = tmp_path / "voice.ogg"
    f.write_bytes(b"fake audio")
    monkeypatch.setattr(bot, "STT_API_KEY", "k")
    monkeypatch.setattr(bot, "httpx", _fake_httpx({}, {}, status=401))
    txt, err = _run(bot.transcribe(str(f)))
    assert txt == ""
    assert "key" in err


def test_transcribe_empty_result_reported(tmp_path, monkeypatch):
    f = tmp_path / "voice.ogg"
    f.write_bytes(b"fake audio")
    monkeypatch.setattr(bot, "STT_API_KEY", "k")
    monkeypatch.setattr(bot, "httpx", _fake_httpx({"text": "   "}, {}))
    txt, err = _run(bot.transcribe(str(f)))
    assert txt == ""
    assert "no speech" in err


def test_transcribe_network_error_reported(tmp_path, monkeypatch):
    f = tmp_path / "voice.ogg"
    f.write_bytes(b"fake audio")

    class _Boom:
        class AsyncClient:
            def __init__(self, **kw):
                raise OSError("network down")

    monkeypatch.setattr(bot, "STT_API_KEY", "k")
    monkeypatch.setattr(bot, "httpx", _Boom)
    txt, err = _run(bot.transcribe(str(f)))
    assert txt == ""
    assert err


def test_stt_key_is_scrubbed_from_logs(monkeypatch):
    monkeypatch.setattr(bot, "STT_API_KEY", "gsk_supersecret")
    f = bot.TokenScrubFilter()
    rec = logging.LogRecord(
        "bot", logging.ERROR, __file__, 1,
        "STT failed for key gsk_supersecret", None, None,
    )
    f.filter(rec)
    assert "gsk_supersecret" not in rec.getMessage()
    assert "REDACTED" in rec.getMessage()