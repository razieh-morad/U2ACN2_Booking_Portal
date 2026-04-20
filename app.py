from __future__ import annotations

import csv
import hmac
import os
import sqlite3
import smtplib
import uuid
from datetime import datetime, date, time, timedelta
from email.message import EmailMessage
from io import BytesIO, StringIO
from typing import Optional, List, Any, Dict, Tuple
from zoneinfo import ZoneInfo

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    session, abort, make_response
)

# ---------------- Config ----------------
TZ = ZoneInfo(os.environ.get("APP_TZ", "Africa/Johannesburg"))
WORKDAY_START = time(8, 0)
WORKDAY_END = time(16, 0)  # boundary (last slot ends at 16:00)
SLOT_MINUTES = int(os.environ.get("SLOT_MINUTES", "60"))

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.lower().startswith("postgres")

if USE_POSTGRES:
    from psycopg2.pool import ThreadedConnectionPool

from openpyxl import Workbook

APP_DIR = os.path.abspath(os.path.dirname(__file__))
SQLITE_PATH = os.path.join(APP_DIR, "bookings.sqlite3")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

_pg_pool: "ThreadedConnectionPool|None" = None
_db_initialized = False

LABS = {
    "furnace": {"title": "Nanomaterials Furnace (Carbonate Furnace)", "subtitle": "Carbonate Furnace"},
    "xps": {"title": "XPS (X-ray Photoelectron Spectroscopy)", "subtitle": "Surface chemical analysis"},

    # New sections
    "manual-drying-oven": {"title": "Manual drying oven", "subtitle": "Drying"},
    "automated-drying-oven": {"title": "Automated drying oven", "subtitle": "Drying"},
    "sputtering": {"title": "Sputtering", "subtitle": "Thin films / coatings"},
    "auto-lab": {"title": "Auto lab", "subtitle": "Automated workflows"},
    "uv-vis-currie-500": {"title": "UV-Vis Currie 500", "subtitle": "Optical spectroscopy"},
    "centrifuge": {"title": "Centrifuge", "subtitle": "Sample separation"},
    "pelletizer": {"title": "Pelletizer", "subtitle": "Pellet pressing"},
    "thermal-conductivity-system": {"title": "Thermal conductivity system", "subtitle": "Thermal transport"},
    "freeze-dryer": {"title": "Freeze dryer", "subtitle": "Lyophilization"},
    "spin-coater": {"title": "Spin coater", "subtitle": "Thin film deposition"},
}

# Per-lab admins (email username + password)
def _env_slug(slug: str) -> str:
    return slug.upper().replace("-", "_").replace(" ", "_")

ADMIN: Dict[str, Dict[str, str]] = {}
for _slug in LABS.keys():
    key = _env_slug(_slug)
    ADMIN[_slug] = {
        "email": os.environ.get(f"ADMIN_{key}_EMAIL", "").strip(),
        "password": os.environ.get(f"ADMIN_{key}_PASSWORD", "").strip(),
    }

# SMTP global host settings
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes", "y")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "U2ACN2 Nanolab Booking Portal")


def _smtp_for_lab(lab_slug: str) -> Dict[str, Any]:
    """
    Defaults:
      user/pass/from = that lab's admin email/password
    Optional overrides:
      SMTP_<LABKEY>_USER / SMTP_<LABKEY>_PASSWORD / SMTP_<LABKEY>_FROM
    """
    labkey = _env_slug(lab_slug)
    user = os.environ.get(f"SMTP_{labkey}_USER", "").strip() or ADMIN[lab_slug]["email"]
    pw = os.environ.get(f"SMTP_{labkey}_PASSWORD", "").strip() or ADMIN[lab_slug]["password"]
    from_addr = os.environ.get(f"SMTP_{labkey}_FROM", "").strip() or user
    return {"host": SMTP_HOST, "port": SMTP_PORT, "tls": SMTP_USE_TLS, "user": user, "password": pw, "from": from_addr}


# ---- schema (auto-migration) ----
PG_COLUMNS = {
    "lab_slug": "TEXT NOT NULL",
    "booking_group_id": "TEXT",
    "user_name": "TEXT NOT NULL",
    "user_email": "TEXT NOT NULL",

    # furnace optional fields
    "nanomaterial_type": "TEXT",
    "melting_point": "TEXT",
    "material_density": "TEXT",
    "anneal_temp_c": "TEXT",
    "anneal_time_h": "TEXT",
    "gas_type": "TEXT",
    "pressure": "TEXT",
    "vacuum": "BOOLEAN NOT NULL DEFAULT FALSE",

    # XPS optional fields
    "sample_name": "TEXT",
    "sample_count": "INTEGER",
    "elements_of_interest": "TEXT",
    "analysis_type": "TEXT",
    "charge_neutralizer": "BOOLEAN NOT NULL DEFAULT FALSE",
    "mounting_method": "TEXT",
    "outgassing_risk": "TEXT",

    "notes": "TEXT",

    "booking_date": "DATE NOT NULL",
    "start_time": "TIME NOT NULL",
    "end_time": "TIME NOT NULL",

    "created_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "updated_at": "TIMESTAMPTZ",
    "updated_by": "TEXT",
}

