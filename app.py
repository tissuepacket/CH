from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib import parse, request as urllib_request

from flask import Flask, jsonify, redirect, render_template, request, url_for

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
        columns = {row[1] for row in connection.execute("PRAGMA table_info(entries)")}
        migrations = {
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


def telegram_call(method, values=None):
    token = get_telegram_token()
    if not token:
        return None
    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    payload = parse.urlencode(values or {}).encode()
    try:
        with urllib_request.urlopen(urllib_request.Request(endpoint, data=payload), timeout=35) as response:
            return json.loads(response.read().decode())
    except Exception:
        return None


def telegram_send(chat_id, text, keyboard=None, remove_keyboard=False):
    values = {"chat_id": chat_id, "text": text}
    if remove_keyboard:
        values["reply_markup"] = json.dumps({"remove_keyboard": True})
    elif keyboard:
        values["reply_markup"] = json.dumps(
            {"keyboard": keyboard, "resize_keyboard": True, "one_time_keyboard": True}
        )
    telegram_call("sendMessage", values)


def telegram_section_keyboard():
    return [["Timetable", "Exams"], ["Important events", "Reminder notes"]]


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
            INSERT INTO entries (section, title, entry_date, entry_time, location, notes,
                                 reminder_count, reminder_interval, reminder_interval_unit, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["section"],
                entry["title"],
                entry["entry_date"],
                entry["entry_time"],
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


def finish_telegram_entry(chat_id, payload):
    save_entry(payload)
    clear_telegram_draft(chat_id)
    schedule = ""
    if payload["section"] == "reminders":
        schedule = f"\nSchedule: {payload['reminder_count']} reminder(s) every {payload['reminder_interval']} {payload['reminder_interval_unit']}"
    telegram_send(
        chat_id,
        f"Saved in {SECTIONS[payload['section']]['label']}: {payload['title']}\n"
        f"Date: {payload['entry_date']}\nTime: {payload['entry_time'] or 'not set'}{schedule}\n\n"
        "Send /new to add another entry.",
        remove_keyboard=True,
    )


def handle_telegram_message(chat_id, text):
    text = text.strip()
    command = text.split()[0].lower().split("@", 1)[0] if text else ""
    if command in {"/cancel", "/stop"}:
        clear_telegram_draft(chat_id)
        telegram_send(chat_id, "Cancelled. Send /new whenever you want to add an entry.", remove_keyboard=True)
        return
    if command in {"/start", "/new", "new"}:
        set_telegram_draft(chat_id, "section", {})
        telegram_send(
            chat_id,
            "Daymark is ready. Choose a section.\n\nSend /cancel to stop.",
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
    if state == "section":
        section = telegram_section(text)
        if not section:
            telegram_send(chat_id, "Please reply with timetable, exams, events, or reminders.")
            return
        payload = {
            "section": section,
            "entry_date": "",
            "entry_time": "",
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
            telegram_send(chat_id, "What date? Use YYYY-MM-DD, for example 2026-09-07.")
        else:
            set_telegram_draft(chat_id, "title", payload)
            telegram_send(chat_id, "What should I remember? Send a short title.")
        return
    if state == "title":
        payload["title"] = text
        set_telegram_draft(chat_id, "date", payload)
        telegram_send(chat_id, "What date? Use YYYY-MM-DD, for example 2026-09-07.")
        return
    if state == "date":
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            telegram_send(chat_id, "Please use the date format YYYY-MM-DD, for example 2026-09-07.")
            return
        payload["entry_date"] = text
        set_telegram_draft(chat_id, "time", payload)
        telegram_send(
            chat_id,
            "Choose a time. Scroll through the buttons to find the 30-minute slot you want.",
            telegram_time_keyboard(payload["section"] != "reminders"),
        )
        return
    if state == "time":
        if text.lower() == "no specific time":
            payload["entry_time"] = ""
        else:
            try:
                datetime.strptime(text, "%H:%M")
            except ValueError:
                telegram_send(chat_id, "Please choose one of the time buttons.", telegram_time_keyboard(payload["section"] != "reminders"))
                return
            payload["entry_time"] = text
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
