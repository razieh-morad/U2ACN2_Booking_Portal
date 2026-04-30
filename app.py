from __future__ import annotations

import csv
import hmac
import os
import sqlite3
import smtplib
import threading
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

# ------------------------------------------------------------------ Config ---
TZ = ZoneInfo(os.environ.get("APP_TZ", "Africa/Johannesburg"))
WORKDAY_START = time(8, 0)
WORKDAY_END   = time(16, 0)
SLOT_MINUTES  = int(os.environ.get("SLOT_MINUTES", "60"))

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.lower().startswith("postgres")

if USE_POSTGRES:
    from psycopg2.pool import ThreadedConnectionPool

from openpyxl import Workbook

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
    "manual-drying-oven":          {"title": "Manual drying oven",          "subtitle": "Drying",               "min_notice_hours": 2,  "max_days_ahead": 30, "max_duration_hours": 8},
    "automated-drying-oven":       {"title": "Automated drying oven",       "subtitle": "Drying",               "min_notice_hours": 2,  "max_days_ahead": 30, "max_duration_hours": 8},
    "sputtering":                  {"title": "Sputtering",                  "subtitle": "Thin films / coatings","min_notice_hours": 24, "max_days_ahead": 30, "max_duration_hours": 8},
    "auto-lab":                    {"title": "Auto lab",                    "subtitle": "Automated workflows",  "min_notice_hours": 4,  "max_days_ahead": 30, "max_duration_hours": 8},
    "uv-vis-currie-500":           {"title": "UV-Vis Currie 500",           "subtitle": "Optical spectroscopy", "min_notice_hours": 2,  "max_days_ahead": 30, "max_duration_hours": 4},
    "centrifuge":                  {"title": "Centrifuge",                  "subtitle": "Sample separation",    "min_notice_hours": 1,  "max_days_ahead": 30, "max_duration_hours": 4},
    "pelletizer":                  {"title": "Pelletizer",                  "subtitle": "Pellet pressing",      "min_notice_hours": 1,  "max_days_ahead": 30, "max_duration_hours": 4},
    "thermal-conductivity-system": {"title": "Thermal conductivity system", "subtitle": "Thermal transport",    "min_notice_hours": 24, "max_days_ahead": 30, "max_duration_hours": 8},
    "freeze-dryer":                {"title": "Freeze dryer",                "subtitle": "Lyophilization",       "min_notice_hours": 24, "max_days_ahead": 30, "max_duration_hours": 8},
    "spin-coater":                 {"title": "Spin coater",                 "subtitle": "Thin film deposition", "min_notice_hours": 2,  "max_days_ahead": 30, "max_duration_hours": 4},
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

# --------------------------------------------------------- SMTP (shared) ----
SMTP_HOST      = os.environ.get("SMTP_HOST",      "").strip()
SMTP_PORT      = int(os.environ.get("SMTP_PORT",  "587"))
SMTP_USE_TLS   = os.environ.get("SMTP_USE_TLS",   "true").lower() in ("1","true","yes","y")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "U2ACN2 Nanolab Booking Portal")
SMTP_USER      = os.environ.get("SMTP_USER",      "").strip()
SMTP_PASSWORD  = os.environ.get("SMTP_PASSWORD",  "").strip()

def smtp_ready() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)

def smtp_ready_for_lab(_: str) -> bool:
    return smtp_ready()

# ---------------------------------------------------------- DB schema --------
PG_COLUMNS = {
    "lab_slug":             "TEXT NOT NULL",
    "booking_group_id":     "TEXT",
    "user_name":            "TEXT NOT NULL",
    "user_email":           "TEXT NOT NULL",
    "nanomaterial_type":    "TEXT",
    "melting_point":        "TEXT",
    "material_density":     "TEXT",
    "anneal_temp_c":        "TEXT",
    "anneal_time_h":        "TEXT",
    "gas_type":             "TEXT",
    "pressure":             "TEXT",
    "vacuum":               "BOOLEAN NOT NULL DEFAULT FALSE",
    "sample_name":          "TEXT",
    "sample_count":         "INTEGER",
    "elements_of_interest": "TEXT",
    "analysis_type":        "TEXT",
    "charge_neutralizer":   "BOOLEAN NOT NULL DEFAULT FALSE",
    "mounting_method":      "TEXT",
    "outgassing_risk":      "TEXT",
    "notes":                "TEXT",
    "booking_date":         "DATE NOT NULL",
    "start_time":           "TIME NOT NULL",
    "end_time":             "TIME NOT NULL",
    "created_at":           "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "updated_at":           "TIMESTAMPTZ",
    "updated_by":           "TEXT",
    "status":               "TEXT NOT NULL DEFAULT 'pending'",
    "approval_token":       "TEXT",
    "rejection_reason":     "TEXT",
    "approval_note":        "TEXT",
    "cancel_token":         "TEXT",
    "cancelled_at":         "TIMESTAMPTZ",
    "reminder_sent":        "BOOLEAN NOT NULL DEFAULT FALSE",
}

SQLITE_COLUMNS = {
    "lab_slug":             "TEXT NOT NULL",
    "booking_group_id":     "TEXT",
    "user_name":            "TEXT NOT NULL",
    "user_email":           "TEXT NOT NULL",
    "nanomaterial_type":    "TEXT",
    "melting_point":        "TEXT",
    "material_density":     "TEXT",
    "anneal_temp_c":        "TEXT",
    "anneal_time_h":        "TEXT",
    "gas_type":             "TEXT",
    "pressure":             "TEXT",
    "vacuum":               "INTEGER NOT NULL DEFAULT 0",
    "sample_name":          "TEXT",
    "sample_count":         "INTEGER",
    "elements_of_interest": "TEXT",
    "analysis_type":        "TEXT",
    "charge_neutralizer":   "INTEGER NOT NULL DEFAULT 0",
    "mounting_method":      "TEXT",
    "outgassing_risk":      "TEXT",
    "notes":                "TEXT",
    "booking_date":         "TEXT NOT NULL",
    "start_time":           "TEXT NOT NULL",
    "end_time":             "TEXT NOT NULL",
    "created_at":           "TEXT NOT NULL",
    "updated_at":           "TEXT",
    "updated_by":           "TEXT",
    "status":               "TEXT NOT NULL DEFAULT 'pending'",
    "approval_token":       "TEXT",
    "rejection_reason":     "TEXT",
    "approval_note":        "TEXT",
    "cancel_token":         "TEXT",
    "cancelled_at":         "TEXT",
    "reminder_sent":        "INTEGER NOT NULL DEFAULT 0",
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

def _sqlite_existing_columns(conn: sqlite3.Connection) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(bookings);").fetchall()}

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

# ------------------------------------------------------- General helpers ----
def parse_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None

def parse_time(value: str) -> Optional[time]:
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except Exception:
            pass
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
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)