SQLITE_COLUMNS = {
    "lab_slug": "TEXT NOT NULL",
    "booking_group_id": "TEXT",
    "user_name": "TEXT NOT NULL",
    "user_email": "TEXT NOT NULL",

    "nanomaterial_type": "TEXT",
    "melting_point": "TEXT",
    "material_density": "TEXT",
    "anneal_temp_c": "TEXT",
    "anneal_time_h": "TEXT",
    "gas_type": "TEXT",
    "pressure": "TEXT",
    "vacuum": "INTEGER NOT NULL DEFAULT 0",

    "sample_name": "TEXT",
    "sample_count": "INTEGER",
    "elements_of_interest": "TEXT",
    "analysis_type": "TEXT",
    "charge_neutralizer": "INTEGER NOT NULL DEFAULT 0",
    "mounting_method": "TEXT",
    "outgassing_risk": "TEXT",

    "notes": "TEXT",

    "booking_date": "TEXT NOT NULL",
    "start_time": "TEXT NOT NULL",
    "end_time": "TEXT NOT NULL",

    "created_at": "TEXT NOT NULL",
    "updated_at": "TEXT",
    "updated_by": "TEXT",
}

EXPORT_COLUMNS = [
    "id", "lab_slug", "booking_group_id",
    "user_name", "user_email",
    "booking_date", "start_time", "end_time",
    "nanomaterial_type", "melting_point", "material_density",
    "anneal_temp_c", "anneal_time_h", "gas_type", "pressure", "vacuum",
    "sample_name", "sample_count", "elements_of_interest", "analysis_type",
    "charge_neutralizer", "mounting_method", "outgassing_risk",
    "notes", "created_at", "updated_at", "updated_by",
]


# ---------------- DB / Migration ----------------
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

def _sqlite_existing_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(bookings);").fetchall()
    return {r[1] for r in rows}

def _migrate_sqlite(conn: sqlite3.Connection):
    conn.execute("CREATE TABLE IF NOT EXISTS bookings (id INTEGER PRIMARY KEY AUTOINCREMENT);")
    existing = _sqlite_existing_columns(conn)
    for col, ddl in SQLITE_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {col} {ddl};")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_lab_date ON bookings(lab_slug, booking_date);")

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


# ---------------- Helpers ----------------
def parse_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None

def parse_time(value: str) -> Optional[time]:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except Exception:
        try:
            return datetime.strptime(value, "%H:%M:%S").time()
        except Exception:
            return None

def overlaps(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    return (a_start < b_end) and (a_end > b_start)

def normalize_booking_time(v: Any) -> time:
    if isinstance(v, time):
        return v
    return parse_time(str(v)) or time(0, 0)

def normalize_booking_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return parse_date(str(v)) or date.min

def iter_workdays(start_d: date, end_d: date):
    d = start_d
    while d <= end_d:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)

def build_slots_for_day(d: date, lab_slug: str) -> List[Tuple[time, time]]:
    # Furnace: two fixed blocks (08:00–12:00 and 12:00–16:00)
    if lab_slug == "furnace":
        return [(time(8, 0), time(12, 0)), (time(12, 0), time(16, 0))]

    # Others: default SLOT_MINUTES slots between WORKDAY_START and WORKDAY_END
    slots: List[Tuple[time, time]] = []
    cur = datetime.combine(d, WORKDAY_START)
    end = datetime.combine(d, WORKDAY_END)
    while cur < end:
        nxt = cur + timedelta(minutes=SLOT_MINUTES)
        if nxt > end:
            break
        slots.append((cur.time(), nxt.time()))
        cur = nxt
    return slots

def next_two_weeks_window() -> Tuple[date, date]:
    today = datetime.now(TZ).date()
    return today, today + timedelta(days=13)

def has_conflict(lab_slug: str, booking_date: str, start_hhmm: str, end_hhmm: str, exclude_id: Optional[int] = None) -> bool:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                if exclude_id is None:
                    cur.execute(
                        """
                        SELECT 1 FROM bookings
                        WHERE lab_slug=%s AND booking_date=%s::date
                          AND start_time < %s::time AND end_time > %s::time
                        LIMIT 1
                        """,
                        (lab_slug, booking_date, end_hhmm, start_hhmm),
                    )
                else:
                    cur.execute(
                        """
                        SELECT 1 FROM bookings
                        WHERE lab_slug=%s AND booking_date=%s::date
                          AND start_time < %s::time AND end_time > %s::time
                          AND id <> %s
                        LIMIT 1
                        """,
                        (lab_slug, booking_date, end_hhmm, start_hhmm, exclude_id),
                    )
                return cur.fetchone() is not None
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.cursor()
        if exclude_id is None:
            cur.execute(
                """
                SELECT 1 FROM bookings
                WHERE lab_slug=? AND booking_date=?
                  AND start_time < ? AND end_time > ?
                LIMIT 1
                """,
                (lab_slug, booking_date, end_hhmm, start_hhmm),
            )
        else:
            cur.execute(
                """
                SELECT 1 FROM bookings
                WHERE lab_slug=? AND booking_date=?
                  AND start_time < ? AND end_time > ?
                  AND id <> ?
                LIMIT 1
                """,
                (lab_slug, booking_date, end_hhmm, start_hhmm, exclude_id),
            )
        hit = cur.fetchone() is not None
        conn.close()
        return hit

def default_booking_form() -> Dict[str, str]:
    now = datetime.now(TZ)
    return {
        "booking_date": now.date().isoformat(),
        "start_time": now.strftime("%H:%M"),
        "end_time": (now + timedelta(hours=1)).strftime("%H:%M"),
        "vacuum": "no",
        "charge_neutralizer": "no",
    }

