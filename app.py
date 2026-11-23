from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from jinja2 import DictLoader, ChoiceLoader, FileSystemLoader
import sqlite3, os, hashlib, io, traceback
from functools import wraps
from datetime import datetime, timedelta, timezone

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _find_dir(name):
    """Find templates/ or static/ even if repo root layout differs on Render."""
    candidates = [
        os.path.join(_BASE_DIR, name),
        os.path.join(_BASE_DIR, "kgpl_cloud", name),
        os.path.join(os.getcwd(), name),
        os.path.join(os.getcwd(), "kgpl_cloud", name),
        os.path.join(_BASE_DIR, "src", name),
        os.path.join(_BASE_DIR, "src", "kgpl_cloud", name),
    ]
    for d in candidates:
        if os.path.isdir(d):
            # prefer a templates dir that actually has login.html
            if name == "templates" and not os.path.isfile(os.path.join(d, "login.html")):
                continue
            return d
    return os.path.join(_BASE_DIR, name)  # default

_TEMPLATE_DIR = _find_dir("templates")
_STATIC_DIR = _find_dir("static")
print("TEMPLATE_DIR=", _TEMPLATE_DIR, "exists=", os.path.isdir(_TEMPLATE_DIR))
print("STATIC_DIR=", _STATIC_DIR)

app = Flask(__name__, template_folder=_TEMPLATE_DIR, static_folder=_STATIC_DIR)
app.secret_key = os.environ.get("SECRET_KEY", "kgpl-ihb-secret-change-in-production-2026")

# Prefer disk templates; fall back to embedded (fixes Render TemplateNotFound)
try:
    from embedded_templates import TEMPLATES as _EMBEDDED
except Exception:
    _EMBEDDED = {}
_loaders = []
if os.path.isdir(_TEMPLATE_DIR if "_TEMPLATE_DIR" in dir() else os.path.join(_BASE_DIR, "templates")):
    _t = _TEMPLATE_DIR if "_TEMPLATE_DIR" in dir() else os.path.join(_BASE_DIR, "templates")
    _loaders.append(FileSystemLoader(_t))
if _EMBEDDED:
    _loaders.append(DictLoader(_EMBEDDED))
if _loaders:
    app.jinja_loader = ChoiceLoader(_loaders) if len(_loaders) > 1 else _loaders[0]


# India Standard Time
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(IST)

def effective_date(dt=None):
    """Operational day: 22:00 IST to next day 22:00 IST. Returns date string YYYY-MM-DD for the window start day."""
    dt = dt or now_ist()
    if dt.hour >= 22:
        return dt.date().isoformat()
    # before 22:00 belongs to previous calendar day's operational window that started yesterday 22:00
    return (dt.date() - timedelta(days=1)).isoformat()

