from __future__ import annotations

import csv
import hmac
import json as _json
import os
import sqlite3
import smtplib
import threading
import urllib.request
import uuid
from datetime import datetime, date, time, timedelta
from email.message import EmailMessage
from io import BytesIO, StringIO
from typing import Optional, List, Any, Dict, Tuple
from zoneinfo import ZoneInfo

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    session, abort, make_response, jsonify
)

# ------------------------------------------------------------------ Config ---

TZ = ZoneInfo(os.environ.get("APP_TZ", "Africa/Johannesburg"))
WORKDAY_START = time(8, 0)
WORKDAY_END   = time(16, 0)
SLOT_MINUTES  = int(os.environ.get("SLOT_MINUTES", "60"))

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.lower().startswith("postgres")

if USE_POSTGRES:
    from psycopg2.pool import ThreadedConnectionPool

APP_DIR     = os.path.abspath(os.path.dirname(__file__))
SQLITE_PATH = os.path.join(APP_DIR, "bookings.sqlite3")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

_pg_pool: "ThreadedConnectionPool|None" = None
_db_initialized = False

# -------------------------------------------------------------------- Labs ---

LABS: Dict[str, Dict[str, Any]] = {
    "furnace": {
        "title": "Nanomaterials Furnace (Carbonate Furnace)",
        "subtitle": "Carbonate Furnace",
        "min_notice_hours": 24,
        "max_days_ahead": 30,
        "max_duration_hours": 4,
    },
    "xps": {
        "title": "XPS (X-ray Photoelectron Spectroscopy)",
        "subtitle": "Surface chemical analysis",
        "min_notice_hours": 48,
        "max_days_ahead": 30,
        "max_duration_hours": 8,
    },
    "manual-drying-oven":    {"title": "Manual drying oven",          "subtitle": "Drying",               "min_notice_hours": 2,  "max_days_ahead": 30, "max_duration_hours": 8},
    "automated-drying-oven": {"title": "Automated drying oven",       "subtitle": "Drying",               "min_notice_hours": 2,  "max_days_ahead": 30, "max_duration_hours": 8},
    "sputtering":            {"title": "Sputtering",                   "subtitle": "Thin films / coatings","min_notice_hours": 24, "max_days_ahead": 30, "max_duration_hours": 8},
    "auto-lab":              {"title": "Auto lab",                     "subtitle": "Automated workflows",  "min_notice_hours": 4,  "max_days_ahead": 30, "max_duration_hours": 8},
    "uv-vis-currie-500":     {"title": "UV-Vis Currie 500",            "subtitle": "Optical spectroscopy", "min_notice_hours": 2,  "max_days_ahead": 30, "max_duration_hours": 4},
    "centrifuge":            {"title": "Centrifuge",                   "subtitle": "Sample separation",    "min_notice_hours": 1,  "max_days_ahead": 30, "max_duration_hours": 4},
    "pelletizer":            {"title": "Pelletizer",                   "subtitle": "Pellet pressing",      "min_notice_hours": 1,  "max_days_ahead": 30, "max_duration_hours": 4},
    "thermal-conductivity-system": {"title": "Thermal conductivity system", "subtitle": "Thermal transport", "min_notice_hours": 24, "max_days_ahead": 30, "max_duration_hours": 8},
    "freeze-dryer":          {"title": "Freeze dryer",                 "subtitle": "Lyophilization",       "min_notice_hours": 24, "max_days_ahead": 30, "max_duration_hours": 8},
    "spin-coater":           {"title": "Spin coater",                  "subtitle": "Thin film deposition", "min_notice_hours": 2,  "max_days_ahead": 30, "max_duration_hours": 4},
}

# -------------------------------------------------------- Per-lab admins ----

def _env_slug(slug: str) -> str:
    return slug.upper().replace("-", "_").replace(" ", "_")

ADMIN: Dict[str, Dict[str, str]] = {}
for _slug in LABS.keys():
    _key = _env_slug(_slug)
    ADMIN[_slug] = {
        "username": os.environ.get(f"ADMIN_{_key}_USERNAME", "").strip(),
        "password": os.environ.get(f"ADMIN_{_key}_PASSWORD", "").strip(),
    }

# ------------------------------------------------ Chemical inventory admin --
CHEM_ADMIN_EMAIL    = os.environ.get("CHEM_ADMIN_EMAIL", "").strip()
CHEM_ADMIN_PASSWORD = os.environ.get("CHEM_ADMIN_PASSWORD", "admin123").strip()

# Purchase request notification recipients (comma-separated emails)
PURCHASE_NOTIFY_EMAILS = [
    e.strip() for e in os.environ.get("PURCHASE_NOTIFY_EMAILS", CHEM_ADMIN_EMAIL).split(",")
    if e.strip()
]

# --------------------------------------------------------- EMAIL config ----
# Uses Resend HTTP API — works on Render free tier (SMTP ports are blocked).
# Sign up free at resend.com, create an API key, add your sending domain.

RESEND_API_KEY  = os.environ.get("RESEND_API_KEY",  "").strip()
BREVO_API_KEY   = os.environ.get("BREVO_API_KEY",   "").strip()
SMTP_FROM_NAME  = os.environ.get("SMTP_FROM_NAME",  "U2ACN2 Nanolab Portal")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", "").strip()

# BOOKING_ADMIN_EMAIL — who receives new booking notifications
BOOKING_ADMIN_EMAIL = os.environ.get("BOOKING_ADMIN_EMAIL", SMTP_FROM_EMAIL).strip()

def smtp_ready() -> bool:
    return bool(SMTP_FROM_EMAIL and (BREVO_API_KEY or RESEND_API_KEY))

# ============================================================= DB SCHEMA ====

PG_COLUMNS = {
    "lab_slug":              "TEXT NOT NULL",
    "booking_group_id":      "TEXT",
    "user_name":             "TEXT NOT NULL",
    "user_email":            "TEXT NOT NULL",
    "nanomaterial_type":     "TEXT",
    "melting_point":         "TEXT",
    "material_density":      "TEXT",
    "anneal_temp_c":         "TEXT",
    "anneal_time_h":         "TEXT",
    "gas_type":              "TEXT",
    "pressure":              "TEXT",
    "vacuum":                "BOOLEAN NOT NULL DEFAULT FALSE",
    "sample_name":           "TEXT",
    "sample_count":          "INTEGER",
    "elements_of_interest":  "TEXT",
    "analysis_type":         "TEXT",
    "charge_neutralizer":    "BOOLEAN NOT NULL DEFAULT FALSE",
    "mounting_method":       "TEXT",
    "outgassing_risk":       "TEXT",
    "notes":                 "TEXT",
    "booking_date":          "DATE NOT NULL",
    "start_time":            "TIME NOT NULL",
    "end_time":              "TIME NOT NULL",
    "created_at":            "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "updated_at":            "TIMESTAMPTZ",
    "updated_by":            "TEXT",
    "status":                "TEXT NOT NULL DEFAULT 'pending'",
    "approval_token":        "TEXT",
    "rejection_reason":      "TEXT",
    "approval_note":         "TEXT",
    "cancel_token":          "TEXT",
    "cancelled_at":          "TIMESTAMPTZ",
    "reminder_sent":         "BOOLEAN NOT NULL DEFAULT FALSE",
}

SQLITE_COLUMNS = {
    "lab_slug":              "TEXT NOT NULL",
    "booking_group_id":      "TEXT",
    "user_name":             "TEXT NOT NULL",
    "user_email":            "TEXT NOT NULL",
    "nanomaterial_type":     "TEXT",
    "melting_point":         "TEXT",
    "material_density":      "TEXT",
    "anneal_temp_c":         "TEXT",
    "anneal_time_h":         "TEXT",
    "gas_type":              "TEXT",
    "pressure":              "TEXT",
    "vacuum":                "INTEGER NOT NULL DEFAULT 0",
    "sample_name":           "TEXT",
    "sample_count":          "INTEGER",
    "elements_of_interest":  "TEXT",
    "analysis_type":         "TEXT",
    "charge_neutralizer":    "INTEGER NOT NULL DEFAULT 0",
    "mounting_method":       "TEXT",
    "outgassing_risk":       "TEXT",
    "notes":                 "TEXT",
    "booking_date":          "TEXT NOT NULL",
    "start_time":            "TEXT NOT NULL",
    "end_time":              "TEXT NOT NULL",
    "created_at":            "TEXT NOT NULL",
    "updated_at":            "TEXT",
    "updated_by":            "TEXT",
    "status":                "TEXT NOT NULL DEFAULT 'pending'",
    "approval_token":        "TEXT",
    "rejection_reason":      "TEXT",
    "approval_note":         "TEXT",
    "cancel_token":          "TEXT",
    "cancelled_at":          "TEXT",
    "reminder_sent":         "INTEGER NOT NULL DEFAULT 0",
}

EXPORT_COLUMNS = [
    "id","lab_slug","booking_group_id","user_name","user_email",
    "booking_date","start_time","end_time","status",
    "nanomaterial_type","melting_point","material_density",
    "anneal_temp_c","anneal_time_h","gas_type","pressure","vacuum",
    "sample_name","sample_count","elements_of_interest","analysis_type",
    "charge_neutralizer","mounting_method","outgassing_risk",
    "notes","rejection_reason","approval_note","created_at","updated_at","updated_by",
]

# ------------------------------------------------------------- DB helpers ----

def _init_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = ThreadedConnectionPool(minconn=1, maxconn=5, dsn=DATABASE_URL)

def _pg_conn():
    assert _pg_pool is not None
    return _pg_pool.getconn()

