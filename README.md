# CH

The Daymark app sends Telegram notifications for scheduled entries. Reminder notes use their configured count and interval. Timetable, exam, and important-event entries with a date and time send one notification at 12:00 PM on the previous day.

In Telegram, send `/new` or `new`, then tap the section button. The bot shows a calendar for the date, buttons for optional location and notes, reminder count and interval, and a scrollable list of 30-minute time slots. Titles, locations, and notes are still entered as text when needed.

## Run locally on macOS

Open Terminal and run:

```bash
cd /Users/tissuepacket/Desktop/Cloudhack
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Configure Telegram by creating a local `.env` file:

```bash
cp .env.example .env
open -e .env
```

Replace the placeholder value with your `TELEGRAM_BOT_TOKEN`, save the file, and start the app:

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser. The SQLite database is created automatically as `life_admin.db`.

The Flask app starts the Telegram polling worker when a token is configured. Do not run `telegram_bot.py` at the same time, because it starts a separate Telegram polling process. Stop the app with `Control+C`, then leave the virtual environment with:

```bash
deactivate
```

> **Security:** Never commit `.env` or share its bot token. If a real token has been exposed, revoke it with BotFather and replace it in `.env`.