def build_slots_for_day(d: date, lab_slug: str) -> List[Tuple[time, time]]:
    if lab_slug == "furnace":
        return [(time(8, 0), time(12, 0)), (time(12, 0), time(16, 0))]
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

def has_conflict(lab_slug: str, booking_date: str, start_hhmm: str, end_hhmm: str,
                 exclude_id: Optional[int] = None) -> bool:
    init_db()
    base_q = (
        "SELECT 1 FROM bookings WHERE lab_slug={p} AND booking_date={d} "
        "AND start_time<{e} AND end_time>{s} "
        "AND status != 'rejected' AND (cancelled_at IS NULL OR cancelled_at = '') LIMIT 1"
    )
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
        cur  = conn.cursor()
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
        if v:
            out[k] = v
    return out

# ---------------------------------------------------- Booking rule check ----
def check_booking_rules(lab_slug: str, booking_date: str, start_hhmm: str,
                        end_hhmm: str) -> List[str]:
    errors: List[str] = []
    lab = LABS.get(lab_slug, {})
    now = datetime.now(TZ)
    bd = parse_date(booking_date)
    st = parse_time(start_hhmm)
    et = parse_time(end_hhmm)
    if not bd or not st or not et:
        return errors
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

# ---------------------------------------------------------- Availability ----
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
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT booking_date,start_time,end_time FROM bookings "
            "WHERE lab_slug=? AND booking_date>=? AND booking_date<=? "
            "AND status!='rejected' AND (cancelled_at IS NULL OR cancelled_at='')",
            (lab_slug, start_d.isoformat(), end_d.isoformat())).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def is_slot_free(bookings: List[Dict], d: date, s: time, e: time) -> bool:
    for b in bookings:
        if normalize_booking_date(b["booking_date"]) != d:
            continue
        if overlaps(s, e, normalize_booking_time(b["start_time"]),
                    normalize_booking_time(b["end_time"])):
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
                if slot_dt < now + timedelta(hours=min_notice):
                    free = False
            slots.append({
                "date":  d.isoformat(),
                "start": s.strftime("%H:%M"),
                "end":   e.strftime("%H:%M"),
                "free":  free,
                "value": f"{d.isoformat()}|{s.strftime('%H:%M')}|{e.strftime('%H:%M')}",
            })
        days.append({"date": d, "slots": slots})
    return days

# ------------------------------------------------------------------ Email ----
def _send_email(to: str, subject: str, body: str) -> None:
    if not smtp_ready():
        raise RuntimeError("SMTP not configured.")
    msg = EmailMessage()
    msg["From"]    = f"{SMTP_FROM_NAME} <{SMTP_USER}>"
    msg["To"]      = to
    msg["Subject"] = subject
    msg["Reply-To"]= SMTP_USER
    msg.set_content(body)
    if SMTP_USE_TLS:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo(); s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)

def _send_async(to: str, subject: str, body: str) -> None:
    def _run():
        try:
            _send_email(to, subject, body)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()

def send_email_for_lab(_: str, to: str, subject: str, body: str) -> None:
    _send_email(to, subject, body)

def _slot_str(b: Dict) -> str:
    return f"{b['booking_date']}  {str(b['start_time'])[:5]}–{str(b['end_time'])[:5]}"

def _cancel_url_for(b: Dict) -> str:
    token = b.get("cancel_token", "")
    if not token:
        return ""
    return url_for("cancel_booking_get", token=token, _external=True)