def _pg_putconn(conn):
    assert _pg_pool is not None
    _pg_pool.putconn(conn)

def _migrate_postgres(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS bookings (id SERIAL PRIMARY KEY);")
    for col, ddl in PG_COLUMNS.items():
        cur.execute(f'ALTER TABLE bookings ADD COLUMN IF NOT EXISTS "{col}" {ddl};')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bookings_lab_date ON bookings(lab_slug, booking_date);")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chemicals (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            formula TEXT DEFAULT '',
            mw TEXT DEFAULT '',
            cas_no TEXT DEFAULT '',
            supplier TEXT DEFAULT '',
            amount TEXT DEFAULT '',
            expiry_date TEXT DEFAULT '',
            storage_group TEXT DEFAULT '',
            location TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            reserved_for TEXT DEFAULT '',
            reserved_label TEXT DEFAULT ''
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chemical_requests (
            id SERIAL PRIMARY KEY,
            chem_id INTEGER NOT NULL REFERENCES chemicals(id),
            first_name TEXT NOT NULL,
            surname TEXT NOT NULL,
            requester_email TEXT NOT NULL,
            quantity TEXT NOT NULL,
            purpose TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_requests (
            id SERIAL PRIMARY KEY,
            material_name TEXT NOT NULL,
            formula TEXT DEFAULT '',
            cas_number TEXT DEFAULT '',
            specifications TEXT DEFAULT '',
            amount TEXT NOT NULL,
            unit TEXT NOT NULL,
            requester_first_name TEXT NOT NULL,
            requester_surname TEXT NOT NULL,
            requester_email TEXT NOT NULL,
            comments TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

def _sqlite_existing_columns(conn: sqlite3.Connection) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(bookings);").fetchall()}

def _migrate_sqlite(conn: sqlite3.Connection):
    conn.execute("CREATE TABLE IF NOT EXISTS bookings (id INTEGER PRIMARY KEY AUTOINCREMENT);")
    existing = _sqlite_existing_columns(conn)
    for col, ddl in SQLITE_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {col} {ddl};")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_lab_date ON bookings(lab_slug, booking_date);")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS chemicals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            formula TEXT DEFAULT '',
            mw TEXT DEFAULT '',
            cas_no TEXT DEFAULT '',
            supplier TEXT DEFAULT '',
            amount TEXT DEFAULT '',
            expiry_date TEXT DEFAULT '',
            storage_group TEXT DEFAULT '',
            location TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            reserved_for TEXT DEFAULT '',
            reserved_label TEXT DEFAULT ''
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chemical_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chem_id INTEGER NOT NULL,
            first_name TEXT NOT NULL,
            surname TEXT NOT NULL,
            requester_email TEXT NOT NULL,
            quantity TEXT NOT NULL,
            purpose TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS purchase_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_name TEXT NOT NULL,
            formula TEXT DEFAULT '',
            cas_number TEXT DEFAULT '',
            specifications TEXT DEFAULT '',
            amount TEXT NOT NULL,
            unit TEXT NOT NULL,
            requester_first_name TEXT NOT NULL,
            requester_surname TEXT NOT NULL,
            requester_email TEXT NOT NULL,
            comments TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
    """)
    # Migrate existing purchase_requests table
    pr_cols = {r[1] for r in conn.execute("PRAGMA table_info(purchase_requests);").fetchall()}
    if 'admin_note' not in pr_cols:
        conn.execute("ALTER TABLE purchase_requests ADD COLUMN admin_note TEXT DEFAULT '';")

def init_db():
    global _db_initialized
    if _db_initialized:
        return
    if USE_POSTGRES:
        _init_pg_pool()
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                _migrate_postgres(cur)
            conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        _migrate_sqlite(conn)
        conn.commit()
        conn.close()
    _db_initialized = True
    try:
        _seed_chemicals()
    except Exception as _seed_err:
        import sys
        print(f"[WARNING] Chemical seed failed: {_seed_err}", file=sys.stderr)

# ================================================ Chemical DB helpers =======

def _chem_conn():
    c = sqlite3.connect(SQLITE_PATH)
    c.row_factory = sqlite3.Row
    return c

def _get_chemicals_all() -> List[Dict]:
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM chemicals ORDER BY name ASC")
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = _chem_conn()
        rows = conn.execute("SELECT * FROM chemicals ORDER BY name ASC").fetchall()
        conn.close()
        return [dict(r) for r in rows]

def _search_chemicals(q: str) -> List[Dict]:
    q_like = f"%{q}%"
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM chemicals WHERE LOWER(name) LIKE LOWER(%s) OR LOWER(formula) LIKE LOWER(%s) ORDER BY name ASC",
                    (q_like, q_like))
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = _chem_conn()
        rows = conn.execute(
            "SELECT * FROM chemicals WHERE LOWER(name) LIKE LOWER(?) OR LOWER(formula) LIKE LOWER(?) ORDER BY name ASC",
            (q_like, q_like)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def _get_chemical_by_id(chem_id: int) -> Optional[Dict]:
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM chemicals WHERE id=%s", (chem_id,))
                row = cur.fetchone()
                if not row: return None
                return dict(zip([d.name for d in cur.description], row))
        finally:
            _pg_putconn(conn)
    else:
        conn = _chem_conn()
        row = conn.execute("SELECT * FROM chemicals WHERE id=?", (chem_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

def _upsert_chemical(data: Dict) -> int:
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM chemicals WHERE LOWER(name)=LOWER(%s)", (data["name"],))
                row = cur.fetchone()
                if row:
                    cid = row[0]
                    cur.execute("""UPDATE chemicals SET formula=%s,mw=%s,cas_no=%s,supplier=%s,
                        amount=%s,expiry_date=%s,storage_group=%s,location=%s,notes=%s,
                        reserved_for=%s,reserved_label=%s WHERE id=%s""",
                        (data.get("formula",""), data.get("mw",""), data.get("cas_no",""),
                         data.get("supplier",""), data.get("amount",""), data.get("expiry_date",""),
                         data.get("storage_group",""), data.get("location",""), data.get("notes",""),
                         data.get("reserved_for",""), data.get("reserved_label",""), cid))
                else:
                    cur.execute("""INSERT INTO chemicals (name,formula,mw,cas_no,supplier,amount,
                        expiry_date,storage_group,location,notes,reserved_for,reserved_label)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (data["name"], data.get("formula",""), data.get("mw",""), data.get("cas_no",""),
                         data.get("supplier",""), data.get("amount",""), data.get("expiry_date",""),
                         data.get("storage_group",""), data.get("location",""), data.get("notes",""),
                         data.get("reserved_for",""), data.get("reserved_label","")))
                    cid = cur.fetchone()[0]
                conn.commit()
                return cid
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.cursor()
        row = cur.execute("SELECT id FROM chemicals WHERE LOWER(name)=LOWER(?)", (data["name"],)).fetchone()
        if row:
            cid = row[0]
            cur.execute("""UPDATE chemicals SET formula=?,mw=?,cas_no=?,supplier=?,amount=?,
                expiry_date=?,storage_group=?,location=?,notes=?,reserved_for=?,reserved_label=?
                WHERE id=?""",
                (data.get("formula",""), data.get("mw",""), data.get("cas_no",""),
                 data.get("supplier",""), data.get("amount",""), data.get("expiry_date",""),
                 data.get("storage_group",""), data.get("location",""), data.get("notes",""),
                 data.get("reserved_for",""), data.get("reserved_label",""), cid))
        else:
            cur.execute("""INSERT INTO chemicals (name,formula,mw,cas_no,supplier,amount,
                expiry_date,storage_group,location,notes,reserved_for,reserved_label)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["name"], data.get("formula",""), data.get("mw",""), data.get("cas_no",""),
                 data.get("supplier",""), data.get("amount",""), data.get("expiry_date",""),
                 data.get("storage_group",""), data.get("location",""), data.get("notes",""),
                 data.get("reserved_for",""), data.get("reserved_label","")))
            cid = cur.lastrowid
        conn.commit()
        conn.close()
        return cid

def _delete_chemical(chem_id: int):
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chemicals WHERE id=%s", (chem_id,))
            conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("DELETE FROM chemicals WHERE id=?", (chem_id,))
        conn.commit()
        conn.close()

def _add_chemical_request(chem_id: int, first_name: str, surname: str,
                           email: str, quantity: str, purpose: str) -> int:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO chemical_requests
                    (chem_id,first_name,surname,requester_email,quantity,purpose,status,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,'pending',NOW()) RETURNING id""",
                    (chem_id, first_name.strip(), surname.strip(), email.strip(),
                     quantity.strip(), purpose.strip()))
                rid = cur.fetchone()[0]
            conn.commit()
            return rid
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.cursor()
        cur.execute("""INSERT INTO chemical_requests
            (chem_id,first_name,surname,requester_email,quantity,purpose,status,created_at)
            VALUES(?,?,?,?,?,?,'pending',?)""",
            (chem_id, first_name.strip(), surname.strip(), email.strip(),
             quantity.strip(), purpose.strip(), now))
        conn.commit()
        rid = cur.lastrowid
        conn.close()
        return rid

def _list_chemical_requests(status: Optional[str] = None) -> List[Dict]:
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                if status:
                    cur.execute("""SELECT cr.*,c.name as chem_name,c.formula,c.cas_no
                        FROM chemical_requests cr JOIN chemicals c ON cr.chem_id=c.id
                        WHERE cr.status=%s ORDER BY cr.id DESC""", (status,))
                else:
                    cur.execute("""SELECT cr.*,c.name as chem_name,c.formula,c.cas_no
                        FROM chemical_requests cr JOIN chemicals c ON cr.chem_id=c.id
                        ORDER BY cr.id DESC""")
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        if status:
            rows = conn.execute("""SELECT cr.*,c.name as chem_name,c.formula,c.cas_no
                FROM chemical_requests cr JOIN chemicals c ON cr.chem_id=c.id
                WHERE cr.status=? ORDER BY cr.id DESC""", (status,)).fetchall()
        else:
            rows = conn.execute("""SELECT cr.*,c.name as chem_name,c.formula,c.cas_no
                FROM chemical_requests cr JOIN chemicals c ON cr.chem_id=c.id
                ORDER BY cr.id DESC""").fetchall()
        conn.close()
        return [dict(r) for r in rows]

def _set_chemical_request_status(req_id: int, status: str):
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE chemical_requests SET status=%s WHERE id=%s", (status, req_id))
            conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("UPDATE chemical_requests SET status=? WHERE id=?", (status, req_id))
        conn.commit()
        conn.close()

def _add_purchase_request(data: Dict) -> int:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO purchase_requests
                    (material_name,formula,cas_number,specifications,amount,unit,
                     requester_first_name,requester_surname,requester_email,comments,status,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',NOW()) RETURNING id""",
                    (data["material_name"], data.get("formula",""), data.get("cas_number",""),
                     data.get("specifications",""), data["amount"], data["unit"],
                     data["requester_first_name"], data["requester_surname"],
                     data["requester_email"], data.get("comments","")))
                rid = cur.fetchone()[0]
            conn.commit()
            return rid
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.cursor()
        cur.execute("""INSERT INTO purchase_requests
            (material_name,formula,cas_number,specifications,amount,unit,
             requester_first_name,requester_surname,requester_email,comments,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?)""",
            (data["material_name"], data.get("formula",""), data.get("cas_number",""),
             data.get("specifications",""), data["amount"], data["unit"],
             data["requester_first_name"], data["requester_surname"],
             data["requester_email"], data.get("comments",""), now))
        conn.commit()
        rid = cur.lastrowid
        conn.close()
        return rid

def _list_purchase_requests(status: Optional[str] = None) -> List[Dict]:
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                if status:
                    cur.execute("SELECT * FROM purchase_requests WHERE status=%s ORDER BY id DESC", (status,))
                else:
                    cur.execute("SELECT * FROM purchase_requests ORDER BY id DESC")
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        if status:
            rows = conn.execute("SELECT * FROM purchase_requests WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM purchase_requests ORDER BY id DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]

def _set_purchase_request_status(req_id: int, status: str, admin_note: str = "") -> None:
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE purchase_requests SET status=%s, admin_note=%s WHERE id=%s",
                            (status, admin_note, req_id))
            conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("UPDATE purchase_requests SET status=?, admin_note=? WHERE id=?",
                     (admin_note, status, req_id))
        conn.commit()
        conn.close()

def _update_purchase_admin_note(req_id: int, admin_note: str) -> None:
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE purchase_requests SET admin_note=%s WHERE id=%s",
                            (admin_note, req_id))
            conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("UPDATE purchase_requests SET admin_note=? WHERE id=?",
                     (admin_note, req_id))
        conn.commit()
        conn.close()

