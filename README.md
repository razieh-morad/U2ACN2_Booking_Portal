# Nanolab Booking Portal (Render + Neon ready)

Endpoints:
- /health (ping this to keep Render awake)
- /warm-db (optional: pings DB too)

Run locally:
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FLASK_SECRET_KEY=dev
export DATABASE_URL='postgresql://...?...sslmode=require'
python app.py
