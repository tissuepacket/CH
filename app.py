from datetime import datetime, time as datetime_time, timedelta
import calendar
import json
import os
from pathlib import Path
import re
import ssl
import sqlite3
import threading
import time
from urllib import parse, request as urllib_request

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from calendar_import.export_calendar import (
    CalendarExportError,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_TIMEZONE,
    build_calendar,
    load_entries,
)

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "life_admin.db"
ENV_FILE = BASE_DIR / ".env"

app = Flask(__name__)

SECTIONS = {
    "timetable": {"label": "Timetable", "icon": "◷", "color": "coral"},
    "exams": {"label": "Exams", "icon": "✦", "color": "violet"},
    "events": {"label": "Important events", "icon": "◆", "color": "teal"},
    "reminders": {"label": "Reminder notes", "icon": "!", "color": "gold"},
}

DEFAULT_ENTRY_DURATION_MINUTES = 60
TIME_TOKEN_PATTERN = r"\d{1,2}(?::\d{2})?\s*(?:[AaPp][Mm])?"
_telegram_ssl_context = None


def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with get_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section TEXT NOT NULL,
                title TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                entry_time TEXT,
                entry_end_time TEXT,
                location TEXT,
                notes TEXT,
                reminder_count INTEGER NOT NULL DEFAULT 1,
                reminder_interval INTEGER NOT NULL DEFAULT 0,
                reminder_interval_unit TEXT NOT NULL DEFAULT 'hours',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS telegram_chats (chat_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS telegram_sent (chat_id TEXT, entry_id INTEGER, occurrence TEXT, PRIMARY KEY (chat_id, entry_id, occurrence))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS telegram_drafts (chat_id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, processed_at TEXT NOT NULL)"
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(entries)")}
        migrations = {
            "entry_end_time": "ALTER TABLE entries ADD COLUMN entry_end_time TEXT",
            "reminder_count": "ALTER TABLE entries ADD COLUMN reminder_count INTEGER NOT NULL DEFAULT 1",
            "reminder_interval": "ALTER TABLE entries ADD COLUMN reminder_interval INTEGER NOT NULL DEFAULT 0",
            "reminder_interval_unit": "ALTER TABLE entries ADD COLUMN reminder_interval_unit TEXT NOT NULL DEFAULT 'hours'",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)
        connection.commit()


def format_entry(entry):
    item = dict(entry)
    if item["entry_date"]:
        try:
            item["display_date"] = datetime.strptime(item["entry_date"], "%Y-%m-%d").strftime("%a, %d %b")
        except ValueError:
            item["display_date"] = item["entry_date"]
    else:
        item["display_date"] = "No date"
    item["section_label"] = SECTIONS[item["section"]]["label"]
    item["section_color"] = SECTIONS[item["section"]]["color"]
    item["display_time"] = item["entry_time"] or ""
    if item["entry_time"] and item["entry_end_time"]:
        item["display_time"] = f"{item['entry_time']}–{item['entry_end_time']}"
    item["is_complete"] = is_reminder_complete(item)
    if item["section"] == "reminders" and item["reminder_interval"]:
        count_label = "reminder" if item["reminder_count"] == 1 else "reminders"
        unit_label = item["reminder_interval_unit"]
        if item["reminder_interval"] == 1:
            unit_label = unit_label.rstrip("s")
        item["schedule_label"] = f"{item['reminder_count']} {count_label} every {item['reminder_interval']} {unit_label}"
    return item


def is_reminder_complete(entry):
    if entry["section"] != "reminders" or not entry["entry_date"] or not entry["entry_time"]:
        return False
    unit_milliseconds = {"minutes": 60000, "hours": 3600000, "days": 86400000, "weeks": 604800000}
    step = entry["reminder_interval"] * unit_milliseconds.get(entry["reminder_interval_unit"], 0)
    if not step:
        return False
    start = datetime.strptime(f"{entry['entry_date']}T{entry['entry_time']}", "%Y-%m-%dT%H:%M")
    final_occurrence = start.timestamp() * 1000 + (entry["reminder_count"] - 1) * step
    return datetime.now().timestamp() * 1000 > final_occurrence


@app.route("/")
def dashboard():
    selected = request.args.get("section", "all")
    view = request.args.get("view", "active")
    with get_db() as connection:
        if selected in SECTIONS:
            rows = connection.execute(
                "SELECT * FROM entries WHERE section = ? ORDER BY entry_date, entry_time, id",
                (selected,),
            ).fetchall()
        else:
            selected = "all"
            rows = connection.execute(
                "SELECT * FROM entries ORDER BY entry_date, entry_time, id"
            ).fetchall()
    all_entries = [format_entry(row) for row in rows]
    entries = [entry for entry in all_entries if entry["is_complete"] == (view == "completed")]
    return render_template(
        "index.html",
        entries=entries,
        completed_count=sum(entry["is_complete"] for entry in all_entries),
        sections=SECTIONS,
        selected=selected,
        view=view,
        today=datetime.now().strftime("%A, %d %B"),
    )


@app.post("/entries")
def create_entry():
    section = request.form.get("section", "reminders")
    title = request.form.get("title", "").strip()
    if section not in SECTIONS or not title:
        return redirect(url_for("dashboard", section=section if section in SECTIONS else "all"))

    reminder_count = request.form.get("reminder_count", "1")
    reminder_interval = request.form.get("reminder_interval", "0")
    try:
        reminder_count = max(1, min(100, int(reminder_count)))
        reminder_interval = max(0, min(100000, int(reminder_interval)))
    except ValueError:
        reminder_count, reminder_interval = 1, 0
    reminder_interval_unit = request.form.get("reminder_interval_unit", "hours")
    if reminder_interval_unit not in {"minutes", "hours", "days", "weeks"}:
        reminder_interval_unit = "hours"
    if section != "reminders":
        reminder_count, reminder_interval = 1, 0

    save_entry(
        {
            "section": section,
            "title": title,
            "entry_date": request.form.get("entry_date", ""),
            "entry_time": request.form.get("entry_time", ""),
            "entry_end_time": request.form.get("entry_end_time", ""),
            "location": request.form.get("location", "").strip(),
            "notes": request.form.get("notes", "").strip(),
            "reminder_count": reminder_count,
            "reminder_interval": reminder_interval,
            "reminder_interval_unit": reminder_interval_unit,
        }
    )
    return redirect(url_for("dashboard", section=section))


@app.post("/entries/<int:entry_id>/delete")
def delete_entry(entry_id):
    with get_db() as connection:
        connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        connection.commit()
    return redirect(request.referrer or url_for("dashboard"))


@app.get("/api/entries")
def api_entries():
    with get_db() as connection:
        rows = connection.execute(
            "SELECT * FROM entries ORDER BY entry_date, entry_time, id"
        ).fetchall()
    return jsonify([format_entry(row) for row in rows])


@app.get("/calendar.ics")
def download_calendar():
    try:
        entries = load_entries(DATABASE)
        calendar = build_calendar(entries, DEFAULT_TIMEZONE, DEFAULT_DURATION_MINUTES)
    except (CalendarExportError, OSError) as error:
        return jsonify({"error": f"Calendar export failed: {error}"}), 500

    response = Response(calendar, mimetype="text/calendar")
    response.headers["Content-Disposition"] = 'attachment; filename="daymark_calendar.ics"'
    return response


def get_telegram_token():
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "TELEGRAM_BOT_TOKEN":
                token = value.strip().strip('"').strip("'")
                if token:
                    return token
    except OSError:
        pass
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def get_telegram_ssl_context():
    """Use Windows' trusted roots when the bundled Python roots are incomplete."""

    global _telegram_ssl_context
    if _telegram_ssl_context is not None:
        return _telegram_ssl_context

    if os.name == "nt":
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            loaded = 0
            for certificate, encoding, _trust in ssl.enum_certificates("ROOT"):
                if encoding != "x509_asn":
                    continue
                try:
                    context.load_verify_locations(
                        cadata=ssl.DER_cert_to_PEM_cert(certificate)
                    )
                    loaded += 1
                except ssl.SSLError:
                    continue
            if loaded:
                _telegram_ssl_context = context
                return _telegram_ssl_context
        except (AttributeError, OSError, ssl.SSLError):
            pass

    _telegram_ssl_context = ssl.create_default_context()
    return _telegram_ssl_context


def telegram_call(method, values=None):
    token = get_telegram_token()
    if not token:
        return None
    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    payload = parse.urlencode(values or {}).encode()
    try:
        with urllib_request.urlopen(
            urllib_request.Request(endpoint, data=payload),
            timeout=35,
            context=get_telegram_ssl_context(),
        ) as response:
            return json.loads(response.read().decode())
    except Exception as error:
        print(f"Telegram {method} failed: {error}", flush=True)
        return None


def telegram_send(chat_id, text, keyboard=None, remove_keyboard=False, inline_keyboard=None):
    values = {"chat_id": chat_id, "text": text}
    if remove_keyboard:
        values["reply_markup"] = json.dumps({"remove_keyboard": True})
    elif keyboard:
        values["reply_markup"] = json.dumps(
            {"keyboard": keyboard, "resize_keyboard": True, "one_time_keyboard": True}
        )
    elif inline_keyboard:
        values["reply_markup"] = json.dumps({"inline_keyboard": inline_keyboard})
    telegram_call("sendMessage", values)


def telegram_section_keyboard():
    return [
        ["Timetable", "Exams"],
        ["Important events", "Reminder notes"],
        ["Export calendar"],
    ]


def telegram_yes_no_keyboard():
    return [["Yes", "No"]]


def telegram_time_keyboard(include_no_time=False):
    slots = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 30)]
    keyboard = [slots[index:index + 4] for index in range(0, len(slots), 4)]
    if include_no_time:
        keyboard.append(["No specific time"])
    return keyboard