# -------------------------------------------------- Seed chemicals from CSV --

CHEMICALS_SEED = []  # Chemicals are managed via CSV upload in the admin panel


def _import_chemicals_from_csv(file_stream) -> Tuple[int, List[str]]:
    """Parse a CSV file and upsert chemicals. Returns (count, errors)."""
    imported = 0
    errors = []
    try:
        text = file_stream.read().decode("utf-8-sig")  # handle BOM
        reader = csv.DictReader(text.splitlines())
        # Normalise header names: lowercase, strip spaces
        fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
        # Map common column name variants
        col_map = {
            "chemical name": "name", "chemical": "name",
            "molecular formula": "formula", "molecular weight": "mw",
            "mol. weight": "mw", "mol weight": "mw",
            "cas number": "cas_no", "cas no": "cas_no", "cas no.": "cas_no", "cas": "cas_no",
            "supplier": "supplier", "amount": "amount", "quantity": "amount",
            "expiry": "expiry_date", "expiry date": "expiry_date",
            "storage": "storage_group", "storage group": "storage_group",
            "location": "location", "notes": "notes",
            "reserved for": "reserved_for", "reserved label": "reserved_label",
            "mw": "mw", "formula": "formula", "name": "name",
        }
        for i, row in enumerate(csv.DictReader(text.splitlines()), start=2):
            # normalise keys
            norm = {col_map.get(k.strip().lower(), k.strip().lower()): (v or "").strip()
                    for k, v in row.items()}
            name = norm.get("name", "").strip()
            if not name:
                errors.append(f"Row {i}: skipped (no name)")
                continue
            data = {
                "name":          name,
                "formula":       norm.get("formula", ""),
                "mw":            norm.get("mw", ""),
                "cas_no":        norm.get("cas_no", ""),
                "supplier":      norm.get("supplier", ""),
                "amount":        norm.get("amount", ""),
                "expiry_date":   norm.get("expiry_date", ""),
                "storage_group": norm.get("storage_group", ""),
                "location":      norm.get("location", ""),
                "notes":         norm.get("notes", ""),
                "reserved_for":  norm.get("reserved_for", ""),
                "reserved_label":norm.get("reserved_label", ""),
            }
            try:
                _upsert_chemical(data)
                imported += 1
            except Exception as e:
                errors.append(f"Row {i} ({name}): {e}")
    except Exception as e:
        errors.append(f"File parse error: {e}")
    return imported, errors

# ============================================================ EMAIL =========

