# Daymark calendar import

This is a standalone utility. It lives in its own folder and only reads the
Daymark SQLite database; it does not modify the Flask app.

## Create a Google Calendar import file

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

Google Calendar imports a snapshot of the entries. Run the exporter again and
re-import the new file whenever the Daymark database changes.
