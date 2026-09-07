# Daymark calendar import

This is a standalone utility. It lives in its own folder and only reads the
Daymark SQLite database; it does not modify the Flask app.

## Create a Google Calendar import file

When Daymark is running, the easiest option is to click **Export calendar**
on the Daymark dashboard. Your browser downloads `daymark_calendar.ics`.

You can also open the Daymark Telegram bot and send `/export`. The bot sends
the same `.ics` file directly in Telegram.

The command-line option remains available:

From the `CH` folder, run:

```powershell
python calendar_import\export_calendar.py
```

The utility creates `calendar_import\daymark_calendar.ics`.

If Daymark is running from another location, provide the database explicitly:

```powershell
python calendar_import\export_calendar.py `
  --database "C:\path\to\life_admin.db" `
  --timezone "Asia/Singapore"
```

Timed entries become one-hour events by default. Entries with a reminder
schedule become finite recurring events using their existing count and
interval. To use a different event length, add for example:

```powershell
python calendar_import\export_calendar.py --duration-minutes 30
```

## Import the file into Google Calendar

1. Open Google Calendar in a browser.
2. Open **Settings** → **Import & export**.
3. Choose `calendar_import\daymark_calendar.ics`.
4. Select the destination calendar and click **Import**.

Google Calendar imports a snapshot of the entries; it is not a live connection.
If an entry changes in Daymark, send `/export` again (or use the website's
export button) and import the updated file. For clean updates, import into a
separate `Daymark` calendar and replace that calendar's old events before
importing the new snapshot.