def telegram_count_keyboard():
    return [["1", "2", "3", "4", "5"], ["10", "20", "50", "100"]]


def telegram_interval_keyboard():
    return [["30 minutes", "1 hour"], ["2 hours", "1 day"], ["1 week"]]


def telegram_export_keyboard():
    return [[{"text": "Export to Google Calendar", "callback_data": "export_calendar"}]]


def telegram_calendar_keyboard(year, month):
    month_name = calendar.month_name[month]
    keyboard = [[
        {"text": "‹", "callback_data": f"calendar:{year}:{month - 1}"},
        {"text": f"{month_name} {year}", "callback_data": "calendar:current"},
        {"text": "›", "callback_data": f"calendar:{year}:{month + 1}"},
    ]]
    keyboard.append([
        {"text": day, "callback_data": "calendar:current"}
        for day in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
    ])
    for week in calendar.monthcalendar(year, month):
        keyboard.append([
            {"text": str(day) if day else " ", "callback_data": f"date:{year:04d}-{month:02d}-{day:02d}" if day else "calendar:current"}
            for day in week
        ])
    return keyboard


def telegram_send_date_prompt(chat_id, year=None, month=None):
    today = datetime.now()
    year = year or today.year
    month = month or today.month
    telegram_send(
        chat_id,
        "Choose the date from the calendar, or type it as YYYY-MM-DD.",
        inline_keyboard=telegram_calendar_keyboard(year, month),
    )


