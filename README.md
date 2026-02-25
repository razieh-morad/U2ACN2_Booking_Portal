# U2ACN2 Nanolab Booking Portal (v6)

This version fixes the template errors and implements **separate admin credentials per lab**.

## Admin login (different for each lab)
Set these Render env vars:
- ADMIN_FURNACE_EMAIL
- ADMIN_FURNACE_PASSWORD
- ADMIN_XPS_EMAIL
- ADMIN_XPS_PASSWORD

Admin URLs:
- /admin/furnace  (redirects to /admin/furnace/login if not logged in)
- /admin/xps      (redirects to /admin/xps/login if not logged in)

## Email notifications (when admin changes a booking)
Minimum:
- SMTP_HOST (e.g., smtp.gmail.com)
- SMTP_PORT (587)
- SMTP_USE_TLS (true)

Default behavior:
- Each lab sends from its own admin email/password.

Optional overrides (per lab):
- SMTP_FURNACE_USER / SMTP_FURNACE_PASSWORD / SMTP_FURNACE_FROM
- SMTP_XPS_USER / SMTP_XPS_PASSWORD / SMTP_XPS_FROM

Note: many providers require an **app password** for SMTP.

## Multi-slot booking
Users and admins can select multiple available slots (next 2 weeks). Each slot is stored as its own row with a shared booking_group_id.

## Health check
Use /health for Render Health Check Path and for external pings.

## Furnace slots
- Furnace uses two fixed daily slots: 08:00–12:00 and 12:00–16:00.
- Other labs continue to use hourly slots (SLOT_MINUTES).

## Admin delete
- Admin edit page includes a Delete booking action (optional user notification by email).
