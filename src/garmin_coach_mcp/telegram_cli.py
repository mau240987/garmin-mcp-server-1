"""`garmin-coach-telegram`: send a coach report (Markdown) to a Telegram chat.

Reads the bot token and the chat id from TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Without
them it does nothing and exits 0, so the workflows work the same with Telegram not set up.
The report's light Markdown (headings, **bold**, `code`) becomes Telegram HTML; long reports
are split into several messages on line boundaries.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

#: Telegram's limit is 4096 characters per message; keep a margin for the HTML tags.
MAX_CHARS = 3800

Post = Callable[[str, dict[str, str]], None]


def to_html(markdown: str) -> str:
    """Telegram HTML from the reports' Markdown subset. Everything else stays plain text."""
    lines = []
    for line in markdown.splitlines():
        text = html.escape(line, quote=False)
        heading = re.match(r"#{1,6}\s+(.*)", text)
        if heading:
            text = f"<b>{heading.group(1)}</b>"
        else:
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
            text = re.sub(r"(?<![\w*])_(.+?)_(?![\w*])", r"<i>\1</i>", text)
            text = re.sub(r"^(\s*)- ", r"\1• ", text)
        lines.append(text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def chunks(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split on line boundaries (tags never span lines), hard-cutting only an overlong line."""
    out: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                out.append(current)
                current = ""
            out.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            out.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        out.append(current)
    return out


def _http_post(url: str, data: dict[str, str]) -> None:
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https host
        resp.read()


def send(text: str, token: str, chat_id: str, post: Post = _http_post) -> int:
    """Send the report; returns the number of messages sent."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    parts = chunks(to_html(text))
    for part in parts:
        data = {"chat_id": chat_id, "text": part, "disable_web_page_preview": "true"}
        try:
            post(url, {**data, "parse_mode": "HTML"})
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
            # Formatting Telegram cannot parse: send the same part as plain text.
            post(url, {**data, "text": html.unescape(re.sub(r"</?(b|i|code)>", "", part))})
    return len(parts)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="garmin-coach-telegram", description=__doc__.splitlines()[0])
    p.add_argument("report", type=Path, help="Markdown report to send.")
    p.add_argument("--footer", default="", help="Line appended to the message (e.g. a link).")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None, post: Post = _http_post) -> int:
    args = parse_args(argv)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram not configured (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID): skipped.")
        return 0
    if not args.report.is_file():
        print(f"No report at {args.report}: nothing to send.")
        return 0
    text = args.report.read_text(encoding="utf-8")
    if args.footer:
        text = f"{text.rstrip()}\n\n{args.footer}"
    try:
        n = send(text, token, chat_id, post)
    except urllib.error.HTTPError as exc:
        # Never print the URL: it contains the bot token.
        detail = exc.read().decode("utf-8", "replace")[:300]
        print(f"Telegram refused the message (HTTP {exc.code}): {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Telegram unreachable: {getattr(exc, 'reason', exc)}", file=sys.stderr)
        return 1
    print(f"Sent the report to Telegram ({n} message(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