def telegram_send_time_start_prompt(chat_id, section):
    telegram_send(
        chat_id,
        "What time does it start? Choose a time, or choose No specific time.",
        telegram_time_keyboard(section != "reminders"),
    )


def telegram_send_time_end_prompt(chat_id):
    telegram_send(
        chat_id,
        "What time does it end? Choose an end time after the start time.",
        telegram_time_keyboard(),
    )


def telegram_edit_calendar(chat_id, message_id, year, month):
    telegram_call(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": "Choose the date from the calendar, or type it as YYYY-MM-DD.",
            "reply_markup": json.dumps({"inline_keyboard": telegram_calendar_keyboard(year, month)}),
        },
    )


def handle_telegram_callback(chat_id, callback_id, message_id, data):
    telegram_call("answerCallbackQuery", {"callback_query_id": callback_id})
    if data == "export_calendar":
        telegram_export_calendar(chat_id)
        return
    if data == "calendar:current":
        return
    if data.startswith("calendar:"):
        _, year_text, month_text = data.split(":")
        year, month = int(year_text), int(month_text)
        if month == 0:
            year, month = year - 1, 12
        elif month == 13:
            year, month = year + 1, 1
        telegram_edit_calendar(chat_id, message_id, year, month)
        return
    if not data.startswith("date:"):
        return
    draft = get_telegram_draft(chat_id)
    if not draft or draft["state"] != "date":
        return
    selected_date = data.removeprefix("date:")
    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        return
    payload = draft["payload"]
    payload["entry_date"] = selected_date
    set_telegram_draft(chat_id, "time_start", payload)
    telegram_call(
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "text": f"Date selected: {selected_date}"},
    )
    telegram_send_time_start_prompt(chat_id, payload["section"])


