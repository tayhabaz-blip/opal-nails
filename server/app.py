"""Booking server for Nail Studio by Opal Ben Moha.

Customers book without logging in. The server is the only place that knows
names and phone numbers: the public endpoints return busy time ranges only.
Opal manages everything from the admin panel with a password.
"""
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import asyncio
from contextlib import contextmanager, asynccontextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

import sms

log = logging.getLogger("opal")
logging.basicConfig(level=logging.INFO)

TZ = ZoneInfo("Asia/Jerusalem")
DB_PATH = os.environ.get("DB_PATH", "opal.db")
GALLERY_DIR = os.environ.get("GALLERY_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "gallery"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")  # when not set, one is created on the disk at startup
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "https://opal-nails.onrender.com,https://tayhabaz-blip.github.io").split(",") if o.strip()]
REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "7"))  # reminders go out from this hour on the day of the booking
STUDIO = "Nail Studio by Opal Ben Moha"
ADDRESS = "אהרון בן יוסף 27, רמלה"
SITE_URL = os.environ.get("SITE_URL", "https://opal-nails.onrender.com").rstrip("/")

STEP = 15          # slot grid, minutes
MAX_DAYS_AHEAD = 365
TOKEN_DAYS = 30

# The server's own price list. The site shows the same list, but duration and
# price of a booking are always computed here, never taken from the browser.
SERVICES = {
    "anat":    {"name": "מניקור לק ג׳ל מבנה אנטומי", "d": 65,  "p": 150, "f": 0},
    "manirem": {"name": "מניקור + הסרה",               "d": 30,  "p": 90,  "f": 0},
    "feet":    {"name": "לק ג׳ל רגליים",               "d": 60,  "p": 120, "f": 0},
    "feetped": {"name": "לק ג׳ל רגליים + פדיקור כף הרגל ללא סכין", "d": 80, "p": 190, "f": 0},
    "gel":     {"name": "בנייה בג׳ל",                  "d": 120, "p": 330, "f": 1},
    "revtips": {"name": "בנייה בטיפס הפוך",            "d": 120, "p": 380, "f": 0},
    "fill":    {"name": "השלמה",                       "d": 15,  "p": 10,  "f": 0},
}
# Day of week: 0 is Sunday. A day with no entry is closed.
DEFAULT_HOURS = {"0": [8, 17], "1": [8, 18], "2": [8, 17], "3": [8, 18], "4": [8, 17], "5": [8, 13]}

