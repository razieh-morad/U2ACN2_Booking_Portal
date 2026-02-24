from __future__ import annotations

import os
import sqlite3
from datetime import datetime, date, time
from typing import Optional, List, Any, Dict

from flask import Flask, render_template, request, redirect, url_for, flash

# Optional Postgres (Neon) support
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.lower().startswith("postgres")

if USE_POSTGRES:
    import psycopg2
    from psycopg2.pool import ThreadedConnectionPool

APP_DIR = os.path.abspath(os.path.dirname(__file__))
SQLITE_PATH = os.path.join(APP_DIR, "bookings.sqlite3")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

_pg_pool: "ThreadedConnectionPool|None" = None
_db_initialized = False


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
                        nanomaterial_type TEXT,
                        melting_point TEXT,
                        material_density TEXT,
                        anneal_temp_c TEXT,
                        anneal_time_h TEXT,
                        gas_type TEXT,
                        pressure TEXT,
                        vacuum BOOLEAN NOT NULL DEFAULT FALSE,
                        notes TEXT,
                        booking_date DATE NOT NULL,
                        start_time TIME NOT NULL,
                        end_time TIME NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_bookings_lab_date ON bookings(lab_slug, booking_date);"
                )
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
        return None


def overlaps(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    return (a_start < b_end) and (a_end > b_start)


def db_fetch_bookings_for_day(lab_slug: str, booking_date: str) -> List[Dict[str, Any]]:
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM bookings
                    WHERE lab_slug = %s AND booking_date = %s::date
                    ORDER BY start_time ASC
                    """,
                    (lab_slug, booking_date),
                )
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM bookings WHERE lab_slug=? AND booking_date=? ORDER BY start_time ASC",
            (lab_slug, booking_date),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


def find_conflicts(lab_slug: str, booking_date: str, start_hhmm: str, end_hhmm: str) -> List[Dict[str, Any]]:
    rows = db_fetch_bookings_for_day(lab_slug, booking_date)
    s = parse_time(start_hhmm)
    e = parse_time(end_hhmm)
    if not s or not e:
        return []
    conflicts = []
    for r in rows:
        rs = r["start_time"]
        re_ = r["end_time"]
        rs_t = rs if isinstance(rs, time) else parse_time(str(rs)[:5])
        re_t = re_ if isinstance(re_, time) else parse_time(str(re_)[:5])
        if rs_t and re_t and overlaps(s, e, rs_t, re_t):
            conflicts.append(r)
    return conflicts


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
                        anneal_temp_c, anneal_time_h, gas_type, pressure, vacuum, notes,
                        booking_date, start_time, end_time
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::date,%s::time,%s::time)
                    RETURNING id
                    """,
                    (
                        payload["lab_slug"], payload["user_name"], payload["user_email"],
                        payload.get("nanomaterial_type"), payload.get("melting_point"), payload.get("material_density"),
                        payload.get("anneal_temp_c"), payload.get("anneal_time_h"), payload.get("gas_type"),
                        payload.get("pressure"), payload.get("vacuum"), payload.get("notes"),
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
                anneal_temp_c, anneal_time_h, gas_type, pressure, vacuum, notes,
                booking_date, start_time, end_time, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["lab_slug"], payload["user_name"], payload["user_email"],
                payload.get("nanomaterial_type"), payload.get("melting_point"), payload.get("material_density"),
                payload.get("anneal_temp_c"), payload.get("anneal_time_h"), payload.get("gas_type"),
                payload.get("pressure"), 1 if payload.get("vacuum") else 0, payload.get("notes"),
                payload["booking_date"], payload["start_time"], payload["end_time"],
                datetime.utcnow().isoformat(timespec="seconds") + "Z"
            ),
        )
        conn.commit()
        booking_id = cur.lastrowid
        conn.close()
        return int(booking_id)


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
                return dict(zip(cols, row))
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()
        return dict(row) if row else None


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
                return [dict(zip(cols, r)) for r in cur.fetchall()]
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
        return [dict(r) for r in rows]


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.get("/warm-db")
def warm_db():
    init_db()
    if USE_POSTGRES:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
        finally:
            _pg_putconn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("SELECT 1;").fetchone()
        conn.close()
    return {"db": "ok"}, 200


@app.route("/")
def index():
    labs = [{"title": "Nanomaterials Furnace", "slug": "furnace", "subtitle": "Carbonate Furnace (iThemba Labs UNISA–UNESCO Chair)"}]
    return render_template("index.html", labs=labs)


@app.route("/labs/furnace", methods=["GET", "POST"])
def furnace():
    lab_info = {
        "institution": "iThemba Labs UNISA–UNESCO Chair",
        "furnace_type": "Carbonate Furnace",
        "administrators": [
            {"name": "Dr Itani Madiba", "contact": "06598853331"},
            {"name": "Mr Basil Martin", "contact": "0796330278"},
        ],
        "title": "Nanomaterials Furnace Processing Lab Form",
    }

    if request.method == "POST":
        user_name = request.form.get("user_name", "").strip()
        user_email = request.form.get("user_email", "").strip()
        booking_date = request.form.get("booking_date", "").strip()
        start_time = request.form.get("start_time", "").strip()
        end_time = request.form.get("end_time", "").strip()

        payload = {
            "lab_slug": "furnace",
            "user_name": user_name,
            "user_email": user_email,
            "nanomaterial_type": request.form.get("nanomaterial_type", "").strip(),
            "melting_point": request.form.get("melting_point", "").strip(),
            "material_density": request.form.get("material_density", "").strip(),
            "anneal_temp_c": request.form.get("anneal_temp_c", "").strip(),
            "anneal_time_h": request.form.get("anneal_time_h", "").strip(),
            "gas_type": request.form.get("gas_type", "").strip(),
            "pressure": request.form.get("pressure", "").strip(),
            "vacuum": True if request.form.get("vacuum") == "yes" else False,
            "notes": request.form.get("notes", "").strip(),
            "booking_date": booking_date,
            "start_time": start_time,
            "end_time": end_time,
        }

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

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("furnace.html", lab=lab_info, form=request.form)

        conflicts = find_conflicts("furnace", booking_date, start_time, end_time)
        if conflicts:
            flash("Time conflict: this slot overlaps an existing booking.", "error")
            return render_template("furnace.html", lab=lab_info, form=request.form, conflicts=conflicts)

        booking_id = db_insert_booking(payload)
        return redirect(url_for("booking_success", booking_id=booking_id))

    return render_template("furnace.html", lab=lab_info, form={})


@app.route("/bookings/<int:booking_id>")
def booking_success(booking_id: int):
    b = db_get_booking(booking_id)
    if not b:
        flash("Booking not found.", "error")
        return redirect(url_for("index"))
    return render_template("success.html", b=b)


@app.route("/admin/bookings/furnace")
def admin_furnace_bookings():
    rows = db_list_bookings("furnace")
    return render_template("admin_bookings.html", rows=rows, lab_title="Nanomaterials Furnace")


try:
    init_db()
except Exception:
    pass


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