def effective_window_label(dt=None):
    dt = dt or now_ist()
    ed = effective_date(dt)
    start = datetime.fromisoformat(ed).replace(tzinfo=IST) + timedelta(hours=22)
    # if we're in the window that started "ed" at 22:00
    if dt.hour >= 22:
        start = dt.replace(hour=22, minute=0, second=0, microsecond=0)
    else:
        start = (dt - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.strftime("%d-%b-%Y %H:%M") + " to " + end.strftime("%d-%b-%Y %H:%M") + " IST"

def can_plant_source_modify(dt=None):
    """Plant/Source may modify from 12:00 to 21:00 IST inclusive (until 21:00:59).
    Locked from 21:01 IST to next day 12:00 IST. Master is never locked by this rule."""
    dt = dt or now_ist()
    if dt.hour < 12:
        return False
    if dt.hour < 21:
        return True
    # hour == 21: allow only minute == 0 (21:00:xx); from 21:01 locked
    if dt.hour == 21 and dt.minute == 0:
        return True
    return False

def modification_status():
    dt = now_ist()
    allowed = can_plant_source_modify(dt)
    if allowed:
        return True, "Open for Plant/Source entry until 21:00 IST"
    if dt.hour < 12:
        return False, "Locked until 12:00 IST (Plant/Source cannot modify)"
    return False, "Locked from 21:01 IST until next day 12:00 IST (Plant/Source cannot modify)"



DB = os.environ.get("DATABASE_PATH", os.path.join(_BASE_DIR, "kgpl.db"))

SOURCES = ["Kandla", "Mundra (Mithi Rohar)", "Dumad", "Dahej", "Pipavav", "Bina"]
OMCS = ["IOCL", "BPCL", "HPCL"]

PLANTS = [
    ("IOCL Sanand", "IOCL", "Gujarat"),
    ("HPCL Gandhinagar", "HPCL", "Gujarat"),
    ("BPCL Hariyala", "BPCL", "Gujarat"),
    ("HPCL Indore", "HPCL", "Madhya Pradesh"),
    ("BPCL Pithampur", "BPCL", "Madhya Pradesh"),
    ("IOCL Ujjain", "IOCL", "Madhya Pradesh"),
    ("IOCL Bhopal", "IOCL", "Madhya Pradesh"),
    ("HPCL Bhopal", "HPCL", "Madhya Pradesh"),
    ("BPCL Bhopal", "BPCL", "Madhya Pradesh"),
    ("BPCL Jhansi", "BPCL", "Uttar Pradesh"),
    ("IOCL Kanpur", "IOCL", "Uttar Pradesh"),
    ("BPCL Kanpur", "BPCL", "Uttar Pradesh"),
    ("HPCL Unnao", "HPCL", "Uttar Pradesh"),
    ("IOCL Lucknow", "IOCL", "Uttar Pradesh"),
    ("BPCL Lucknow", "BPCL", "Uttar Pradesh"),
    ("IOCL Allahabad", "IOCL", "Uttar Pradesh"),
    ("BPCL Allahabad", "BPCL", "Uttar Pradesh"),
    ("IOCL Varanasi", "IOCL", "Uttar Pradesh"),
    ("HPCL Varanasi", "HPCL", "Uttar Pradesh"),
    ("IOCL Gorakhpur", "IOCL", "Uttar Pradesh"),
    ("HPCL Gorakhpur", "HPCL", "Uttar Pradesh"),
    ("BPCL Baitalpur", "BPCL", "Uttar Pradesh"),
]

# Distance matrix: plant -> source -> km
DIST = {
    "IOCL Sanand": {"Kandla": 266.748, "Mundra (Mithi Rohar)": 259.361},
    "HPCL Gandhinagar": {"Kandla": 298.845, "Mundra (Mithi Rohar)": 291.458},
    "BPCL Hariyala": {"Kandla": 313.451, "Mundra (Mithi Rohar)": 306.064, "Dumad": 77.617},
    "HPCL Indore": {"Kandla": 731.591, "Mundra (Mithi Rohar)": 724.204, "Pipavav": 671.523, "Dahej": 472.523, "Dumad": 340.523},
    "BPCL Pithampur": {"Kandla": 693.693, "Mundra (Mithi Rohar)": 686.306, "Pipavav": 633.625, "Dahej": 434.625, "Dumad": 302.625},
    "IOCL Ujjain": {"Kandla": 782.932, "Mundra (Mithi Rohar)": 775.544, "Pipavav": 722.863, "Dahej": 523.863, "Dumad": 391.863},
    "IOCL Bhopal": {"Kandla": 879.788, "Mundra (Mithi Rohar)": 872.401, "Pipavav": 819.720, "Dahej": 620.720, "Dumad": 488.720},
    "HPCL Bhopal": {"Kandla": 915.884, "Mundra (Mithi Rohar)": 908.497, "Pipavav": 855.815, "Dahej": 656.815, "Dumad": 524.815},
    "BPCL Bhopal": {"Kandla": 882.148, "Mundra (Mithi Rohar)": 874.760, "Pipavav": 822.079, "Dahej": 623.079, "Dumad": 491.079},
    "BPCL Jhansi": {"Kandla": 1205.799, "Mundra (Mithi Rohar)": 1198.412, "Pipavav": 1145.730, "Dahej": 946.730, "Dumad": 814.730, "Bina": 166.282},
    "IOCL Kanpur": {"Kandla": 1422.727, "Mundra (Mithi Rohar)": 1415.340, "Pipavav": 1362.658, "Dahej": 1163.658, "Dumad": 1031.658, "Bina": 383.210},
    "BPCL Kanpur": {"Kandla": 1428.727, "Mundra (Mithi Rohar)": 1421.340, "Pipavav": 1368.658, "Dahej": 1169.658, "Dumad": 1037.658, "Bina": 389.210},
    "HPCL Unnao": {"Kandla": 1487.768, "Mundra (Mithi Rohar)": 1480.381, "Pipavav": 1427.699, "Dahej": 1228.699, "Dumad": 1096.699, "Bina": 448.251},
    "IOCL Lucknow": {"Kandla": 1529.118, "Mundra (Mithi Rohar)": 1521.731, "Pipavav": 1469.049, "Dahej": 1270.049, "Dumad": 1138.049, "Bina": 489.601},
    "BPCL Lucknow": {"Kandla": 1588.118, "Mundra (Mithi Rohar)": 1580.731, "Pipavav": 1528.049, "Dahej": 1329.049, "Dumad": 1197.049, "Bina": 548.601},
    "IOCL Allahabad": {"Kandla": 1711.158, "Mundra (Mithi Rohar)": 1703.771, "Pipavav": 1651.090, "Dahej": 1452.090, "Dumad": 1320.090, "Bina": 671.641},
    "BPCL Allahabad": {"Kandla": 1725.949, "Mundra (Mithi Rohar)": 1718.562, "Pipavav": 1665.881, "Dahej": 1466.881, "Dumad": 1334.881, "Bina": 686.432},
    "IOCL Varanasi": {"Kandla": 1787.863, "Mundra (Mithi Rohar)": 1780.476, "Pipavav": 1727.794, "Dahej": 1528.794, "Dumad": 1396.794, "Bina": 748.346},
    "HPCL Varanasi": {"Kandla": 1781.286, "Mundra (Mithi Rohar)": 1773.899, "Pipavav": 1721.218, "Dahej": 1522.218, "Dumad": 1390.218, "Bina": 741.769},
    "IOCL Gorakhpur": {"Kandla": 1960.133, "Mundra (Mithi Rohar)": 1952.746, "Pipavav": 1900.064, "Dahej": 1701.064, "Dumad": 1569.064, "Bina": 920.616},
    "HPCL Gorakhpur": {"Kandla": 1964.025, "Mundra (Mithi Rohar)": 1956.638, "Pipavav": 1903.956, "Dahej": 1704.956, "Dumad": 1572.956, "Bina": 924.508},
    "BPCL Baitalpur": {"Kandla": 2021.093, "Mundra (Mithi Rohar)": 2013.706, "Pipavav": 1961.025, "Dahej": 1762.025, "Dumad": 1630.025, "Bina": 981.576},
}

DEFAULT_USERS = [
    ("master", "Master@IHB2026", "master", "Master Admin", None, None),
    ("sanand", "Sanand@IOCL01", "plant", "IOCL Sanand User", "IOCL Sanand", None),
    ("gandhinagar", "Gandhi@HPCL02", "plant", "HPCL Gandhinagar User", "HPCL Gandhinagar", None),
    ("hariyala", "Hariyala@BP03", "plant", "BPCL Hariyala User", "BPCL Hariyala", None),
    ("indore_hp", "Indore@HPCL04", "plant", "HPCL Indore User", "HPCL Indore", None),
    ("pithampur", "Pithampur@BP05", "plant", "BPCL Pithampur User", "BPCL Pithampur", None),
    ("ujjain", "Ujjain@IOCL06", "plant", "IOCL Ujjain User", "IOCL Ujjain", None),
    ("bhopal_iocl", "Bhopal@IOCL07", "plant", "IOCL Bhopal User", "IOCL Bhopal", None),
    ("bhopal_hp", "Bhopal@HPCL08", "plant", "HPCL Bhopal User", "HPCL Bhopal", None),
    ("bhopal_bp", "Bhopal@BPCL09", "plant", "BPCL Bhopal User", "BPCL Bhopal", None),
    ("jhansi", "Jhansi@BPCL10", "plant", "BPCL Jhansi User", "BPCL Jhansi", None),
    ("kanpur_iocl", "Kanpur@IOCL11", "plant", "IOCL Kanpur User", "IOCL Kanpur", None),
    ("kanpur_bp", "Kanpur@BPCL12", "plant", "BPCL Kanpur User", "BPCL Kanpur", None),
    ("unnao", "Unnao@HPCL13", "plant", "HPCL Unnao User", "HPCL Unnao", None),
    ("lucknow_iocl", "Lucknow@IOCL14", "plant", "IOCL Lucknow User", "IOCL Lucknow", None),
    ("lucknow_bp", "Lucknow@BPCL15", "plant", "BPCL Lucknow User", "BPCL Lucknow", None),
    ("allahabad_iocl", "Allahabad@IO16", "plant", "IOCL Allahabad User", "IOCL Allahabad", None),
    ("allahabad_bp", "Allahabad@BP17", "plant", "BPCL Allahabad User", "BPCL Allahabad", None),
    ("varanasi_iocl", "Varanasi@IO18", "plant", "IOCL Varanasi User", "IOCL Varanasi", None),
    ("varanasi_hp", "Varanasi@HP19", "plant", "HPCL Varanasi User", "HPCL Varanasi", None),
    ("gorakhpur_iocl", "Gorakhpur@IO20", "plant", "IOCL Gorakhpur User", "IOCL Gorakhpur", None),
    ("gorakhpur_hp", "Gorakhpur@HP21", "plant", "HPCL Gorakhpur User", "HPCL Gorakhpur", None),
    ("baitalpur", "Baitalpur@BP22", "plant", "BPCL Baitalpur User", "BPCL Baitalpur", None),
    ("kandla", "Kandla@Src01", "source", "Kandla Source User", None, "Kandla"),
    ("mundra", "Mundra@Src02", "source", "Mundra Source User", None, "Mundra (Mithi Rohar)"),
    ("dumad", "Dumad@Src03", "source", "Dumad Source User", None, "Dumad"),
    ("dahej", "Dahej@Src04", "source", "Dahej Source User", None, "Dahej"),
    ("pipavav", "Pipavav@Src05", "source", "Pipavav Source User", None, "Pipavav"),
    ("bina", "Bina@Src06", "source", "Bina Source User", None, "Bina"),
]


def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


# DATABASE_URL = Neon/Postgres connection string. If unset, uses local SQLite.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith("postgres")


class _DB:
    """Thin wrapper: same API for SQLite and Postgres (Neon)."""

    def __init__(self):
        self.pg = USE_PG
        if self.pg:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            url = DATABASE_URL
            # Render/Neon sometimes use postgres:// 
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://"):]
            self.conn = psycopg2.connect(url, sslmode="require")
            self.cur = self.conn.cursor(cursor_factory=RealDictCursor)
        else:
            self.conn = sqlite3.connect(DB, timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.cur = self.conn.cursor()

    def _sql(self, sql):
        if self.pg:
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql, params=None):
        sql = self._sql(sql)
        if params is None:
            self.cur.execute(sql)
        else:
            self.cur.execute(sql, tuple(params))
        return self

    def fetchone(self):
        row = self.cur.fetchone()
        return row

    def fetchall(self):
        return self.cur.fetchall()

    def commit(self):
        self.conn.commit()

    def close(self):
        try:
            self.cur.close()
        except Exception:
            pass
        self.conn.close()

    def scalar(self, sql, params=None):
        row = self.execute(sql, params).fetchone()
        if row is None:
            return None
        if self.pg:
            return list(row.values())[0]
        return row[0]


def get_db():
    return _DB()


def init_db():
    if not USE_PG:
        db_dir = os.path.dirname(DB)
        if db_dir and not os.path.isdir(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    conn = get_db()
    if USE_PG:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, password TEXT NOT NULL, role TEXT NOT NULL,
            name TEXT, plant TEXT, source TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS demands (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL, plant TEXT NOT NULL, omc TEXT NOT NULL,
            source TEXT NOT NULL, req DOUBLE PRECISION NOT NULL, ullage DOUBLE PRECISION DEFAULT 0,
            priority TEXT DEFAULT 'Normal', actual DOUBLE PRECISION, remarks TEXT DEFAULT '',
            created_by TEXT, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS availability (
            id SERIAL PRIMARY KEY,
            source TEXT NOT NULL, omc TEXT NOT NULL, qty DOUBLE PRECISION NOT NULL,
            date TEXT NOT NULL, UNIQUE(source, omc, date))""")
    else:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, password TEXT NOT NULL, role TEXT NOT NULL,
            name TEXT, plant TEXT, source TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS demands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, plant TEXT NOT NULL, omc TEXT NOT NULL,
            source TEXT NOT NULL, req REAL NOT NULL, ullage REAL DEFAULT 0,
            priority TEXT DEFAULT 'Normal', actual REAL, remarks TEXT DEFAULT '',
            created_by TEXT, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, omc TEXT NOT NULL, qty REAL NOT NULL,
            date TEXT NOT NULL, UNIQUE(source, omc, date))""")

    n = conn.scalar("SELECT COUNT(*) FROM users")
    if not n:
        for u in DEFAULT_USERS:
            conn.execute(
                "INSERT INTO users (id, password, role, name, plant, source) VALUES (?,?,?,?,?,?)",
                (u[0], hash_pw(u[1]), u[2], u[3], u[4], u[5]),
            )
    conn.commit()
    conn.close()
    
    # Pumping plan tables
    if USE_PG:
        conn.execute("""CREATE TABLE IF NOT EXISTS pumping_plans (
            id SERIAL PRIMARY KEY,
            plan_date TEXT NOT NULL UNIQUE,
            status TEXT DEFAULT 'draft',
            published_at TEXT,
            created_by TEXT,
            notes TEXT DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pumping_items (
            id SERIAL PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            source TEXT NOT NULL,
            plant TEXT NOT NULL,
            allocation DOUBLE PRECISION NOT NULL,
            flow_rate DOUBLE PRECISION,
            manual_start TEXT,
            start_time TEXT,
            duration_hrs DOUBLE PRECISION,
            completion_time TEXT,
            notes TEXT DEFAULT '',
            demand_id INTEGER
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            role TEXT,
            title TEXT,
            body TEXT,
            created_at TEXT,
            read INTEGER DEFAULT 0
        )""")
    else:
        conn.execute("""CREATE TABLE IF NOT EXISTS pumping_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_date TEXT NOT NULL UNIQUE,
            status TEXT DEFAULT 'draft',
            published_at TEXT,
            created_by TEXT,
            notes TEXT DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pumping_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            source TEXT NOT NULL,
            plant TEXT NOT NULL,
            allocation REAL NOT NULL,
            flow_rate REAL,
            manual_start TEXT,
            start_time TEXT,
            duration_hrs REAL,
            completion_time TEXT,
            notes TEXT DEFAULT '',
            demand_id INTEGER
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            role TEXT,
            title TEXT,
            body TEXT,
            created_at TEXT,
            read INTEGER DEFAULT 0
        )""")

    print("DB ready:", "Postgres/Neon" if USE_PG else "SQLite", flush=True)


def login_required(f):
    @wraps(f)
    def dec(*a, **k):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*a, **k)
    return dec


def current_user():
    if "user_id" not in session:
        return None
    return {
        "id": session["user_id"],
        "role": session["role"],
        "name": session["name"],
        "plant": session.get("plant"),
        "source": session.get("source"),
    }


def tariff_calc(actual, plant, source):
    km = DIST.get(plant, {}).get(source, 0) or 0
    variable = actual * km * 3.95
    fixed = actual * 0.01
    tariff = variable + fixed
    gst = tariff * 0.18
    return km, variable, fixed, tariff, gst, tariff + gst



def parse_dt(s):
    """Parse 'DD-Mon-YYYY HH:MM' or ISO-like to datetime IST-aware naive."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d-%m-%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def fmt_dt(dt):
    if not dt:
        return ""
    return dt.strftime("%d-%b-%Y %H:%M")

def add_hours(dt, hours):
    if not dt:
        return None
    return dt + timedelta(hours=float(hours or 0))

def calc_plan_times(items):
    """items: list of dicts with source, allocation, flow_rate, manual_start.
    Returns same list with start_time, duration_hrs, completion_time filled.
    Per-source sequential: next starts when previous on same source completes,
    unless manual_start overrides.
    """
    last_end = {}  # source -> datetime
    for it in items:
        alloc = float(it.get("allocation") or 0)
        rate = float(it.get("flow_rate") or 0)
        dur = (alloc / rate) if rate > 0 else 0
        it["duration_hrs"] = round(dur, 2)
        man = parse_dt(it.get("manual_start"))
        src = it.get("source") or ""
        if man:
            start = man
        elif src in last_end:
            start = last_end[src]
        else:
            # default window start 22:00 on plan effective logic - use manual empty
            start = None
        end = add_hours(start, dur) if start else None
        it["start_time"] = fmt_dt(start) if start else ""
        it["completion_time"] = fmt_dt(end) if end else ""
        if end:
            last_end[src] = end
    return items

def create_notifications(conn, title, body, roles=None, user_ids=None):
    now = now_ist().isoformat()
    roles = roles or []
    user_ids = user_ids or []
    if roles:
        # notify all users with those roles
        for role in roles:
            rows = conn.execute("SELECT id FROM users WHERE role=?", (role,)).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO notifications (user_id, role, title, body, created_at, read) VALUES (?,?,?,?,?,0)",
                    (r["id"], role, title, body, now),
                )
    for uid in user_ids:
        conn.execute(
            "INSERT INTO notifications (user_id, role, title, body, created_at, read) VALUES (?,?,?,?,?,0)",
            (uid, None, title, body, now),
        )
    conn.commit()

def unread_notifications(user):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM notifications WHERE user_id=? AND read=0 ORDER BY id DESC LIMIT 20",
        (user["id"],),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def master_plan_reminder():
    """True if IST time is 21:30–21:59 and no published plan for effective date."""
    dt = now_ist()
    if dt.hour != 21 or dt.minute < 30:
        return False, None
    ed = effective_date()
    conn = get_db()
    row = conn.execute(
        "SELECT id, status FROM pumping_plans WHERE plan_date=?", (ed,)
    ).fetchone()
    conn.close()
    if row and row["status"] == "published":
        return False, ed
    return True, ed



@app.context_processor
def inject_notes():
    try:
        u = current_user()
        if not u:
            return {"nav_notes": [], "plan_reminder": False, "plan_reminder_date": None}
        notes = unread_notifications(u)
        rem, rd = (False, None)
        if u.get("role") == "master":
            rem, rd = master_plan_reminder()
        return {"nav_notes": notes, "plan_reminder": rem, "plan_reminder_date": rd}
    except Exception:
        return {"nav_notes": [], "plan_reminder": False, "plan_reminder_date": None}


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        uid = (request.form.get("user_id") or "").strip().lower()
        pw = request.form.get("password") or ""
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        conn.close()
        if row and row["password"] == hash_pw(pw):
            session["user_id"] = row["id"]
            session["role"] = row["role"]
            session["name"] = row["name"]
            session["plant"] = row["plant"]
            session["source"] = row["source"]
            if row["role"] == "source":
                return redirect(url_for("availability"))
            if row["role"] == "plant":
                return redirect(url_for("demand"))
            return redirect(url_for("dashboard"))
        flash("Invalid User ID or Password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old = request.form.get("old") or ""
        n1 = request.form.get("new1") or ""
        n2 = request.form.get("new2") or ""
        conn = get_db()
        row = conn.execute("SELECT password FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not row or row["password"] != hash_pw(old):
            flash("Current password is incorrect", "error")
        elif len(n1) < 6:
            flash("New password must be at least 6 characters", "error")
        elif n1 != n2:
            flash("New passwords do not match", "error")
        else:
            conn.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(n1), session["user_id"]))
            conn.commit()
            flash("Password updated successfully", "ok")
            conn.close()
            return redirect(url_for("dashboard"))
        conn.close()
    return render_template("change_password.html", user=current_user())


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    date_f = request.args.get("date") or ""
    conn = get_db()
    q = "SELECT * FROM demands"
    params = []
    if date_f:
        q += " WHERE date=?"
        params.append(date_f)
    rows = conn.execute(q, params).fetchall()
    conn.close()

    tot_req = tot_act = 0
    omc = {o: {"req": 0, "act": 0, "c": 0} for o in OMCS}
    src = {s: {"req": 0, "act": 0, "c": 0} for s in SOURCES}
    tot_tariff = tot_gst = 0
    by_date = {}
    plants = set()

    bill_omc = {o: {"tariff": 0.0, "gst": 0.0, "grand": 0.0} for o in OMCS}

    for d in rows:
        tot_req += d["req"]
        act = d["actual"] or 0
        tot_act += act
        plants.add(d["plant"])
        if d["omc"] in omc:
            omc[d["omc"]]["req"] += d["req"]
            omc[d["omc"]]["act"] += act
            omc[d["omc"]]["c"] += 1
        if d["source"] in src:
            src[d["source"]]["req"] += d["req"]
            src[d["source"]]["act"] += act
            src[d["source"]]["c"] += 1
        if act:
            _, _, _, t, g, _ = tariff_calc(act, d["plant"], d["source"])
            tot_tariff += t
            tot_gst += g
            if d["omc"] in bill_omc:
                bill_omc[d["omc"]]["tariff"] += t
                bill_omc[d["omc"]]["gst"] += g
                bill_omc[d["omc"]]["grand"] += t + g
        dt = d["date"]
        if dt not in by_date:
            by_date[dt] = {"req": 0, "act": 0, "c": 0, "tariff": 0, "gst": 0}
        by_date[dt]["req"] += d["req"]
        by_date[dt]["act"] += act
        by_date[dt]["c"] += 1
        if act:
            _, _, _, t, g, _ = tariff_calc(act, d["plant"], d["source"])
            by_date[dt]["tariff"] += t
            by_date[dt]["gst"] += g

    date_list = sorted(by_date.keys(), reverse=True)
    mod_ok, mod_msg = modification_status()
    return render_template(
        "dashboard.html", user=user, date_f=date_f,
        tot_req=tot_req, tot_act=tot_act, plants=len(plants),
        omc=omc, src=src, tot_tariff=tot_tariff, tot_gst=tot_gst,
        bill_omc=bill_omc,
        by_date=by_date, date_list=date_list, OMCS=OMCS, SOURCES=SOURCES,
        effective_date=effective_date(),
        effective_window=effective_window_label(),
        mod_ok=mod_ok, mod_msg=mod_msg,
        now_ist_str=now_ist().strftime("%d-%b-%Y %H:%M IST"),
    )


@app.route("/availability", methods=["GET", "POST"])
@login_required
def availability():
    user = current_user()
    if user["role"] not in ("master", "source"):
        flash("Access denied", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    if request.method == "POST":
        if user["role"] == "source" and not can_plant_source_modify():
            _, msg = modification_status()
            flash(msg, "error")
            conn.close()
            return redirect(url_for("availability"))
        source = request.form.get("source")
        omc = request.form.get("omc")
        qty = float(request.form.get("qty") or 0)
        date = request.form.get("date") or effective_date()
        if user["role"] == "source" and source != user["source"]:
            flash("You can only set availability for your own source", "error")
        elif source and omc and qty >= 0:
            conn.execute(
                """INSERT INTO availability (source, omc, qty, date) VALUES (?,?,?,?)
                   ON CONFLICT(source, omc, date) DO UPDATE SET qty=excluded.qty""",
                (source, omc, qty, date),
            )
            conn.commit()
            flash("Availability saved", "ok")
        else:
            flash("Please fill all fields", "error")

    date_f = request.args.get("date") or ""
    if user["role"] == "source":
        if date_f:
            rows = conn.execute(
                "SELECT * FROM availability WHERE source=? AND date=? ORDER BY omc",
                (user["source"], date_f),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM availability WHERE source=? ORDER BY date DESC, omc",
                (user["source"],),
            ).fetchall()
        allowed_sources = [user["source"]]
    else:
        if date_f:
            rows = conn.execute(
                "SELECT * FROM availability WHERE date=? ORDER BY source, omc", (date_f,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM availability ORDER BY date DESC, source, omc"
            ).fetchall()
        allowed_sources = SOURCES

    # booked qty
    booked = {}
    dems = conn.execute("SELECT source, omc, date, req FROM demands").fetchall()
    for d in dems:
        k = (d["source"], d["omc"], d["date"])
        booked[k] = booked.get(k, 0) + d["req"]

    # matrix latest
    matrix = {s: {o: 0 for o in OMCS} for s in SOURCES}
    latest = {}
    all_av = conn.execute("SELECT * FROM availability").fetchall()
    for a in all_av:
        k = (a["source"], a["omc"])
        if k not in latest or (a["date"] or "") > (latest[k]["date"] or ""):
            latest[k] = a
    for (s, o), a in latest.items():
        if s in matrix and o in matrix[s]:
            matrix[s][o] = a["qty"]

    conn.close()
    mod_ok, mod_msg = modification_status()
    return render_template(
        "availability.html", user=user, rows=rows, booked=booked,
        matrix=matrix, SOURCES=SOURCES, OMCS=OMCS,
        allowed_sources=allowed_sources, date_f=date_f,
        today=effective_date(),
        mod_ok=mod_ok, mod_msg=mod_msg,
        effective_window=effective_window_label(),
        now_ist_str=now_ist().strftime("%d-%b-%Y %H:%M IST"),
    )


@app.route("/availability/delete/<int:aid>", methods=["POST"])
@login_required
def delete_availability(aid):
    user = current_user()
    if user["role"] != "master":
        flash("Only Master can delete", "error")
        return redirect(url_for("availability"))
    conn = get_db()
    conn.execute("DELETE FROM availability WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    flash("Deleted", "ok")
    return redirect(url_for("availability"))


@app.route("/demand", methods=["GET", "POST"])
@login_required
def demand():
    user = current_user()
    if user["role"] not in ("master", "plant"):
        flash("Access denied", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    if request.method == "POST":
        if user["role"] == "plant" and not can_plant_source_modify():
            _, msg = modification_status()
            flash(msg, "error")
            conn.close()
            return redirect(url_for("demand"))
        plant = request.form.get("plant")
        source = request.form.get("source")
        req = float(request.form.get("req") or 0)
        date = request.form.get("date") or effective_date()
        ullage = float(request.form.get("ullage") or 0)
        priority = request.form.get("priority") or "Normal"
        remarks = request.form.get("remarks") or ""

        if user["role"] == "plant":
            plant = user["plant"]

        plant_info = next((p for p in PLANTS if p[0] == plant), None)
        if not plant_info:
            flash("Invalid plant", "error")
        elif source not in DIST.get(plant, {}):
            flash("Plant not connected to this source", "error")
        elif req <= 0:
            flash("Enter valid required quantity", "error")
        else:
            omc = plant_info[1]
            # availability check
            av = conn.execute(
                "SELECT qty FROM availability WHERE source=? AND omc=? AND date=?",
                (source, omc, date),
            ).fetchone()
            if not av:
                # try latest
                av = conn.execute(
                    "SELECT qty FROM availability WHERE source=? AND omc=? ORDER BY date DESC LIMIT 1",
                    (source, omc),
                ).fetchone()
            if not av:
                flash(f"No product availability set for {source} / {omc}. Set it first.", "error")
            else:
                booked = conn.scalar("SELECT COALESCE(SUM(req),0) FROM demands WHERE source=? AND omc=? AND date=?", (source, omc, date)) or 0
                if booked + req > av["qty"] + 0.0001:
                    bal = av["qty"] - booked
                    flash(
                        f"ERROR: Exceeds availability. Available: {av['qty']} MT | Booked: {booked} MT | Balance: {bal} MT",
                        "error",
                    )
                else:
                    conn.execute(
                        """INSERT INTO demands (date, plant, omc, source, req, ullage, priority, actual, remarks, created_by, created_at)
                           VALUES (?,?,?,?,?,?,?,NULL,?,?,?)""",
                        (date, plant, omc, source, req, ullage, priority, remarks,
                         user["id"], datetime.now().isoformat()),
                    )
                    conn.commit()
                    flash("Demand submitted successfully", "ok")

    date_f = request.args.get("date") or ""
    if user["role"] == "plant":
        if date_f:
            rows = conn.execute(
                "SELECT * FROM demands WHERE plant=? AND date=? ORDER BY id DESC",
                (user["plant"], date_f),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM demands WHERE plant=? ORDER BY date DESC, id DESC",
                (user["plant"],),
            ).fetchall()
        plant_list = [user["plant"]]
    else:
        if date_f:
            rows = conn.execute(
                "SELECT * FROM demands WHERE date=? ORDER BY id DESC", (date_f,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM demands ORDER BY date DESC, id DESC").fetchall()
        plant_list = [p[0] for p in PLANTS]

    conn.close()
    mod_ok, mod_msg = modification_status()
    return render_template(
        "demand.html", user=user, rows=rows, plant_list=plant_list,
        PLANTS=PLANTS, DIST=DIST, date_f=date_f,
        today=effective_date(),
        mod_ok=mod_ok, mod_msg=mod_msg,
        effective_window=effective_window_label(),
        now_ist_str=now_ist().strftime("%d-%b-%Y %H:%M IST"),
    )


@app.route("/demand/actual/<int:did>", methods=["POST"])
@login_required
def set_actual(did):
    user = current_user()
    if user["role"] not in ("master", "plant"):
        flash("No permission", "error")
        return redirect(url_for("demand"))
    if user["role"] == "plant" and not can_plant_source_modify():
        _, msg = modification_status()
        flash(msg, "error")
        return redirect(url_for("demand"))
    actual = request.form.get("actual")
    remarks = (request.form.get("remarks") or "").strip()
    try:
        actual = round(float(actual), 3)
    except Exception:
        flash("Invalid actual quantity", "error")
        return redirect(url_for("demand"))

    conn = get_db()
    row = conn.execute("SELECT * FROM demands WHERE id=?", (did,)).fetchone()
    if not row:
        conn.close()
        flash("Record not found", "error")
        return redirect(url_for("demand"))
    if user["role"] == "plant" and row["plant"] != user["plant"]:
        conn.close()
        flash("You can only update your own plant", "error")
        return redirect(url_for("demand"))
    if actual < row["req"] and not remarks:
        conn.close()
        flash("Actual < Required: Remark is mandatory", "error")
        return redirect(url_for("demand"))
    conn.execute(
        "UPDATE demands SET actual=?, remarks=? WHERE id=?",
        (actual, remarks if remarks else row["remarks"], did),
    )
    conn.commit()
    conn.close()
    flash("Actual quantity saved", "ok")
    return redirect(url_for("demand"))


@app.route("/demand/delete/<int:did>", methods=["POST"])
@login_required
def delete_demand(did):
    if current_user()["role"] != "master":
        flash("Only Master can delete", "error")
        return redirect(url_for("demand"))
    conn = get_db()
    conn.execute("DELETE FROM demands WHERE id=?", (did,))
    conn.commit()
    conn.close()
    flash("Deleted", "ok")
    return redirect(url_for("demand"))


@app.route("/tariff")
@login_required
def tariff():
    user = current_user()
    date_f = request.args.get("date") or ""
    conn = get_db()
    q = "SELECT * FROM demands WHERE actual IS NOT NULL AND actual > 0"
    params = []
    if date_f:
        q += " AND date=?"
        params.append(date_f)
    if user["role"] == "plant":
        q += " AND plant=?"
        params.append(user["plant"])
    q += " ORDER BY date DESC, id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()

    details = []
    omc_map = {o: {"c": 0, "req": 0, "act": 0, "tariff": 0, "gst": 0} for o in OMCS}
    tot_t = tot_g = 0
    for d in rows:
        km, var, fix, t, g, grand = tariff_calc(d["actual"], d["plant"], d["source"])
        details.append({**dict(d), "km": km, "var": var, "fix": fix, "tariff": t, "gst": g, "grand": grand})
        tot_t += t
        tot_g += g
        if d["omc"] in omc_map:
            omc_map[d["omc"]]["c"] += 1
            omc_map[d["omc"]]["req"] += d["req"]
            omc_map[d["omc"]]["act"] += d["actual"]
            omc_map[d["omc"]]["tariff"] += t
            omc_map[d["omc"]]["gst"] += g

    return render_template(
        "tariff.html", user=user, details=details, omc_map=omc_map,
        tot_t=tot_t, tot_g=tot_g, OMCS=OMCS, date_f=date_f,
    )


@app.route("/distance")
@login_required
def distance():
    return render_template(
        "distance.html", user=current_user(), PLANTS=PLANTS,
        SOURCES=SOURCES, DIST=DIST,
    )


@app.route("/masters")
@login_required
def masters():
    user = current_user()
    if user["role"] != "master":
        flash("Master only", "error")
        return redirect(url_for("dashboard"))
    conn = get_db()
    users = conn.execute("SELECT id, role, name, plant, source FROM users ORDER BY role, id").fetchall()
    conn.close()
    return render_template(
        "masters.html", user=user, users=users, PLANTS=PLANTS, SOURCES=SOURCES,
    )


@app.route("/masters/reset-password", methods=["POST"])
@login_required
def reset_password():
    if current_user()["role"] != "master":
        flash("Only Master", "error")
        return redirect(url_for("masters"))
    uid = request.form.get("user_id")
    np = request.form.get("new_pass") or ""
    if not uid or len(np) < 6:
        flash("Invalid password (min 6 chars)", "error")
        return redirect(url_for("masters"))
    conn = get_db()
    conn.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(np), uid))
    conn.commit()
    conn.close()
    flash(f"Password reset for {uid}", "ok")
    return redirect(url_for("masters"))


@app.route("/api/sources/<plant>")
@login_required
def api_sources(plant):
    return jsonify(list(DIST.get(plant, {}).keys()))



@app.route("/report", methods=["GET", "POST"])
@login_required
def report():
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    user = current_user()
    if request.method == "GET":
        return redirect(url_for("dashboard"))
    from_date = request.form.get("from_date") or ""
    to_date = request.form.get("to_date") or ""
    fmt = (request.form.get("format") or "excel").lower()
    if not from_date or not to_date:
        flash("Please select From Date and To Date", "error")
        return redirect(url_for("dashboard"))
    if from_date > to_date:
        flash("From Date cannot be after To Date", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    q = "SELECT * FROM demands WHERE date>=? AND date<=? ORDER BY date, plant, source"
    params = [from_date, to_date]
    if user["role"] == "plant":
        q = "SELECT * FROM demands WHERE date>=? AND date<=? AND plant=? ORDER BY date, source"
        params = [from_date, to_date, user["plant"]]
    rows = conn.execute(q, params).fetchall()
    conn.close()

    # Build data rows
    data = []
    tot_req = tot_act = tot_tariff = tot_gst = 0
    for d in rows:
        act = d["actual"] if d["actual"] is not None else None
        km = var = fix = t = g = grand = None
        if act is not None and act > 0:
            km, var, fix, t, g, grand = tariff_calc(act, d["plant"], d["source"])
            tot_tariff += t
            tot_gst += g
            tot_act += act
        tot_req += d["req"]
        data.append({
            "date": d["date"], "plant": d["plant"], "omc": d["omc"], "source": d["source"],
            "req": d["req"], "actual": act, "priority": d["priority"], "remarks": d["remarks"] or "",
            "km": km, "variable": var, "fixed": fix, "tariff": t, "gst": g, "grand": grand,
        })

    fname_base = f"KGPL_Report_{from_date}_to_{to_date}"

    if fmt == "pdf":
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12*mm, rightMargin=12*mm, topMargin=12*mm, bottomMargin=12*mm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("T", parent=styles["Heading1"], fontSize=14, textColor=colors.HexColor("#1F4E79"))
        story = []
        # IHB logo at top of PDF
        try:
            from reportlab.platypus import Image as RLImage
            logo_path = os.path.join(_BASE_DIR, "static", "ihb_logo.png")
            if not os.path.isfile(logo_path):
                # fallback: write from embedded base64
                try:
                    import base64
                    from logo_b64 import LOGO_B64
                    logo_path = os.path.join("/tmp", "ihb_logo.png")
                    with open(logo_path, "wb") as lf:
                        lf.write(base64.b64decode(LOGO_B64))
                except Exception:
                    logo_path = None
            if logo_path and os.path.isfile(logo_path):
                img = RLImage(logo_path, width=40*mm, height=18*mm)
                story.append(img)
                story.append(Spacer(1, 4))
        except Exception as _logo_err:
            print("logo skip:", _logo_err)
        story.append(Paragraph("KGPL LPG Report — IHB", title_style))
        story.append(Paragraph(f"Period: {from_date} to {to_date} &nbsp;|&nbsp; Generated: {now_ist().strftime('%d-%b-%Y %H:%M IST')}", styles["Normal"]))
        story.append(Spacer(1, 8))
        header = ["Date", "Plant", "OMC", "Source", "Req (MT)", "Actual (MT)", "Tariff", "GST", "Total"]
        table_data = [header]
        for r in data:
            table_data.append([
                r["date"], r["plant"][:18], r["omc"], r["source"][:14],
                f"{r['req']:.2f}",
                f"{r['actual']:.2f}" if r["actual"] is not None else "—",
                f"{r['tariff']:.2f}" if r["tariff"] is not None else "—",
                f"{r['gst']:.2f}" if r["gst"] is not None else "—",
                f"{r['grand']:.2f}" if r["grand"] is not None else "—",
            ])
        table_data.append(["TOTAL", "", "", "", f"{tot_req:.2f}", f"{tot_act:.2f}", f"{tot_tariff:.2f}", f"{tot_gst:.2f}", f"{tot_tariff+tot_gst:.2f}"])
        t = Table(table_data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e8eef5")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))
        doc.build(story)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=fname_base + ".pdf", mimetype="application/pdf")

    # Excel default
    wb = Workbook()
    ws = wb.active
    ws.title = "Demand Report"
    headers = ["Date", "Plant", "OMC", "Source", "Required (MT)", "Actual (MT)", "Priority", "Remarks",
               "Tariff (₹)", "GST 18% (₹)", "Grand Total (₹)"]
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True, size=10)
    thin = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    # IHB logo in Excel
    try:
        from openpyxl.drawing.image import Image as XLImage
        logo_path = os.path.join(_BASE_DIR, "static", "ihb_logo.png")
        if not os.path.isfile(logo_path):
            import base64
            from logo_b64 import LOGO_B64
            logo_path = os.path.join("/tmp", "ihb_logo.png")
            with open(logo_path, "wb") as lf:
                lf.write(base64.b64decode(LOGO_B64))
        if os.path.isfile(logo_path):
            xl_img = XLImage(logo_path)
            xl_img.width = 120
            xl_img.height = 54
            ws.add_image(xl_img, "A1")
            ws.row_dimensions[1].height = 45
            ws.append([])  # row for logo space
    except Exception as _e:
        print("excel logo skip:", _e)
    ws.append(["KGPL LPG Report — IHB"])
    ws.append([f"Period: {from_date} to {to_date}"])
    ws.append([f"Generated: {now_ist().strftime('%d-%b-%Y %H:%M IST')}"])
    ws.append([])
    header_row = ws.max_row + 1
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(header_row, col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for r in data:
        ws.append([
            r["date"], r["plant"], r["omc"], r["source"], r["req"],
            r["actual"] if r["actual"] is not None else "",
            r["priority"], r["remarks"],
            r["km"] if r["km"] is not None else "",
            r["variable"] if r["variable"] is not None else "",
            r["fixed"] if r["fixed"] is not None else "",
            r["tariff"] if r["tariff"] is not None else "",
            r["gst"] if r["gst"] is not None else "",
            r["grand"] if r["grand"] is not None else "",
        ])
    ws.append([])
    ws.append(["TOTAL", "", "", "", tot_req, tot_act, "", "", "", "", "", tot_tariff, tot_gst, tot_tariff + tot_gst])
    # OMC sheet
    ws2 = wb.create_sheet("OMC Summary")
    ws2.append(["OMC", "Required (MT)", "Actual (MT)", "Tariff (₹)", "GST (₹)", "Grand Total (₹)"])
    omc_sum = {o: {"req": 0, "act": 0, "t": 0, "g": 0} for o in OMCS}
    for r in data:
        o = r["omc"]
        if o in omc_sum:
            omc_sum[o]["req"] += r["req"]
            if r["actual"] is not None:
                omc_sum[o]["act"] += r["actual"]
            if r["tariff"] is not None:
                omc_sum[o]["t"] += r["tariff"]
                omc_sum[o]["g"] += r["gst"]
    for o in OMCS:
        s = omc_sum[o]
        ws2.append([o, s["req"], s["act"], s["t"], s["g"], s["t"] + s["g"]])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=fname_base + ".xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")






@app.route("/pumping", methods=["GET", "POST"])
@login_required
def pumping():
    """Master: build / edit pumping plan from daily demands."""
    user = current_user()
    if user["role"] != "master":
        flash("Only Master can edit pumping plan", "error")
        return redirect(url_for("schedule_view"))
    plan_date = request.values.get("date") or effective_date()
    conn = get_db()

    if request.method == "POST":
        action = request.form.get("action") or "save"
        # Load seq rows from form
        seqs = request.form.getlist("seq")
        items = []
        for i, seq in enumerate(seqs):
            items.append({
                "seq": int(seq or i + 1),
                "source": request.form.getlist("source")[i],
                "plant": request.form.getlist("plant")[i],
                "allocation": float(request.form.getlist("allocation")[i] or 0),
                "flow_rate": float(request.form.getlist("flow_rate")[i] or 0),
                "manual_start": request.form.getlist("manual_start")[i] or "",
                "notes": request.form.getlist("notes")[i] if i < len(request.form.getlist("notes")) else "",
                "demand_id": request.form.getlist("demand_id")[i] if i < len(request.form.getlist("demand_id")) else None,
            })
        items = calc_plan_times(items)
        # upsert plan
        plan = conn.execute("SELECT id FROM pumping_plans WHERE plan_date=?", (plan_date,)).fetchone()
        if plan:
            plan_id = plan["id"]
            conn.execute("DELETE FROM pumping_items WHERE plan_id=?", (plan_id,))
        else:
            conn.execute(
                "INSERT INTO pumping_plans (plan_date, status, created_by) VALUES (?,?,?)",
                (plan_date, "draft", user["id"]),
            )
            conn.commit()
            plan = conn.execute("SELECT id FROM pumping_plans WHERE plan_date=?", (plan_date,)).fetchone()
            plan_id = plan["id"]
        for it in items:
            did = it.get("demand_id")
            try:
                did = int(did) if did not in (None, "", "None") else None
            except Exception:
                did = None
            conn.execute(
                """INSERT INTO pumping_items
                   (plan_id, seq, source, plant, allocation, flow_rate, manual_start,
                    start_time, duration_hrs, completion_time, notes, demand_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (plan_id, it["seq"], it["source"], it["plant"], it["allocation"],
                 it["flow_rate"], it["manual_start"], it["start_time"], it["duration_hrs"],
                 it["completion_time"], it.get("notes") or "", did),
            )
        status = "published" if action == "publish" else "draft"
        pub_at = now_ist().isoformat() if action == "publish" else None
        conn.execute(
            "UPDATE pumping_plans SET status=?, published_at=COALESCE(?, published_at) WHERE id=?",
            (status, pub_at, plan_id),
        )
        conn.commit()
        if action == "publish":
            create_notifications(
                conn,
                title="Pumping plan published",
                body=f"Pumping plan for {plan_date} is ready. Kindly check Schedule page.",
                roles=["plant", "source"],
            )
            flash("Plan published. Plants and Sources have been notified.", "ok")
        else:
            flash("Plan saved as draft (times recalculated).", "ok")
        conn.close()
        if action == "pdf":
            return redirect(url_for("pumping_pdf", date=plan_date))
        return redirect(url_for("pumping", date=plan_date))

    # GET: load or seed from demands
    plan = conn.execute("SELECT * FROM pumping_plans WHERE plan_date=?", (plan_date,)).fetchone()
    items = []
    if plan:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM pumping_items WHERE plan_id=? ORDER BY seq", (plan["id"],)
        ).fetchall()]
    if not items:
        # seed from demands for date (aggregate plant+source)
        dems = conn.execute(
            "SELECT id, plant, source, omc, req FROM demands WHERE date=? ORDER BY source, plant",
            (plan_date,),
        ).fetchall()
        # group by plant+source sum req
        key = {}
        for d in dems:
            k = (d["source"], d["plant"])
            if k not in key:
                key[k] = {"allocation": 0, "demand_id": d["id"], "source": d["source"], "plant": d["plant"]}
            key[k]["allocation"] += d["req"] or 0
        seq = 1
        for (source, plant), v in sorted(key.items(), key=lambda x: (x[0][0], x[0][1])):
            items.append({
                "seq": seq, "source": source, "plant": plant,
                "allocation": round(v["allocation"], 3),
                "flow_rate": 70, "manual_start": "", "start_time": "",
                "duration_hrs": "", "completion_time": "", "notes": "",
                "demand_id": v["demand_id"],
            })
            seq += 1
        items = calc_plan_times(items)

    # source totals
    src_tot = {}
    for it in items:
        s = it["source"]
        src_tot[s] = src_tot.get(s, 0) + float(it.get("allocation") or 0)

    need_rem, rem_date = master_plan_reminder()
    conn.close()
    return render_template(
        "pumping.html", user=user, plan_date=plan_date, items=items,
        plan=dict(plan) if plan else None, src_tot=src_tot,
        need_rem=need_rem, rem_date=rem_date,
        SOURCES=SOURCES, PLANTS=PLANTS,
    )


