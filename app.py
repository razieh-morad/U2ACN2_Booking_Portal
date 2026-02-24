from __future__ import annotations

import csv
import hmac
import os
import sqlite3
from datetime import datetime, date, time, timedelta
from io import BytesIO, StringIO
from typing import Optional, List, Any, Dict, Tuple
from zoneinfo import ZoneInfo

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    session, abort, make_response
)

# ---------- Config ----------
TZ = ZoneInfo(os.environ.get("APP_TZ", "Africa/Johannesburg"))
WORKDAY_START = time(8, 0)
WORKDAY_END = time(16, 0)  # boundary (last slot ends at 16:00)
SLOT_MINUTES = 60          # availability table resolution

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # REQUIRED in production

# Optional Postgres (Neon) support
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
    "furnace": {
        "title": "Nanomaterials Furnace (Carbonate Furnace)",
        "subtitle": "Carbonate Furnace",
    },
    "xps": {
        "title": "XPS (X-ray Photoelectron Spectroscopy)",
        "subtitle": "Surface chemical analysis",
    },
}

EXPORT_COLUMNS = [
    "id", "lab_slug",
    "user_name", "user_email",
    "booking_date", "start_time", "end_time",
    "nanomaterial_type", "melting_point", "material_density",
    "anneal_temp_c", "anneal_time_h", "gas_type", "pressure", "vacuum",
    "sample_name", "sample_count", "elements_of_interest", "analysis_type",
    "charge_neutralizer", "mounting_method", "outgassing_risk",
    "notes", "created_at",
]


# ---------- DB Helpers ----------
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