def merge_prefill(form: Dict[str, str], args: Dict[str, str]) -> Dict[str, str]:
    out = dict(form)
    for k in ("booking_date", "start_time", "end_time"):
        v = (args.get(k) or "").strip()
        if v:
            out[k] = v
    return out


# ---------------- Availability ----------------
def db_list_bookings_range_minimal(lab_slug: str, start_d: date, end_d: date) -> List[Dict[str, Any]]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT booking_date, start_time, end_time
                    FROM bookings
                    WHERE lab_slug=%s AND booking_date >= %s::date AND booking_date <= %s::date
                    """,
                    (lab_slug, start_d.isoformat(), end_d.isoformat()),
                )
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT booking_date, start_time, end_time
            FROM bookings
            WHERE lab_slug=? AND booking_date >= ? AND booking_date <= ?
            """,
            (lab_slug, start_d.isoformat(), end_d.isoformat()),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def is_slot_free(bookings: List[Dict[str, Any]], d: date, s: time, e: time) -> bool:
    for b in bookings:
        bd_date = normalize_booking_date(b["booking_date"])
        if bd_date != d:
            continue
        bs = normalize_booking_time(b["start_time"])
        be = normalize_booking_time(b["end_time"])
        if overlaps(s, e, bs, be):
            return False
    return True

def availability_days(lab_slug: str) -> List[Dict[str, Any]]:
    start_d, end_d = next_two_weeks_window()
    bookings = db_list_bookings_range_minimal(lab_slug, start_d, end_d)
    days: List[Dict[str, Any]] = []
    for d in iter_workdays(start_d, end_d):
        slots = []
        for s, e in build_slots_for_day(d, lab_slug):
            free = is_slot_free(bookings, d, s, e)
            slots.append({
                "date": d.isoformat(),
                "start": s.strftime("%H:%M"),
                "end": e.strftime("%H:%M"),
                "free": free,
                "value": f"{d.isoformat()}|{s.strftime('%H:%M')}|{e.strftime('%H:%M')}",
            })
        days.append({"date": d, "slots": slots})
    return days


# ---------------- Email ----------------
def smtp_ready_for_lab(lab_slug: str) -> bool:
    cfg = _smtp_for_lab(lab_slug)
    return bool(cfg["host"] and cfg["user"] and cfg["password"] and cfg["from"])

def send_email_for_lab(lab_slug: str, to_email: str, subject: str, body: str) -> None:
    cfg = _smtp_for_lab(lab_slug)
    if not smtp_ready_for_lab(lab_slug):
        raise RuntimeError("SMTP is not configured for this lab. Set SMTP_HOST and admin email/password (or SMTP_<LABKEY>_USER/PASSWORD).")

    msg = EmailMessage()
    msg["From"] = f"{SMTP_FROM_NAME} <{cfg['from']}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = ADMIN[lab_slug]["email"]
    msg.set_content(body)

    if cfg["tls"]:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            s.ehlo()
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)


# ---------------- Admin auth (per lab) ----------------
def _require_admin_vars(lab_slug: str):
    if not ADMIN[lab_slug]["email"] or not ADMIN[lab_slug]["password"]:
        abort(404, description=f"Admin not configured for {lab_slug}. Set ADMIN_{_env_slug(lab_slug)}_EMAIL and ADMIN_{_env_slug(lab_slug)}_PASSWORD.")

def is_admin_for(lab_slug: str) -> bool:
    return session.get("is_admin") is True and session.get("admin_lab") == lab_slug

def require_admin(lab_slug: str):
    _require_admin_vars(lab_slug)
    if not is_admin_for(lab_slug):
        return redirect(url_for("admin_login_lab", lab_slug=lab_slug, next=request.path))
    return None

@app.route("/admin/<lab_slug>/login", methods=["GET", "POST"])
def admin_login_lab(lab_slug: str):
    if lab_slug not in LABS:
        abort(404)
    _require_admin_vars(lab_slug)

    if request.method == "POST":
        user = (request.form.get("username") or "").strip()
        pw = (request.form.get("password") or "").strip()

        if hmac.compare_digest(user.lower(), ADMIN[lab_slug]["email"].lower()) and hmac.compare_digest(pw, ADMIN[lab_slug]["password"]):
            session.clear()
            session["is_admin"] = True
            session["admin_lab"] = lab_slug
            session["admin_email"] = ADMIN[lab_slug]["email"]
            nxt = request.form.get("next") or url_for("admin_lab", lab_slug=lab_slug)
            return redirect(nxt)

        flash("Invalid credentials.", "error")

    return render_template(
        "admin_login.html",
        lab_slug=lab_slug,
        lab_title=LABS[lab_slug]["title"],
        admin_email_hint=ADMIN[lab_slug]["email"],
        next=request.args.get("next") or url_for("admin_lab", lab_slug=lab_slug),
    )

@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------- Booking URL helper (for availability clicks + homepage links) ----------------
def booking_url_for(lab_slug: str, **params) -> str:
    """Return the correct booking URL for a lab (furnace/xps have dedicated routes)."""
    if lab_slug == "furnace":
        return url_for("furnace", **params)
    if lab_slug == "xps":
        return url_for("xps", **params)
    return url_for("lab_generic", lab_slug=lab_slug, **params)