@app.route("/pumping/pdf")
@login_required
def pumping_pdf():
    user = current_user()
    plan_date = request.args.get("date") or effective_date()
    conn = get_db()
    plan = conn.execute("SELECT * FROM pumping_plans WHERE plan_date=?", (plan_date,)).fetchone()
    if not plan:
        conn.close()
        flash("No plan for this date", "error")
        return redirect(url_for("pumping", date=plan_date))
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM pumping_items WHERE plan_id=? ORDER BY seq", (plan["id"],)
    ).fetchall()]
    conn.close()

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10*mm, rightMargin=10*mm, topMargin=10*mm, bottomMargin=10*mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("t", parent=styles["Heading1"], fontSize=13, textColor=colors.HexColor("#1F4E79"), spaceAfter=4)
    story = []
    try:
        logo_path = os.path.join(_BASE_DIR, "static", "ihb_logo.png")
        if os.path.isfile(logo_path):
            story.append(RLImage(logo_path, width=35*mm, height=16*mm))
            story.append(Spacer(1, 3))
    except Exception:
        pass
    story.append(Paragraph("PUMPING SCHEDULE — MASTER PLAN & ALLOCATION", title_style))
    story.append(Paragraph(f"Plan Date: {plan_date} &nbsp;|&nbsp; Status: {plan['status']} &nbsp;|&nbsp; Generated: {now_ist().strftime('%d-%b-%Y %H:%M IST')}", styles["Normal"]))
    story.append(Spacer(1, 6))
    header = ["Seq", "Source", "Plant", "Allocation (MT)", "Flow Rate (MT/hr)", "Manual Start", "Start Time", "Duration (hrs)", "Completion Time", "Notes"]
    data = [header]
    src_tot = {}
    for it in items:
        data.append([
            str(it["seq"]), it["source"], it["plant"],
            f"{float(it['allocation']):.2f}",
            f"{float(it['flow_rate'] or 0):.0f}" if it.get("flow_rate") else "",
            it.get("manual_start") or "",
            it.get("start_time") or "",
            f"{float(it['duration_hrs'] or 0):.2f}" if it.get("duration_hrs") else "",
            it.get("completion_time") or "",
            it.get("notes") or "",
        ])
        src_tot[it["source"]] = src_tot.get(it["source"], 0) + float(it["allocation"] or 0)
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#70AD47")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#E2EFDA")),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))
    sum_data = [["Source", "Total Allocation (MT)"]]
    for s, tot in sorted(src_tot.items()):
        sum_data.append([s, f"{tot:.2f}"])
    sum_data.append(["GRAND TOTAL", f"{sum(src_tot.values()):.2f}"])
    t2 = Table(sum_data)
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story.append(Paragraph("<b>Source-wise totals</b>", styles["Normal"]))
    story.append(Spacer(1, 3))
    story.append(t2)
    doc.build(story)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"Pumping_Schedule_{plan_date}.pdf", mimetype="application/pdf")


