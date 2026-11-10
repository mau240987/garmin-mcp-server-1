"""garmin-coach-telegram: Markdown report -> Telegram messages, never fatal when not set up."""

from __future__ import annotations

import io
import urllib.error
from pathlib import Path

import pytest

from garmin_coach_mcp import telegram_cli

REPORT = """## Race Coach — weekly check-in (2026-11-16)

Plan `plan_abc` (revision 3).

### Garmin

- create 2026-11-17 RC W2 Tue tempo 7.5km <fast>
**Performance level changed:** VDOT 39.5 & 40.7
_Dry run: nothing was sent._
"""


def test_markdown_becomes_telegram_html() -> None:
    out = telegram_cli.to_html(REPORT)
    assert out.startswith("<b>Race Coach — weekly check-in (2026-11-16)</b>")
    assert "<code>plan_abc</code>" in out and "<b>Garmin</b>" in out
    assert "• create 2026-11-17" in out and "&lt;fast&gt;" in out  # escaped, not a tag
    assert "<b>Performance level changed:</b> VDOT 39.5 &amp; 40.7" in out
    assert "<i>Dry run: nothing was sent.</i>" in out


def test_long_reports_split_on_lines() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(300))
    parts = telegram_cli.chunks(text, limit=1000)
    assert len(parts) > 1 and all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text
    assert all(len(p) <= 10 for p in telegram_cli.chunks("y" * 25, limit=10))


def test_not_configured_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    report = tmp_path / "w.md"
    report.write_text(REPORT)
    sent: list[object] = []
    assert telegram_cli.main([str(report)], post=lambda u, d: sent.append(d)) == 0
    assert sent == []


def test_sends_html_with_footer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    report = tmp_path / "w.md"
    report.write_text(REPORT)
    sent: list[tuple[str, dict[str, str]]] = []
    rc = telegram_cli.main(
        [str(report), "--footer", "https://example.invalid/run"],
        post=lambda u, d: sent.append((u, d)),
    )
    assert rc == 0 and len(sent) == 1
    url, data = sent[0]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert data["chat_id"] == "42" and data["parse_mode"] == "HTML"
    assert data["text"].endswith("https://example.invalid/run")


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.telegram.org/botSECRET/sendMessage",
        code,
        "err",
        {},  # type: ignore[arg-type]
        io.BytesIO(b'{"ok":false,"description":"bad"}'),
    )


def test_unparsable_html_falls_back_to_plain_text() -> None:
    sent: list[dict[str, str]] = []

    def post(url: str, data: dict[str, str]) -> None:
        sent.append(data)
        if "parse_mode" in data:
            raise _http_error(400)

    assert telegram_cli.send("**a** & `b`", "t", "1", post) == 1
    assert sent[-1]["text"] == "a & b" and "parse_mode" not in sent[-1]


def test_refusal_fails_without_leaking_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    report = tmp_path / "w.md"
    report.write_text("hello")

    def post(url: str, data: dict[str, str]) -> None:
        raise _http_error(401)

    assert telegram_cli.main([str(report)], post=post) == 1
    out = capsys.readouterr()
    assert "HTTP 401" in out.err and "SECRET" not in out.err + out.out