def init_db():
    """Create tables if they don't exist (Postgres via Neon or local SQLite)."""
    global _db_initialized
    if _db_initialized:
        return

    if USE_POSTGRES:
        _init_pg_pool()
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bookings (
                        id SERIAL PRIMARY KEY,
                        lab_slug TEXT NOT NULL,
                        user_name TEXT NOT NULL,
                        user_email TEXT NOT NULL,

                        -- furnace optional fields
                        nanomaterial_type TEXT,
                        melting_point TEXT,
                        material_density TEXT,
                        anneal_temp_c TEXT,
                        anneal_time_h TEXT,
                        gas_type TEXT,
                        pressure TEXT,
                        vacuum BOOLEAN NOT NULL DEFAULT FALSE,

                        -- XPS optional fields
                        sample_name TEXT,
                        sample_count INTEGER,
                        elements_of_interest TEXT,
                        analysis_type TEXT,
                        charge_neutralizer BOOLEAN NOT NULL DEFAULT FALSE,
                        mounting_method TEXT,
                        outgassing_risk TEXT,

                        notes TEXT,

                        booking_date DATE NOT NULL,
                        start_time TIME NOT NULL,
                        end_time TIME NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bookings_lab_date ON bookings(lab_slug, booking_date);")
            conn.commit()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lab_slug TEXT NOT NULL,
                user_name TEXT NOT NULL,
                user_email TEXT NOT NULL,
                nanomaterial_type TEXT,
                melting_point TEXT,
                material_density TEXT,
                anneal_temp_c TEXT,
                anneal_time_h TEXT,
                gas_type TEXT,
                pressure TEXT,
                vacuum INTEGER NOT NULL DEFAULT 0,
                sample_name TEXT,
                sample_count INTEGER,
                elements_of_interest TEXT,
                analysis_type TEXT,
                charge_neutralizer INTEGER NOT NULL DEFAULT 0,
                mounting_method TEXT,
                outgassing_risk TEXT,
                notes TEXT,
                booking_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bookings_lab_date ON bookings(lab_slug, booking_date);
            """
        )
        conn.commit()
        conn.close()

    _db_initialized = True


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


def has_conflict(lab_slug: str, booking_date: str, start_hhmm: str, end_hhmm: str) -> bool:
    """Overlap if existing.start < new.end AND existing.end > new.start."""
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM bookings
                    WHERE lab_slug = %s
                      AND booking_date = %s::date
                      AND start_time < %s::time
                      AND end_time > %s::time
                    LIMIT 1
                    """,
                    (lab_slug, booking_date, end_hhmm, start_hhmm),
                )
                return cur.fetchone() is not None
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1
            FROM bookings
            WHERE lab_slug = ?
              AND booking_date = ?
              AND start_time < ?
              AND end_time > ?
            LIMIT 1
            """,
            (lab_slug, booking_date, end_hhmm, start_hhmm),
        )
        hit = cur.fetchone() is not None
        conn.close()
        return hit


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
                cur.execute(
                    "SELECT * FROM bookings WHERE lab_slug=%s ORDER BY booking_date DESC, start_time DESC",
                    (lab_slug,),
                )
                cols = [d.name for d in cur.description]
                return [_row_to_dict_pg(r, cols) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM bookings WHERE lab_slug=? ORDER BY booking_date DESC, start_time DESC",
            (lab_slug,),
        ).fetchall()
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


def db_list_bookings_range_minimal(lab_slug: str, start_d: date, end_d: date) -> List[Dict[str, Any]]:
    """Minimal fields for availability."""
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
                        lab_slug, user_name, user_email,
                        nanomaterial_type, melting_point, material_density,
                        anneal_temp_c, anneal_time_h, gas_type, pressure, vacuum,
                        sample_name, sample_count, elements_of_interest, analysis_type,
                        charge_neutralizer, mounting_method, outgassing_risk,
                        notes,
                        booking_date, start_time, end_time
                    )
                    VALUES (
                        %s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s,%s,%s,
                        %s,%s,%s,%s,
                        %s,%s,%s,
                        %s,
                        %s::date,%s::time,%s::time
                    )
                    RETURNING id
                    """,
                    (
                        payload["lab_slug"], payload["user_name"], payload["user_email"],
                        payload.get("nanomaterial_type"), payload.get("melting_point"), payload.get("material_density"),
                        payload.get("anneal_temp_c"), payload.get("anneal_time_h"), payload.get("gas_type"),
                        payload.get("pressure"), bool(payload.get("vacuum")),
                        payload.get("sample_name"), payload.get("sample_count"), payload.get("elements_of_interest"),
                        payload.get("analysis_type"), bool(payload.get("charge_neutralizer")),
                        payload.get("mounting_method"), payload.get("outgassing_risk"),
                        payload.get("notes"),
                        payload["booking_date"], payload["start_time"], payload["end_time"],
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
                lab_slug, user_name, user_email,
                nanomaterial_type, melting_point, material_density,
                anneal_temp_c, anneal_time_h, gas_type, pressure, vacuum,
                sample_name, sample_count, elements_of_interest, analysis_type,
                charge_neutralizer, mounting_method, outgassing_risk,
                notes,
                booking_date, start_time, end_time, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["lab_slug"], payload["user_name"], payload["user_email"],
                payload.get("nanomaterial_type"), payload.get("melting_point"), payload.get("material_density"),
                payload.get("anneal_temp_c"), payload.get("anneal_time_h"), payload.get("gas_type"),
                payload.get("pressure"), 1 if payload.get("vacuum") else 0,
                payload.get("sample_name"), payload.get("sample_count"), payload.get("elements_of_interest"),
                payload.get("analysis_type"), 1 if payload.get("charge_neutralizer") else 0,
                payload.get("mounting_method"), payload.get("outgassing_risk"),
                payload.get("notes"),
                payload["booking_date"], payload["start_time"], payload["end_time"],
                datetime.utcnow().isoformat(timespec="seconds") + "Z"
            ),
        )
        conn.commit()
        booking_id = cur.lastrowid
        conn.close()
        return int(booking_id)


# ---------- Availability ----------
def iter_workdays(start_d: date, end_d: date):
    d = start_d
    while d <= end_d:
        if d.weekday() < 5:  # Mon-Fri
            yield d
        d = d + timedelta(days=1)


def build_slots_for_day(d: date) -> List[Tuple[time, time]]:
    slots = []
    t0 = datetime.combine(d, WORKDAY_START)
    t1 = datetime.combine(d, WORKDAY_END)
    cur = t0
    while cur < t1:
        nxt = cur + timedelta(minutes=SLOT_MINUTES)
        slots.append((cur.time(), nxt.time()))
        cur = nxt
    return slots


def normalize_booking_time(v: Any) -> time:
    if isinstance(v, time):
        return v
    return parse_time(str(v)) or time(0, 0)