# ---------------- DB CRUD ----------------
def _row_to_dict_pg(row: Any, cols: List[str]) -> Dict[str, Any]:
    d = dict(zip(cols, row))
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, (date, time)):
            out[k] = v.isoformat()
        elif isinstance(v, bool):
            out[k] = "Yes" if v else "No"
        elif v is None:
            out[k] = ""
        else:
            out[k] = str(v)
    return out

def db_list_bookings(lab_slug: str) -> List[Dict[str, Any]]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bookings WHERE lab_slug=%s ORDER BY booking_date DESC, start_time DESC", (lab_slug,))
                cols = [d.name for d in cur.description]
                return [_row_to_dict_pg(r, cols) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM bookings WHERE lab_slug=? ORDER BY booking_date DESC, start_time DESC", (lab_slug,)).fetchall()
        conn.close()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            for k, v in list(d.items()):
                if k in ("vacuum", "charge_neutralizer"):
                    d[k] = "Yes" if int(v or 0) == 1 else "No"
                elif v is None:
                    d[k] = ""
                else:
                    d[k] = str(v)
            out.append(d)
        return out

def db_get_booking(booking_id: int) -> Optional[Dict[str, Any]]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bookings WHERE id=%s", (booking_id,))
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d.name for d in cur.description]
                return _row_to_dict_pg(row, cols)
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        for k, v in list(d.items()):
            if k in ("vacuum", "charge_neutralizer"):
                d[k] = "Yes" if int(v or 0) == 1 else "No"
            elif v is None:
                d[k] = ""
            else:
                d[k] = str(v)
        return d

def db_insert_booking(payload: Dict[str, Any]) -> int:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bookings (
                        lab_slug, booking_group_id, user_name, user_email,
                        nanomaterial_type, melting_point, material_density,
                        anneal_temp_c, anneal_time_h, gas_type, pressure, vacuum,
                        sample_name, sample_count, elements_of_interest, analysis_type,
                        charge_neutralizer, mounting_method, outgassing_risk,
                        notes,
                        booking_date, start_time, end_time,
                        updated_at, updated_by
                    ) VALUES (
                        %s,%s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s,%s,%s,
                        %s,%s,%s,%s,
                        %s,%s,%s,
                        %s,
                        %s::date,%s::time,%s::time,
                        %s,%s
                    )
                    RETURNING id
                    """,
                    (
                        payload["lab_slug"], payload.get("booking_group_id"), payload["user_name"], payload["user_email"],
                        payload.get("nanomaterial_type"), payload.get("melting_point"), payload.get("material_density"),
                        payload.get("anneal_temp_c"), payload.get("anneal_time_h"), payload.get("gas_type"),
                        payload.get("pressure"), bool(payload.get("vacuum")),
                        payload.get("sample_name"), payload.get("sample_count"), payload.get("elements_of_interest"),
                        payload.get("analysis_type"), bool(payload.get("charge_neutralizer")),
                        payload.get("mounting_method"), payload.get("outgassing_risk"),
                        payload.get("notes"),
                        payload["booking_date"], payload["start_time"], payload["end_time"],
                        payload.get("updated_at"), payload.get("updated_by"),
                    ),
                )
                booking_id = cur.fetchone()[0]
            conn.commit()
            return int(booking_id)
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO bookings (
                lab_slug, booking_group_id, user_name, user_email,
                nanomaterial_type, melting_point, material_density,
                anneal_temp_c, anneal_time_h, gas_type, pressure, vacuum,
                sample_name, sample_count, elements_of_interest, analysis_type,
                charge_neutralizer, mounting_method, outgassing_risk,
                notes,
                booking_date, start_time, end_time,
                created_at, updated_at, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["lab_slug"], payload.get("booking_group_id"), payload["user_name"], payload["user_email"],
                payload.get("nanomaterial_type"), payload.get("melting_point"), payload.get("material_density"),
                payload.get("anneal_temp_c"), payload.get("anneal_time_h"), payload.get("gas_type"),
                payload.get("pressure"), 1 if payload.get("vacuum") else 0,
                payload.get("sample_name"), payload.get("sample_count"), payload.get("elements_of_interest"),
                payload.get("analysis_type"), 1 if payload.get("charge_neutralizer") else 0,
                payload.get("mounting_method"), payload.get("outgassing_risk"),
                payload.get("notes"),
                payload["booking_date"], payload["start_time"], payload["end_time"],
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                payload.get("updated_at"), payload.get("updated_by"),
            ),
        )
        conn.commit()
        booking_id = cur.lastrowid
        conn.close()
        return int(booking_id)

def db_update_booking_time(booking_id: int, new_date: str, new_start: str, new_end: str, updated_by: str) -> None:
    init_db()
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bookings
                    SET booking_date=%s::date, start_time=%s::time, end_time=%s::time,
                        updated_at=NOW(), updated_by=%s
                    WHERE id=%s
                    """,
                    (new_date, new_start, new_end, updated_by, booking_id),
                )
            conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute(
            """
            UPDATE bookings
            SET booking_date=?, start_time=?, end_time=?, updated_at=?, updated_by=?
            WHERE id=?
            """,
            (new_date, new_start, new_end, now_iso, updated_by, booking_id),
        )
        conn.commit()
        conn.close()

