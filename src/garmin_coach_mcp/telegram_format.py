"""Italian Telegram message (HTML) from the digest of a weekly check-in or preview.

The digest is the JSON written by `garmin-coach-weekly --digest`. Every text here is built
from its facts (dates, kilometres, statuses, recovery values), so the message is in Italian
even though the engine's own wording is English. Unknown fields are ignored: an older or
partial digest still gives a readable message.
"""

from __future__ import annotations

import datetime as dt
import re
from html import escape
from typing import Any

DAYS = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MONTHS = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]

KINDS: dict[str, tuple[str, str]] = {
    "easy": ("🟢", "Corsa facile"),
    "easy_strides": ("🟢", "Facile + allunghi"),
    "long": ("🔵", "Lungo"),
    "medium_long": ("🔷", "Medio-lungo"),
    "tempo": ("🟠", "Tempo"),
    "intervals": ("🔴", "Ripetute"),
    "race_pace": ("🟣", "Ritmo gara"),
    "fartlek": ("🟡", "Fartlek"),
    "race": ("🏁", "Gara"),
}
VARIANTS = {"progressive": "progressivo", "race_pace": "a ritmo gara"}
PHASES = {"base": "base", "build": "costruzione", "specific": "specifica", "taper": "scarico"}
HRV = {"BALANCED": "equilibrata", "UNBALANCED": "sbilanciata", "LOW": "bassa", "POOR": "scarsa"}
STATUS = {"done": "✅", "partial": "🟡", "missed": "❌", "upcoming": "⏳", "race": "🏁"}
RACES = {
    "5k": "5 km",
    "10k": "10 km",
    "half_marathon": "Mezza maratona",
    "marathon": "Maratona",
    "ultra": "Ultra",
}


def _date(value: str | dt.date | None) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def day(value: str | dt.date | None) -> str:
    """'lun 16/11'."""
    d = _date(value)
    return f"{DAYS[d.weekday()]} {d:%d/%m}" if d else "?"


def long_day(value: str | dt.date | None, year: bool = False) -> str:
    """'lun 16 nov' (with the year on request)."""
    d = _date(value)
    if d is None:
        return "?"
    return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if year else "")


def num(x: float | int | None) -> str:
    """Italian decimal comma: 7.5 -> '7,5'."""
    return "?" if x is None else f"{x:g}".replace(".", ",")