PHONE_RE = re.compile(r"^(0|\+?972)\d{8,9}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ---------------------------------------------------------------- database

_lock = threading.Lock()


def connect():
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


@contextmanager
def tx():
    """One write transaction at a time, so two customers can never take the same slot."""
    with _lock:
        c = connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()


def read():
    return connect()


def load_secret(d):
    """Signing key for admin logins. Kept next to the database so logins survive restarts."""
    global SECRET_KEY
    if SECRET_KEY:
        return
    p = os.path.join(d, "secret.key")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(p, 0o600)
    with open(p) as f:
        SECRET_KEY = f.read().strip()


def init_db():
    d = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(d, exist_ok=True)
    os.makedirs(GALLERY_DIR, exist_ok=True)
    load_secret(d)
    c = connect()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bookings(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, tel TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            services TEXT NOT NULL, date TEXT NOT NULL, start INTEGER NOT NULL, dur INTEGER NOT NULL,
            price INTEGER NOT NULL, from_price INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
            confirm_sent TEXT, reminder_sent TEXT, sms_error TEXT);
        CREATE INDEX IF NOT EXISTS bookings_day ON bookings(date, status);
        CREATE TABLE IF NOT EXISTS waitlist(id TEXT PRIMARY KEY, name TEXT, tel TEXT, date TEXT, services TEXT, dur INTEGER, created_at TEXT);
        CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, n TEXT, t TEXT, approved INTEGER NOT NULL DEFAULT 0, created_at TEXT);
        CREATE TABLE IF NOT EXISTS vacations(id TEXT PRIMARY KEY, start TEXT, end TEXT, note TEXT);
        CREATE TABLE IF NOT EXISTS gallery(id TEXT PRIMARY KEY, cat TEXT, created_at TEXT);
        """)
        c.execute("INSERT OR IGNORE INTO settings VALUES('hours', ?)", (json.dumps(DEFAULT_HOURS),))
        cols = {r["name"] for r in c.execute("PRAGMA table_info(bookings)")}
        if "k" not in cols:  # secret that lets the customer see her own booking
            c.execute("ALTER TABLE bookings ADD COLUMN k TEXT")
    finally:
        c.close()


def get_hours(c):
    return json.loads(c.execute("SELECT v FROM settings WHERE k='hours'").fetchone()["v"])


def get_vacations(c):
    return [dict(r) for r in c.execute("SELECT id, start AS 'from', end AS 'to', note FROM vacations ORDER BY start")]


def new_id(prefix):
    return prefix + secrets.token_hex(6)


def now_il():
    return datetime.now(TZ)


def today_il():
    return now_il().date().isoformat()

# ---------------------------------------------------------------- rate limits

_hits = {}
_hits_lock = threading.Lock()
# Limits for all visitors together. The per-visitor limit depends on the visitor's
# address, which a determined attacker can fake by sending a forged
# X-Forwarded-For header (Render appends to that header and never resets it).
# These caps hold no matter what address is claimed.
GLOBAL_LIMITS = {"login": (30, 900), "book": (150, 3600), "wait": (150, 3600), "rev": (20, 3600)}


def client_ip(req: Request):
    # Cloudflare in front of Render sets CF-Connecting-IP itself and overwrites any
    # value the visitor sent, so it is the trustworthy one when present.
    cf = req.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    xf = req.headers.get("x-forwarded-for", "")
    return (xf.split(",")[0].strip() if xf else (req.client.host if req.client else "?"))


def _take(k, n, per_s, t):
    q = [x for x in _hits.get(k, []) if t - x < per_s]
    if len(q) >= n:
        _hits[k] = q
        return False
    q.append(t)
    _hits[k] = q
    return True


def limit(req: Request, bucket: str, n: int, per_s: int):
    t = time.time()
    with _hits_lock:
        if len(_hits) > 5000:  # forget visitors with no recent hits, so the table cannot grow without end
            for k in [k for k, v in _hits.items() if not v or t - v[-1] > 3600]:
                del _hits[k]
        ok = _take((bucket, client_ip(req)), n, per_s, t)
        if ok and bucket in GLOBAL_LIMITS:
            ok = _take((bucket, "*"), *GLOBAL_LIMITS[bucket], t)
    if not ok:
        raise HTTPException(429, "יש כרגע עומס, נא לנסות שוב בעוד כמה דקות")

# ---------------------------------------------------------------- admin auth


def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token():
    exp = int(time.time()) + TOKEN_DAYS * 86400
    p = f"admin.{exp}"
    return base64.urlsafe_b64encode(f"{p}.{_sign(p)}".encode()).decode()


def check_token(tok: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(tok.encode()).decode()
        who, exp, sig = raw.split(".")
        p = f"{who}.{exp}"
        return who == "admin" and hmac.compare_digest(sig, _sign(p)) and int(exp) > time.time()
    except Exception:
        return False


def admin(req: Request):
    if not SECRET_KEY or not ADMIN_PASSWORD:
        raise HTTPException(503, "הניהול לא הוגדר בשרת")
    h = req.headers.get("authorization", "")
    if not (h.startswith("Bearer ") and check_token(h[7:])):
        raise HTTPException(401, "נא להתחבר מחדש")

# ---------------------------------------------------------------- booking rules


def parse_day(s: str) -> date:
    if not DATE_RE.match(s or ""):
        raise HTTPException(400, "תאריך לא תקין")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, "תאריך לא תקין")


def norm_tel(t: str) -> str:
    t = re.sub(r"[\s\-()]", "", t or "")
    if not PHONE_RE.match(t):
        raise HTTPException(400, "נא להזין מספר טלפון תקין")
    return t


def dow(d: date) -> str:
    return str((d.weekday() + 1) % 7)  # Python Monday=0 -> Sunday=0


def busy(c, day: str, exclude=None):
    rows = c.execute("SELECT id, start, dur FROM bookings WHERE date=? AND status='active'", (day,)).fetchall()
    return [(r["start"], r["start"] + r["dur"]) for r in rows if r["id"] != exclude]


def overlaps(ranges, s, e):
    return any(s < b and a < e for a, b in ranges)


def day_open(c, d: date):
    """Returns (open_min, close_min) or None when closed or on vacation."""
    h = get_hours(c).get(dow(d))
    if not h:
        return None
    ds = d.isoformat()
    if c.execute("SELECT 1 FROM vacations WHERE start<=? AND end>=?", (ds, ds)).fetchone():
        return None
    return h[0] * 60, h[1] * 60


def clean(s, n):
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", (s or "").strip())[:n]


LINKISH = re.compile(r"(https?:|www\.|\w\.(com|co|il|net|org|ly|me|io|link|xyz|ru)\b|@)", re.I)


def clean_name(s):
    """The name goes into the SMS the studio sends, so it must not carry a link
    that would turn the studio's message into someone else's scam."""
    name = clean(s, 60)
    if not name:
        raise HTTPException(400, "נא למלא שם")
    if LINKISH.search(name) or re.search(r"\d{3}", name):
        raise HTTPException(400, "נא להזין שם בלבד")
    return name

# ---------------------------------------------------------------- messages


def hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def ddmm(ds):
    y, m, d = ds.split("-")
    return f"{d}/{m}/{y}"


def my_link(b):
    return f"{SITE_URL}/#t={b['id']}.{b['k']}" if b.get("k") else ""


def confirm_text(b):
    link = my_link(b)
    return (f"שלום {b['name']}, התור שלך ב-{STUDIO} נקבע ואושר: "
            f"{ddmm(b['date'])} בשעה {hhmm(b['start'])}. {ADDRESS}, חניה גדולה ממול. "
            + (f"התור שלך באתר: {link} " if link else "")
            + "לשינוי או ביטול יש לפנות לסטודיו לפחות 24 שעות מראש.")


def reminder_text(b):
    return (f"שלום {b['name']}, תזכורת לתור שלך היום בשעה {hhmm(b['start'])} ב-{STUDIO}, "
            f"{ADDRESS}. חניה גדולה ממול. מחכים לך!")


def send_and_mark(bid, kind):
    """kind is 'confirm' or 'reminder'. Marks the booking whatever happens, so a message is never sent twice."""
    c = read()
    b = c.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
    c.close()
    if not b or b["status"] != "active":
        return
    b = dict(b)
    col = "confirm_sent" if kind == "confirm" else "reminder_sent"
    with tx() as w:
        r = w.execute(f"UPDATE bookings SET {col}=? WHERE id=? AND {col} IS NULL", ("sending", bid))
        if r.rowcount == 0:
            return
    text = confirm_text(b) if kind == "confirm" else reminder_text(b)
    try:
        sms.send(b["tel"], text)
        with tx() as w:
            w.execute(f"UPDATE bookings SET {col}=? WHERE id=?", (now_il().isoformat(timespec="seconds"), bid))
    except Exception as e:  # keep the booking; Opal sees the error in the panel
        log.warning("sms %s failed for %s: %s", kind, bid, e)
        with tx() as w:
            w.execute(f"UPDATE bookings SET {col}=?, sms_error=? WHERE id=?", ("failed", str(e)[:200], bid))


def due_reminders(now: datetime):
    """Bookings that should get today's reminder right now."""
    if now.hour < REMINDER_HOUR:
        return []
    ds = now.date().isoformat()
    nm = now.hour * 60 + now.minute
    c = read()
    rows = c.execute(
        "SELECT id, start, created_at FROM bookings WHERE date=? AND status='active' AND reminder_sent IS NULL",
        (ds,)).fetchall()
    c.close()
    out = []
    for r in rows:
        if r["start"] <= nm:
            continue  # already started
        created = datetime.fromisoformat(r["created_at"])
        if created.date().isoformat() == ds and created.hour >= REMINDER_HOUR:
            continue  # booked this morning after reminder time; the confirmation was enough
        out.append(r["id"])
    return out