@app.route("/schedule")
@login_required
def schedule_view():
    """Plant/Source/Master view of published pumping schedule."""
    user = current_user()
    plan_date = request.args.get("date") or effective_date()
    conn = get_db()
    plan = conn.execute("SELECT * FROM pumping_plans WHERE plan_date=?", (plan_date,)).fetchone()
    items = []
    if plan and (plan["status"] == "published" or user["role"] == "master"):
        q = "SELECT * FROM pumping_items WHERE plan_id=?"
        params = [plan["id"]]
        if user["role"] == "plant" and user.get("plant"):
            q += " AND plant=?"
            params.append(user["plant"])
        if user["role"] == "source" and user.get("source"):
            q += " AND source=?"
            params.append(user["source"])
        q += " ORDER BY seq"
        items = [dict(r) for r in conn.execute(q, params).fetchall()]
    # mark notifications read
    conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (user["id"],))
    conn.commit()
    notes = unread_notifications(user)
    conn.close()
    return render_template(
        "schedule.html", user=user, plan_date=plan_date,
        plan=dict(plan) if plan else None, items=items,
    )


@app.route("/notifications")
@login_required
def notifications():
    user = current_user()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50",
        (user["id"],),
    ).fetchall()
    conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (user["id"],))
    conn.commit()
    conn.close()
    return render_template("notifications.html", user=user, rows=[dict(r) for r in rows])


