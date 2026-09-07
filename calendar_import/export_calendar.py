"""Export Daymark entries to an iCalendar file for Google Calendar.

This utility deliberately lives outside the Flask application. It reads the
SQLite database but never imports or modifies the main app.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import sys
from typing import Iterable


DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "life_admin.db"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "daymark_calendar.ics"
DEFAULT_TIMEZONE = os.environ.get("DAYMARK_TIMEZONE", "Asia/Singapore")
DEFAULT_DURATION_MINUTES = 60

SECTION_LABELS = {
    "timetable": "Timetable",
    "exams": "Exams",
    "events": "Important events",
    "reminders": "Reminder notes",
}

RECURRENCE_FREQUENCIES = {
    "minutes": "MINUTELY",
    "hours": "HOURLY",
    "days": "DAILY",
    "weeks": "WEEKLY",
}


class CalendarExportError(ValueError):
    """Raised when an entry cannot be represented safely in iCalendar."""


def escape_ical(value: str) -> str:
    """Escape text according to RFC 5545."""

    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold_line(line: str, limit: int = 75) -> str:
    """Fold an iCalendar content line at a UTF-8 byte boundary."""

    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in line:
        character_bytes = len(character.encode("utf-8"))
        if current and current_bytes + character_bytes > limit:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += character_bytes
    chunks.append("".join(current))
    return "\r\n ".join(chunks)


def format_utc_stamp(value: datetime | None = None) -> str:
    stamp = value or datetime.now(timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def entry_value(entry: sqlite3.Row, key: str, default: str = "") -> str:
    """Read a column while remaining compatible with older databases."""

    try:
        value = entry[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def parse_entry_datetimes(
    entry: sqlite3.Row, duration_minutes: int
) -> tuple[date, datetime | None, datetime | None]:
    entry_id = entry["id"]
    try:
        entry_date = datetime.strptime(entry_value(entry, "entry_date"), "%Y-%m-%d").date()
    except (TypeError, ValueError) as error:
        raise CalendarExportError(
            f"Entry {entry_id} has an invalid date: {entry_value(entry, 'entry_date')!r}"
        ) from error

    if not entry_value(entry, "entry_time"):
        return entry_date, None, None

    try:
        entry_time = datetime.strptime(entry_value(entry, "entry_time"), "%H:%M").time()
    except (TypeError, ValueError) as error:
        raise CalendarExportError(
            f"Entry {entry_id} has an invalid time: {entry_value(entry, 'entry_time')!r}"
        ) from error

    start = datetime.combine(entry_date, entry_time)
    end_time_value = entry_value(entry, "entry_end_time")
    if end_time_value:
        try:
            end_time = datetime.strptime(end_time_value, "%H:%M").time()
        except (TypeError, ValueError) as error:
            raise CalendarExportError(
                f"Entry {entry_id} has an invalid end time: {end_time_value!r}"
            ) from error
        end = datetime.combine(entry_date, end_time)
    else:
        end = start + timedelta(minutes=duration_minutes)
    if end <= start:
        raise CalendarExportError(
            f"Entry {entry_id} has an end time that is not after its start time"
        )
    return entry_date, start, end


def recurrence_rule(entry: sqlite3.Row) -> str | None:
    count = int(entry["reminder_count"] or 1)
    interval = int(entry["reminder_interval"] or 0)
    unit = entry["reminder_interval_unit"] or "hours"
    frequency = RECURRENCE_FREQUENCIES.get(unit)
    if count <= 1 or interval <= 0 or not frequency:
        return None
    return f"FREQ={frequency};INTERVAL={interval};COUNT={count}"


def entry_lines(entry: sqlite3.Row, timezone_name: str, duration_minutes: int) -> list[str]:
    entry_date, entry_datetime, end_datetime = parse_entry_datetimes(entry, duration_minutes)
    section = SECTION_LABELS.get(entry["section"], entry["section"] or "Daymark")
    title = str(entry["title"] or "Untitled entry")

    description_parts = [f"Daymark section: {section}"]
    if entry["notes"]:
        description_parts.append(str(entry["notes"]))
    description = "\n\n".join(description_parts)

    lines = [
        "BEGIN:VEVENT",
        f"UID:daymark-entry-{entry['id']}@calendar-import",
        f"DTSTAMP:{format_utc_stamp()}",
        f"SUMMARY:{escape_ical(title)}",
        f"DESCRIPTION:{escape_ical(description)}",
        f"CATEGORIES:{escape_ical(section)}",
    ]
    if entry["location"]:
        lines.append(f"LOCATION:{escape_ical(entry['location'])}")

    if entry_datetime is None:
        next_date = entry_date + timedelta(days=1)
        lines.extend(
            [
                f"DTSTART;VALUE=DATE:{entry_date:%Y%m%d}",
                f"DTEND;VALUE=DATE:{next_date:%Y%m%d}",
            ]
        )
    else:
        start_value = entry_datetime.strftime("%Y%m%dT%H%M%S")
        end_value = end_datetime.strftime("%Y%m%dT%H%M%S")
        lines.extend(
            [
                f"DTSTART;TZID={escape_ical(timezone_name)}:{start_value}",
                f"DTEND;TZID={escape_ical(timezone_name)}:{end_value}",
            ]
        )

    rule = recurrence_rule(entry)
    if rule:
        lines.append(f"RRULE:{rule}")
    lines.append("END:VEVENT")
    return lines


def build_calendar(
    entries: Iterable[sqlite3.Row],
    timezone_name: str,
    duration_minutes: int,
) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Daymark//Google Calendar Export//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{escape_ical('Daymark')}",
    ]
    for entry in entries:
        lines.extend(entry_lines(entry, timezone_name, duration_minutes))
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"


def load_entries(database: Path) -> list[sqlite3.Row]:
    if not database.exists():
        raise CalendarExportError(f"Database not found: {database}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM entries ORDER BY entry_date, entry_time, id"
        ).fetchall()
    except sqlite3.Error as error:
        raise CalendarExportError(f"Could not read entries from {database}: {error}") from error
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Daymark entries to an .ics file for Google Calendar."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"Daymark SQLite database (default: {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output .ics file (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone for timed events (default: {DEFAULT_TIMEZONE})",
    )
    parser.add_argument(
        "--duration-minutes",
        type=int,
        default=DEFAULT_DURATION_MINUTES,
        help=f"Length of timed events (default: {DEFAULT_DURATION_MINUTES})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration_minutes <= 0:
        print("--duration-minutes must be greater than zero.", file=sys.stderr)
        return 2

    try:
        entries = load_entries(args.database)
        calendar = build_calendar(entries, args.timezone, args.duration_minutes)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(calendar, encoding="utf-8", newline="")
    except (CalendarExportError, OSError) as error:
        print(f"Calendar export failed: {error}", file=sys.stderr)
        return 1

    print(f"Exported {len(entries)} entries to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