async def reminder_loop():
    while True:
        try:
            for bid in due_reminders(now_il()):
                await asyncio.to_thread(send_and_mark, bid, "reminder")
        except Exception as e:
            log.exception("reminder loop: %s", e)
        await asyncio.sleep(60)

# ---------------------------------------------------------------- app


@asynccontextmanager
async def lifespan(app):
    init_db()
    task = asyncio.create_task(reminder_loop()) if os.environ.get("REMINDERS", "1") == "1" else None
    yield
    if task:
        task.cancel()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"],
                   allow_headers=["authorization", "content-type"], max_age=86400)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/public")
def public():
    c = read()
    try:
        return {
            "hours": get_hours(c),
            "vacations": [{"from": v["from"], "to": v["to"]} for v in get_vacations(c)],
            "reviews": [{"id": r["id"], "n": r["n"], "t": r["t"]} for r in
                        c.execute("SELECT * FROM reviews WHERE approved=1 ORDER BY created_at")],
            "gallery": [dict(r) for r in c.execute("SELECT id, cat FROM gallery ORDER BY created_at DESC")],
            "services": SERVICES,
            "today": today_il(),
            "sms": os.environ.get("SMS_PROVIDER", "log") != "log",
        }
    finally:
        c.close()


@app.get("/api/availability")
def availability(start: str, days: int = 7):
    d0 = parse_day(start)
    days = max(1, min(days, 14))
    c = read()
    try:
        return {"busy": {(d0 + timedelta(i)).isoformat(): busy(c, (d0 + timedelta(i)).isoformat())
                         for i in range(days)}}
    finally:
        c.close()