@app.errorhandler(500)
def internal_error(e):
    tb = traceback.format_exc()
    print(tb)
    # Temporary: show traceback so we can fix the real bug
    safe = tb.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<h1>Internal Server Error</h1>"
        "<p>Copy the text below and send it for fixing:</p>"
        "<pre style=\"white-space:pre-wrap;background:#111;color:#0f0;padding:12px;font-size:12px;\">"
        + safe +
        "</pre>"
        "<p><a href=\"/\">Home</a> | <a href=\"/debug\">Debug</a></p>",
        500,
    )



@app.route("/debug")
def debug_info():
    import sys
    info = {
        "python": sys.version,
        "cwd": os.getcwd(),
        "base_dir": _BASE_DIR if "_BASE_DIR" in dir() else os.path.dirname(os.path.abspath(__file__)),
        "db_path": DB,
        "db_exists": os.path.exists(DB),
        "templates_dir": app.template_folder,
        "template_dir_resolved": globals().get("_TEMPLATE_DIR"),
        "templates_exist": os.path.isdir(app.template_folder),
        "template_files": sorted(os.listdir(app.template_folder)) if os.path.isdir(app.template_folder) else [],
        "files_in_base": sorted(os.listdir(os.path.dirname(os.path.abspath(__file__))))[:50],
    }
    try:
        init_db()
        conn = get_db()
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.close()
        info["users_count"] = n
        info["db_ok"] = True
    except Exception as e:
        info["db_ok"] = False
        info["db_error"] = str(e)
    return jsonify(info)


@app.route("/health")
def health():
    return {"status": "ok", "time": now_ist().isoformat()}


try:
    init_db()
except Exception as e:
    print("WARNING: init_db failed:", e)
    traceback.print_exc()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
