# KGPL LPG Dashboard (Flask)

## Data persistence (important)

GitHub stores **code only**. Runtime data (demands, availability) must live in **Neon Postgres**.

1. Follow **NEON_SETUP.txt** (create Neon DB + set `DATABASE_URL` on Render).
2. Without `DATABASE_URL`, the app uses SQLite on Render free disk → data can erase on sleep/redeploy.

## Deploy on Render

- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
- Env: `DATABASE_URL` = Neon connection string

## Default login

- User: `master`
- Password: `Master@IHB2026`

Change passwords after first login.