class BookingIn(BaseModel):
    name: str = Field(max_length=80)
    tel: str = Field(max_length=20)
    note: str = Field(default="", max_length=500)
    services: list[str] = Field(max_length=7)
    date: str
    start: int
    terms: bool = False


@app.post("/api/bookings")
async def create_booking(b: BookingIn, req: Request):
    limit(req, "book", 8, 3600)
    name = clean_name(b.name)
    tel = norm_tel(b.tel)
    svc = list(dict.fromkeys(b.services))
    if not svc or any(s not in SERVICES for s in svc):
        raise HTTPException(400, "נא לבחור לפחות טיפול אחד")
    if not b.terms:
        raise HTTPException(400, "נא לאשר את התקנון")
    d = parse_day(b.date)
    now = now_il()
    if d < now.date() or d > now.date() + timedelta(MAX_DAYS_AHEAD):
        raise HTTPException(400, "אי אפשר לקבוע בתאריך הזה")
    dur = sum(SERVICES[s]["d"] for s in svc)
    price = sum(SERVICES[s]["p"] for s in svc)
    frm = int(any(SERVICES[s]["f"] for s in svc))
    if b.start % STEP:
        raise HTTPException(400, "שעה לא תקינה")
    bid = new_id("b")
    bkey = secrets.token_urlsafe(9)
    with tx() as c:
        oc = day_open(c, d)
        if not oc:
            raise HTTPException(409, "הסטודיו סגור ביום הזה")
        if b.start < oc[0] or b.start + dur > oc[1]:
            raise HTTPException(409, "השעה מחוץ לשעות הפעילות")
        if d == now.date() and b.start <= now.hour * 60 + now.minute:
            raise HTTPException(409, "השעה הזו כבר עברה")
        if overlaps(busy(c, d.isoformat()), b.start, b.start + dur):
            raise HTTPException(409, "מצטערים, השעה הזו כבר נתפסה. נא לבחור שעה אחרת.")
        c.execute("INSERT INTO bookings(id,name,tel,note,services,date,start,dur,price,from_price,created_at,k)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (bid, name, tel, clean(b.note, 400), json.dumps([SERVICES[s]["name"] for s in svc], ensure_ascii=False),
                   d.isoformat(), b.start, dur, price, frm, now.isoformat(timespec="seconds"), bkey))
    asyncio.get_running_loop().run_in_executor(None, send_and_mark, bid, "confirm")
    return {"id": bid, "k": bkey, "date": d.isoformat(), "start": b.start, "dur": dur, "price": price, "from": frm}