def _send_email(to: str, subject: str, body: str) -> None:
    if not smtp_ready():
        raise RuntimeError("Email not configured. Set BREVO_API_KEY and SMTP_FROM_EMAIL.")
    if BREVO_API_KEY:
        payload = _json.dumps({
            "sender":      {"name": SMTP_FROM_NAME, "email": SMTP_FROM_EMAIL},
            "to":          [{"email": to}],
            "subject":     subject,
            "textContent": body,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={
                "api-key":      BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept":       "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"Brevo API error {resp.status}: {resp.read().decode()}")
        return
    payload = _json.dumps({
        "from": f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>",
        "to":   [to],
        "subject": subject,
        "text": body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status not in (200, 201):
            raise RuntimeError(f"Resend API error {resp.status}: {resp.read().decode()}")

def _send_async(to: str, subject: str, body: str) -> None:
    def _run():
        try:
            _send_email(to, subject, body)
        except Exception as e:
            import sys
            print(f"[EMAIL ERROR] to={to} subject={subject!r} error={e}", file=sys.stderr)
    threading.Thread(target=_run, daemon=False).start()

def _send_async_multi(recipients: List[str], subject: str, body: str) -> None:
    for r in recipients:
        _send_async(r, subject, body)

# ------------ Chemical-specific email notifications -------------------------

def notify_chem_request(chem_name: str, req: Dict) -> None:
    if not smtp_ready() or not CHEM_ADMIN_EMAIL:
        return
    body = (
        f"New chemical request submitted.\n\n"
        f"  Chemical : {chem_name}\n"
        f"  Requester: {req['first_name']} {req['surname']} ({req['requester_email']})\n"
        f"  Quantity : {req['quantity']}\n"
        f"  Purpose  : {req.get('purpose','—')}\n\n"
        f"Please review in the admin panel.\n\nRegards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(CHEM_ADMIN_EMAIL,
                f"[Chemical Request] {chem_name} — {req['first_name']} {req['surname']}", body)

def notify_user_chem_request_received(req: Dict, chem_name: str) -> None:
    if not smtp_ready(): return
    body = (
        f"Hello {req['first_name']},\n\n"
        f"Your request for {chem_name} has been received.\n\n"
        f"  Quantity  : {req['quantity']}\n"
        f"  Status    : Pending review\n\n"
        f"You will be notified once your request is processed.\n\n"
        f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(req["requester_email"], f"Chemical request received — {chem_name}", body)

def notify_chem_purchase_request(pr: Dict) -> None:
    if not smtp_ready() or not PURCHASE_NOTIFY_EMAILS:
        return
    body = (
        f"New chemical purchase request submitted.\n\n"
        f"  Material      : {pr['material_name']}\n"
        f"  Formula       : {pr.get('formula','—')}\n"
        f"  CAS No.       : {pr.get('cas_number','—')}\n"
        f"  Specifications: {pr.get('specifications','—')}\n"
        f"  Amount        : {pr['amount']} {pr['unit']}\n"
        f"  Requester     : {pr['requester_first_name']} {pr['requester_surname']} ({pr['requester_email']})\n"
        f"  Comments      : {pr.get('comments','—')}\n\n"
        f"Please review in the admin panel.\n\nRegards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async_multi(
        PURCHASE_NOTIFY_EMAILS,
        f"[Purchase Request] {pr['material_name']} — {pr['requester_first_name']} {pr['requester_surname']}",
        body)

def notify_user_purchase_received(pr: Dict) -> None:
    if not smtp_ready(): return
    body = (
        f"Hello {pr['requester_first_name']},\n\n"
        f"Your purchase request for {pr['material_name']} has been submitted.\n\n"
        f"  Amount : {pr['amount']} {pr['unit']}\n"
        f"  Status : Pending\n\n"
        f"We will contact you once the request is reviewed.\n\n"
        f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(pr["requester_email"], f"Purchase request received — {pr['material_name']}", body)

def notify_user_chem_status(req: Dict, chem_name: str) -> None:
    if not smtp_ready(): return
    body = (
        f"Hello {req['first_name']},\n\n"
        f"Your chemical request for {chem_name} has been updated.\n\n"
        f"  New status : {req['status'].upper()}\n\n"
        f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(req["requester_email"], f"Chemical request {req['status']} — {chem_name}", body)

# ====================================================== BOOKING HELPERS =====

def parse_date(value: str) -> Optional[date]:
    try: return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception: return None

def parse_time(value: str) -> Optional[time]:
    for fmt in ("%H:%M", "%H:%M:%S"):
        try: return datetime.strptime(value, fmt).time()
        except Exception: pass
    return None

def overlaps(a_s: time, a_e: time, b_s: time, b_e: time) -> bool:
    return (a_s < b_e) and (a_e > b_s)

def normalize_booking_time(v: Any) -> time:
    return v if isinstance(v, time) else (parse_time(str(v)) or time(0, 0))

def normalize_booking_date(v: Any) -> date:
    return v if isinstance(v, date) else (parse_date(str(v)) or date.min)

def iter_workdays(start_d: date, end_d: date):
    d = start_d
    while d <= end_d:
        if d.weekday() < 5: yield d
        d += timedelta(days=1)

def build_slots_for_day(d: date, lab_slug: str) -> List[Tuple[time, time]]:
    if lab_slug == "furnace":
        return [(time(8, 0), time(12, 0)), (time(12, 0), time(16, 0))]
    slots: List[Tuple[time, time]] = []
    cur = datetime.combine(d, WORKDAY_START)
    end = datetime.combine(d, WORKDAY_END)
    while cur < end:
        nxt = cur + timedelta(minutes=SLOT_MINUTES)
        if nxt > end: break
        slots.append((cur.time(), nxt.time()))
        cur = nxt
    return slots

def next_two_weeks_window() -> Tuple[date, date]:
    today = datetime.now(TZ).date()
    return today, today + timedelta(days=13)

def has_conflict(lab_slug: str, booking_date: str, start_hhmm: str, end_hhmm: str,
                 exclude_id: Optional[int] = None) -> bool:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                if exclude_id is None:
                    cur.execute(
                        "SELECT 1 FROM bookings WHERE lab_slug=%s AND booking_date=%s::date "
                        "AND start_time<%s::time AND end_time>%s::time "
                        "AND status!='rejected' AND cancelled_at IS NULL LIMIT 1",
                        (lab_slug, booking_date, end_hhmm, start_hhmm))
                else:
                    cur.execute(
                        "SELECT 1 FROM bookings WHERE lab_slug=%s AND booking_date=%s::date "
                        "AND start_time<%s::time AND end_time>%s::time "
                        "AND status!='rejected' AND cancelled_at IS NULL AND id<>%s LIMIT 1",
                        (lab_slug, booking_date, end_hhmm, start_hhmm, exclude_id))
                return cur.fetchone() is not None
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.cursor()
        if exclude_id is None:
            cur.execute(
                "SELECT 1 FROM bookings WHERE lab_slug=? AND booking_date=? "
                "AND start_time<? AND end_time>? "
                "AND status!='rejected' AND (cancelled_at IS NULL OR cancelled_at='') LIMIT 1",
                (lab_slug, booking_date, end_hhmm, start_hhmm))
        else:
            cur.execute(
                "SELECT 1 FROM bookings WHERE lab_slug=? AND booking_date=? "
                "AND start_time<? AND end_time>? "
                "AND status!='rejected' AND (cancelled_at IS NULL OR cancelled_at='') AND id<>? LIMIT 1",
                (lab_slug, booking_date, end_hhmm, start_hhmm, exclude_id))
        hit = cur.fetchone() is not None
        conn.close()
        return hit

def default_booking_form() -> Dict[str, str]:
    now = datetime.now(TZ)
    return {"booking_date": now.date().isoformat(),
            "start_time": now.strftime("%H:%M"),
            "end_time": (now + timedelta(hours=1)).strftime("%H:%M"),
            "vacuum": "no", "charge_neutralizer": "no"}

def merge_prefill(form: Dict[str, str], args: Any) -> Dict[str, str]:
    out = dict(form)
    for k in ("booking_date", "start_time", "end_time"):
        v = (args.get(k) or "").strip()
        if v: out[k] = v
    return out

def check_booking_rules(lab_slug: str, booking_date: str, start_hhmm: str, end_hhmm: str) -> List[str]:
    errors: List[str] = []
    lab = LABS.get(lab_slug, {})
    now = datetime.now(TZ)
    bd  = parse_date(booking_date)
    st  = parse_time(start_hhmm)
    et  = parse_time(end_hhmm)
    if not bd or not st or not et: return errors
    slot_start = datetime.combine(bd, st).replace(tzinfo=TZ)
    slot_end   = datetime.combine(bd, et).replace(tzinfo=TZ)
    min_notice = lab.get("min_notice_hours", 0)
    if min_notice and slot_start < now + timedelta(hours=min_notice):
        errors.append(f"This lab requires at least {min_notice} hour(s) advance notice.")
    max_ahead = lab.get("max_days_ahead", 90)
    if (bd - now.date()).days > max_ahead:
        errors.append(f"You can only book up to {max_ahead} days in advance.")
    max_dur = lab.get("max_duration_hours", 0)
    dur_h = (slot_end - slot_start).total_seconds() / 3600
    if max_dur and dur_h > max_dur:
        errors.append(f"Maximum booking duration for this lab is {max_dur} hour(s).")
    if bd.weekday() >= 5:
        errors.append("Bookings are only available on weekdays (Mon–Fri).")
    return errors

def db_list_bookings_range_minimal(lab_slug: str, start_d: date, end_d: date) -> List[Dict]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT booking_date,start_time,end_time FROM bookings "
                    "WHERE lab_slug=%s AND booking_date>=%s::date AND booking_date<=%s::date "
                    "AND status!='rejected' AND cancelled_at IS NULL",
                    (lab_slug, start_d.isoformat(), end_d.isoformat()))
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH); conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT booking_date,start_time,end_time FROM bookings "
            "WHERE lab_slug=? AND booking_date>=? AND booking_date<=? "
            "AND status!='rejected' AND (cancelled_at IS NULL OR cancelled_at='')",
            (lab_slug, start_d.isoformat(), end_d.isoformat())).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def is_slot_free(bookings: List[Dict], d: date, s: time, e: time) -> bool:
    for b in bookings:
        if normalize_booking_date(b["booking_date"]) != d: continue
        if overlaps(s, e, normalize_booking_time(b["start_time"]), normalize_booking_time(b["end_time"])):
            return False
    return True

def availability_days(lab_slug: str) -> List[Dict[str, Any]]:
    start_d, end_d = next_two_weeks_window()
    bookings = db_list_bookings_range_minimal(lab_slug, start_d, end_d)
    now = datetime.now(TZ)
    min_notice = LABS.get(lab_slug, {}).get("min_notice_hours", 0)
    days: List[Dict[str, Any]] = []
    for d in iter_workdays(start_d, end_d):
        slots = []
        for s, e in build_slots_for_day(d, lab_slug):
            free = is_slot_free(bookings, d, s, e)
            if free and min_notice:
                slot_dt = datetime.combine(d, s).replace(tzinfo=TZ)
                if slot_dt < now + timedelta(hours=min_notice): free = False
            slots.append({"date": d.isoformat(), "start": s.strftime("%H:%M"),
                          "end": e.strftime("%H:%M"), "free": free,
                          "value": f"{d.isoformat()}|{s.strftime('%H:%M')}|{e.strftime('%H:%M')}"})
        days.append({"date": d, "slots": slots})
    return days

# ------------------------------------------------------------ Booking email --

def _slot_str(b: Dict) -> str:
    return f"{b['booking_date']} {str(b['start_time'])[:5]}–{str(b['end_time'])[:5]}"

def _cancel_url_for(b: Dict) -> str:
    token = b.get("cancel_token", "")
    if not token: return ""
    return url_for("cancel_booking_get", token=token, _external=True)

def notify_user_submission(lab_slug: str, b: Dict) -> None:
    if not smtp_ready(): return
    lab_title = LABS[lab_slug]["title"]
    cancel_url = _cancel_url_for(b)
    body = (
        f"Hello {b['user_name']},\n\n"
        f"We have received your booking request for {lab_title}.\n\n"
        f" Lab  : {lab_title}\n Slot : {_slot_str(b)}\n Ref  : #{b['id']}\n\n"
        f"Your request is pending review.\n\n"
        + (f"To cancel: {cancel_url}\n\n" if cancel_url else "")
        + f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(b["user_email"], f"Booking request received — {lab_title}", body)

def notify_admin_new_booking(lab_slug: str, b: Dict, approve_url: str, reject_url: str) -> None:
    if not smtp_ready(): return
    lab_title = LABS[lab_slug]["title"]
    body = (
        f"New booking request for {lab_title}.\n\n"
        f" Name  : {b['user_name']}\n Email : {b['user_email']}\n Slot  : {_slot_str(b)}\n\n"
        f"APPROVE: {approve_url}\nREJECT:  {reject_url}\n\nRegards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(BOOKING_ADMIN_EMAIL, f"[Action required] New booking — {lab_title}", body)

def notify_user_approved(lab_slug: str, b: Dict, note: str = "") -> None:
    if not smtp_ready(): return
    lab_title = LABS[lab_slug]["title"]
    cancel_url = _cancel_url_for(b)
    note_line = f"\n Note: {note}\n" if note else ""
    body = (
        f"Hello {b['user_name']},\n\nYour booking for {lab_title} has been approved.\n\n"
        f" Lab  : {lab_title}\n Slot : {_slot_str(b)}\n Ref  : #{b['id']}\n{note_line}\n"
        + (f"To cancel: {cancel_url}\n\n" if cancel_url else "")
        + f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(b["user_email"], f"Booking confirmed — {lab_title}", body)

def notify_user_rejected(lab_slug: str, b: Dict, reason: str = "") -> None:
    if not smtp_ready(): return
    lab_title = LABS[lab_slug]["title"]
    body = (
        f"Hello {b['user_name']},\n\nYour booking for {lab_title} was declined.\n\n"
        f" Lab  : {lab_title}\n Slot : {_slot_str(b)}\n Ref  : #{b['id']}\n"
        + (f" Reason: {reason}\n" if reason else "")
        + f"\nRegards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(b["user_email"], f"Booking declined — {lab_title}", body)

def notify_user_cancelled(lab_slug: str, b: Dict) -> None:
    if not smtp_ready(): return
    lab_title = LABS[lab_slug]["title"]
    body = (
        f"Hello {b['user_name']},\n\nYour booking for {lab_title} has been cancelled.\n\n"
        f" Lab  : {lab_title}\n Slot : {_slot_str(b)}\n Ref  : #{b['id']}\n\n"
        f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(b["user_email"], f"Booking cancelled — {lab_title}", body)

def notify_user_reminder(lab_slug: str, b: Dict) -> None:
    if not smtp_ready(): return
    lab_title = LABS[lab_slug]["title"]
    body = (
        f"Hello {b['user_name']},\n\nReminder: you have a booking tomorrow.\n\n"
        f" Lab  : {lab_title}\n Slot : {_slot_str(b)}\n Ref  : #{b['id']}\n\n"
        f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(b["user_email"], f"Reminder: booking tomorrow — {lab_title}", body)

# ----------------------------------------------------------- Admin auth ------

def _require_admin_vars(lab_slug: str):
    if not ADMIN[lab_slug]["username"] or not ADMIN[lab_slug]["password"]:
        abort(404, description=f"Admin not configured for {lab_slug}.")

def is_admin_for(lab_slug: str) -> bool:
    return session.get("is_admin") is True and session.get("admin_lab") == lab_slug

def is_chem_admin() -> bool:
    return session.get("is_chem_admin") is True

def require_admin(lab_slug: str):
    _require_admin_vars(lab_slug)
    if not is_admin_for(lab_slug):
        return redirect(url_for("admin_login_lab", lab_slug=lab_slug, next=request.path))
    return None

def booking_url_for(lab_slug: str, **params) -> str:
    if lab_slug == "furnace": return url_for("furnace", **params)
    if lab_slug == "xps":     return url_for("xps", **params)
    return url_for("lab_generic", lab_slug=lab_slug, **params)

# ---------------------------------------------------------- Booking DB CRUD --

def _row_to_dict_pg(row: Any, cols: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in dict(zip(cols, row)).items():
        if isinstance(v, (date, time)): out[k] = v.isoformat()
        elif isinstance(v, bool):       out[k] = "Yes" if v else "No"
        elif v is None:                 out[k] = ""
        else:                           out[k] = str(v)
    return out

def _normalise_sqlite_row(d: Dict) -> Dict:
    for k, v in list(d.items()):
        if k in ("vacuum","charge_neutralizer","reminder_sent"):
            d[k] = "Yes" if int(v or 0) == 1 else "No"
        elif v is None: d[k] = ""
        else:           d[k] = str(v)
    return d

def db_list_bookings(lab_slug: str) -> List[Dict[str, Any]]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bookings WHERE lab_slug=%s ORDER BY booking_date DESC,start_time DESC", (lab_slug,))
                cols = [d.name for d in cur.description]
                return [_row_to_dict_pg(r, cols) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH); conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM bookings WHERE lab_slug=? ORDER BY booking_date DESC,start_time DESC",(lab_slug,)).fetchall()
        conn.close()
        return [_normalise_sqlite_row(dict(r)) for r in rows]

def db_get_booking(booking_id: int) -> Optional[Dict[str, Any]]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bookings WHERE id=%s",(booking_id,))
                row = cur.fetchone()
                if not row: return None
                return _row_to_dict_pg(row,[d.name for d in cur.description])
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH); conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bookings WHERE id=?",(booking_id,)).fetchone()
        conn.close()
        return _normalise_sqlite_row(dict(row)) if row else None

def db_get_booking_by_token(token: str) -> Optional[Dict[str, Any]]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bookings WHERE approval_token=%s",(token,))
                row = cur.fetchone()
                if not row: return None
                return _row_to_dict_pg(row,[d.name for d in cur.description])
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH); conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bookings WHERE approval_token=?",(token,)).fetchone()
        conn.close()
        return _normalise_sqlite_row(dict(row)) if row else None

def db_get_booking_by_cancel_token(token: str) -> Optional[Dict[str, Any]]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bookings WHERE cancel_token=%s",(token,))
                row = cur.fetchone()
                if not row: return None
                return _row_to_dict_pg(row,[d.name for d in cur.description])
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH); conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bookings WHERE cancel_token=?",(token,)).fetchone()
        conn.close()
        return _normalise_sqlite_row(dict(row)) if row else None

def db_insert_booking(payload: Dict[str, Any]) -> int:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bookings (lab_slug,booking_group_id,user_name,user_email,"
                    "nanomaterial_type,melting_point,material_density,anneal_temp_c,anneal_time_h,"
                    "gas_type,pressure,vacuum,sample_name,sample_count,elements_of_interest,"
                    "analysis_type,charge_neutralizer,mounting_method,outgassing_risk,notes,"
                    "booking_date,start_time,end_time,updated_at,updated_by,"
                    "status,approval_token,cancel_token) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "%s::date,%s::time,%s::time,%s,%s,%s,%s,%s) RETURNING id",
                    (payload["lab_slug"],payload.get("booking_group_id"),payload["user_name"],payload["user_email"],
                     payload.get("nanomaterial_type"),payload.get("melting_point"),payload.get("material_density"),
                     payload.get("anneal_temp_c"),payload.get("anneal_time_h"),payload.get("gas_type"),
                     payload.get("pressure"),bool(payload.get("vacuum")),
                     payload.get("sample_name"),payload.get("sample_count"),payload.get("elements_of_interest"),
                     payload.get("analysis_type"),bool(payload.get("charge_neutralizer")),
                     payload.get("mounting_method"),payload.get("outgassing_risk"),payload.get("notes"),
                     payload["booking_date"],payload["start_time"],payload["end_time"],
                     payload.get("updated_at"),payload.get("updated_by"),
                     payload.get("status","pending"),payload.get("approval_token"),payload.get("cancel_token")))
                bid = cur.fetchone()[0]
                conn.commit()
                return int(bid)
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH); cur = conn.cursor()
        cur.execute(
            "INSERT INTO bookings (lab_slug,booking_group_id,user_name,user_email,"
            "nanomaterial_type,melting_point,material_density,anneal_temp_c,anneal_time_h,"
            "gas_type,pressure,vacuum,sample_name,sample_count,elements_of_interest,"
            "analysis_type,charge_neutralizer,mounting_method,outgassing_risk,notes,"
            "booking_date,start_time,end_time,created_at,updated_at,updated_by,"
            "status,approval_token,cancel_token) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payload["lab_slug"],payload.get("booking_group_id"),payload["user_name"],payload["user_email"],
             payload.get("nanomaterial_type"),payload.get("melting_point"),payload.get("material_density"),
             payload.get("anneal_temp_c"),payload.get("anneal_time_h"),payload.get("gas_type"),
             payload.get("pressure"),1 if payload.get("vacuum") else 0,
             payload.get("sample_name"),payload.get("sample_count"),payload.get("elements_of_interest"),
             payload.get("analysis_type"),1 if payload.get("charge_neutralizer") else 0,
             payload.get("mounting_method"),payload.get("outgassing_risk"),payload.get("notes"),
             payload["booking_date"],payload["start_time"],payload["end_time"],
             datetime.utcnow().isoformat(timespec="seconds")+"Z",
             payload.get("updated_at"),payload.get("updated_by"),
             payload.get("status","pending"),payload.get("approval_token"),payload.get("cancel_token")))
        conn.commit(); bid = cur.lastrowid; conn.close()
        return int(bid)

def db_set_booking_status(booking_id: int, status: str, rejection_reason: str = "",
                          approval_note: str = "", updated_by: str = "") -> None:
    init_db()
    now_iso = datetime.utcnow().isoformat(timespec="seconds")+"Z"
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE bookings SET status=%s,rejection_reason=%s,approval_note=%s,"
                            "updated_at=NOW(),updated_by=%s,approval_token=NULL WHERE id=%s",
                            (status,rejection_reason or None,approval_note or None,updated_by or None,booking_id))
                conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("UPDATE bookings SET status=?,rejection_reason=?,approval_note=?,"
                     "updated_at=?,updated_by=?,approval_token=NULL WHERE id=?",
                     (status,rejection_reason or None,approval_note or None,now_iso,updated_by or None,booking_id))
        conn.commit(); conn.close()

def db_cancel_booking(booking_id: int) -> None:
    init_db()
    now_iso = datetime.utcnow().isoformat(timespec="seconds")+"Z"
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE bookings SET status='rejected',cancelled_at=NOW(),cancel_token=NULL WHERE id=%s",(booking_id,))
                conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("UPDATE bookings SET status='rejected',cancelled_at=?,cancel_token=NULL WHERE id=?",(now_iso,booking_id))
        conn.commit(); conn.close()

def db_delete_booking(booking_id: int) -> None:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bookings WHERE id=%s",(booking_id,))
                conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("DELETE FROM bookings WHERE id=?",(booking_id,))
        conn.commit(); conn.close()

def db_pending_counts() -> Dict[str, int]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT lab_slug,COUNT(*) FROM bookings WHERE status='pending' "
                            "AND cancelled_at IS NULL GROUP BY lab_slug")
                return {r[0]:r[1] for r in cur.fetchall()}
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        rows = conn.execute("SELECT lab_slug,COUNT(*) FROM bookings WHERE status='pending' "
                            "AND (cancelled_at IS NULL OR cancelled_at='') GROUP BY lab_slug").fetchall()
        conn.close()
        return {r[0]:r[1] for r in rows}

def collect_selected_slots() -> List[Tuple[str, str, str]]:
    slots: List[Tuple[str, str, str]] = []
    for v in request.form.getlist("slot"):
        parts = (v or "").split("|")
        if len(parts) != 3: continue
        d, s, e = parts
        if parse_date(d) and parse_time(s) and parse_time(e):
            slots.append((d, s, e))
    return sorted(list({x for x in slots}))

def is_valid_furnace_block(s: str, e: str) -> bool:
    return (s, e) in (("08:00","12:00"),("12:00","16:00"))

def db_mark_reminder_sent(booking_id: int) -> None:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE bookings SET reminder_sent=TRUE WHERE id=%s",(booking_id,))
                conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("UPDATE bookings SET reminder_sent=1 WHERE id=?",(booking_id,))
        conn.commit(); conn.close()

def db_get_reminder_candidates() -> List[Dict[str, Any]]:
    init_db()
    tomorrow = (datetime.now(TZ).date()+timedelta(days=1)).isoformat()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bookings WHERE status='approved' AND reminder_sent=FALSE "
                            "AND booking_date=%s::date AND cancelled_at IS NULL",(tomorrow,))
                cols=[d.name for d in cur.description]
                return [_row_to_dict_pg(r,cols) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH); conn.row_factory=sqlite3.Row
        rows=conn.execute("SELECT * FROM bookings WHERE status='approved' AND reminder_sent=0 "
                          "AND booking_date=? AND (cancelled_at IS NULL OR cancelled_at='')",(tomorrow,)).fetchall()
        conn.close()
        return [_normalise_sqlite_row(dict(r)) for r in rows]

# ================================================================= ROUTES ===

@app.get("/debug/init")
def debug_init():
    import traceback
    results = {}
    try:
        init_db()
        results["init_db"] = "OK"
    except Exception as e:
        results["init_db"] = f"ERROR: {traceback.format_exc()}"
    try:
        results["chemicals_count"] = len(_get_chemicals_all())
    except Exception as e:
        results["chemicals_count"] = f"ERROR: {e}"
    try:
        results["chem_requests_count"] = len(_list_chemical_requests())
    except Exception as e:
        results["chem_requests_count"] = f"ERROR: {e}"
    try:
        results["purchase_requests_count"] = len(_list_purchase_requests())
    except Exception as e:
        results["purchase_requests_count"] = f"ERROR: {e}"
    return jsonify(results)

@app.get("/debug/email")
def debug_email():
    import traceback
    result = {
        "smtp_ready":          smtp_ready(),
        "BREVO_API_KEY":       ("set (" + BREVO_API_KEY[:8] + "...)") if BREVO_API_KEY else "(not set)",
        "RESEND_API_KEY":      ("set (" + RESEND_API_KEY[:6] + "...)") if RESEND_API_KEY else "(not set)",
        "SMTP_FROM_EMAIL":     SMTP_FROM_EMAIL or "(not set)",
        "SMTP_FROM_NAME":      SMTP_FROM_NAME,
        "BOOKING_ADMIN_EMAIL": BOOKING_ADMIN_EMAIL or "(not set)",
        "CHEM_ADMIN_EMAIL":    CHEM_ADMIN_EMAIL or "(not set)",
        "PURCHASE_NOTIFY_EMAILS": PURCHASE_NOTIFY_EMAILS,
    }
    if smtp_ready():
        try:
            _send_email(
                BOOKING_ADMIN_EMAIL or SMTP_FROM_EMAIL,
                "U2ACN2 Portal — email test",
                f"This is a test email from the U2ACN2 Nanolab Portal.\n\n"
                f"Sender: {SMTP_FROM_EMAIL}\nRecipient: {BOOKING_ADMIN_EMAIL}\n"
            )
            result["send_result"] = "SUCCESS — check your inbox"
        except Exception:
            result["send_result"] = f"FAILED: {traceback.format_exc()}"
    else:
        result["send_result"] = "SKIPPED — set RESEND_API_KEY and SMTP_FROM_EMAIL"
    return jsonify(result)

@app.get("/health")
def health():
    return {"status": "ok"}, 200

@app.route("/")
def index():
    init_db()
    pending = db_pending_counts()
    try:
        chem_pending     = len(_list_chemical_requests(status="pending"))
        purchase_pending = len(_list_purchase_requests(status="pending"))
    except Exception:
        chem_pending = 0
        purchase_pending = 0
    labs = sorted(
        [{"title": LABS[k]["title"], "slug": k, "subtitle": LABS[k]["subtitle"],
          "booking_url": booking_url_for(k),
          "availability_url": url_for("lab_availability", lab_slug=k),
          "admin_url": url_for("admin_lab", lab_slug=k),
          "pending_count": pending.get(k, 0)} for k in LABS],
        key=lambda x: x["title"].lower())
    return render_template("index.html", labs=labs,
                           admin_portal_url=url_for("admin_portal"),
                           chem_url=url_for("chemicals"),
                           chem_pending=chem_pending,
                           purchase_pending=purchase_pending)

# ---------------------------------------- Chemical Inventory Routes ---------

@app.route("/chemicals")
def chemicals():
    init_db()
    q = (request.args.get("q") or "").strip()
    results = _search_chemicals(q) if q else _get_chemicals_all()
    return render_template("chemicals.html", chemicals=results, query=q)

@app.route("/chemicals/request", methods=["POST"])
def chemical_request():
    init_db()
    chem_id    = int(request.form.get("chem_id", 0))
    first_name = (request.form.get("first_name") or "").strip()
    surname    = (request.form.get("surname") or "").strip()
    email      = (request.form.get("email") or "").strip()
    quantity   = (request.form.get("quantity") or "").strip()
    purpose    = (request.form.get("purpose") or "").strip()
    errors = []
    if not first_name: errors.append("First name is required.")
    if not surname:    errors.append("Surname is required.")
    if not email or "@" not in email: errors.append("Valid email is required.")
    if not quantity:   errors.append("Quantity is required.")
    chem = _get_chemical_by_id(chem_id)
    if not chem: errors.append("Chemical not found.")
    if errors:
        flash(" ".join(errors), "error")
        return redirect(url_for("chemicals"))
    _add_chemical_request(chem_id, first_name, surname, email, quantity, purpose)
    req = {"first_name": first_name, "surname": surname, "requester_email": email,
           "quantity": quantity, "purpose": purpose}
    notify_chem_request(chem["name"], req)
    notify_user_chem_request_received(req, chem["name"])
    flash(f"Request for {chem['name']} submitted! The lab admin will review it.", "success")
    return redirect(url_for("chemicals"))

@app.route("/chemicals/orders")
def chemical_orders():
    """Public view of purchase requests — shows status and admin notes."""
    init_db()
    orders = _list_purchase_requests()
    return render_template("chemical_orders.html", orders=orders)

@app.route("/chemicals/purchase", methods=["GET", "POST"])
def purchase_request():
    init_db()
    if request.method == "GET":
        prefill = {k: (request.args.get(k) or "") for k in ("material_name","formula","cas_number")}
        return render_template("purchase_request.html", prefill=prefill)
    data = {
        "material_name":        (request.form.get("material_name") or "").strip(),
        "formula":              (request.form.get("formula") or "").strip(),
        "cas_number":           (request.form.get("cas_number") or "").strip(),
        "specifications":       (request.form.get("specifications") or "").strip(),
        "amount":               (request.form.get("amount") or "").strip(),
        "unit":                 (request.form.get("unit") or "g").strip(),
        "requester_first_name": (request.form.get("requester_first_name") or "").strip(),
        "requester_surname":    (request.form.get("requester_surname") or "").strip(),
        "requester_email":      (request.form.get("requester_email") or "").strip(),
        "comments":             (request.form.get("comments") or "").strip(),
    }
    errors = []
    if not data["material_name"]:        errors.append("Material name is required.")
    if not data["amount"]:               errors.append("Amount is required.")
    if not data["requester_first_name"]: errors.append("First name is required.")
    if not data["requester_surname"]:    errors.append("Surname is required.")
    if not data["requester_email"] or "@" not in data["requester_email"]:
        errors.append("Valid email is required.")
    if errors:
        flash(" ".join(errors), "error")
        return render_template("purchase_request.html", prefill=data, errors=errors)
    _add_purchase_request(data)
    notify_chem_purchase_request(data)
    notify_user_purchase_received(data)
    flash("Purchase request submitted! You will be contacted once reviewed.", "success")
    return redirect(url_for("chemicals"))

# ---------------------------------------- Chemical Admin Routes --------------

@app.route("/admin/chemicals/login", methods=["GET", "POST"])
def chem_admin_login():
    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        if hmac.compare_digest(pw, CHEM_ADMIN_PASSWORD):
            session["is_chem_admin"] = True
            return redirect(request.form.get("next") or url_for("chem_admin"))
        flash("Invalid password.", "error")
    return render_template("chem_admin_login.html",
                           next=request.args.get("next") or url_for("chem_admin"))

@app.get("/admin/chemicals/logout")
def chem_admin_logout():
    session.pop("is_chem_admin", None)
    return redirect(url_for("chemicals"))

@app.route("/admin/chemicals")
def chem_admin():
    if not is_chem_admin():
        return redirect(url_for("chem_admin_login", next=request.path))
    init_db()
    return render_template("chem_admin.html",
                           chemicals=_get_chemicals_all(),
                           chem_requests=_list_chemical_requests(),
                           purchase_requests=_list_purchase_requests())

@app.route("/admin/chemicals/add", methods=["POST"])
def chem_admin_add():
    if not is_chem_admin(): abort(403)
    init_db()
    data = {k: (request.form.get(k) or "").strip() for k in
            ("name","formula","mw","cas_no","supplier","amount","expiry_date",
             "storage_group","location","notes","reserved_for","reserved_label")}
    if not data["name"]:
        flash("Chemical name is required.", "error")
        return redirect(url_for("chem_admin"))
    _upsert_chemical(data)
    flash(f"Chemical '{data['name']}' saved.", "success")
    return redirect(url_for("chem_admin"))

@app.route("/admin/chemicals/delete/<int:chem_id>", methods=["POST"])
def chem_admin_delete(chem_id: int):
    if not is_chem_admin(): abort(403)
    init_db()
    chem = _get_chemical_by_id(chem_id)
    if chem:
        _delete_chemical(chem_id)
        flash(f"Deleted '{chem['name']}'.", "success")
    return redirect(url_for("chem_admin"))

@app.route("/admin/chemicals/reserve/<int:chem_id>", methods=["POST"])
def chem_admin_reserve(chem_id: int):
    if not is_chem_admin(): abort(403)
    init_db()
    chem = _get_chemical_by_id(chem_id)
    if not chem: abort(404)
    data = dict(chem)
    data["reserved_for"]   = (request.form.get("reserved_for") or "").strip()
    data["reserved_label"] = (request.form.get("reserved_label") or "").strip()
    _upsert_chemical(data)
    flash(f"Reservation updated for '{chem['name']}'.", "success")
    return redirect(url_for("chem_admin"))

@app.route("/admin/chemicals/request/<int:req_id>/status", methods=["POST"])
def chem_admin_request_status(req_id: int):
    if not is_chem_admin(): abort(403)
    init_db()
    status = (request.form.get("status") or "").strip()
    if status not in ("approved","rejected","fulfilled"):
        flash("Invalid status.", "error")
        return redirect(url_for("chem_admin"))
    _set_chemical_request_status(req_id, status)
    reqs = _list_chemical_requests()
    req = next((r for r in reqs if r["id"] == req_id), None)
    if req:
        notify_user_chem_status(req, req.get("chem_name", ""))
    flash(f"Request #{req_id} marked as {status}.", "success")
    return redirect(url_for("chem_admin"))

@app.route("/admin/chemicals/purchase/<int:req_id>/status", methods=["POST"])
def chem_admin_purchase_status(req_id: int):
    if not is_chem_admin(): abort(403)
    init_db()
    status    = (request.form.get("status") or "").strip()
    admin_note = (request.form.get("admin_note") or "").strip()
    if status and status not in ("pending","approved","rejected","ordered","purchased"):
        flash("Invalid status.", "error")
        return redirect(url_for("chem_admin"))
    if status:
        _set_purchase_request_status(req_id, status, admin_note)
    elif admin_note:
        _update_purchase_admin_note(req_id, admin_note)
    flash(f"Purchase request #{req_id} updated.", "success")
    return redirect(url_for("chem_admin"))

@app.route("/admin/chemicals/csv-upload", methods=["POST"])
def chem_admin_csv_upload():
    if not is_chem_admin(): abort(403)
    init_db()
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("chem_admin"))
    if not f.filename.lower().endswith(".csv"):
        flash("Please upload a .csv file.", "error")
        return redirect(url_for("chem_admin"))
    mode = request.form.get("import_mode", "merge")
    if mode == "replace":
        # Delete all chemicals first
        if USE_POSTGRES:
            conn = _pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM chemicals")
                conn.commit()
            finally:
                _pg_putconn(conn)
        else:
            conn = sqlite3.connect(SQLITE_PATH)
            conn.execute("DELETE FROM chemicals")
            conn.commit()
            conn.close()
    imported, errors = _import_chemicals_from_csv(f.stream)
    if errors:
        flash(f"Imported {imported} chemicals. {len(errors)} issue(s): {'; '.join(errors[:3])}", "error")
    else:
        flash(f"Successfully imported {imported} chemicals.", "success")
    return redirect(url_for("chem_admin"))

@app.route("/admin/chemicals/export.csv")
def chem_admin_export():
    if not is_chem_admin(): abort(403)
    init_db()
    chems = _get_chemicals_all()
    si = StringIO()
    w = csv.writer(si)
    w.writerow(["ID","Name","Formula","MW","CAS No.","Supplier","Amount",
                "Expiry Date","Storage Group","Location","Notes","Reserved For","Reserved Label"])
    for c in chems:
        w.writerow([c.get(k,"") for k in
                    ("id","name","formula","mw","cas_no","supplier","amount",
                     "expiry_date","storage_group","location","notes","reserved_for","reserved_label")])
    resp = make_response(si.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = "attachment; filename=chemicals.csv"
    return resp

# ---------------------------------------- Booking Admin Routes --------------

@app.route("/admin/<lab_slug>/login", methods=["GET","POST"])
def admin_login_lab(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    _require_admin_vars(lab_slug)
    if request.method == "POST":
        user = (request.form.get("username") or "").strip()
        pw   = (request.form.get("password") or "").strip()
        if (hmac.compare_digest(user.lower(), ADMIN[lab_slug]["username"].lower())
                and hmac.compare_digest(pw, ADMIN[lab_slug]["password"])):
            session.clear()
            session["is_admin"]       = True
            session["admin_lab"]      = lab_slug
            session["admin_username"] = ADMIN[lab_slug]["username"]
            return redirect(request.form.get("next") or url_for("admin_lab", lab_slug=lab_slug))
        flash("Invalid credentials.", "error")
    return render_template("admin_login.html", lab_slug=lab_slug,
                           lab_title=LABS[lab_slug]["title"],
                           admin_username_hint=ADMIN[lab_slug]["username"],
                           next=request.args.get("next") or url_for("admin_lab", lab_slug=lab_slug))

@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))

@app.get("/admin")
def admin_portal():
    pending = db_pending_counts()
    order = sorted(LABS.keys(), key=lambda k: LABS[k]["title"].lower())
    labs = []
    for slug in order:
        labs.append({
            "slug":             slug,
            "title":            LABS[slug]["title"],
            "pending_count":    pending.get(slug, 0),
            "admin_url":        url_for("admin_lab", lab_slug=slug),
            "availability_url": url_for("lab_availability", lab_slug=slug),
            "booking_url":      booking_url_for(slug),
        })
    return render_template("admin_portal.html", labs=labs)

@app.route("/admin/<lab_slug>")
def admin_lab(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    redir = require_admin(lab_slug)
    if redir: return redir
    return render_template("admin.html", lab_slug=lab_slug,
                           lab_title=LABS[lab_slug]["title"],
                           bookings=db_list_bookings(lab_slug),
                           admin_username=session.get("admin_username",""))

@app.route("/admin/<lab_slug>/booking/<int:booking_id>", methods=["GET","POST"])
def admin_edit_booking(lab_slug: str, booking_id: int):
    if lab_slug not in LABS: abort(404)
    redir = require_admin(lab_slug)
    if redir: return redir
    b = db_get_booking(booking_id)
    if not b or b.get("lab_slug") != lab_slug: abort(404)
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "approve":
            note = (request.form.get("approval_note") or "").strip()
            db_set_booking_status(booking_id, "approved", approval_note=note,
                                  updated_by=session.get("admin_username","admin"))
            try: notify_user_approved(lab_slug, b, note)
            except Exception: pass
            flash("Booking approved.", "success")
        elif action == "reject":
            reason = (request.form.get("rejection_reason") or "").strip()
            db_set_booking_status(booking_id, "rejected", rejection_reason=reason,
                                  updated_by=session.get("admin_username","admin"))
            try: notify_user_rejected(lab_slug, b, reason)
            except Exception: pass
            flash("Booking rejected.", "success")
        elif action == "delete":
            if request.form.get("notify_user") == "1":
                try: notify_user_cancelled(lab_slug, b)
                except Exception: pass
            db_delete_booking(booking_id)
            flash("Booking deleted.", "success")
            return redirect(url_for("admin_lab", lab_slug=lab_slug))
        return redirect(url_for("admin_lab", lab_slug=lab_slug))
    return render_template("admin_booking.html", lab_slug=lab_slug,
                           lab_title=LABS[lab_slug]["title"], booking=b)

@app.route("/labs/<lab_slug>/availability")
def lab_availability(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    days = availability_days(lab_slug)
    return render_template("availability.html", lab_slug=lab_slug,
                           lab_title=LABS[lab_slug]["title"], days=days,
                           booking_url=booking_url_for(lab_slug))

@app.get("/labs/<lab_slug>/prefill")
def prefill_booking(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    return redirect(booking_url_for(lab_slug,
                                    booking_date=(request.args.get("booking_date") or "").strip(),
                                    start_time=(request.args.get("start_time") or "").strip(),
                                    end_time=(request.args.get("end_time") or "").strip()))

@app.route("/labs/<lab_slug>", methods=["GET","POST"])
def lab_generic(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    if lab_slug in ("furnace","xps"): return redirect(booking_url_for(lab_slug))
    lab  = LABS[lab_slug]
    form = merge_prefill(default_booking_form(), request.args)
    errors = []
    if request.method == "POST":
        slots      = collect_selected_slots()
        user_name  = (request.form.get("user_name") or "").strip()
        user_email = (request.form.get("user_email") or "").strip()
        notes      = (request.form.get("notes") or "").strip()
        if not user_name:  errors.append("Name is required.")
        if not user_email or "@" not in user_email: errors.append("Valid email required.")
        if not slots: errors.append("Select at least one slot.")
        for d, s, e in slots:
            errors.extend(check_booking_rules(lab_slug, d, s, e))
            if has_conflict(lab_slug, d, s, e): errors.append(f"Slot {d} {s}–{e} is already taken.")
        if not errors:
            group_id = str(uuid.uuid4()) if len(slots) > 1 else None
            first_id = None
            for d, s, e in slots:
                payload = {"lab_slug": lab_slug, "booking_group_id": group_id,
                           "user_name": user_name, "user_email": user_email,
                           "notes": notes, "booking_date": d, "start_time": s, "end_time": e,
                           "status": "pending", "approval_token": str(uuid.uuid4()),
                           "cancel_token": str(uuid.uuid4())}
                bid = db_insert_booking(payload)
                if first_id is None: first_id = bid
            b = db_get_booking(first_id)
            try:
                notify_user_submission(lab_slug, b)
                notify_admin_new_booking(lab_slug, b,
                    url_for("approve_booking", token=b["approval_token"], _external=True),
                    url_for("reject_booking",  token=b["approval_token"], _external=True))
            except Exception: pass
            flash(f"Booking submitted for {lab['title']}! You'll receive a confirmation email.", "success")
            return redirect(url_for("lab_generic", lab_slug=lab_slug))
    days = availability_days(lab_slug)
    return render_template("lab_generic.html", lab_slug=lab_slug,
                           lab_title=lab["title"], lab_subtitle=lab["subtitle"],
                           form=form, errors=errors, days=days,
                           availability_url=url_for("lab_availability", lab_slug=lab_slug))

@app.route("/furnace", methods=["GET","POST"])
def furnace():
    lab_slug = "furnace"
    lab  = LABS[lab_slug]
    form = merge_prefill(default_booking_form(), request.args)
    errors = []
    if request.method == "POST":
        slots      = collect_selected_slots()
        user_name  = (request.form.get("user_name") or "").strip()
        user_email = (request.form.get("user_email") or "").strip()
        for d, s, e in slots:
            if not is_valid_furnace_block(s, e):
                errors.append(f"Invalid furnace slot: {s}–{e}.")
        if not user_name:  errors.append("Name is required.")
        if not user_email or "@" not in user_email: errors.append("Valid email required.")
        if not slots: errors.append("Select at least one slot.")
        for d, s, e in slots:
            errors.extend(check_booking_rules(lab_slug, d, s, e))
            if has_conflict(lab_slug, d, s, e): errors.append(f"Slot {d} {s}–{e} is taken.")
        if not errors:
            group_id = str(uuid.uuid4()) if len(slots) > 1 else None
            first_id = None
            for d, s, e in slots:
                payload = {
                    "lab_slug": lab_slug, "booking_group_id": group_id,
                    "user_name": user_name, "user_email": user_email,
                    "nanomaterial_type":  (request.form.get("nanomaterial_type") or "").strip(),
                    "melting_point":      (request.form.get("melting_point") or "").strip(),
                    "material_density":   (request.form.get("material_density") or "").strip(),
                    "anneal_temp_c":      (request.form.get("anneal_temp_c") or "").strip(),
                    "anneal_time_h":      (request.form.get("anneal_time_h") or "").strip(),
                    "gas_type":           (request.form.get("gas_type") or "").strip(),
                    "pressure":           (request.form.get("pressure") or "").strip(),
                    "vacuum":             request.form.get("vacuum","no") == "yes",
                    "notes":              (request.form.get("notes") or "").strip(),
                    "booking_date": d, "start_time": s, "end_time": e,
                    "status": "pending", "approval_token": str(uuid.uuid4()),
                    "cancel_token": str(uuid.uuid4())}
                bid = db_insert_booking(payload)
                if first_id is None: first_id = bid
            b = db_get_booking(first_id)
            try:
                notify_user_submission(lab_slug, b)
                notify_admin_new_booking(lab_slug, b,
                    url_for("approve_booking", token=b["approval_token"], _external=True),
                    url_for("reject_booking",  token=b["approval_token"], _external=True))
            except Exception: pass
            flash("Furnace booking submitted!", "success")
            return redirect(url_for("furnace"))
    days = availability_days(lab_slug)
    return render_template("furnace.html", lab_slug=lab_slug,
                           lab_title=lab["title"], lab_subtitle=lab["subtitle"],
                           form=form, errors=errors, days=days,
                           availability_url=url_for("lab_availability", lab_slug=lab_slug))

@app.route("/xps", methods=["GET","POST"])
def xps():
    lab_slug = "xps"
    lab  = LABS[lab_slug]
    form = merge_prefill(default_booking_form(), request.args)
    errors = []
    if request.method == "POST":
        slots      = collect_selected_slots()
        user_name  = (request.form.get("user_name") or "").strip()
        user_email = (request.form.get("user_email") or "").strip()
        if not user_name:  errors.append("Name is required.")
        if not user_email or "@" not in user_email: errors.append("Valid email required.")
        if not slots: errors.append("Select at least one slot.")
        for d, s, e in slots:
            errors.extend(check_booking_rules(lab_slug, d, s, e))
            if has_conflict(lab_slug, d, s, e): errors.append(f"Slot {d} {s}–{e} is taken.")
        if not errors:
            group_id = str(uuid.uuid4()) if len(slots) > 1 else None
            first_id = None
            for d, s, e in slots:
                payload = {
                    "lab_slug": lab_slug, "booking_group_id": group_id,
                    "user_name": user_name, "user_email": user_email,
                    "sample_name":           (request.form.get("sample_name") or "").strip(),
                    "sample_count":          request.form.get("sample_count"),
                    "elements_of_interest":  (request.form.get("elements_of_interest") or "").strip(),
                    "analysis_type":         (request.form.get("analysis_type") or "").strip(),
                    "charge_neutralizer":    request.form.get("charge_neutralizer","no") == "yes",
                    "mounting_method":       (request.form.get("mounting_method") or "").strip(),
                    "outgassing_risk":       (request.form.get("outgassing_risk") or "").strip(),
                    "notes":                 (request.form.get("notes") or "").strip(),
                    "booking_date": d, "start_time": s, "end_time": e,
                    "status": "pending", "approval_token": str(uuid.uuid4()),
                    "cancel_token": str(uuid.uuid4())}
                bid = db_insert_booking(payload)
                if first_id is None: first_id = bid
            b = db_get_booking(first_id)
            try:
                notify_user_submission(lab_slug, b)
                notify_admin_new_booking(lab_slug, b,
                    url_for("approve_booking", token=b["approval_token"], _external=True),
                    url_for("reject_booking",  token=b["approval_token"], _external=True))
            except Exception: pass
            flash("XPS booking submitted!", "success")
            return redirect(url_for("xps"))
    days = availability_days(lab_slug)
    return render_template("xps.html", lab_slug=lab_slug,
                           lab_title=lab["title"], lab_subtitle=lab["subtitle"],
                           form=form, errors=errors, days=days,
                           availability_url=url_for("lab_availability", lab_slug=lab_slug))

@app.get("/approve/<token>")
def approve_booking(token: str):
    b = db_get_booking_by_token(token)
    if not b: return "Link invalid or already used.", 404
    db_set_booking_status(b["id"], "approved")
    try: notify_user_approved(b["lab_slug"], b)
    except Exception: pass
    return f"<p>Booking #{b['id']} approved. <a href='/'>Home</a></p>"

@app.get("/reject/<token>")
def reject_booking(token: str):
    b = db_get_booking_by_token(token)
    if not b: return "Link invalid or already used.", 404
    db_set_booking_status(b["id"], "rejected")
    try: notify_user_rejected(b["lab_slug"], b)
    except Exception: pass
    return f"<p>Booking #{b['id']} rejected. <a href='/'>Home</a></p>"

@app.route("/cancel/<token>", methods=["GET","POST"])
def cancel_booking_get(token: str):
    b = db_get_booking_by_cancel_token(token)
    if not b: return "Cancellation link invalid or already used.", 404
    if request.method == "POST":
        db_cancel_booking(b["id"])
        try: notify_user_cancelled(b["lab_slug"], b)
        except Exception: pass
        return f"<p>Booking #{b['id']} cancelled. <a href='/'>Home</a></p>"
    return (f"<p>Cancel booking #{b['id']} for {b['lab_slug']} on {b['booking_date']}?</p>"
            f"<form method='post'><button type='submit'>Confirm Cancellation</button></form>")

@app.get("/reminders/send")
def send_reminders():
    candidates = db_get_reminder_candidates()
    sent = 0
    for b in candidates:
        try:
            notify_user_reminder(b["lab_slug"], b)
            db_mark_reminder_sent(b["id"])
            sent += 1
        except Exception:
            pass
    return {"sent": sent}, 200

if __name__ == "__main__":
    app.run(debug=True)