def telegram_send_document(chat_id, content, filename, caption=""):
    """Send an in-memory file to Telegram using the Bot API multipart format."""

    token = get_telegram_token()
    if not token:
        return False

    boundary = f"----DaymarkBoundary{int(time.time() * 1000)}"
    boundary_bytes = boundary.encode("ascii")
    parts = [
        b"--" + boundary_bytes + b"\r\n"
        b'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        + str(chat_id).encode("utf-8")
        + b"\r\n"
    ]
    if caption:
        parts.extend(
            [
                b"--" + boundary_bytes + b"\r\n"
                b'Content-Disposition: form-data; name="caption"\r\n\r\n'
                + caption.encode("utf-8")
                + b"\r\n"
            ]
        )
    parts.extend(
        [
            b"--" + boundary_bytes + b"\r\n"
            + f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode(
                "utf-8"
            )
            + b"Content-Type: text/calendar\r\n\r\n"
            + content
            + b"\r\n",
            b"--" + boundary_bytes + b"--\r\n",
        ]
    )

    endpoint = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        with urllib_request.urlopen(
            urllib_request.Request(
                endpoint,
                data=b"".join(parts),
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            ),
            timeout=35,
            context=get_telegram_ssl_context(),
        ) as response:
            result = json.loads(response.read().decode())
            return bool(result.get("ok"))
    except Exception as error:
        print(f"Telegram sendDocument failed: {error}", flush=True)
        return False


def telegram_export_calendar(chat_id):
    """Build the current Daymark snapshot and send it to the requesting chat."""

    try:
        entries = load_entries(DATABASE)
        if not entries:
            telegram_send(chat_id, "There are no Daymark entries to export yet.")
            return
        calendar = build_calendar(entries, DEFAULT_TIMEZONE, DEFAULT_DURATION_MINUTES)
    except (CalendarExportError, OSError) as error:
        telegram_send(chat_id, f"Calendar export failed: {error}")
        return

    sent = telegram_send_document(
        chat_id,
        calendar.encode("utf-8"),
        "daymark_calendar.ics",
        caption=(
            f"Daymark export: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}. "
            "Import this .ics file into Google Calendar."
        ),
    )
    if not sent:
        telegram_send(chat_id, "I could not send the calendar file. Please try /export again.")