def db_delete_booking(booking_id: int) -> None:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bookings WHERE id=%s", (booking_id,))
            conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
        conn.commit()
        conn.close()


# ---------------- Slot selection ----------------
def collect_selected_slots() -> List[Tuple[str, str, str]]:
    slots: List[Tuple[str, str, str]] = []
    for v in request.form.getlist("slot"):
        parts = (v or "").split("|")
        if len(parts) != 3:
            continue
        d, s, e = parts
        if parse_date(d) and parse_time(s) and parse_time(e):
            slots.append((d, s, e))
    return sorted(list({x for x in slots}))

def is_valid_furnace_block(start_hhmm: str, end_hhmm: str) -> bool:
    """Furnace allows only two blocks: 08:00–12:00 and 12:00–16:00."""
    return (start_hhmm, end_hhmm) in (("08:00", "12:00"), ("12:00", "16:00"))


# ---------------- Routes ----------------
@app.get("/health")
def health():
    return {"status": "ok"}, 200

@app.route("/")
def index():
    labs = sorted(
        [
            {
                "title": LABS[k]["title"],
                "slug": k,
                "subtitle": LABS[k]["subtitle"],
                "booking_url": booking_url_for(k),
                "availability_url": url_for("lab_availability", lab_slug=k),
                "admin_url": url_for("admin_lab", lab_slug=k),
            }
            for k in LABS.keys()
        ],
        key=lambda x: x["title"].lower(),
    )

    return render_template(
        "index.html",
        labs=labs,
        admin_portal_url=url_for("admin_portal"),
    )
    
@app.get("/admin")
def admin_portal():
    """
    Admin hub page (no login here): it lists each lab and links to the per-lab admin timetable.
    Each per-lab admin page still requires that lab’s credentials.
    """
    order = ["furnace", "xps"] + sorted([k for k in LABS.keys() if k not in ("furnace", "xps")])
    labs = []
    for k in order:
        labs.append({
            "title": LABS[k]["title"],
            "slug": k,
            "subtitle": LABS[k]["subtitle"],
            "admin_url": url_for("admin_lab", lab_slug=k),
            "availability_url": url_for("lab_availability", lab_slug=k),
            "booking_url": booking_url_for(k),
        })
    return render_template("admin_portal.html", labs=labs)

@app.route("/labs/<lab_slug>/availability")
def lab_availability(lab_slug: str):
    if lab_slug not in LABS:
        abort(404)
    days = availability_days(lab_slug)
    # booking_url helps your availability.html link back to the right booking page
    return render_template(
        "availability.html",
        lab_slug=lab_slug,
        lab_title=LABS[lab_slug]["title"],
        days=days,
        booking_url=booking_url_for(lab_slug),
    )

@app.get("/labs/<lab_slug>/prefill")
def prefill_booking(lab_slug: str):
    """
    Used by availability tables:
    clicking a free slot should always go to the correct lab booking page (furnace/xps/generic)
    with booking_date/start_time/end_time prefilled.
    """
    if lab_slug not in LABS:
        abort(404)

    booking_date = (request.args.get("booking_date") or "").strip()
    start_time = (request.args.get("start_time") or "").strip()
    end_time = (request.args.get("end_time") or "").strip()

    return redirect(
        booking_url_for(
            lab_slug,
            booking_date=booking_date,
            start_time=start_time,
            end_time=end_time,
        )
    )

@app.route("/labs/<lab_slug>", methods=["GET", "POST"])
def lab_generic(lab_slug: str):
    # Furnace and XPS have dedicated pages
    if lab_slug == "furnace":
        return redirect(url_for("furnace"))
    if lab_slug == "xps":
        return redirect(url_for("xps"))
    if lab_slug not in LABS:
        abort(404)

    lab_info = {
        "brand": "iThemba Labs/U2ACN2",
        "title": LABS[lab_slug]["title"],
        "slug": lab_slug,
        "administrators": [],
    }

    if request.method == "POST":
        return handle_generic_booking(lab_info)

    form = merge_prefill(default_booking_form(), request.args)
    days = availability_days(lab_slug)
    return render_template("lab_generic.html", lab=lab_info, form=form, days=days)

def handle_generic_booking(lab_info: Dict[str, Any]):
    lab_slug = lab_info["slug"]
    user_name = (request.form.get("user_name") or "").strip()
    user_email = (request.form.get("user_email") or "").strip()
    selected_slots = collect_selected_slots()

    booking_date = (request.form.get("booking_date") or "").strip()
    start_time = (request.form.get("start_time") or "").strip()
    end_time = (request.form.get("end_time") or "").strip()

    notes = (request.form.get("notes") or "").strip()

    errors: List[str] = []
    if not user_name:
        errors.append("Name is required.")
    if not user_email or "@" not in user_email:
        errors.append("A valid email is required.")

    if selected_slots:
        for d, s, e in selected_slots:
            if has_conflict(lab_slug, d, s, e):
                errors.append(f"Conflict: {d} {s}–{e} is already booked.")
    else:
        if not parse_date(booking_date):
            errors.append("Please choose a valid date.")
        st = parse_time(start_time)
        et = parse_time(end_time)
        if not st or not et:
            errors.append("Please choose valid start/end times.")
        elif et <= st:
            errors.append("End time must be after start time.")
        if not errors and has_conflict(lab_slug, booking_date, start_time, end_time):
            errors.append("Time conflict: this slot overlaps an existing booking.")

    if errors:
        for e in errors:
            flash(e, "error")
        days = availability_days(lab_slug)
        return render_template("lab_generic.html", lab=lab_info, form=request.form, days=days)

    group_id = str(uuid.uuid4()) if selected_slots else None
    created_ids: List[int] = []

    base_payload = {
        "lab_slug": lab_slug,
        "booking_group_id": group_id,
        "user_name": user_name,
        "user_email": user_email,
        "notes": notes,
        "updated_at": None,
        "updated_by": None,
    }

    if selected_slots:
        for d, s, e in selected_slots:
            payload = dict(base_payload)
            payload.update({"booking_date": d, "start_time": s, "end_time": e})
            created_ids.append(db_insert_booking(payload))
    else:
        payload = dict(base_payload)
        payload.update({"booking_date": booking_date, "start_time": start_time, "end_time": end_time})
        created_ids.append(db_insert_booking(payload))

    return redirect(url_for("booking_success", booking_id=created_ids[-1]))