class MyItem(BaseModel):
    id: str = Field(max_length=20)
    k: str = Field(max_length=40)


class MyIn(BaseModel):
    items: list[MyItem] = Field(max_length=20)


@app.post("/api/my")
def my_bookings(x: MyIn, req: Request):
    """The customer's own bookings. Each one is shown only with the secret the
    customer got when she booked, so nobody can look up someone else's booking."""
    limit(req, "my", 60, 3600)
    out = []
    c = read()
    try:
        for it in x.items:
            r = c.execute("SELECT * FROM bookings WHERE id=?", (it.id,)).fetchone()
            if not r or not r["k"] or not hmac.compare_digest(r["k"], it.k):
                continue
            out.append({"id": r["id"], "k": r["k"], "name": r["name"], "services": json.loads(r["services"]),
                        "date": r["date"], "start": r["start"], "dur": r["dur"], "price": r["price"],
                        "from": r["from_price"], "status": "active" if r["status"] == "active" else "cancelled"})
        return {"bookings": out, "today": today_il()}
    finally:
        c.close()


class WaitIn(BaseModel):
    name: str = Field(max_length=80)
    tel: str = Field(max_length=20)
    date: str
    services: list[str] = Field(max_length=7)


@app.post("/api/waitlist")
def join_waitlist(w: WaitIn, req: Request):
    limit(req, "wait", 6, 3600)
    name = clean_name(w.name)
    tel = norm_tel(w.tel)
    d = parse_day(w.date)
    svc = [s for s in dict.fromkeys(w.services) if s in SERVICES]
    with tx() as c:
        c.execute("INSERT INTO waitlist VALUES(?,?,?,?,?,?,?)",
                  (new_id("w"), name, tel, d.isoformat(),
                   json.dumps([SERVICES[s]["name"] for s in svc], ensure_ascii=False),
                   sum(SERVICES[s]["d"] for s in svc), now_il().isoformat(timespec="seconds")))
    return {"ok": True}


class ReviewIn(BaseModel):
    n: str = Field(max_length=60)
    t: str = Field(max_length=600)
    ok: bool = False


@app.post("/api/reviews")
def send_review(r: ReviewIn, req: Request):
    limit(req, "rev", 3, 3600)
    n, t = clean(r.n, 40), clean(r.t, 400)
    if not n or not t:
        raise HTTPException(400, "נא למלא שם והמלצה")
    if not r.ok:
        raise HTTPException(400, "נא לאשר פרסום ההמלצה")
    with tx() as c:
        c.execute("INSERT INTO reviews(id,n,t,approved,created_at) VALUES(?,?,?,0,?)",
                  (new_id("r"), n, t, now_il().isoformat(timespec="seconds")))
    return {"ok": True}


@app.get("/api/gallery/{gid}")
def gallery_image(gid: str):
    if not re.fullmatch(r"g[0-9a-f]{12}", gid):
        raise HTTPException(404)
    p = os.path.join(GALLERY_DIR, gid + ".jpg")
    if not os.path.exists(p):
        raise HTTPException(404)
    with open(p, "rb") as f:
        return Response(f.read(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})

# ---------------------------------------------------------------- admin endpoints