def notify_user_submission(lab_slug: str, b: Dict) -> None:
    if not smtp_ready(): return
    lab_title = LABS[lab_slug]["title"]
    cancel_url = _cancel_url_for(b)
    body = (
        f"Hello {b['user_name']},\n\n"
        f"We have received your booking request for {lab_title}.\n\n"
        f"  Lab  : {lab_title}\n"
        f"  Slot : {_slot_str(b)}\n"
        f"  Ref  : #{b['id']}\n\n"
        f"Your request is pending review. You will receive another email once approved or declined.\n\n"
        + (f"To cancel this request:\n{cancel_url}\n\n" if cancel_url else "")
        + f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(b["user_email"], f"Booking request received — {lab_title}", body)

def notify_admin_new_booking(lab_slug: str, b: Dict, approve_url: str, reject_url: str) -> None:
    if not smtp_ready(): return
    lab_title = LABS[lab_slug]["title"]
    details = ""
    if lab_slug == "furnace":
        details = (
            f"\n  Nanomaterial : {b.get('nanomaterial_type') or '—'}\n"
            f"  Melting pt   : {b.get('melting_point') or '—'}\n"
            f"  Density      : {b.get('material_density') or '—'}\n"
            f"  Anneal temp  : {b.get('anneal_temp_c') or '—'} °C\n"
            f"  Anneal time  : {b.get('anneal_time_h') or '—'} h\n"
            f"  Gas type     : {b.get('gas_type') or '—'}\n"
            f"  Pressure     : {b.get('pressure') or '—'}\n"
            f"  Vacuum       : {b.get('vacuum') or '—'}\n"
        )
    elif lab_slug == "xps":
        details = (
            f"\n  Sample name  : {b.get('sample_name') or '—'}\n"
            f"  Sample count : {b.get('sample_count') or '—'}\n"
            f"  Elements     : {b.get('elements_of_interest') or '—'}\n"
            f"  Analysis     : {b.get('analysis_type') or '—'}\n"
            f"  Charge neut. : {b.get('charge_neutralizer') or '—'}\n"
            f"  Mounting     : {b.get('mounting_method') or '—'}\n"
            f"  Outgassing   : {b.get('outgassing_risk') or '—'}\n"
        )
    body = (
        f"New booking request for {lab_title}.\n\n"
        f"  Name  : {b['user_name']}\n"
        f"  Email : {b['user_email']}\n"
        f"  Slot  : {_slot_str(b)}\n"
        f"  Notes : {b.get('notes') or '—'}\n"
        f"{details}\n"
        f"─────────────────────────────────────\n"
        f"APPROVE:  {approve_url}\n\n"
        f"REJECT:   {reject_url}\n"
        f"─────────────────────────────────────\n\n"
        f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(SMTP_USER, f"[Action required] New booking — {lab_title}", body)

def notify_user_approved(lab_slug: str, b: Dict, note: str = "") -> None:
    if not smtp_ready(): return
    lab_title  = LABS[lab_slug]["title"]
    cancel_url = _cancel_url_for(b)
    note_line  = f"\n  Note from admin: {note}\n" if note else ""
    body = (
        f"Hello {b['user_name']},\n\n"
        f"Your booking for {lab_title} has been approved.\n\n"
        f"  Lab  : {lab_title}\n"
        f"  Slot : {_slot_str(b)}\n"
        f"  Ref  : #{b['id']}\n"
        f"{note_line}\n"
        f"Please arrive on time and follow all lab safety protocols.\n\n"
        + (f"To cancel:\n{cancel_url}\n\n" if cancel_url else "")
        + f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(b["user_email"], f"Booking confirmed — {lab_title}", body)

def notify_user_rejected(lab_slug: str, b: Dict, reason: str = "") -> None:
    if not smtp_ready(): return
    lab_title   = LABS[lab_slug]["title"]
    reason_line = f"\n  Reason : {reason}\n" if reason else ""
    body = (
        f"Hello {b['user_name']},\n\n"
        f"Your booking request for {lab_title} could not be approved.\n\n"
        f"  Lab  : {lab_title}\n"
        f"  Slot : {_slot_str(b)}\n"
        f"  Ref  : #{b['id']}\n"
        f"{reason_line}\n"
        f"Please contact the lab administrator to discuss alternatives.\n\n"
        f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(b["user_email"], f"Booking declined — {lab_title}", body)

def notify_user_cancelled(lab_slug: str, b: Dict) -> None:
    if not smtp_ready(): return
    lab_title = LABS[lab_slug]["title"]
    body = (
        f"Hello {b['user_name']},\n\n"
        f"Your booking for {lab_title} has been cancelled.\n\n"
        f"  Lab  : {lab_title}\n"
        f"  Slot : {_slot_str(b)}\n"
        f"  Ref  : #{b['id']}\n\n"
        f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(b["user_email"], f"Booking cancelled — {lab_title}", body)

def notify_user_reminder(lab_slug: str, b: Dict) -> None:
    if not smtp_ready(): return
    lab_title = LABS[lab_slug]["title"]
    body = (
        f"Hello {b['user_name']},\n\n"
        f"Reminder: you have a booking tomorrow.\n\n"
        f"  Lab  : {lab_title}\n"
        f"  Slot : {_slot_str(b)}\n"
        f"  Ref  : #{b['id']}\n\n"
        f"Please arrive on time and follow all safety protocols.\n\n"
        f"Regards,\n{SMTP_FROM_NAME}\n"
    )
    _send_async(b["user_email"], f"Reminder: booking tomorrow — {lab_title}", body)

# ------------------------------------------------------- Admin auth --------
def _require_admin_vars(lab_slug: str):
    if not ADMIN[lab_slug]["username"] or not ADMIN[lab_slug]["password"]:
        abort(404, description=f"Admin not configured for {lab_slug}.")

def is_admin_for(lab_slug: str) -> bool:
    return session.get("is_admin") is True and session.get("admin_lab") == lab_slug

def require_admin(lab_slug: str):
    _require_admin_vars(lab_slug)
    if not is_admin_for(lab_slug):
        return redirect(url_for("admin_login_lab", lab_slug=lab_slug, next=request.path))
    return None

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

# --------------------------------------------------- Booking URL helper -----
def booking_url_for(lab_slug: str, **params) -> str:
    if lab_slug == "furnace": return url_for("furnace", **params)
    if lab_slug == "xps":     return url_for("xps",     **params)
    return url_for("lab_generic", lab_slug=lab_slug, **params)

# ---------------------------------------------------------------- DB CRUD ---
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

def db_list_bookings_by_email(email: str) -> List[Dict[str, Any]]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bookings WHERE user_email=%s ORDER BY booking_date DESC,start_time DESC",(email,))
                cols = [d.name for d in cur.description]
                return [_row_to_dict_pg(r, cols) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH); conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM bookings WHERE user_email=? ORDER BY booking_date DESC,start_time DESC",(email,)).fetchall()
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

def db_update_booking_time(booking_id: int, new_date: str, new_start: str,
                           new_end: str, updated_by: str) -> None:
    init_db()
    now_iso = datetime.utcnow().isoformat(timespec="seconds")+"Z"
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE bookings SET booking_date=%s::date,start_time=%s::time,"
                            "end_time=%s::time,updated_at=NOW(),updated_by=%s WHERE id=%s",
                            (new_date,new_start,new_end,updated_by,booking_id))
            conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("UPDATE bookings SET booking_date=?,start_time=?,end_time=?,updated_at=?,updated_by=? WHERE id=?",
                     (new_date,new_start,new_end,now_iso,updated_by,booking_id))
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
        rows=conn.execute("SELECT lab_slug,COUNT(*) FROM bookings WHERE status='pending' "
                          "AND (cancelled_at IS NULL OR cancelled_at='') GROUP BY lab_slug").fetchall()
        conn.close()
        return {r[0]:r[1] for r in rows}

# --------------------------------------------------- Slot / form helpers ----
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

def _generate_ical(b: Dict) -> str:
    lab_title = LABS.get(b["lab_slug"],{}).get("title",b["lab_slug"])
    ds = f"{b['booking_date'].replace('-','')}T{b['start_time'][:5].replace(':','')}00"
    de = f"{b['booking_date'].replace('-','')}T{b['end_time'][:5].replace(':','')}00"
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//U2ACN2 Nanolab//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:booking-{b['id']}@u2acn2\r\n"
        f"DTSTART;TZID=Africa/Johannesburg:{ds}\r\n"
        f"DTEND;TZID=Africa/Johannesburg:{de}\r\n"
        f"SUMMARY:{lab_title} booking\r\n"
        f"DESCRIPTION:Booking #{b['id']} for {b['user_name']}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )

# ----------------------------------------------------------------- Routes ---
@app.get("/health")
def health():
    return {"status":"ok"}, 200

@app.route("/")
def index():
    pending = db_pending_counts()
    labs = sorted(
        [{"title":LABS[k]["title"],"slug":k,"subtitle":LABS[k]["subtitle"],
          "booking_url":booking_url_for(k),
          "availability_url":url_for("lab_availability",lab_slug=k),
          "admin_url":url_for("admin_lab",lab_slug=k),
          "pending_count":pending.get(k,0)} for k in LABS],
        key=lambda x: x["title"].lower())
    return render_template("index.html", labs=labs, admin_portal_url=url_for("admin_portal"))

@app.get("/admin")
def admin_portal():
    pending = db_pending_counts()
    order   = sorted(LABS.keys(), key=lambda k: LABS[k]["title"].lower())
    items   = []
    for slug in order:
        cnt   = pending.get(slug, 0)
        badge = f' <span style="color:#ffc107;font-weight:700;">({cnt} pending)</span>' if cnt else ""
        items.append(
            f'<li style="margin:10px 0;"><strong>{LABS[slug]["title"]}</strong>{badge}<br/>'
            f'<a href="{url_for("admin_lab",lab_slug=slug)}">Open admin</a>'
            f' &nbsp;·&nbsp;<a href="{url_for("lab_availability",lab_slug=slug)}">Availability</a>'
            f' &nbsp;·&nbsp;<a href="{booking_url_for(slug)}">Booking</a></li>')
    return (
        '<!doctype html><html><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width,initial-scale=1"/>'
        '<title>Admin Portal</title><link rel="stylesheet" href="/static/style.css"/></head><body>'
        '<header class="topbar"><div class="container"><h1>Admin Portal</h1>'
        '<p class="sub">Select a lab — login required per lab.</p></div></header>'
        f'<main class="container"><div class="card"><ul style="padding-left:18px;">{"".join(items)}</ul>'
        f'<p><a href="{url_for("index")}">← Back to homepage</a></p></div></main></body></html>')

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
    if lab_slug == "furnace": return redirect(url_for("furnace"))
    if lab_slug == "xps":     return redirect(url_for("xps"))
    if lab_slug not in LABS:  abort(404)
    lab_info = {"brand":"iThemba Labs/U2ACN2","title":LABS[lab_slug]["title"],"slug":lab_slug,"administrators":[]}
    if request.method == "POST": return handle_generic_booking(lab_info)
    form  = merge_prefill(default_booking_form(), request.args)
    days  = availability_days(lab_slug)
    rules = {k:LABS[lab_slug].get(k) for k in ("min_notice_hours","max_days_ahead","max_duration_hours")}
    return render_template("lab_generic.html", lab=lab_info, form=form, days=days, rules=rules)

def _fire_booking_emails(lab_slug: str, bid: int, token: str, cancel_token: str):
    b = db_get_booking(bid)
    if not b or not smtp_ready(): return
    notify_user_submission(lab_slug, b)
    notify_admin_new_booking(lab_slug, b,
        approve_url=url_for("approve_booking",     token=token,        _external=True),
        reject_url =url_for("reject_booking_get",  token=token,        _external=True))

def _insert_slots(base: Dict, slots: List[Tuple[str,str,str]]) -> List[int]:
    ids = []
    for d, s, e in slots:
        token  = str(uuid.uuid4())
        ctok   = str(uuid.uuid4())
        p = dict(base); p.update({"booking_date":d,"start_time":s,"end_time":e,
                                   "approval_token":token,"cancel_token":ctok})
        bid = db_insert_booking(p); ids.append(bid)
        _fire_booking_emails(base["lab_slug"], bid, token, ctok)
    return ids

def _insert_single(base: Dict, bd: str, st: str, et: str) -> int:
    token = str(uuid.uuid4()); ctok = str(uuid.uuid4())
    p = dict(base); p.update({"booking_date":bd,"start_time":st,"end_time":et,
                               "approval_token":token,"cancel_token":ctok})
    bid = db_insert_booking(p)
    _fire_booking_emails(base["lab_slug"], bid, token, ctok)
    return bid

def handle_generic_booking(lab_info: Dict):
    lab_slug = lab_info["slug"]
    user_name  = (request.form.get("user_name")  or "").strip()
    user_email = (request.form.get("user_email") or "").strip()
    notes      = (request.form.get("notes")      or "").strip()
    slots      = collect_selected_slots()
    bd = (request.form.get("booking_date") or "").strip()
    st = (request.form.get("start_time")   or "").strip()
    et = (request.form.get("end_time")     or "").strip()
    errors: List[str] = []
    if not user_name:  errors.append("Name is required.")
    if not user_email or "@" not in user_email: errors.append("A valid email is required.")
    if slots:
        for d,s,e in slots:
            errors += check_booking_rules(lab_slug,d,s,e)
            if has_conflict(lab_slug,d,s,e): errors.append(f"Conflict: {d} {s}–{e} already booked.")
    else:
        if not parse_date(bd): errors.append("Please choose a valid date.")
        _st=parse_time(st); _et=parse_time(et)
        if not _st or not _et: errors.append("Please choose valid start/end times.")
        elif _et <= _st:        errors.append("End time must be after start time.")
        if not errors: errors += check_booking_rules(lab_slug,bd,st,et)
        if not errors and has_conflict(lab_slug,bd,st,et): errors.append("Time conflict.")
    if errors:
        for e in errors: flash(e,"error")
        rules={k:LABS[lab_slug].get(k) for k in ("min_notice_hours","max_days_ahead","max_duration_hours")}
        return render_template("lab_generic.html",lab=lab_info,form=request.form,days=availability_days(lab_slug),rules=rules)
    base={"lab_slug":lab_slug,"booking_group_id":str(uuid.uuid4()) if slots else None,
          "user_name":user_name,"user_email":user_email,"notes":notes,
          "status":"pending","updated_at":None,"updated_by":None}
    if slots:
        ids=_insert_slots(base,slots); return redirect(url_for("booking_success",booking_id=ids[-1]))
    else:
        return redirect(url_for("booking_success",booking_id=_insert_single(base,bd,st,et)))

@app.route("/labs/furnace", methods=["GET","POST"])
def furnace():
    lab_info={"brand":"iThemba Labs/U2ACN2","title":LABS["furnace"]["title"],"slug":"furnace",
              "administrators":[{"name":"Dr Itani Madiba","contact":"06598853331"},
                                {"name":"Mr Basil Martin","contact":"0796330278"}]}
    if request.method=="POST": return handle_booking_submit(lab_info,"furnace")
    form=merge_prefill(default_booking_form(),request.args)
    rules={k:LABS["furnace"].get(k) for k in ("min_notice_hours","max_days_ahead","max_duration_hours")}
    return render_template("furnace.html",lab=lab_info,form=form,days=availability_days("furnace"),rules=rules)

@app.route("/labs/xps", methods=["GET","POST"])
def xps():
    lab_info={"brand":"iThemba Labs/U2ACN2","title":LABS["xps"]["title"],"slug":"xps",
              "administrators":[{"name":"Dr Itani Madiba","contact":"06598853331"}]}
    if request.method=="POST": return handle_booking_submit(lab_info,"xps")
    form=merge_prefill(default_booking_form(),request.args)
    rules={k:LABS["xps"].get(k) for k in ("min_notice_hours","max_days_ahead","max_duration_hours")}
    return render_template("xps.html",lab=lab_info,form=form,days=availability_days("xps"),rules=rules)

def handle_booking_submit(lab_info: Dict, kind: str):
    lab_slug   = lab_info["slug"]
    user_name  = (request.form.get("user_name")  or "").strip()
    user_email = (request.form.get("user_email") or "").strip()
    slots      = collect_selected_slots()
    bd=(request.form.get("booking_date") or "").strip()
    st=(request.form.get("start_time")   or "").strip()
    et=(request.form.get("end_time")     or "").strip()
    errors: List[str]=[]
    if not user_name:  errors.append("Name is required.")
    if not user_email or "@" not in user_email: errors.append("A valid email is required.")
    if slots:
        for d,s,e in slots:
            if kind=="furnace" and not is_valid_furnace_block(s,e):
                errors.append(f"Invalid furnace slot: {s}–{e}."); continue
            errors+=check_booking_rules(lab_slug,d,s,e)
            if has_conflict(lab_slug,d,s,e): errors.append(f"Conflict: {d} {s}–{e} already booked.")
    else:
        if not parse_date(bd): errors.append("Please choose a valid date.")
        _st=parse_time(st); _et=parse_time(et)
        if not _st or not _et: errors.append("Please choose valid start/end times.")
        elif _et<=_st:          errors.append("End time must be after start time.")
        if not errors and kind=="furnace" and not is_valid_furnace_block(st,et):
            errors.append("Furnace booking must be 08:00–12:00 or 12:00–16:00.")
        if not errors: errors+=check_booking_rules(lab_slug,bd,st,et)
        if not errors and has_conflict(lab_slug,bd,st,et): errors.append("Time conflict.")
    if errors:
        for e in errors: flash(e,"error")
        rules={k:LABS[lab_slug].get(k) for k in ("min_notice_hours","max_days_ahead","max_duration_hours")}
        tmpl="furnace.html" if kind=="furnace" else "xps.html"
        return render_template(tmpl,lab=lab_info,form=request.form,days=availability_days(lab_slug),rules=rules)
    def _toi(v):
        try: return int((v or "").strip())
        except: return None
    base={"lab_slug":lab_slug,"booking_group_id":str(uuid.uuid4()) if slots else None,
          "user_name":user_name,"user_email":user_email,
          "notes":(request.form.get("notes") or "").strip(),
          "status":"pending","updated_at":None,"updated_by":None}
    if kind=="furnace":
        base.update({"nanomaterial_type":(request.form.get("nanomaterial_type") or "").strip(),
                     "melting_point":(request.form.get("melting_point") or "").strip(),
                     "material_density":(request.form.get("material_density") or "").strip(),
                     "anneal_temp_c":(request.form.get("anneal_temp_c") or "").strip(),
                     "anneal_time_h":(request.form.get("anneal_time_h") or "").strip(),
                     "gas_type":(request.form.get("gas_type") or "").strip(),
                     "pressure":(request.form.get("pressure") or "").strip(),
                     "vacuum":request.form.get("vacuum")=="yes"})
    else:
        base.update({"sample_name":(request.form.get("sample_name") or "").strip(),
                     "sample_count":_toi(request.form.get("sample_count")),
                     "elements_of_interest":(request.form.get("elements_of_interest") or "").strip(),
                     "analysis_type":(request.form.get("analysis_type") or "").strip(),
                     "charge_neutralizer":request.form.get("charge_neutralizer")=="yes",
                     "mounting_method":(request.form.get("mounting_method") or "").strip(),
                     "outgassing_risk":(request.form.get("outgassing_risk") or "").strip()})
    if slots:
        ids=_insert_slots(base,slots); return redirect(url_for("booking_success",booking_id=ids[-1]))
    else:
        return redirect(url_for("booking_success",booking_id=_insert_single(base,bd,st,et)))

@app.route("/bookings/<int:booking_id>")
def booking_success(booking_id: int):
    b = db_get_booking(booking_id)
    if not b: flash("Booking not found.","error"); return redirect(url_for("index"))
    lab_title = LABS.get(b.get("lab_slug",""),{}).get("title",b.get("lab_slug",""))
    return render_template("success.html", b=b, lab_title=lab_title)

@app.get("/bookings/<int:booking_id>/calendar.ics")
def booking_ical(booking_id: int):
    b = db_get_booking(booking_id)
    if not b or b.get("status") != "approved": abort(404)
    resp = make_response(_generate_ical(b))
    resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="booking-{booking_id}.ics"'
    return resp

# -------------------------------------------------------- Booking history ---
@app.route("/my-bookings", methods=["GET","POST"])
def my_bookings():
    bookings=[]; email=""
    if request.method=="POST":
        email=(request.form.get("email") or "").strip().lower()
        if email and "@" in email:
            bookings=db_list_bookings_by_email(email)
            for b in bookings:
                b["lab_title"]=LABS.get(b.get("lab_slug",""),{}).get("title",b.get("lab_slug",""))
        else:
            flash("Please enter a valid email address.","error")
    return render_template("my_bookings.html", bookings=bookings, email=email)

# ----------------------------------------- Recurring booking ----------------
@app.route("/labs/<lab_slug>/recurring", methods=["GET","POST"])
def lab_recurring(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    lab_info={"brand":"iThemba Labs/U2ACN2","title":LABS[lab_slug]["title"],"slug":lab_slug,"administrators":[]}
    if request.method=="GET":
        return render_template("recurring.html",lab=lab_info,form=default_booking_form(),weeks_range=range(1,9))
    user_name =(request.form.get("user_name")  or "").strip()
    user_email=(request.form.get("user_email") or "").strip()
    notes     =(request.form.get("notes")      or "").strip()
    start_date=(request.form.get("start_date") or "").strip()
    start_time=(request.form.get("start_time") or "").strip()
    end_time  =(request.form.get("end_time")   or "").strip()
    weeks     =max(1,min(8,int(request.form.get("weeks","1") or 1)))
    errors: List[str]=[]
    if not user_name:  errors.append("Name is required.")
    if not user_email or "@" not in user_email: errors.append("A valid email is required.")
    sd=parse_date(start_date)
    if not sd: errors.append("Please choose a valid start date.")
    _st=parse_time(start_time); _et=parse_time(end_time)
    if not _st or not _et: errors.append("Please choose valid start/end times.")
    elif _et<=_st:          errors.append("End time must be after start time.")
    good_slots=[]
    if not errors:
        for w in range(weeks):
            d=sd+timedelta(weeks=w)
            if d.weekday()>=5: errors.append(f"Week {w+1}: {d} is a weekend."); continue
            re=check_booking_rules(lab_slug,d.isoformat(),start_time,end_time)
            if re: errors+=re; continue
            if has_conflict(lab_slug,d.isoformat(),start_time,end_time):
                errors.append(f"Conflict on {d}: {start_time}–{end_time} already booked.")
            else:
                good_slots.append((d.isoformat(),start_time,end_time))
    if errors:
        for e in errors: flash(e,"error")
        return render_template("recurring.html",lab=lab_info,form=request.form,weeks_range=range(1,9))
    base={"lab_slug":lab_slug,"booking_group_id":str(uuid.uuid4()),
          "user_name":user_name,"user_email":user_email,"notes":notes,
          "status":"pending","updated_at":None,"updated_by":None}
    ids=_insert_slots(base,good_slots)
    flash(f"Submitted {len(ids)} recurring booking(s) — all pending approval.","ok")
    return redirect(url_for("booking_success",booking_id=ids[-1]))

# ---------------------------------------- One-click approve / reject --------
@app.get("/bookings/approve/<token>")
def approve_booking(token: str):
    b=db_get_booking_by_token(token)
    if not b: return _token_page("Already used","This link has already been used or is invalid.",False)
    lab_slug=b["lab_slug"]
    db_set_booking_status(int(b["id"]),"approved",updated_by="email-link")
    fresh=db_get_booking(int(b["id"]))
    if fresh: notify_user_approved(lab_slug,fresh)
    return _token_page("Booking approved ✓",
        f"Approved for <strong>{b['user_name']}</strong> — {_slot_str(b)}.<br/>"
        f"Confirmation sent to {b['user_email']}.",True,lab_slug=lab_slug)

@app.get("/bookings/reject/<token>")
def reject_booking_get(token: str):
    b=db_get_booking_by_token(token)
    if not b: return _token_page("Already used","This link has already been used or is invalid.",False)
    return render_template("reject_form.html",b=b,
                           lab_title=LABS.get(b["lab_slug"],{}).get("title",b["lab_slug"]),token=token)

@app.post("/bookings/reject/<token>")
def reject_booking_post(token: str):
    b=db_get_booking_by_token(token)
    if not b: return _token_page("Already used","This link has already been used or is invalid.",False)
    reason=(request.form.get("reason") or "").strip()
    lab_slug=b["lab_slug"]
    db_set_booking_status(int(b["id"]),"rejected",rejection_reason=reason,updated_by="email-link")
    notify_user_rejected(lab_slug,b,reason=reason)
    return _token_page("Booking rejected",
        f"Rejected for <strong>{b['user_name']}</strong> — {_slot_str(b)}.<br/>"
        f"User notified at {b['user_email']}.",False,lab_slug=lab_slug)

# ---------------------------------------------- User self-cancellation ------
@app.get("/bookings/cancel/<token>")
def cancel_booking_get(token: str):
    b=db_get_booking_by_cancel_token(token)
    if not b: return _token_page("Invalid link","This cancellation link has already been used or is invalid.",False)
    return render_template("cancel_confirm.html",b=b,
                           lab_title=LABS.get(b["lab_slug"],{}).get("title",b["lab_slug"]),token=token)

@app.post("/bookings/cancel/<token>")
def cancel_booking_post(token: str):
    b=db_get_booking_by_cancel_token(token)
    if not b: return _token_page("Invalid link","This cancellation link has already been used or is invalid.",False)
    db_cancel_booking(int(b["id"]))
    notify_user_cancelled(b["lab_slug"],b)
    return _token_page("Booking cancelled",
        f"Your booking ({_slot_str(b)}) has been cancelled. Confirmation sent to {b['user_email']}.",False)

def _token_page(title: str, message: str, ok: bool, lab_slug: str="") -> str:
    colour=  "ok" if ok else "error"
    admin_lnk=(f'<p><a href="{url_for("admin_lab",lab_slug=lab_slug)}">Go to admin panel</a></p>'
               if lab_slug else "")
    return render_template("token_result.html",title=title,message=message,
                           colour=colour,admin_link=admin_lnk)

# --------------------------------------- Admin panel approve / reject --------
@app.post("/admin/<lab_slug>/approve/<int:booking_id>")
def admin_approve_booking(lab_slug: str, booking_id: int):
    if lab_slug not in LABS: abort(404)
    guard=require_admin(lab_slug)
    if guard: return guard
    b=db_get_booking(booking_id)
    if not b or b.get("lab_slug")!=lab_slug:
        flash("Booking not found.","error"); return redirect(url_for("admin_lab",lab_slug=lab_slug))
    note=(request.form.get("approval_note") or "").strip()
    db_set_booking_status(booking_id,"approved",approval_note=note,updated_by=session.get("admin_username",""))
    fresh=db_get_booking(booking_id)
    if fresh: notify_user_approved(lab_slug,fresh,note=note)
    flash(f"Booking #{booking_id} approved.","ok")
    return redirect(url_for("admin_lab",lab_slug=lab_slug))

@app.post("/admin/<lab_slug>/reject/<int:booking_id>")
def admin_reject_booking(lab_slug: str, booking_id: int):
    if lab_slug not in LABS: abort(404)
    guard=require_admin(lab_slug)
    if guard: return guard
    b=db_get_booking(booking_id)
    if not b or b.get("lab_slug")!=lab_slug:
        flash("Booking not found.","error"); return redirect(url_for("admin_lab",lab_slug=lab_slug))
    reason=(request.form.get("reason") or "").strip()
    db_set_booking_status(booking_id,"rejected",rejection_reason=reason,updated_by=session.get("admin_username",""))
    notify_user_rejected(lab_slug,b,reason=reason)
    flash(f"Booking #{booking_id} rejected.","ok")
    return redirect(url_for("admin_lab",lab_slug=lab_slug))

# ------------------------------------- Admin bulk approve / reject ----------
@app.post("/admin/<lab_slug>/bulk-action")
def admin_bulk_action(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    guard=require_admin(lab_slug)
    if guard: return guard
    action =(request.form.get("action") or "").strip()
    ids    =[int(x) for x in request.form.getlist("booking_ids") if x.isdigit()]
    reason =(request.form.get("bulk_reason") or "").strip()
    actor  = session.get("admin_username","")
    if not ids or action not in ("approve","reject"):
        flash("No bookings selected or invalid action.","error")
        return redirect(url_for("admin_lab",lab_slug=lab_slug))
    count=0
    for bid in ids:
        b=db_get_booking(bid)
        if not b or b.get("lab_slug")!=lab_slug: continue
        status="approved" if action=="approve" else "rejected"
        db_set_booking_status(bid,status,rejection_reason=reason if action=="reject" else "",updated_by=actor)
        fresh=db_get_booking(bid)
        if fresh:
            if action=="approve": notify_user_approved(lab_slug,fresh)
            else:                 notify_user_rejected(lab_slug,fresh,reason=reason)
        count+=1
    flash(f"Bulk {action}d {count} booking(s).","ok")
    return redirect(url_for("admin_lab",lab_slug=lab_slug))

# ----------------------------------------------- Admin per-lab dashboard ---
@app.route("/admin/<lab_slug>")
def admin_lab(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    guard=require_admin(lab_slug)
    if guard: return guard
    rows=db_list_bookings(lab_slug); days=availability_days(lab_slug)
    tmpl=("admin_furnace.html" if lab_slug=="furnace"
          else "admin_xps.html" if lab_slug=="xps"
          else "admin_generic.html")
    return render_template(tmpl,lab_title=LABS[lab_slug]["title"],lab_slug=lab_slug,
                           rows=rows,days=days,admin_username=session.get("admin_username",""),
                           smtp_ready=smtp_ready())

@app.post("/admin/<lab_slug>/reserve")
def admin_reserve_slots(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    guard=require_admin(lab_slug)
    if guard: return guard
    slots  =collect_selected_slots()
    bd=(request.form.get("booking_date") or "").strip()
    st=(request.form.get("start_time")   or "").strip()
    et=(request.form.get("end_time")     or "").strip()
    u_name =(request.form.get("user_name")  or "").strip() or "ADMIN RESERVED"
    u_email=(request.form.get("user_email") or "").strip() or SMTP_USER
    notes  =(request.form.get("notes")      or "").strip() or "Reserved by admin"
    actor  =session.get("admin_username","")
    errors: List[str]=[]
    if slots:
        for d,s,e in slots:
            if lab_slug=="furnace" and not is_valid_furnace_block(s,e):
                errors.append(f"Invalid furnace slot: {s}–{e}.")
            elif has_conflict(lab_slug,d,s,e): errors.append(f"Conflict: {d} {s}–{e} already booked.")
    else:
        if not parse_date(bd): errors.append("Please choose a valid date.")
        _st=parse_time(st); _et=parse_time(et)
        if not _st or not _et: errors.append("Please choose valid start/end times.")
        elif _et<=_st:          errors.append("End time must be after start time.")
        if not errors and lab_slug=="furnace" and not is_valid_furnace_block(st,et):
            errors.append("Furnace booking must be 08:00–12:00 or 12:00–16:00.")
        if not errors and has_conflict(lab_slug,bd,st,et): errors.append("Time conflict.")
    if errors:
        for e in errors: flash(e,"error")
        return redirect(url_for("admin_lab",lab_slug=lab_slug))
    now_iso=datetime.utcnow().isoformat(timespec="seconds")+"Z"
    created=0
    for d,s,e in (slots or [(bd,st,et)]):
        db_insert_booking({"lab_slug":lab_slug,"booking_group_id":str(uuid.uuid4()) if slots else None,
                            "user_name":u_name,"user_email":u_email,"notes":notes,
                            "booking_date":d,"start_time":s,"end_time":e,
                            "status":"approved","approval_token":None,"cancel_token":None,
                            "updated_at":now_iso,"updated_by":actor})
        created+=1
    flash(f"Reserved {created} slot(s).","ok")
    return redirect(url_for("admin_lab",lab_slug=lab_slug))

@app.get("/admin/<lab_slug>/edit/<int:booking_id>")
def admin_edit_booking(lab_slug: str, booking_id: int):
    if lab_slug not in LABS: abort(404)
    guard=require_admin(lab_slug)
    if guard: return guard
    b=db_get_booking(booking_id)
    if not b or b.get("lab_slug")!=lab_slug:
        flash("Booking not found.","error"); return redirect(url_for("admin_lab",lab_slug=lab_slug))
    return render_template("admin_edit.html",lab_slug=lab_slug,lab_title=LABS[lab_slug]["title"],
                           b=b,days=availability_days(lab_slug),smtp_ready=smtp_ready(),
                           admin_username=session.get("admin_username",""))

@app.post("/admin/<lab_slug>/edit/<int:booking_id>")
def admin_update_booking(lab_slug: str, booking_id: int):
    if lab_slug not in LABS: abort(404)
    guard=require_admin(lab_slug)
    if guard: return guard
    b=db_get_booking(booking_id)
    if not b or b.get("lab_slug")!=lab_slug:
        flash("Booking not found.","error"); return redirect(url_for("admin_lab",lab_slug=lab_slug))
    nd=(request.form.get("booking_date") or "").strip()
    ns=(request.form.get("start_time")   or "").strip()
    ne=(request.form.get("end_time")     or "").strip()
    errors: List[str]=[]
    if not parse_date(nd): errors.append("Please choose a valid date.")
    _st=parse_time(ns); _et=parse_time(ne)
    if not _st or not _et: errors.append("Please choose valid start/end times.")
    elif _et<=_st:          errors.append("End time must be after start time.")
    if not errors and has_conflict(lab_slug,nd,ns,ne,exclude_id=booking_id): errors.append("Time conflict.")
    if errors:
        for e in errors: flash(e,"error")
        return redirect(url_for("admin_edit_booking",lab_slug=lab_slug,booking_id=booking_id))
    old=_slot_str(b); new=f"{nd} {ns}–{ne}"
    db_update_booking_time(booking_id,nd,ns,ne,updated_by=session.get("admin_username",""))
    if smtp_ready():
        _send_async(b.get("user_email",""),f"Booking updated: {LABS[lab_slug]['title']}",
                    f"Hello {b.get('user_name')},\n\nYour booking has been rescheduled.\n\n"
                    f"  Old: {old}\n  New: {new}\n\nRegards,\n{SMTP_FROM_NAME}\n")
        flash("Booking updated and user notified.","ok")
    else:
        flash("Booking updated.","ok")
    return redirect(url_for("admin_lab",lab_slug=lab_slug))

@app.post("/admin/<lab_slug>/delete/<int:booking_id>")
def admin_delete_booking(lab_slug: str, booking_id: int):
    if lab_slug not in LABS: abort(404)
    guard=require_admin(lab_slug)
    if guard: return guard
    b=db_get_booking(booking_id)
    if not b or b.get("lab_slug")!=lab_slug:
        flash("Booking not found.","error"); return redirect(url_for("admin_lab",lab_slug=lab_slug))
    db_delete_booking(booking_id)
    if smtp_ready():
        _send_async(b.get("user_email",""),f"Booking cancelled: {LABS[lab_slug]['title']}",
                    f"Hello {b.get('user_name')},\n\nYour booking ({_slot_str(b)}) has been cancelled "
                    f"by the lab administrator.\n\nRegards,\n{SMTP_FROM_NAME}\n")
        flash("Booking deleted and user notified.","ok")
    else:
        flash("Booking deleted.","ok")
    return redirect(url_for("admin_lab",lab_slug=lab_slug))

# ---------------------------------------------------------------- Export ----
def export_rows(lab_slug: str) -> List[Dict]:
    return [{c:r.get(c,"") for c in EXPORT_COLUMNS} for r in db_list_bookings(lab_slug)]

@app.get("/admin/export/<lab_slug>.csv")
def admin_export_csv(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    guard=require_admin(lab_slug)
    if guard: return guard
    si=StringIO(); writer=csv.DictWriter(si,fieldnames=EXPORT_COLUMNS)
    writer.writeheader(); writer.writerows(export_rows(lab_slug))
    resp=make_response(si.getvalue())
    resp.headers["Content-Type"]="text/csv; charset=utf-8"
    resp.headers["Content-Disposition"]=f'attachment; filename="{lab_slug}_bookings.csv"'
    return resp

@app.get("/admin/export/<lab_slug>.xlsx")
def admin_export_xlsx(lab_slug: str):
    if lab_slug not in LABS: abort(404)
    guard=require_admin(lab_slug)
    if guard: return guard
    rows=export_rows(lab_slug); wb=Workbook(); ws=wb.active; ws.title="bookings"
    ws.append(EXPORT_COLUMNS)
    for r in rows: ws.append([r.get(c,"") for c in EXPORT_COLUMNS])
    bio=BytesIO(); wb.save(bio); bio.seek(0)
    resp=make_response(bio.read())
    resp.headers["Content-Type"]="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    resp.headers["Content-Disposition"]=f'attachment; filename="{lab_slug}_bookings.xlsx"'
    return resp

# ---------------------------------------------- Reminder cron endpoint ------
def _run_reminders():
    try:
        init_db()
        for b in db_get_reminder_candidates():
            try:
                notify_user_reminder(b["lab_slug"],b)
                db_mark_reminder_sent(int(b["id"]))
            except Exception:
                pass
    except Exception:
        pass

@app.get("/internal/send-reminders")
def trigger_reminders():
    """Call once daily from a cron job / Render cron service."""
    threading.Thread(target=_run_reminders,daemon=True).start()
    return {"status":"reminders triggered"}, 200

# ---------------------------------------------------------------- Bootstrap -
try:
    init_db()
except Exception:
    pass

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","5000")),debug=True)
