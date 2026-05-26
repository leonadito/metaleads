# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**LeadManager 2.0** is a multi-tenant web application for real estate lead management. Users configure a public Google Sheets spreadsheet as their lead source; the app displays leads in a Kanban board and sends Telegram notifications when new leads arrive.

The full specification lives in [PRD_LEADMANAGER_2.0.md](PRD_LEADMANAGER_2.0.md).

## Tech Stack

- **Backend:** Django + Django REST Framework, SQLite, Celery/APScheduler for polling
- **Frontend:** Vibe Code (JavaScript SPA)
- **Data source:** Public Google Sheets via CSV export (no Google API key needed)
- **Notifications:** Telegram Bot API
- **Tests:** pytest

## Environment Variables

```
DEBUG=True
SECRET_KEY=xxx
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_BOT_NAME=xxx
DATABASE_URL=sqlite:///db.sqlite3
```

## Common Commands

```bash
# Backend setup
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser   # required to create users (no self-signup)
python manage.py runserver

# Run tests
pytest

# Run a single test
pytest path/to/test_file.py::test_function_name
```

## Architecture

### Multi-tenancy model
Every user has a `UserProfile` (1-to-1 with Django's built-in `User`) that stores their `sheet_id` (Google Sheets ID) and Telegram config. All queries must filter by `request.user` — users are fully isolated.

### Google Sheets integration
Sheets are public. Data is fetched via the CSV export URL:
```
https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}
```
No Google API key or OAuth is required. Sheet names/tabs must be discovered by parsing the HTML page for `gid` values, or stored in `SheetMetadata` after first detection.

### Kanban status sync
When a user drags a card, the frontend sends `PATCH /api/lead/<row_index>/` with the new status. The backend must write the status back to the Google Sheet (the status column in the row at `row_index`). Writing to a public Sheet requires Selenium or the Sheets API with a service account — clarify this before implementing.

### Lead polling (background task)
`check_new_leads()` runs every 5 minutes via Celery Beat or APScheduler. It:
1. For each active user, fetches their sheet CSV
2. Compares row count / last known row index against `SyncLog`
3. Sends Telegram notification for each new lead

### Database models (SQLite)
- `UserProfile` — `sheet_id`, `telegram_chat_id`, `telegram_enabled`
- `SheetMetadata` — `sheet_names` (JSON), `last_sync` timestamp, per user+sheet
- `SyncLog` — `last_lead_row_index` per user+sheet name, used for change detection

### API surface
```
POST   /api/auth/login/
POST   /api/auth/logout/
GET    /api/profile/
PUT    /api/profile/
POST   /api/profile/test-telegram/
GET    /api/dashboard/
GET    /api/sheets/
GET    /api/kanban/<sheet_name>/
PATCH  /api/lead/<row_index>/
POST   /api/telegram/webhook/
```

All endpoints require authentication (`@login_required` / `IsAuthenticated`). API docs served at `/api/docs/` (Swagger/OpenAPI).

### User creation
There is **no self-signup**. Users are created exclusively through Django Admin (`/admin`). Redirect first-time users (no `sheet_id` configured) to the Profile page.

## Key Implementation Notes

- Kanban columns are always the fixed 6: `Criado`, `Em Análise`, `Qualificado`, `Não Qualificado`, `Convertido`, `Perdido`
- Google Sheets polling rate limit: keep requests under 100/minute
- Telegram: validate `telegram_chat_id` is set before sending; skip notification if `telegram_enabled=False`
- Cache sheet tab names in `SheetMetadata`; refresh every hour or on explicit user request