@app.route("/labs/furnace", methods=["GET", "POST"])
def furnace():
    lab_info = {
        "brand": "iThemba Labs/U2ACN2",
        "title": LABS["furnace"]["title"],
        "slug": "furnace",
        "administrators": [
            {"name": "Dr Itani Madiba", "contact": "06598853331"},
            {"name": "Mr Basil Martin", "contact": "0796330278"},
        ],
    }
    if request.method == "POST":
        return handle_booking_submit(lab_info, kind="furnace")
    form = merge_prefill(default_booking_form(), request.args)
    days = availability_days("furnace")
    return render_template("furnace.html", lab=lab_info, form=form, days=days)

@app.route("/labs/xps", methods=["GET", "POST"])
def xps():
    lab_info = {
        "brand": "iThemba Labs/U2ACN2",
        "title": LABS["xps"]["title"],
        "slug": "xps",
        "administrators": [{"name": "Dr Itani Madiba", "contact": "06598853331"}],
    }
    if request.method == "POST":
        return handle_booking_submit(lab_info, kind="xps")
    form = merge_prefill(default_booking_form(), request.args)
    days = availability_days("xps")
    return render_template("xps.html", lab=lab_info, form=form, days=days)

def handle_booking_submit(lab_info: Dict[str, Any], kind: str):
    lab_slug = lab_info["slug"]
    user_name = (request.form.get("user_name") or "").strip()
    user_email = (request.form.get("user_email") or "").strip()

    selected_slots = collect_selected_slots()

    booking_date = (request.form.get("booking_date") or "").strip()
    start_time = (request.form.get("start_time") or "").strip()
    end_time = (request.form.get("end_time") or "").strip()

    errors: List[str] = []
    if not user_name:
        errors.append("Name is required.")
    if not user_email or "@" not in user_email:
        errors.append("A valid email is required.")

    if selected_slots:
        for d, s, e in selected_slots:
            if kind == "furnace" and not is_valid_furnace_block(s, e):
                errors.append(f"Invalid furnace slot: {s}–{e}. Please choose 08:00–12:00 or 12:00–16:00.")
                continue
            if has_conflict(lab_slug, d, s, e):
                errors.append(f"Conflict: {d} {s}–{e} is already booked.")
    else:
        if not parse_date(booking_date):
            errors.append("Please choose a valid date.")
        st = parse_time(start_time)
        et = parse_time(end_time)
        if not st or not et:
            errors.append("Please choose valid start/end times.")
        elif et <= st:
            errors.append("End time must be after start time.")
        if not errors and kind == "furnace" and not is_valid_furnace_block(start_time, end_time):
            errors.append("Furnace booking must be either 08:00–12:00 or 12:00–16:00.")
        if not errors and has_conflict(lab_slug, booking_date, start_time, end_time):
            errors.append("Time conflict: this slot overlaps an existing booking.")

    if errors:
        for e in errors:
            flash(e, "error")
        days = availability_days(lab_slug)
        template = "furnace.html" if kind == "furnace" else "xps.html"
        return render_template(template, lab=lab_info, form=request.form, days=days)

    group_id = str(uuid.uuid4()) if selected_slots else None
    created_ids: List[int] = []

    base_payload = {
        "lab_slug": lab_slug,
        "booking_group_id": group_id,
        "user_name": user_name,
        "user_email": user_email,
        "notes": (request.form.get("notes") or "").strip(),
        "updated_at": None,
        "updated_by": None,
    }

    if kind == "furnace":
        extra = {
            "nanomaterial_type": (request.form.get("nanomaterial_type") or "").strip(),
            "melting_point": (request.form.get("melting_point") or "").strip(),
            "material_density": (request.form.get("material_density") or "").strip(),
            "anneal_temp_c": (request.form.get("anneal_temp_c") or "").strip(),
            "anneal_time_h": (request.form.get("anneal_time_h") or "").strip(),
            "gas_type": (request.form.get("gas_type") or "").strip(),
            "pressure": (request.form.get("pressure") or "").strip(),
            "vacuum": True if request.form.get("vacuum") == "yes" else False,
        }
    else:
        def _to_int(v: str) -> Optional[int]:
            v = (v or "").strip()
            if not v:
                return None
            try:
                return int(v)
            except Exception:
                return None

        extra = {
            "sample_name": (request.form.get("sample_name") or "").strip(),
            "sample_count": _to_int(request.form.get("sample_count") or ""),
            "elements_of_interest": (request.form.get("elements_of_interest") or "").strip(),
            "analysis_type": (request.form.get("analysis_type") or "").strip(),
            "charge_neutralizer": True if request.form.get("charge_neutralizer") == "yes" else False,
            "mounting_method": (request.form.get("mounting_method") or "").strip(),
            "outgassing_risk": (request.form.get("outgassing_risk") or "").strip(),
        }

    if selected_slots:
        for d, s, e in selected_slots:
            payload = dict(base_payload)
            payload.update(extra)
            payload.update({"booking_date": d, "start_time": s, "end_time": e})
            created_ids.append(db_insert_booking(payload))
    else:
        payload = dict(base_payload)
        payload.update(extra)
        payload.update({"booking_date": booking_date, "start_time": start_time, "end_time": end_time})
        created_ids.append(db_insert_booking(payload))

    return redirect(url_for("booking_success", booking_id=created_ids[-1]))