def is_slot_free(bookings: List[Dict[str, Any]], d: date, s: time, e: time) -> bool:
    for b in bookings:
        bd = b["booking_date"]
        bd_date = bd if isinstance(bd, date) else (parse_date(str(bd)) or date.min)
        if bd_date != d:
            continue
        bs = normalize_booking_time(b["start_time"])
        be = normalize_booking_time(b["end_time"])
        if overlaps(s, e, bs, be):
            return False
    return True


def next_two_weeks_window() -> Tuple[date, date]:
    today = datetime.now(TZ).date()
    end = today + timedelta(days=13)
    return today, end


# ---------- UI Defaults ----------
def default_booking_form() -> Dict[str, str]:
    now = datetime.now(TZ)
    start = now.strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")
    return {
        "booking_date": now.date().isoformat(),
        "start_time": start,
        "end_time": end,
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


# ---------- Admin Auth ----------
def is_admin() -> bool:
    return bool(session.get("is_admin") is True)


def require_admin():
    if not ADMIN_PASSWORD:
        abort(500, description="ADMIN_PASSWORD is not set on the server.")
    if not is_admin():
        return redirect(url_for("admin_login", next=request.path))
    return None


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        abort(500, description="ADMIN_PASSWORD is not set on the server.")
    if request.method == "POST":
        user = (request.form.get("username") or "").strip()
        pw = (request.form.get("password") or "").strip()
        if user == ADMIN_USER and hmac.compare_digest(pw, ADMIN_PASSWORD):
            session["is_admin"] = True
            nxt = request.form.get("next") or url_for("index")
            return redirect(nxt)
        flash("Invalid credentials.", "error")
        return render_template("admin_login.html", next=request.form.get("next") or url_for("index"))
    return render_template("admin_login.html", next=request.args.get("next") or url_for("index"))


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


# ---------- Common ----------
@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/")
def index():
    labs = [
        {"title": LABS["furnace"]["title"], "slug": "furnace", "subtitle": LABS["furnace"]["subtitle"]},
        {"title": LABS["xps"]["title"], "slug": "xps", "subtitle": LABS["xps"]["subtitle"]},
    ]
    return render_template("index.html", labs=labs)


def render_availability(lab_slug: str):
    start_d, end_d = next_two_weeks_window()
    bookings = db_list_bookings_range_minimal(lab_slug, start_d, end_d)

    days = []
    for d in iter_workdays(start_d, end_d):
        slots = []
        for s, e in build_slots_for_day(d):
            slots.append({
                "start": s.strftime("%H:%M"),
                "end": e.strftime("%H:%M"),
                "free": is_slot_free(bookings, d, s, e),
                "date": d.isoformat(),
            })
        days.append({"date": d, "slots": slots})

    title = LABS[lab_slug]["title"]
    return render_template("availability.html", lab_slug=lab_slug, lab_title=title, days=days)


@app.route("/labs/<lab_slug>/availability")
def lab_availability(lab_slug: str):
    if lab_slug not in LABS:
        abort(404)
    return render_availability(lab_slug)


# ---------- Furnace ----------
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
    return render_template("furnace.html", lab=lab_info, form=form)


# ---------- XPS ----------
@app.route("/labs/xps", methods=["GET", "POST"])
def xps():
    lab_info = {
        "brand": "iThemba Labs/U2ACN2",
        "title": LABS["xps"]["title"],
        "slug": "xps",
        "administrators": [
            {"name": "Dr Itani Madiba", "contact": "06598853331"},
        ],
    }
    if request.method == "POST":
        return handle_booking_submit(lab_info, kind="xps")
    form = merge_prefill(default_booking_form(), request.args)
    return render_template("xps.html", lab=lab_info, form=form)


def handle_booking_submit(lab_info: Dict[str, Any], kind: str):
    lab_slug = lab_info["slug"]
    user_name = (request.form.get("user_name") or "").strip()
    user_email = (request.form.get("user_email") or "").strip()
    booking_date = (request.form.get("booking_date") or "").strip()
    start_time = (request.form.get("start_time") or "").strip()
    end_time = (request.form.get("end_time") or "").strip()

    errors = []
    if not user_name:
        errors.append("Name is required.")
    if not user_email or "@" not in user_email:
        errors.append("A valid email is required.")
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
        if kind == "furnace":
            return render_template("furnace.html", lab=lab_info, form=request.form)
        return render_template("xps.html", lab=lab_info, form=request.form)

    payload: Dict[str, Any] = {
        "lab_slug": lab_slug,
        "user_name": user_name,
        "user_email": user_email,
        "booking_date": booking_date,
        "start_time": start_time,
        "end_time": end_time,
        "notes": (request.form.get("notes") or "").strip(),
    }

    if kind == "furnace":
        payload.update({
            "nanomaterial_type": (request.form.get("nanomaterial_type") or "").strip(),
            "melting_point": (request.form.get("melting_point") or "").strip(),
            "material_density": (request.form.get("material_density") or "").strip(),
            "anneal_temp_c": (request.form.get("anneal_temp_c") or "").strip(),
            "anneal_time_h": (request.form.get("anneal_time_h") or "").strip(),
            "gas_type": (request.form.get("gas_type") or "").strip(),
            "pressure": (request.form.get("pressure") or "").strip(),
            "vacuum": True if (request.form.get("vacuum") == "yes") else False,
        })
    else:
        def _to_int(v: str) -> Optional[int]:
            v = (v or "").strip()
            if not v:
                return None
            try:
                return int(v)
            except Exception:
                return None

        payload.update({
            "sample_name": (request.form.get("sample_name") or "").strip(),
            "sample_count": _to_int(request.form.get("sample_count") or ""),
            "elements_of_interest": (request.form.get("elements_of_interest") or "").strip(),
            "analysis_type": (request.form.get("analysis_type") or "").strip(),
            "charge_neutralizer": True if (request.form.get("charge_neutralizer") == "yes") else False,
            "mounting_method": (request.form.get("mounting_method") or "").strip(),
            "outgassing_risk": (request.form.get("outgassing_risk") or "").strip(),
        })

    booking_id = db_insert_booking(payload)
    return redirect(url_for("booking_success", booking_id=booking_id))


@app.route("/bookings/<int:booking_id>")
def booking_success(booking_id: int):
    b = db_get_booking(booking_id)
    if not b:
        flash("Booking not found.", "error")
        return redirect(url_for("index"))
    return render_template("success.html", b=b)


# ---------- Admin ----------
@app.route("/admin/bookings/<lab_slug>")
def admin_bookings(lab_slug: str):
    if lab_slug not in LABS:
        abort(404)
    guard = require_admin()
    if guard is not None:
        return guard

    rows = db_list_bookings(lab_slug)
    lab_title = LABS[lab_slug]["title"]
    return render_template("admin_bookings.html", rows=rows, lab_title=lab_title, lab_slug=lab_slug)


@app.post("/admin/bookings/<lab_slug>/quickbook")
def admin_quickbook(lab_slug: str):
    if lab_slug not in LABS:
        abort(404)
    guard = require_admin()
    if guard is not None:
        return guard

    user_name = (request.form.get("user_name") or "").strip() or "ADMIN"
    user_email = (request.form.get("user_email") or "").strip() or "admin@local"
    booking_date = (request.form.get("booking_date") or "").strip()
    start_time = (request.form.get("start_time") or "").strip()
    end_time = (request.form.get("end_time") or "").strip()
    notes = (request.form.get("notes") or "").strip()

    errors = []
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
        return redirect(url_for("admin_bookings", lab_slug=lab_slug))

    booking_id = db_insert_booking({
        "lab_slug": lab_slug,
        "user_name": user_name,
        "user_email": user_email,
        "booking_date": booking_date,
        "start_time": start_time,
        "end_time": end_time,
        "notes": notes,
    })
    flash(f"Admin booking created (ID {booking_id}).", "ok")
    return redirect(url_for("admin_bookings", lab_slug=lab_slug))


def _export_rows(lab_slug: str) -> List[Dict[str, Any]]:
    rows = db_list_bookings(lab_slug)
    out = []
    for r in rows:
        out.append({c: r.get(c, "") for c in EXPORT_COLUMNS})
    return out


@app.get("/admin/export/<lab_slug>.csv")
def admin_export_csv(lab_slug: str):
    if lab_slug not in LABS:
        abort(404)
    guard = require_admin()
    if guard is not None:
        return guard

    rows = _export_rows(lab_slug)
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
    guard = require_admin()
    if guard is not None:
        return guard

    rows = _export_rows(lab_slug)
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


# Initialize DB when module is imported (gunicorn)
try:
    init_db()
except Exception:
    pass


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