def hms(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, rest = divmod(int(seconds), 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}"


def bar(share: float, width: int = 10) -> str:
    filled = max(0, min(width, round(share * width)))
    return "▰" * filled + "▱" * (width - filled)


def session_line(s: dict[str, Any]) -> str:
    icon, label = KINDS.get(str(s.get("kind")), ("⚪", str(s.get("kind", "")).replace("_", " ")))
    variant = VARIANTS.get(str(s.get("long_variant")))
    if variant:
        label += f" {variant}"
        if s.get("fast_km"):
            label += f" ({num(s['fast_km'])} km veloci)"
    parts = [f"{icon} <b>{day(s.get('date'))}</b> · {escape(label)} <b>{num(s.get('km'))} km</b>"]
    if s.get("minutes"):
        parts.append(f"~{s['minutes']} min")
    hint = re.sub(r"\s*\([^)]*\)\s*$", "", str(s.get("pace_hint") or "")).replace(" - ", "-")
    if hint:
        parts.append(escape(hint))
    elif s.get("zone"):
        parts.append(escape(str(s["zone"])))
    return " · ".join(parts)


def _goal(goal: dict[str, Any], today: dt.date) -> str:
    name = goal.get("race_name") or RACES.get(str(goal.get("race_type")), "Gara")
    race = _date(goal.get("race_date"))
    line = f"🏁 <b>{escape(str(name))}</b>"
    if race:
        days = (race - today).days
        line += f" · {long_day(race, year=True)}"
        if days > 0:
            line += f" · tra <b>{days}</b> giorni"
    target = hms(goal.get("target_time_s"))
    if target:
        line += f" · 🎯 {target}"
    return line


def _review(review: dict[str, Any]) -> list[str]:
    start = _date(review.get("week_start"))
    end = start + dt.timedelta(days=6) if start else None
    phase = PHASES.get(str(review.get("phase")), str(review.get("phase", "")))
    lines = [
        f"📊 <b>Settimana {review.get('week_index', '?')}</b> "
        f"({day(start)} - {day(end)}) · fase {escape(phase)}"
    ]
    for c in review.get("sessions") or []:
        _icon, label = KINDS.get(str(c.get("planned_kind")), ("", str(c.get("planned_kind"))))
        mark = STATUS.get(str(c.get("status")), "•")
        text = f"{mark} {day(c.get('date'))} · {label} {num(c.get('planned_km'))} km"
        if c.get("status") in ("done", "partial", "missed"):
            text += f" → <b>{num(c.get('actual_km') or 0)} km</b>"
        if c.get("too_hard"):
            text += " 🔥 troppo intensa"
        lines.append(text)
    for e in review.get("extra_runs") or []:
        lines.append(f"🆕 {day(e.get('date'))} · corsa extra {num(e.get('km'))} km")
    planned, actual = review.get("planned_km_to_date"), review.get("actual_km_to_date")
    if planned:
        share = (actual or 0) / planned
        lines.append(
            f"📏 <b>{num(actual or 0)}</b> / {num(planned)} km  {bar(share)} {round(share * 100)}%"
        )
    lines += _recovery(review.get("recovery") or {})
    return lines


def _recovery(rec: dict[str, Any]) -> list[str]:
    values = []
    if rec.get("resting_hr"):
        avg = rec.get("resting_hr_7d_avg")
        values.append(f"❤️ FC riposo {rec['resting_hr']}" + (f" (media {avg})" if avg else ""))
    if rec.get("hrv_status"):
        status = str(rec["hrv_status"]).upper()
        values.append(f"💓 HRV {escape(HRV.get(status, status.lower()))}")
    if rec.get("sleep_avg_h_7d"):
        values.append(f"😴 sonno {num(rec['sleep_avg_h_7d'])} h")
    if not values:
        return []
    signals = len(rec.get("signals") or [])
    head = (
        "🔋 Recupero nella norma"
        if not signals
        else f"⚠️ <b>{signals} segnal{'e' if signals == 1 else 'i'} di affaticamento</b>"
    )
    return [head, " · ".join(values)]


def _adaptation(adapt: dict[str, Any], preview: bool) -> list[str]:
    status = str(adapt.get("status", "none"))
    changes = adapt.get("changes") or []
    if status == "applied":
        lines = ["🔧 <b>Adattamento previsto</b>" if preview else "🔧 <b>Piano adattato</b>"]
        for c in changes:
            if c.get("op") == "scale_week":
                cut = round((1 - float(c.get("factor", 1))) * 100)
                lines.append(f"📉 Settimana {c.get('week')}: volume -{cut}%")
            elif c.get("op") == "make_easy":
                lines.append(f"😌 {day(c.get('date'))}: diventa corsa facile")
        return lines
    if status == "failed":
        return ["❗ Adattamento non applicato (vedi problemi)"]
    if status.startswith("skipped:") and changes:
        why = {
            "no_activity": "nessuna corsa trovata su Garmin (non ancora sincronizzata?)",
            "already_reviewed": "già applicato da un'esecuzione precedente",
            "no_adapt": "solo revisione richiesta",
        }.get(status.split(":", 1)[1], "non applicabile")
        return [f"⏸️ Modifiche proposte non applicate: {why}"]
    return ["👍 In linea con il piano: nessuna modifica"]


def _garmin(garmin: dict[str, Any], preview: bool) -> str:
    n, result = garmin.get("to_send", 0), garmin.get("result")
    workouts = f"{n} allenament{'o' if n == 1 else 'i'}"
    if preview:
        return f"⌚ Lunedì su Garmin: <b>{workouts}</b> (se l'invio automatico è attivo)"
    return {
        "sent": f"⌚ ✅ Inviati a Garmin: <b>{workouts}</b>",
        "partial": "⌚ ⚠️ Invio parziale: controlla Garmin Connect",
        "failed": "⌚ ❌ Invio a Garmin non riuscito",
        "up_to_date": "⌚ ✅ Garmin è già aggiornato",
    }.get(
        str(result),
        f"⌚ Invio automatico spento: lancia «Push to Garmin» ({workouts})",
    )


def format_digest(d: dict[str, Any]) -> str:
    preview = d.get("mode") == "preview"
    week_of = _date(d.get("week_of")) or dt.date.today()
    as_of = _date(d.get("as_of")) or week_of
    if preview:
        lines = [
            f"🔮 <b>Anteprima della settimana</b> · da {long_day(week_of)}",
            f"<i>Dati fino a {long_day(as_of)}: ora non viene modificato né inviato nulla.</i>",
        ]
    else:
        lines = [f"🏃 <b>Check-in settimanale</b> · {long_day(week_of)}"]
    if d.get("goal"):
        lines.append(_goal(d["goal"], as_of))
    if d.get("nothing_to_do"):
        return "\n".join([*lines, "", "😴 Nessun piano per una gara futura: niente da fare."])

    if d.get("review"):
        lines += ["", *_review(d["review"])]
    if d.get("adaptation") is not None:
        lines += ["", *_adaptation(d["adaptation"], preview)]
    vdot = d.get("vdot_change")
    if vdot:
        up = float(vdot.get("now", 0)) > float(vdot.get("plan", 0))
        lines += [
            "",
            f"{'📈' if up else '📉'} <b>Livello {'in crescita' if up else 'in calo'}</b>: "
            f"VDOT {num(vdot.get('plan'))} → <b>{num(vdot.get('now'))}</b>",
            "Chiedi al coach di aggiornare i ritmi del piano.",
        ]
    week = d.get("week") or {}
    sessions = week.get("sessions") or []
    if sessions:
        head = f"🗓️ <b>Settimana {week.get('index', '?')}</b>"
        if week.get("phase"):
            head += f" · fase {PHASES.get(week['phase'], week['phase'])}"
        if week.get("target_km"):
            head += f" · {num(week['target_km'])} km"
        lines += ["", head, *[session_line(s) for s in sessions]]
    if d.get("garmin"):
        lines += ["", _garmin(d["garmin"], preview)]
    if d.get("errors"):
        lines += ["", "❗ <b>Problemi</b>", *[f"• {escape(str(e))[:300]}" for e in d["errors"]]]
    elif sessions:
        lines += ["", "💪 Buon allenamento!"]
    return "\n".join(lines)