@app.route("/bookings/<int:booking_id>")
def booking_success(booking_id: int):
    b = db_get_booking(booking_id)
    if not b:
        flash("Booking not found.", "error")
        return redirect(url_for("index"))
    return render_template("success.html", b=b)

# ---- Admin per-lab ----
@app.route("/admin/<lab_slug>")
def admin_lab(lab_slug: str):
    if lab_slug not in LABS:
        abort(404)
    guard = require_admin(lab_slug)
    if guard is not None:
        return guard

    rows = db_list_bookings(lab_slug)
    days = availability_days(lab_slug)
    admin_email = session.get("admin_email", "")

    template = "admin_furnace.html" if lab_slug == "furnace" else ("admin_xps.html" if lab_slug == "xps" else "admin_generic.html")
    return render_template(
        template,
        lab_title=LABS[lab_slug]["title"],
        lab_slug=lab_slug,
        rows=rows,
        days=days,
        admin_email=admin_email,
        smtp_ready=smtp_ready_for_lab(lab_slug),
    )

@app.post("/admin/<lab_slug>/reserve")
def admin_reserve_slots(lab_slug: str):
    if lab_slug not in LABS:
        abort(404)
    guard = require_admin(lab_slug)
    if guard is not None:
        return guard

    selected_slots = collect_selected_slots()
    booking_date = (request.form.get("booking_date") or "").strip()
    start_time = (request.form.get("start_time") or "").strip()
    end_time = (request.form.get("end_time") or "").strip()

    user_name = (request.form.get("user_name") or "").strip() or "ADMIN RESERVED"
    user_email = (request.form.get("user_email") or "").strip() or session.get("admin_email", "")
    notes = (request.form.get("notes") or "").strip() or "Reserved by admin"

    errors: List[str] = []
    if selected_slots:
        for d, s, e in selected_slots:
            if lab_slug == "furnace" and not is_valid_furnace_block(s, e):
                errors.append(f"Invalid furnace slot: {s}–{e}. Please choose 08:00–12:00 or 12:00–16:00.")
                continue
            if has_conflict(lab_slug, d, s, e):
                errors.append(f"Conflict: {d} {s}–{e} is already booked.")
    else:
        if not parse_date(booking_date):
            errors.append("Please choose a valid date.")
        st = parse_time(start_time)
        et = parse_time(end_time)
        if not st or not et:
            errors.append("Please choose valid start/end times.")
        elif et <= st:
            errors.append("End time must be after start time.")
        if not errors and lab_slug == "furnace" and not is_valid_furnace_block(start_time, end_time):
            errors.append("Furnace booking must be either 08:00–12:00 or 12:00–16:00.")
        if not errors and has_conflict(lab_slug, booking_date, start_time, end_time):
            errors.append("Time conflict: this slot overlaps an existing booking.")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("admin_lab", lab_slug=lab_slug))

    group_id = str(uuid.uuid4()) if selected_slots else None
    created = 0
    admin_email = session.get("admin_email", "")

    for d, s, e in (selected_slots or [(booking_date, start_time, end_time)]):
        payload = {
            "lab_slug": lab_slug,
            "booking_group_id": group_id,
            "user_name": user_name,
            "user_email": user_email,
            "notes": notes,
            "booking_date": d,
            "start_time": s,
            "end_time": e,
            "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "updated_by": admin_email,
        }
        db_insert_booking(payload)
        created += 1

    flash(f"Reserved {created} slot(s).", "ok")
    return redirect(url_for("admin_lab", lab_slug=lab_slug))

@app.get("/admin/<lab_slug>/edit/<int:booking_id>")
def admin_edit_booking(lab_slug: str, booking_id: int):
    if lab_slug not in LABS:
        abort(404)
    guard = require_admin(lab_slug)
    if guard is not None:
        return guard

    b = db_get_booking(booking_id)
    if not b or b.get("lab_slug") != lab_slug:
        flash("Booking not found.", "error")
        return redirect(url_for("admin_lab", lab_slug=lab_slug))

    days = availability_days(lab_slug)
    return render_template(
        "admin_edit.html",
        lab_slug=lab_slug,
        lab_title=LABS[lab_slug]["title"],
        b=b,
        days=days,
        smtp_ready=smtp_ready_for_lab(lab_slug),
        admin_email=session.get("admin_email", ""),
    )

