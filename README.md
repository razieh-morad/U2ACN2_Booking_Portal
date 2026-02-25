# U2ACN2 Nanolab Booking Portal

A lightweight, web-based booking system for U2ACN2/iThemba Labs instruments.  
Built with **Flask + Neon (Postgres) + Render** so students can book lab time online and admins can manage schedules.

---

## Features

### Public (Students / Users)
- **Lab list homepage** with links to each instrument
- **Booking pages per lab**
  - Submit user details + experiment/sample details
  - Choose a date/time manually (single slot)
  - **Select multiple slots** from the “available slots” table (next 2 weeks)
- **Availability pages per lab**
  - Shows next 2 weeks, Monday–Friday, 08:00–16:00
  - Booked slots are blocked
  - Clicking an available slot pre-fills the booking form (where applicable)
- **No overlapping bookings**: the system blocks conflicts automatically

### Admin (Per Lab)
- **Separate admin login per lab** (different credentials)
- **Admin dashboard per lab**
  - View all bookings for that lab
  - **Reserve slots** (single or multiple) for maintenance/VIP usage
  - Export bookings to **CSV** and **Excel**
  - **Edit booking time/date**
  - **Delete a booking** from the edit page (optional user email notification)

### Email Notifications (Optional)
- If SMTP is configured, when an admin **changes a booking time** (or deletes it), the user can be automatically notified by email.
- Email settings are not stored in code — they are set via environment variables on Render.

---

## Webpages / Routes

### Public pages
- `/`  
  Landing page listing all available labs.
- `/labs/furnace`  
  Furnace booking page.
- `/labs/xps`  
  XPS booking page.
- `/labs/<lab_slug>/availability`  
  Availability table for next 2 weeks (Mon–Fri, 08:00–16:00).  
  Examples:
  - `/labs/furnace/availability`
  - `/labs/xps/availability`
- `/bookings/<id>`  
  Booking confirmation page after submitting.

### Admin pages (lab-specific)
- `/admin/furnace`  
  Furnace admin dashboard (redirects to furnace login if not logged in)
- `/admin/xps`  
  XPS admin dashboard
- `/admin/<lab_slug>/login`  
  Lab-specific admin login form
- `/admin/<lab_slug>/edit/<id>`  
  Edit booking time/date (and delete booking) for a single record

### Exports
- `/admin/export/<lab_slug>.csv`
- `/admin/export/<lab_slug>.xlsx`

### Health
- `/health`  
  Returns JSON `{ "status": "ok" }`  
  Use this for **Render Health Check Path** and external uptime monitors.

---

## Time Slot Rules

### Default slot logic
- Availability is shown for the **next 2 weeks**.
- Only **weekdays (Mon–Fri)** are offered.
- Working hours are **08:00–16:00**.
- By default, slots follow `SLOT_MINUTES` (commonly 60 minutes).

### Furnace slot logic (if enabled in your latest code)
Only **two blocks per day**:
- 08:00–12:00
- 12:00–16:00

---

## Data Storage

### Production
- Uses **Neon Postgres** via `DATABASE_URL`
- Table name: `bookings`
- The app auto-migrates missing columns on startup to avoid schema mismatch errors.

### Local development (optional)
- Falls back to a local SQLite database file (`bookings.sqlite3`) if `DATABASE_URL` is not set.

---

## Deployment (Render + Neon)

### 1) Neon
1. Create a Neon project and database.
2. Copy the **connection string** and set it as `DATABASE_URL` on Render.

### 2) Render
1. Create a new **Web Service** from your GitHub repo.
2. Build command:
   - `pip install -r requirements.txt`
3. Start command:
   - `gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`
4. Health Check Path:
   - `/health`

---

## Required Environment Variables (Render)

### Database
- `DATABASE_URL`  
  Neon Postgres connection string.

### Per-lab admin accounts
These control who can access each lab’s admin dashboard:

**Furnace**
- `ADMIN_FURNACE_EMAIL`
- `ADMIN_FURNACE_PASSWORD`

**XPS**
- `ADMIN_XPS_EMAIL`
- `ADMIN_XPS_PASSWORD`

---

## Optional Environment Variables

### Timezone
- `APP_TZ` (default: `Africa/Johannesburg`)

### Slot size (for non-Furnace labs)
- `SLOT_MINUTES` (default: `60`)

### SMTP Email (Optional but recommended for notifications)

**Global SMTP server**
- `SMTP_HOST` (e.g., `smtp.gmail.com`)
- `SMTP_PORT` (commonly `587`)
- `SMTP_USE_TLS` (`true` recommended)
- `SMTP_FROM_NAME` (display name in outgoing emails)

**Per-lab sender overrides (recommended)**
If you want explicit SMTP credentials (instead of using admin login details):
- `SMTP_FURNACE_USER`
- `SMTP_FURNACE_PASSWORD`
- `SMTP_FURNACE_FROM`

- `SMTP_XPS_USER`
- `SMTP_XPS_PASSWORD`
- `SMTP_XPS_FROM`

> For Gmail, use an **App Password** (not your normal password).  
> Many organizations block SMTP auth without app passwords.

---

## Admin Workflow

### Reserve slots (admin)
1. Login to `/admin/<lab>`
2. Use “Admin quick booking”
3. Select multiple available slots (checkboxes) OR enter a manual slot
4. Submit → slots become unavailable to users

### Modify a user booking
1. Admin dashboard → click **Edit**
2. Change date/time
3. Save
4. If SMTP is configured, user receives an email notification.

### Delete a booking
1. Admin dashboard → **Edit**
2. Click **Delete booking**
3. Confirm
4. If SMTP is configured, user receives a cancellation email.

---

## Troubleshooting

### “Internal Server Error” on booking
Most common causes:
- `DATABASE_URL` missing/incorrect
- Old database schema missing columns (auto-migration usually fixes this)
- Render still running an older commit (clear build cache & redeploy)

Check: Render → **Logs** → submit booking → read the traceback.

### Admin page errors
- Missing `ADMIN_<LAB>_EMAIL` / `ADMIN_<LAB>_PASSWORD` env vars
- Wrong credentials entered

### Email not sending
- SMTP not configured (`SMTP_HOST` missing)
- Wrong SMTP password (Gmail requires **App Password**)
- Provider blocks SMTP AUTH (common for institutional emails)

---

## Notes / Future Improvements
- Add calendar-style UI (weekly view)
- Add user cancellation link with token
- Add “maintenance mode” per lab
- Add admin management for multiple admins per lab
