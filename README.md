# Nanolab Booking Portal (v3)

## What you asked for (implemented)
- Click any **Available** slot → opens booking page with **date/time prefilled**
- XPS administrators: Dr Itani Madiba (06598853331)
- Booking + availability pages use the same title:
  - Nanomaterials Furnace (Carbonate Furnace)
  - XPS (X-ray Photoelectron Spectroscopy)
- Admin pages protected with a password login
- Admin can export bookings to CSV or Excel
- Admin can create bookings directly (quick booking) to reserve slots

## Setup on Render
1) Push this repo to GitHub
2) Render Web Service:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`
3) Render env vars:
   - DATABASE_URL  (Neon connection string)
   - FLASK_SECRET_KEY (Render can auto-generate)
   - ADMIN_PASSWORD  (set a strong password)
   - ADMIN_USER (optional, default: admin)

## URLs
- /labs/furnace
- /labs/furnace/availability
- /labs/xps
- /labs/xps/availability
- /admin/login
- /admin/bookings/furnace
- /admin/bookings/xps
- /health