@app.post("/admin/<lab_slug>/edit/<int:booking_id>")
def admin_update_booking(lab_slug: str, booking_id: int):
    if lab_slug not in LABS:
        abort(404)
    guard = require_admin(lab_slug)
    if guard is not None:
        return guard

    b = db_get_booking(booking_id)
    if not b or b.get("lab_slug") != lab_slug:
        flash("Booking not found.", "error")
        return redirect(url_for("admin_lab", lab_slug=lab_slug))

    new_date = (request.form.get("booking_date") or "").strip()
    new_start = (request.form.get("start_time") or "").strip()
    new_end = (request.form.get("end_time") or "").strip()

    errors: List[str] = []
    if not parse_date(new_date):
        errors.append("Please choose a valid date.")
    st = parse_time(new_start)
    et = parse_time(new_end)
    if not st or not et:
        errors.append("Please choose valid start/end times.")
    elif et <= st:
        errors.append("End time must be after start time.")
    if not errors and has_conflict(lab_slug, new_date, new_start, new_end, exclude_id=booking_id):
        errors.append("Time conflict: this slot overlaps an existing booking.")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("admin_edit_booking", lab_slug=lab_slug, booking_id=booking_id))

    old = f"{b.get('booking_date')} {str(b.get('start_time'))[:5]}–{str(b.get('end_time'))[:5]}"
    new = f"{new_date} {new_start}–{new_end}"

    admin_email = session.get("admin_email", "")
    db_update_booking_time(booking_id, new_date, new_start, new_end, updated_by=admin_email)

    try:
        subject = f"Booking updated: {LABS[lab_slug]['title']}"
        body = (
            f"Hello {b.get('user_name')},\n\n"
            f"Your booking for {LABS[lab_slug]['title']} has been updated by the lab administrator.\n\n"
            f"Old slot: {old}\n"
            f"New slot: {new}\n\n"
            f"If you have any questions, reply to this email.\n\n"
            f"Regards,\n"
            f"{SMTP_FROM_NAME}\n"
        )
        if smtp_ready_for_lab(lab_slug):
            send_email_for_lab(lab_slug, b.get("user_email", ""), subject, body)
            flash("Booking updated and email sent to user.", "ok")
        else:
            flash("Booking updated. SMTP not configured for this lab, so no email was sent.", "error")
    except Exception as e:
        flash(f"Booking updated, but email sending failed: {e}", "error")

    return redirect(url_for("admin_lab", lab_slug=lab_slug))

@app.post("/admin/<lab_slug>/delete/<int:booking_id>")
def admin_delete_booking(lab_slug: str, booking_id: int):
    if lab_slug not in LABS:
        abort(404)
    guard = require_admin(lab_slug)
    if guard is not None:
        return guard

    b = db_get_booking(booking_id)
    if not b or b.get("lab_slug") != lab_slug:
        flash("Booking not found.", "error")
        return redirect(url_for("admin_lab", lab_slug=lab_slug))

    db_delete_booking(booking_id)

    try:
        subject = f"Booking cancelled: {LABS[lab_slug]['title']}"
        old = f"{b.get('booking_date')} {str(b.get('start_time'))[:5]}–{str(b.get('end_time'))[:5]}"
        body = (
            f"Hello {b.get('user_name')},\n\n"
            f"Your booking for {LABS[lab_slug]['title']} has been cancelled by the lab administrator.\n\n"
            f"Cancelled slot: {old}\n\n"
            f"If you have questions, reply to this email.\n\n"
            f"Regards,\n"
            f"{SMTP_FROM_NAME}\n"
        )
        if smtp_ready_for_lab(lab_slug):
            send_email_for_lab(lab_slug, b.get("user_email", ""), subject, body)
            flash("Booking deleted and user notified by email.", "ok")
        else:
            flash("Booking deleted. SMTP not configured for this lab, so no email was sent.", "ok")
    except Exception as e:
        flash(f"Booking deleted, but email sending failed: {e}", "error")

    return redirect(url_for("admin_lab", lab_slug=lab_slug))


# ---- Export ----
def export_rows(lab_slug: str) -> List[Dict[str, Any]]:
    rows = db_list_bookings(lab_slug)
    return [{c: r.get(c, "") for c in EXPORT_COLUMNS} for r in rows]

@app.get("/admin/export/<lab_slug>.csv")
def admin_export_csv(lab_slug: str):
    if lab_slug not in LABS:
        abort(404)
    guard = require_admin(lab_slug)
    if guard is not None:
        return guard

    rows = export_rows(lab_slug)
    si = StringIO()
    writer = csv.DictWriter(si, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)

    resp = make_response(si.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{lab_slug}_bookings.csv"'
    return resp

@app.get("/admin/export/<lab_slug>.xlsx")
def admin_export_xlsx(lab_slug: str):
    if lab_slug not in LABS:
        abort(404)
    guard = require_admin(lab_slug)
    if guard is not None:
        return guard

    rows = export_rows(lab_slug)
    wb = Workbook()
    ws = wb.active
    ws.title = "bookings"
    ws.append(EXPORT_COLUMNS)
    for r in rows:
        ws.append([r.get(c, "") for c in EXPORT_COLUMNS])

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    resp = make_response(bio.read())
    resp.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    resp.headers["Content-Disposition"] = f'attachment; filename="{lab_slug}_bookings.xlsx"'
    return resp


# --- bootstrap ---
try:
    init_db()
except Exception:
    pass

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
