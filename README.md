# Daymark

A mobile-first life admin dashboard built with Flask and SQLite. It can be opened from a Telegram bot's web app button or any browser on your phone.

## Manual setup

1. Make sure Python 3.10 or newer is installed:

	```bash
	python3 --version
	```

2. From the project directory, create and activate a virtual environment:

	```bash
	python3 -m venv .venv
	source .venv/bin/activate
	```

	On Windows PowerShell, activate it with:

	```powershell
	.venv\Scripts\Activate.ps1
	```

3. Install the dependencies:

	```bash
	python -m pip install --upgrade pip
	python -m pip install -r requirements.txt
	```

4. Start the app:

	```bash
	python app.py
	```

5. Open `http://localhost:5000` in a browser. The SQLite database is created automatically as `life_admin.db` in the project directory.

To stop the app, press `Ctrl+C`. To leave the virtual environment, run `deactivate`.

## Run locally

```bash
python -m pip install -r requirements.txt
python app.py
```

Then open `http://localhost:5000`.

Entries are stored in `life_admin.db` next to the app. The optional location field can be left blank for non-location reminders. Reminder notes can store a count and interval (minutes, hours, days, or weeks).

## Telegram bot

The Telegram bot token is the credential used to connect this app to Telegram. Create a bot with `@BotFather`, copy its token, and put it in the local `.env` file:

```text
TELEGRAM_BOT_TOKEN=your-token-here
```

The `.env` file is excluded from version control and should only be readable by your user account. Start the app normally:

```bash
python app.py
```

Open the bot in Telegram and send `/start` or `/new`. The bot guides you through:

1. Choosing `timetable`, `exams`, `events`, or `reminders`.
2. Entering the title, date, time, location, and notes.
3. For reminders, entering the number of reminders and an interval such as `30 minutes`, `2 hours`, `1 day`, or `1 week`.

The app saves the completed entry in SQLite and sends a confirmation. The background worker then sends scheduled reminder messages to every Telegram chat that has started the bot. Send `/cancel` to abandon an entry.

The dashboard is available locally at `http://127.0.0.1:5000`. A public HTTPS URL is only needed if Telegram must open the dashboard itself as a Web App.

## Simple Telegram test bot

`telegram_bot.py` is a small standalone bot with only `/start` and `/help` commands. Run it with the same virtual environment as the app:

```bash
source .venv/bin/activate
python telegram_bot.py
```

Do not run `telegram_bot.py` and `app.py` at the same time because both use Telegram polling. The two `.venv` directories are separate virtual environments; `.venv-1` was likely created automatically because `.venv` already existed. Keep one and use it consistently. Both currently use Python 3.9.6.
