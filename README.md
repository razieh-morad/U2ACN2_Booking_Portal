# U2ACN2 Nanolab Portal

A unified Flask app for **lab equipment booking** and **chemical inventory management** at the U2ACN2 Nanolab, University of the Western Cape.

---

## Features

### Equipment Booking
- 12 lab instruments, each with its own admin login
- Slot-based availability calendar (2-week rolling window)
- Instrument-specific booking forms (Furnace, XPS, and 10 generic labs)
- Email notifications: submission → admin approval/rejection → user confirmation
- One-click approve/reject links in admin emails
- User cancellation via token link
- Reminder emails (call `/reminders/send` from a cron job)

### Chemical Inventory
- 83 chemicals pre-loaded (your lab's inventory CSV)
- Search by **name or formula**, case-insensitive
- **Reserved chemicals** shown in amber with the reserved person/label — still visible and searchable
- Inline request form on each chemical card
- Purchase request form for unlisted chemicals
- Admin dashboard: add/edit/delete chemicals, manage reservations, approve/reject/fulfil requests
- Email alerts to admin on every request; purchase requests notify all configured recipients
- CSV export of full inventory

---

## File Structure

```
portal/
├── app.py                  ← main application
├── requirements.txt
├── Procfile                ← for Render/Heroku
├── render.yaml             ← one-click Render deploy config
├── .env.example            ← copy to .env for local dev
├── static/
│   └── style.css           ← complete stylesheet
└── templates/
    ├── base.html           ← shared layout
    ├── index.html          ← homepage / lab listing
    ├── availability.html   ← 2-week availability calendar
    ├── lab_generic.html    ← booking form (10 standard labs)
    ├── furnace.html        ← booking form (Furnace, with extra fields)
    ├── xps.html            ← booking form (XPS, with extra fields)
    ├── admin_login.html    ← per-lab admin login
    ├── admin.html          ← admin bookings list
    ├── admin_booking.html  ← approve/reject/delete a booking
    ├── chemicals.html      ← public chemical inventory
    ├── purchase_request.html ← purchase request form
    ├── chem_admin_login.html ← chemical admin login
    └── chem_admin.html     ← chemical admin dashboard
```

---

## Local Development

```bash
# 1. Clone and enter
git clone https://github.com/razieh-morad/U2ACN2_Booking_Portal
cd U2ACN2_Booking_Portal

# 2. Copy all files from this package into the repo root,
#    replacing app.py and adding the new templates + static files.

# 3. Set up environment
cp .env.example .env
# Edit .env with your credentials

# 4. Install dependencies
pip install -r requirements.txt

# 5. Run
flask run
# Or: python app.py
```

Visit http://localhost:5000

---

## Deployment on Render

1. Push all files to your GitHub repo
2. In Render: **New → Web Service** → connect your repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --workers 2 --threads 2 --timeout 60`
5. Add environment variables (see below)

### Required Environment Variables

| Variable | Description |
|----------|-------------|
| `FLASK_SECRET_KEY` | Random secret (Render can auto-generate) |
| `APP_TZ` | Timezone, e.g. `Africa/Johannesburg` |
| `SMTP_HOST` | SMTP server hostname |
| `SMTP_PORT` | Usually `587` |
| `SMTP_USER` | Sender email address |
| `SMTP_PASSWORD` | SMTP password or app password |
| `CHEM_ADMIN_PASSWORD` | Password for `/admin/chemicals` |
| `CHEM_ADMIN_EMAIL` | Receives all in-stock request notifications |
| `PURCHASE_NOTIFY_EMAILS` | Comma-separated list — all receive purchase requests |
| `ADMIN_<LAB>_USERNAME` | Per-lab admin username (see `.env.example`) |
| `ADMIN_<LAB>_PASSWORD` | Per-lab admin password |

For Gmail SMTP: use an **App Password** (not your regular password).
Enable 2FA → Google Account → Security → App Passwords.

---

## Database

- **SQLite** (default, local dev): `bookings.sqlite3` in the app directory
- **PostgreSQL** (Render production): set `DATABASE_URL` env var — tables are auto-created on first startup

On first startup, 83 chemicals are seeded automatically into the `chemicals` table.

---

## URLs

| URL | Description |
|-----|-------------|
| `/` | Homepage — all labs |
| `/chemicals` | Public chemical inventory |
| `/chemicals?q=NaCl` | Search chemicals |
| `/chemicals/purchase` | Purchase request form |
| `/admin/chemicals` | Chemical admin dashboard |
| `/labs/<slug>` | Generic lab booking form |
| `/furnace` | Furnace booking |
| `/xps` | XPS booking |
| `/labs/<slug>/availability` | Availability calendar |
| `/admin/<slug>` | Per-lab admin (requires login) |
| `/admin` | Admin portal index |
| `/health` | Health check endpoint |
| `/debug/init` | Diagnostic — remove after confirming OK |
| `/reminders/send` | Trigger reminder emails (call from cron) |

---

## Reservation Feature

In the admin dashboard → Inventory tab → click **Reserve** on any chemical:
- Set **Reserved For**: the person's name or email
- Set **Reserved Label**: shown on the card (e.g. "PhD Project — MXene synthesis")
- Click **Clear** to remove a reservation

Reserved chemicals appear to all users with an **amber banner** — they are not hidden, just flagged.

---

## Removing the old `chemical-inventory` repo

1. Export data from the old Streamlit app (Download CSV button)
2. Import into the new portal via `/admin/chemicals` → Add Chemical (or re-seed from CSV)
3. Archive or delete the old repo on GitHub

---

## Admin URL Reference (per-lab slugs)

| Lab | Slug | Admin URL |
|-----|------|-----------|
| Carbonate Furnace | `furnace` | `/admin/furnace` |
| XPS | `xps` | `/admin/xps` |
| Manual Drying Oven | `manual-drying-oven` | `/admin/manual-drying-oven` |
| Automated Drying Oven | `automated-drying-oven` | `/admin/automated-drying-oven` |
| Sputtering | `sputtering` | `/admin/sputtering` |
| Auto Lab | `auto-lab` | `/admin/auto-lab` |
| UV-Vis Currie 500 | `uv-vis-currie-500` | `/admin/uv-vis-currie-500` |
| Centrifuge | `centrifuge` | `/admin/centrifuge` |
| Pelletizer | `pelletizer` | `/admin/pelletizer` |
| Thermal Conductivity System | `thermal-conductivity-system` | `/admin/thermal-conductivity-system` |
| Freeze Dryer | `freeze-dryer` | `/admin/freeze-dryer` |
| Spin Coater | `spin-coater` | `/admin/spin-coater` |
