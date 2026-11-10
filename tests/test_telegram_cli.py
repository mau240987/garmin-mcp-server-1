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


# Italian Telegram card from the weekly digest.

DIGEST = {
    "mode": "check-in",
    "week_of": "2026-11-16",
    "as_of": "2026-11-16",
    "auto_push": True,
    "errors": [],
    "goal": {
        "race_name": "Marathon de Paris",
        "race_type": "marathon",
        "race_date": "2027-04-04",
        "target_time_s": 13920,
    },
    "review": {
        "week_index": 1,
        "week_start": "2026-11-09",
        "phase": "base",
        "planned_km_to_date": 35.0,
        "actual_km_to_date": 30.5,
        "sessions": [
            {
                "date": "2026-11-10",
                "planned_kind": "easy",
                "planned_km": 6.5,
                "actual_km": 6.8,
                "status": "done",
                "too_hard": True,
            },
            {
                "date": "2026-11-12",
                "planned_kind": "easy",
                "planned_km": 11.5,
                "actual_km": 0,
                "status": "missed",
            },
        ],
        "extra_runs": [{"date": "2026-11-14", "km": 5.0}],
        "recovery": {
            "resting_hr": 58,
            "resting_hr_7d_avg": 49,
            "hrv_status": "LOW",
            "signals": ["a", "b"],
        },
    },
    "adaptation": {
        "status": "applied",
        "changes": [
            {"op": "scale_week", "week": 2, "factor": 0.8},
            {"op": "make_easy", "date": "2026-11-17"},
        ],
    },
    "vdot_change": {"plan": 40.7, "now": 41.9, "source": "x"},
    "week": {
        "index": 2,
        "phase": "build",
        "target_km": 41.0,
        "sessions": [
            {
                "date": "2026-11-17",
                "kind": "tempo",
                "km": 7.5,
                "minutes": None,
                "zone": "Z3",
                "pace_hint": None,
            },
            {
                "date": "2026-11-22",
                "kind": "long",
                "km": 20.0,
                "minutes": 119,
                "zone": "Z2",
                "pace_hint": "6:02/km - 6:32/km (easy)",
                "long_variant": "progressive",
                "fast_km": 4,
            },
        ],
    },
    "garmin": {"to_send": 2, "to_delete": 0, "result": "sent"},
}


def test_digest_becomes_an_italian_card() -> None:
    from garmin_coach_mcp.telegram_format import format_digest

    out = format_digest(DIGEST)
    assert out.startswith("🏃 <b>Check-in settimanale</b> · lun 16 nov")
    assert (
        "🏁 <b>Marathon de Paris</b> · dom 4 apr 2027 · tra <b>139</b> giorni · 🎯 3:52:00" in out
    )
    assert "✅ mar 10/11 · Corsa facile 6,5 km → <b>6,8 km</b> 🔥 troppo intensa" in out
    assert "❌ gio 12/11 · Corsa facile 11,5 km → <b>0 km</b>" in out
    assert "🆕 sab 14/11 · corsa extra 5 km" in out
    assert "📏 <b>30,5</b> / 35 km" in out and "87%" in out
    assert "⚠️ <b>2 segnali di affaticamento</b>" in out and "FC riposo 58 (media 49)" in out
    assert "HRV bassa" in out
    assert "📉 Settimana 2: volume -20%" in out and "😌 mar 17/11: diventa corsa facile" in out
    assert "📈 <b>Livello in crescita</b>: VDOT 40,7 → <b>41,9</b>" in out
    assert "🗓️ <b>Settimana 2</b> · fase costruzione · 41 km" in out
    assert "🟠 <b>mar 17/11</b> · Tempo <b>7,5 km</b> · Z3" in out
    assert (
        "🔵 <b>dom 22/11</b> · Lungo progressivo (4 km veloci) <b>20 km</b> · ~119 min · "
        "6:02/km-6:32/km" in out
    )
    assert "⌚ ✅ Inviati a Garmin: <b>2 allenamenti</b>" in out and "Buon allenamento" in out


def test_preview_card_and_errors() -> None:
    from garmin_coach_mcp.telegram_format import format_digest

    d = {
        **DIGEST,
        "mode": "preview",
        "week_of": "2026-11-23",
        "as_of": "2026-11-22",
        "adaptation": {"status": "skipped:no_activity", "changes": [{"op": "scale_week"}]},
        "garmin": {"to_send": 1, "result": "dry_run"},
        "errors": ["Garmin push: <boom>"],
    }
    out = format_digest(d)
    assert out.startswith("🔮 <b>Anteprima della settimana</b> · da lun 23 nov")
    assert "Dati fino a dom 22 nov" in out
    assert "⏸️ Modifiche proposte non applicate: nessuna corsa trovata su Garmin" in out
    assert "⌚ Lunedì su Garmin: <b>1 allenamento</b>" in out
    assert "• Garmin push: &lt;boom&gt;" in out and "Buon allenamento" not in out


def test_sends_the_digest_card_with_a_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    report, digest = tmp_path / "w.md", tmp_path / "w.json"
    report.write_text("## English report")
    digest.write_text(json.dumps(DIGEST))
    sent: list[dict[str, object]] = []
    rc = telegram_cli.main(
        [str(report), "--digest", str(digest), "--button-url", "https://example.invalid/r"],
        post=lambda u, d: sent.append(d),
    )
    assert rc == 0 and len(sent) == 1
    assert str(sent[0]["text"]).startswith("🏃 <b>Check-in settimanale</b>")
    assert sent[0]["reply_markup"] == {
        "inline_keyboard": [[{"text": "📄 Report completo", "url": "https://example.invalid/r"}]]
    }


def test_unreadable_digest_falls_back_to_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    report, digest = tmp_path / "w.md", tmp_path / "w.json"
    report.write_text("## English report")
    digest.write_text("{not json")
    sent: list[dict[str, object]] = []
    assert (
        telegram_cli.main([str(report), "--digest", str(digest)], post=lambda u, d: sent.append(d))
        == 0
    )
    assert sent[0]["text"] == "<b>English report</b>"