def get_telegram_draft(chat_id):
    with get_db() as connection:
        row = connection.execute(
            "SELECT state, payload FROM telegram_drafts WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
    if not row:
        return None
    return {"state": row["state"], "payload": json.loads(row["payload"])}


def set_telegram_draft(chat_id, state, payload):
    with get_db() as connection:
        connection.execute(
            """
            INSERT INTO telegram_drafts (chat_id, state, payload, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET state = excluded.state,
                payload = excluded.payload, updated_at = excluded.updated_at
            """,
            (str(chat_id), state, json.dumps(payload), datetime.now().isoformat(timespec="seconds")),
        )
        connection.commit()


def clear_telegram_draft(chat_id):
    with get_db() as connection:
        connection.execute("DELETE FROM telegram_drafts WHERE chat_id = ?", (str(chat_id),))
        connection.commit()


def save_entry(entry):
    with get_db() as connection:
        cursor = connection.execute(
            """
            INSERT INTO entries (section, title, entry_date, entry_time, entry_end_time, location, notes,
                                 reminder_count, reminder_interval, reminder_interval_unit, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["section"],
                entry["title"],
                entry["entry_date"],
                entry["entry_time"],
                entry.get("entry_end_time", ""),
                entry["location"],
                entry["notes"],
                entry["reminder_count"],
                entry["reminder_interval"],
                entry["reminder_interval_unit"],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        connection.commit()
        return cursor.lastrowid


def telegram_section(text):
    options = {
        "timetable": "timetable",
        "schedule": "timetable",
        "exams": "exams",
        "exam": "exams",
        "events": "events",
        "event": "events",
        "important events": "events",
        "reminders": "reminders",
        "reminder": "reminders",
        "reminder notes": "reminders",
    }
    return options.get(text.strip().lower())


def parse_time_token(value, meridiem_hint=None):
    value = value.strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", value)
    if not match:
        raise ValueError

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3) or meridiem_hint
    if not 0 <= minute <= 59:
        raise ValueError
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError
        if meridiem == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    elif not 0 <= hour <= 23:
        raise ValueError
    return datetime_time(hour, minute)


def time_meridiem(value):
    match = re.search(r"(am|pm)\s*$", value.strip(), re.IGNORECASE)
    return match.group(1).lower() if match else None


def parse_telegram_time_window(text):
    """Return HH:MM start/end values; a single time means a one-hour block."""

    normalized = text.strip().replace("–", "-").replace("—", "-")
    range_match = re.fullmatch(
        rf"({TIME_TOKEN_PATTERN})\s*-\s*({TIME_TOKEN_PATTERN})", normalized
    )
    if range_match:
        raw_start, raw_end = range_match.groups()
        start_meridiem = time_meridiem(raw_start)
        end_meridiem = time_meridiem(raw_end)
        start = parse_time_token(raw_start, end_meridiem if not start_meridiem else None)
        end = parse_time_token(raw_end, start_meridiem if not end_meridiem else None)
        if end <= start:
            raise ValueError
    else:
        start = parse_time_token(normalized)
        end = (
            datetime.combine(datetime.today(), start)
            + timedelta(minutes=DEFAULT_ENTRY_DURATION_MINUTES)
        ).time()

    return start.strftime("%H:%M"), end.strftime("%H:%M")


def entry_value(entry, key, default=""):
    try:
        value = entry[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def entry_window(entry):
    entry_date = entry_value(entry, "entry_date")
    entry_time = entry_value(entry, "entry_time")
    if not entry_date or not entry_time:
        return None

    start = datetime.strptime(f"{entry_date}T{entry_time}", "%Y-%m-%dT%H:%M")
    end_time = entry_value(entry, "entry_end_time")
    if end_time:
        end = datetime.strptime(f"{entry_date}T{end_time}", "%Y-%m-%dT%H:%M")
    else:
        end = start + timedelta(minutes=DEFAULT_ENTRY_DURATION_MINUTES)
    if end <= start:
        return None
    return start, end


def find_schedule_conflicts(candidate):
    candidate_window = entry_window(candidate)
    if not candidate_window:
        return []

    candidate_id = entry_value(candidate, "id")
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT id, section, title, entry_date, entry_time, entry_end_time
            FROM entries
            WHERE entry_date = ? AND entry_time != ''
            ORDER BY entry_time, id
            """,
            (entry_value(candidate, "entry_date"),),
        ).fetchall()

    conflicts = []
    candidate_start, candidate_end = candidate_window
    for row in rows:
        if candidate_id and str(row["id"]) == str(candidate_id):
            continue
        other_window = entry_window(row)
        if not other_window:
            continue
        other_start, other_end = other_window
        if candidate_start < other_end and other_start < candidate_end:
            conflicts.append(
                {
                    "title": row["title"],
                    "section": SECTIONS[row["section"]]["label"],
                    "start": other_start.strftime("%H:%M"),
                    "end": other_end.strftime("%H:%M"),
                }
            )
    return conflicts


def finish_telegram_entry(chat_id, payload, confirmed=False):
    if not confirmed:
        conflicts = find_schedule_conflicts(payload)
        if conflicts:
            entry_end_time = payload.get("entry_end_time", "")
            new_time = payload["entry_time"]
            if entry_end_time:
                new_time = f"{new_time}-{entry_end_time}"
            conflict_date = datetime.strptime(
                payload["entry_date"], "%Y-%m-%d"
            ).strftime("%A, %d %B %Y")
            conflict_lines = "\n".join(
                f"  • {item['title']} [{item['section']}] — {item['start']}-{item['end']}"
                for item in conflicts
            )
            set_telegram_draft(chat_id, "conflict_confirmation", payload)
            telegram_send(
                chat_id,
                "⚠️ SCHEDULE CONFLICT — NOT SAVED YET ⚠️\n\n"
                f"New entry: {payload['title']}\n"
                f"Date: {conflict_date}\n"
                f"Time: {new_time}\n\n"
                "Overlaps with:\n"
                f"{conflict_lines}\n\n"
                "Reply YES to save anyway, or NO to cancel.",
            )
            return

    save_entry(payload)
    clear_telegram_draft(chat_id)
    schedule = ""
    if payload["section"] == "reminders":
        schedule = f"\nSchedule: {payload['reminder_count']} reminder(s) every {payload['reminder_interval']} {payload['reminder_interval_unit']}"
    time_display = payload["entry_time"] or "not set"
    if payload.get("entry_end_time"):
        time_display = f"{payload['entry_time']}-{payload['entry_end_time']}"
    telegram_send(
        chat_id,
        f"Saved in {SECTIONS[payload['section']]['label']}: {payload['title']}\n"
        f"Date: {payload['entry_date']}\nTime: {time_display}{schedule}\n\n"
        "Send /new to add another entry.",
        remove_keyboard=True,
    )
    telegram_send(
        chat_id,
        "Export your current Daymark calendar:",
        inline_keyboard=telegram_export_keyboard(),
    )


def handle_telegram_message(chat_id, text):
    text = text.strip()
    command = text.split()[0].lower().split("@", 1)[0] if text else ""
    if command in {"/export", "/calendar", "export"} or text.lower() == "export calendar":
        telegram_export_calendar(chat_id)
        return
    if command == "/help":
        telegram_send(
            chat_id,
            "Daymark commands:\n"
            "/new - add a task\n"
            "/export - receive the current calendar file for Google Calendar\n"
            "/cancel - cancel the current entry",
            inline_keyboard=telegram_export_keyboard(),
        )
        return
    if command in {"/cancel", "/stop"}:
        clear_telegram_draft(chat_id)
        telegram_send(chat_id, "Cancelled. Send /new whenever you want to add an entry.", remove_keyboard=True)
        return
    if command in {"/start", "/new", "new"}:
        set_telegram_draft(chat_id, "section", {})
        telegram_send(
            chat_id,
"Daymark is ready. Where should I put this? Reply with timetable, exams, events, or reminders.\n\nSend /cancel to stop, /export to receive your calendar file, or /help for commands.",
            telegram_section_keyboard(),
        )
        return

    draft = get_telegram_draft(chat_id)
    if not draft:
        set_telegram_draft(chat_id, "section", {"initial_text": text})
        telegram_send(chat_id, "Where should I put that? Reply with timetable, exams, events, or reminders.")
        return

    state = draft["state"]
    payload = draft["payload"]
    if state == "conflict_confirmation":
        answer = text.lower()
        if answer in {"yes", "y", "save", "proceed", "continue"}:
            finish_telegram_entry(chat_id, payload, confirmed=True)
        elif answer in {"no", "n", "cancel", "stop"}:
            clear_telegram_draft(chat_id)
            telegram_send(chat_id, "Not saved. Send /new to add a different entry.")
        else:
            telegram_send(chat_id, "Please reply yes to save it or no to cancel.")
        return
    if state == "section":
        section = telegram_section(text)
        if not section:
            telegram_send(chat_id, "Please reply with timetable, exams, events, or reminders.")
            return
        payload = {
            "section": section,
            "entry_date": "",
            "entry_time": "",
            "entry_end_time": "",
            "location": "",
            "notes": "",
            "reminder_count": 1,
            "reminder_interval": 0,
            "reminder_interval_unit": "hours",
        }
        initial_text = draft["payload"].get("initial_text", "")
        if initial_text:
            payload["title"] = initial_text
            set_telegram_draft(chat_id, "date", payload)
            telegram_send_date_prompt(chat_id)
        else:
            set_telegram_draft(chat_id, "title", payload)
            telegram_send(chat_id, "What should I remember? Send a short title.")
        return
    if state == "title":
        payload["title"] = text
        set_telegram_draft(chat_id, "date", payload)
        telegram_send_date_prompt(chat_id)
        return
    if state == "date":
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            telegram_send(chat_id, "Please use the date format YYYY-MM-DD, for example 2026-09-07.")
            return
        payload["entry_date"] = text
        set_telegram_draft(chat_id, "time_start", payload)
        telegram_send_time_start_prompt(chat_id, payload["section"])
        return
    if state == "time_start":
        if text.lower() == "no specific time":
            payload["entry_time"] = ""
            payload["entry_end_time"] = ""
        else:
            try:
                if "-" in text:
                    payload["entry_time"], payload["entry_end_time"] = parse_telegram_time_window(text)
                else:
                    payload["entry_time"] = parse_time_token(text).strftime("%H:%M")
                    payload["entry_end_time"] = ""
            except ValueError:
                telegram_send(
                    chat_id,
                    "Please choose a start time, or type it as HH:MM (for example 13:00).",
                    telegram_time_keyboard(payload["section"] != "reminders"),
                )
                return
            if not payload["entry_end_time"]:
                set_telegram_draft(chat_id, "time_end", payload)
                telegram_send_time_end_prompt(chat_id)
                return
        if payload["section"] == "reminders" and not payload["entry_time"]:
            telegram_send(chat_id, "Reminder notes need a start time. Please choose a time.", telegram_time_keyboard())
            return
        set_telegram_draft(chat_id, "location_choice", payload)
        telegram_send(chat_id, "Does it have a location?", telegram_yes_no_keyboard())
        return
    if state == "time_end":
        try:
            end_time = parse_time_token(text)
            start_time = datetime.strptime(payload["entry_time"], "%H:%M").time()
            if end_time <= start_time:
                raise ValueError
            payload["entry_end_time"] = end_time.strftime("%H:%M")
        except ValueError:
            telegram_send(
                chat_id,
                "Please choose an end time after the start time.",
                telegram_time_keyboard(),
            )
            return
        set_telegram_draft(chat_id, "location_choice", payload)
        telegram_send(chat_id, "Does it have a location?", telegram_yes_no_keyboard())
        return
    if state == "time":
        # Compatibility for drafts created before the start/end time flow.
        if text.lower() == "no specific time":
            payload["entry_time"] = ""
            payload["entry_end_time"] = ""
        else:
            try:
                payload["entry_time"], payload["entry_end_time"] = parse_telegram_time_window(text)
            except ValueError:
                telegram_send(
                chat_id,
                "Please use HH:MM for a one-hour entry, or a range like "
                "13:00-15:00 (or 1-3pm). Reply skip if there is no specific time.",
                telegram_time_keyboard(payload["section"] != "reminders"),
                )
                return
        if payload["section"] == "reminders" and not payload["entry_time"]:
            telegram_send(chat_id, "Reminder notes need a time. Please choose a time button.", telegram_time_keyboard())
            return
        set_telegram_draft(chat_id, "location_choice", payload)
        telegram_send(chat_id, "Does it have a location?", telegram_yes_no_keyboard())
        return
    if state == "location_choice":
        if text.lower() == "yes":
            set_telegram_draft(chat_id, "location", payload)
            telegram_send(chat_id, "Type the location.")
        elif text.lower() == "no":
            payload["location"] = ""
            set_telegram_draft(chat_id, "notes_choice", payload)
            telegram_send(chat_id, "Does it have notes?", telegram_yes_no_keyboard())
        else:
            telegram_send(chat_id, "Please press Yes or No.", telegram_yes_no_keyboard())
        return
    if state == "location":
        payload["location"] = text
        set_telegram_draft(chat_id, "notes_choice", payload)
        telegram_send(chat_id, "Does it have notes?", telegram_yes_no_keyboard())
        return
    if state == "notes_choice":
        if text.lower() == "yes":
            set_telegram_draft(chat_id, "notes", payload)
            telegram_send(chat_id, "Type the notes.")
        elif text.lower() == "no":
            payload["notes"] = ""
            if payload["section"] == "reminders":
                set_telegram_draft(chat_id, "reminder_count", payload)
                telegram_send(chat_id, "How many reminders? Choose a number.", telegram_count_keyboard())
            else:
                finish_telegram_entry(chat_id, payload)
        else:
            telegram_send(chat_id, "Please press Yes or No.", telegram_yes_no_keyboard())
        return
    if state == "notes":
        payload["notes"] = text
        if payload["section"] == "reminders":
            set_telegram_draft(chat_id, "reminder_count", payload)
            telegram_send(chat_id, "How many reminders? Choose a number.", telegram_count_keyboard())
        else:
            finish_telegram_entry(chat_id, payload)
        return
    if state == "reminder_count":
        try:
            count = int(text)
        except ValueError:
            count = 0
        if not 1 <= count <= 100:
            telegram_send(chat_id, "Please send a whole number from 1 to 100.")
            return
        payload["reminder_count"] = count
        set_telegram_draft(chat_id, "reminder_interval", payload)
        telegram_send(chat_id, "How often? Choose an interval.", telegram_interval_keyboard())
        return
    if state == "reminder_interval":
        parts = text.lower().split()
        if len(parts) != 2 or not parts[0].isdigit() or parts[1].rstrip("s") not in {"minute", "hour", "day", "week"}:
            telegram_send(chat_id, "Please use a format like 30 minutes, 2 hours, 1 day, or 1 week.")
            return
        interval = int(parts[0])
        if not 1 <= interval <= 100000:
            telegram_send(chat_id, "Please use an interval from 1 to 100000.")
            return
        payload["reminder_interval"] = interval
        payload["reminder_interval_unit"] = parts[1] if parts[1].endswith("s") else f"{parts[1]}s"
        finish_telegram_entry(chat_id, payload)


def reminder_occurrence(entry, now):
    if not entry["entry_time"] or not entry["reminder_interval"]:
        return None
    units = {"minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800}
    step = entry["reminder_interval"] * units.get(entry["reminder_interval_unit"], 0)
    if not step:
        return None
    start = datetime.strptime(
        f"{entry['entry_date']}T{entry['entry_time']}", "%Y-%m-%dT%H:%M"
    ).timestamp()
    elapsed = now - start
    if elapsed < 0:
        return None
    number = min(entry["reminder_count"], int(elapsed // step) + 1)
    if number > entry["reminder_count"]:
        return None
    return number, start + (number - 1) * step


def event_notification(entry, now):
    if entry["section"] == "reminders" or not entry["entry_date"] or not entry["entry_time"]:
        return None
    event_time = datetime.strptime(
        f"{entry['entry_date']}T{entry['entry_time']}", "%Y-%m-%dT%H:%M"
    )
    notification_time = event_time.replace(hour=12, minute=0, second=0, microsecond=0)
    notification_time -= timedelta(days=1)
    notification_timestamp = notification_time.timestamp()
    event_timestamp = event_time.timestamp()
    if now >= notification_timestamp and now < event_timestamp:
        return notification_timestamp
    return None


def telegram_worker():
    offset = None
    while True:
        updates = telegram_call("getUpdates", {"timeout": 10, "offset": offset} if offset else {"timeout": 10})
        if updates and updates.get("ok"):
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                with get_db() as connection:
                    claimed = connection.execute(
                        "INSERT OR IGNORE INTO telegram_updates (update_id, processed_at) VALUES (?, ?)",
                        (update["update_id"], datetime.now().isoformat(timespec="seconds")),
                    ).rowcount
                    connection.commit()
                if not claimed:
                    continue
                message = update.get("message", {})
                chat = message.get("chat", {})
                chat_id = chat.get("id")
                if chat_id is not None:
                    with get_db() as connection:
                        connection.execute(
                            "INSERT OR IGNORE INTO telegram_chats (chat_id, created_at) VALUES (?, ?)",
                            (str(chat_id), datetime.now().isoformat(timespec="seconds")),
                        )
                        connection.commit()
                    text = message.get("text", "")
                    if text:
                        handle_telegram_message(chat_id, text)
                callback = update.get("callback_query")
                if callback:
                    callback_message = callback.get("message", {})
                    callback_chat_id = callback_message.get("chat", {}).get("id")
                    if callback_chat_id is not None:
                        handle_telegram_callback(
                            callback_chat_id,
                            callback.get("id", ""),
                            callback_message.get("message_id"),
                            callback.get("data", ""),
                        )

        with get_db() as connection:
            entries = connection.execute(
                "SELECT * FROM entries WHERE entry_date != '' AND entry_time != ''"
            ).fetchall()
            chats = [row[0] for row in connection.execute("SELECT chat_id FROM telegram_chats")]
            for entry in entries:
                if entry["section"] == "reminders":
                    occurrence = reminder_occurrence(entry, time.time())
                    if not occurrence:
                        continue
                    reminder_number, occurrence_time = occurrence
                    occurrence_key = str(int(occurrence_time))
                    text = f"Daymark reminder\n{entry['title']}\nReminder {reminder_number} of {entry['reminder_count']}"
                else:
                    occurrence_time = event_notification(entry, time.time())
                    if occurrence_time is None:
                        continue
                    occurrence_key = f"event:{int(occurrence_time)}"
                    text = f"Daymark upcoming {entry['section']}\n{entry['title']}\nTomorrow at {entry['entry_time']}"
                for chat_id in chats:
                    already_sent = connection.execute(
                        "SELECT 1 FROM telegram_sent WHERE chat_id = ? AND entry_id = ? AND occurrence = ?",
                        (chat_id, entry["id"], occurrence_key),
                    ).fetchone()
                    if already_sent:
                        continue
                    if entry["location"]:
                        text += f"\nLocation: {entry['location']}"
                    if telegram_call("sendMessage", {"chat_id": chat_id, "text": text}):
                        connection.execute(
                            "INSERT OR IGNORE INTO telegram_sent (chat_id, entry_id, occurrence) VALUES (?, ?, ?)",
                            (chat_id, entry["id"], occurrence_key),
                        )
            connection.commit()
        time.sleep(2)


init_db()

if __name__ == "__main__":
    if get_telegram_token():
        threading.Thread(target=telegram_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