class LoginIn(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/admin/login")
def login(x: LoginIn, req: Request):
    limit(req, "login", 6, 900)
    if not SECRET_KEY or not ADMIN_PASSWORD:
        raise HTTPException(503, "הניהול לא הוגדר בשרת")
    if not hmac.compare_digest(x.password.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "סיסמה שגויה")
    return {"token": make_token()}


def booking_out(r):
    d = dict(r)
    d["services"] = json.loads(d["services"])
    return d


@app.get("/api/admin/bookings", dependencies=[Depends(admin)])
def admin_bookings():
    c = read()
    try:
        rows = c.execute("SELECT * FROM bookings WHERE date>=? AND status='active' ORDER BY date, start",
                         (today_il(),)).fetchall()
        return {"bookings": [booking_out(r) for r in rows]}
    finally:
        c.close()


class CancelIn(BaseModel):
    late: bool = False


@app.post("/api/admin/bookings/{bid}/cancel", dependencies=[Depends(admin)])
def admin_cancel(bid: str, x: CancelIn):
    with tx() as c:
        r = c.execute("UPDATE bookings SET status=? WHERE id=? AND status='active'",
                      ("late_cancel" if x.late else "cancelled", bid))
        if r.rowcount == 0:
            raise HTTPException(404, "התור לא נמצא")
    return {"ok": True}


class MoveIn(BaseModel):
    date: str
    start: int


@app.post("/api/admin/bookings/{bid}/move", dependencies=[Depends(admin)])
def admin_move(bid: str, x: MoveIn):
    d = parse_day(x.date)
    if not (0 <= x.start < 24 * 60) or x.start % 5:
        raise HTTPException(400, "שעה לא תקינה")
    with tx() as c:
        b = c.execute("SELECT * FROM bookings WHERE id=? AND status='active'", (bid,)).fetchone()
        if not b:
            raise HTTPException(404, "התור לא נמצא")
        if overlaps(busy(c, d.isoformat(), exclude=bid), x.start, x.start + b["dur"]):
            raise HTTPException(409, "השעה החדשה תפוסה")
        same_day = b["date"] == d.isoformat()
        c.execute("UPDATE bookings SET date=?, start=?, reminder_sent=CASE WHEN ? THEN reminder_sent ELSE NULL END WHERE id=?",
                  (d.isoformat(), x.start, same_day, bid))
    return {"ok": True}


@app.get("/api/admin/waitlist", dependencies=[Depends(admin)])
def admin_waitlist():
    c = read()
    try:
        rows = c.execute("SELECT * FROM waitlist WHERE date>=? ORDER BY date, created_at", (today_il(),)).fetchall()
        return {"waitlist": [dict(r, services=json.loads(r["services"])) for r in rows]}
    finally:
        c.close()


@app.delete("/api/admin/waitlist/{wid}", dependencies=[Depends(admin)])
def admin_waitlist_del(wid: str):
    with tx() as c:
        c.execute("DELETE FROM waitlist WHERE id=?", (wid,))
    return {"ok": True}


class HoursIn(BaseModel):
    h: dict[str, list[int]]


@app.put("/api/admin/hours", dependencies=[Depends(admin)])
def admin_hours(x: HoursIn):
    out = {}
    for k, v in x.h.items():
        if k not in {"0", "1", "2", "3", "4", "5", "6"} or len(v) != 2 or not (0 <= v[0] < v[1] <= 24):
            raise HTTPException(400, "שעות לא תקינות")
        out[k] = v
    with tx() as c:
        c.execute("UPDATE settings SET v=? WHERE k='hours'", (json.dumps(out),))
    return {"ok": True, "hours": out}


class VacIn(BaseModel):
    start: str = Field(alias="from")
    end: str = Field(alias="to")
    note: str = Field(default="", max_length=100)


@app.get("/api/admin/vacations", dependencies=[Depends(admin)])
def admin_vac_list():
    c = read()
    try:
        return {"vacations": get_vacations(c)}
    finally:
        c.close()


@app.post("/api/admin/vacations", dependencies=[Depends(admin)])
def admin_vac_add(x: VacIn):
    a, b = parse_day(x.start), parse_day(x.end)
    if b < a:
        raise HTTPException(400, "תאריך הסיום לפני תאריך ההתחלה")
    with tx() as c:
        c.execute("INSERT INTO vacations VALUES(?,?,?,?)", (new_id("v"), a.isoformat(), b.isoformat(), clean(x.note, 100)))
        clash = c.execute("SELECT COUNT(*) n FROM bookings WHERE status='active' AND date BETWEEN ? AND ?",
                          (a.isoformat(), b.isoformat())).fetchone()["n"]
    return {"ok": True, "existing_bookings": clash}


@app.delete("/api/admin/vacations/{vid}", dependencies=[Depends(admin)])
def admin_vac_del(vid: str):
    with tx() as c:
        c.execute("DELETE FROM vacations WHERE id=?", (vid,))
    return {"ok": True}


@app.get("/api/admin/reviews", dependencies=[Depends(admin)])
def admin_reviews():
    c = read()
    try:
        return {"reviews": [dict(r) for r in c.execute("SELECT * FROM reviews ORDER BY approved, created_at")]}
    finally:
        c.close()


class AdminReviewIn(BaseModel):
    n: str = Field(max_length=60)
    t: str = Field(max_length=600)


@app.post("/api/admin/reviews", dependencies=[Depends(admin)])
def admin_review_add(x: AdminReviewIn):
    n, t = clean(x.n, 40), clean(x.t, 400)
    if not n or not t:
        raise HTTPException(400, "נא למלא שם והמלצה")
    with tx() as c:
        c.execute("INSERT INTO reviews(id,n,t,approved,created_at) VALUES(?,?,?,1,?)",
                  (new_id("r"), n, t, now_il().isoformat(timespec="seconds")))
    return {"ok": True}


@app.post("/api/admin/reviews/{rid}/approve", dependencies=[Depends(admin)])
def admin_review_approve(rid: str):
    with tx() as c:
        c.execute("UPDATE reviews SET approved=1 WHERE id=?", (rid,))
    return {"ok": True}


@app.delete("/api/admin/reviews/{rid}", dependencies=[Depends(admin)])
def admin_review_del(rid: str):
    with tx() as c:
        c.execute("DELETE FROM reviews WHERE id=?", (rid,))
    return {"ok": True}


GALLERY_CATS = ["בנייה", "מבנה אנטומי", "פרנץ׳", "עיצובים", "פדיקור"]


@app.post("/api/admin/gallery", dependencies=[Depends(admin)])
async def admin_gallery_add(cat: str = Form(...), file: UploadFile = File(...)):
    from PIL import Image, ImageOps
    if cat not in GALLERY_CATS:
        raise HTTPException(400, "קטגוריה לא מוכרת")
    data = await file.read(12 * 1024 * 1024 + 1)
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(413, "התמונה גדולה מדי")
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im).convert("RGB")  # re-encoding drops location data from the photo
    except Exception:
        raise HTTPException(400, "הקובץ אינו תמונה")
    im.thumbnail((1400, 1400))
    gid = "g" + secrets.token_hex(6)
    im.save(os.path.join(GALLERY_DIR, gid + ".jpg"), "JPEG", quality=84, optimize=True)
    with tx() as c:
        c.execute("INSERT INTO gallery VALUES(?,?,?)", (gid, cat, now_il().isoformat(timespec="seconds")))
    return {"id": gid, "cat": cat}


@app.delete("/api/admin/gallery/{gid}", dependencies=[Depends(admin)])
def admin_gallery_del(gid: str):
    if not re.fullmatch(r"g[0-9a-f]{12}", gid):
        raise HTTPException(404)
    with tx() as c:
        c.execute("DELETE FROM gallery WHERE id=?", (gid,))
    try:
        os.remove(os.path.join(GALLERY_DIR, gid + ".jpg"))
    except FileNotFoundError:
        pass
    return {"ok": True}
