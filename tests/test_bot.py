"""Unit tests for the opencode Telegram bot.

Run with:
    cd ~/telegram-opencode-bot
    python3 -m pytest tests/ -v
"""

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