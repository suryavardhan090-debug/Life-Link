"""LifeLink backend: FastAPI + SQLite.

Run:  uvicorn main:app --reload      (from the backend/ folder)
Docs: http://localhost:8000/docs
"""
import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

BASE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("LIFELINK_DB", str(BASE / "lifelink.db"))
FRONTEND_DIR = BASE.parent / "frontend"
# Set LIFELINK_REQUIRE_AUTH=1 to force hospital login on hospital endpoints.
# Default is off so the hackathon demo works straight from the frontend.
REQUIRE_AUTH = os.environ.get("LIFELINK_REQUIRE_AUTH", "0") == "1"

GROUPS = ["A+", "A-", "B+", "B-", "O+", "O-", "AB+", "AB-"]
# patient blood group -> donor groups that are compatible (red cells)
COMPAT = {
    "O-": ["O-"],
    "O+": ["O+", "O-"],
    "A-": ["A-", "O-"],
    "A+": ["A+", "A-", "O+", "O-"],
    "B-": ["B-", "O-"],
    "B+": ["B+", "B-", "O+", "O-"],
    "AB-": ["AB-", "A-", "B-", "O-"],
    "AB+": ["AB+", "AB-", "A+", "A-", "B+", "B-", "O+", "O-"],
}
RARE = {"AB-", "B-", "A-", "O-"}
AVAILABILITY = ["Available now", "Available after selected time", "Busy", "Unavailable"]
URGENCY = ["CRITICAL", "URGENT", "SCHEDULED"]
MAX_SEARCH_KM = 25
MIN_GAP_DAYS = 90  # minimum days between whole-blood donations
SESSION_HOURS = 24  # hospital login lifetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS hospitals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL, salt TEXT NOT NULL,
  phone TEXT, address TEXT, city TEXT, lat REAL, lng REAL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS donors(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE, email TEXT NOT NULL,
  blood_group TEXT NOT NULL, city TEXT NOT NULL, area TEXT,
  lat REAL, lng REAL,
  availability TEXT NOT NULL DEFAULT 'Available now',
  preferred_time TEXT DEFAULT 'Any time',
  last_donated TEXT,
  total_donations INTEGER NOT NULL DEFAULT 0,
  alerts_accepted INTEGER NOT NULL DEFAULT 0,
  alerts_total INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_donors_bg ON donors(blood_group);
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
  blood_group TEXT NOT NULL, units INTEGER NOT NULL, urgency TEXT NOT NULL,
  radius_km INTEGER NOT NULL, context TEXT, notes TEXT, required_by TEXT,
  rare_mode INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS request_donors(
  request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  donor_id INTEGER NOT NULL REFERENCES donors(id) ON DELETE CASCADE,
  distance_km REAL NOT NULL,
  readiness INTEGER NOT NULL,
  can_alert INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'idle',   -- idle | pending | accepted | declined | closed
  alerted_at TEXT, responded_at TEXT,
  PRIMARY KEY(request_id, donor_id));
"""


# ---------------------------------------------------------------- helpers
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def haversine(lat1, lng1, lat2, lng2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def is_eligible(last_donated: Optional[str]) -> bool:
    if not last_donated:
        return True
    return (date.today() - date.fromisoformat(last_donated)).days >= MIN_GAP_DAYS


def readiness(km: float, availability: str, last_donated, accepted: int, total: int) -> int:
    """Donor Readiness Score /100: distance 40 + availability 25 + eligibility 20 + past response 15."""
    dist = 40 * max(0.0, 1 - km / 20)
    avail = {"Available now": 25, "Available after selected time": 12}.get(availability, 0)
    elig = 20 if is_eligible(last_donated) else 0
    past = 15 * accepted / total if total else 7.5
    return round(dist + avail + elig + past)


def donor_code(i: int) -> str:
    return f"LL-D-{4200 + i}"


def request_code(i: int) -> str:
    return f"LL-{i}"


def parse_id(raw: str, prefix: str, base: int = 0) -> int:
    s = raw.strip()
    if s.upper().startswith(prefix.upper()):
        s = s[len(prefix):]
    if not s.isdigit():
        raise HTTPException(404, "Not found")
    return int(s) - base


def donor_km(d, h_lat, h_lng, h_city) -> float:
    """Distance donor -> hospital. Donors without coordinates fall back to a
    city-level guess (12 km if same city) so they are still reachable."""
    if d["lat"] is not None and d["lng"] is not None and h_lat is not None and h_lng is not None:
        return haversine(d["lat"], d["lng"], h_lat, h_lng)
    same_city = (d["city"] or "").strip().lower() == (h_city or "").strip().lower()
    return 12.0 if same_city else 999.0


def match_donors(con, blood_group, h_lat, h_lng, h_city, rare_mode):
    groups = COMPAT[blood_group]
    sql = f"SELECT * FROM donors WHERE blood_group IN ({','.join('?' * len(groups))})"
    out = []
    for d in con.execute(sql, groups).fetchall():
        km = donor_km(d, h_lat, h_lng, h_city)
        if km > MAX_SEARCH_KM:
            continue
        sc = readiness(km, d["availability"], d["last_donated"], d["alerts_accepted"], d["alerts_total"])
        can_alert = d["availability"] == "Available now" and is_eligible(d["last_donated"])
        out.append((d, round(km, 1), sc, can_alert))
    # rare-group donors first in rare mode, then best readiness, then nearest
    out.sort(key=lambda x: (0 if (rare_mode and x[0]["blood_group"] in RARE) else 1, -x[2], x[1]))
    return out


def donors_out(con, request_id: int):
    rows = con.execute(
        """SELECT rd.*, d.name, d.blood_group, d.area, d.phone, d.availability, d.lat, d.lng
           FROM request_donors rd JOIN donors d ON d.id = rd.donor_id
           WHERE rd.request_id=? ORDER BY rd.readiness DESC, rd.distance_km""",
        (request_id,),
    ).fetchall()
    res = []
    for r in rows:
        if r["can_alert"]:
            av = "A"
        elif r["availability"] == "Unavailable":
            av = "U"
        else:
            av = "B"
        item = {
            "id": r["donor_id"], "name": r["name"], "g": r["blood_group"], "km": r["distance_km"],
            "sc": r["readiness"], "av": av, "area": r["area"] or "", "status": r["status"],
            "alerted": r["status"] != "idle",
            # approximate position (about 1 km) so the hospital can see where help is coming from
            "lat": round(r["lat"], 2) if r["lat"] is not None else None,
            "lng": round(r["lng"], 2) if r["lng"] is not None else None,
        }
        if r["status"] == "accepted":  # contact is shared only after the donor accepts
            item["phone"] = r["phone"]
        res.append(item)
    return res


def counts_out(donors):
    n = lambda s: sum(1 for d in donors if d["status"] == s)
    return {
        "found": len(donors),
        "eligible": sum(1 for d in donors if d["av"] == "A"),
        "alerted": sum(1 for d in donors if d["alerted"]),
        "accepted": n("accepted"), "declined": n("declined"), "pending": n("pending"),
    }


def request_out(con, req, with_donors=True):
    donors = donors_out(con, req["id"])
    h = con.execute("SELECT name, city, lat, lng FROM hospitals WHERE id=?", (req["hospital_id"],)).fetchone()
    out = {
        "hospital": {"name": h["name"], "city": h["city"], "lat": h["lat"], "lng": h["lng"]} if h else None,
        "id": request_code(req["id"]), "blood_group": req["blood_group"], "units": req["units"],
        "urgency": req["urgency"], "radius_km": req["radius_km"], "context": req["context"],
        "notes": req["notes"], "rare_mode": bool(req["rare_mode"]), "status": req["status"],
        "created_at": req["created_at"], "counts": counts_out(donors),
    }
    if with_donors:
        out["donors"] = donors
    return out


def current_hospital(con, authorization: Optional[str]):
    if authorization and authorization.lower().startswith("bearer "):
        row = con.execute(
            "SELECT h.* FROM sessions s JOIN hospitals h ON h.id = s.hospital_id "
            "WHERE s.token=? AND s.created_at > ?",
            (authorization[7:].strip(), datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() - SESSION_HOURS * 3600, timezone.utc
            ).isoformat(timespec="seconds")),
        ).fetchone()
        if not row:
            raise HTTPException(401, "Session expired. Please log in again.")
        return row
    if REQUIRE_AUTH:
        raise HTTPException(401, "Hospital login required")
    row = con.execute("SELECT * FROM hospitals ORDER BY id LIMIT 1").fetchone()
    if not row:
        raise HTTPException(401, "No hospital registered")
    return row


def load_request(con, request_id: str, hospital):
    req = con.execute("SELECT * FROM requests WHERE id=?", (parse_id(request_id, "LL-"),)).fetchone()
    if not req:
        raise HTTPException(404, "Request not found")
    if req["hospital_id"] != hospital["id"]:
        raise HTTPException(403, "This request belongs to another hospital")
    return req


# ---------------------------------------------------------------- seed data
SEED_DONORS = [
    ("Rahul Verma", "O-", 1.2, 92, "A", "Arera Colony"), ("Priya Sharma", "O-", 2.1, 88, "A", "MP Nagar"),
    ("Imran Khan", "O-", 3.4, 81, "A", "Kolar Road"), ("Anjali Mehta", "O-", 4.8, 76, "B", "Shahpura"),
    ("Vikram Singh", "AB-", 2.9, 90, "A", "TT Nagar"), ("Sneha Patil", "AB-", 6.2, 79, "A", "Bairagarh"),
    ("Arjun Nair", "A-", 3.8, 84, "A", "Arera Colony"), ("Kavya Reddy", "A-", 7.1, 70, "A", "Hoshangabad Rd"),
    ("Rohan Gupta", "B-", 5, 83, "A", "Jahangirabad"), ("Neha Joshi", "B-", 9.4, 66, "B", "Berasia Rd"),
    ("Aman Tiwari", "B+", 1.8, 89, "A", "Habibganj"), ("Pooja Yadav", "B+", 4.1, 77, "A", "Govindpura"),
    ("Sameer Ali", "B+", 8.8, 68, "U", "Piplani"), ("Deepak Rao", "O+", 2.6, 86, "A", "Shyamla Hills"),
    ("Ritu Jain", "O+", 5.7, 80, "A", "Ashoka Garden"), ("Karan Malhotra", "O+", 11.2, 61, "A", "Misrod"),
    ("Meera Iyer", "A+", 3.3, 85, "A", "Bittan Market"), ("Sanjay Dubey", "A+", 6.9, 72, "A", "Kolar Road"),
    ("Farah Siddiqui", "AB+", 4.4, 78, "A", "Old City"), ("Harsh Pandey", "O+", 12.5, 58, "B", "Bhel"),
]
HOSPITAL_LAT, HOSPITAL_LNG = 23.2330, 77.4210  # City Care Hospital (demo), Bhopal


def seed(con):
    if con.execute("SELECT COUNT(*) FROM hospitals").fetchone()[0]:
        return
    salt = secrets.token_hex(8)
    con.execute(
        "INSERT INTO hospitals(name,email,password_hash,salt,phone,address,city,lat,lng,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("City Care Hospital", "demo@citycare.com", hash_pw("hospital123", salt), salt, "+91 755 000 0000",
         "Emergency Ward, Arera Hills, Bhopal", "Bhopal", HOSPITAL_LAT, HOSPITAL_LNG, now()),
    )
    avail_map = {"A": "Available now", "B": "Busy", "U": "Unavailable"}
    for i, (name, g, km, sc, av, area) in enumerate(SEED_DONORS):
        ang = i * 2.399
        lat = HOSPITAL_LAT + km * math.sin(ang) / 111.0
        lng = HOSPITAL_LNG + km * math.cos(ang) / (111.0 * math.cos(math.radians(HOSPITAL_LAT)))
        con.execute(
            """INSERT INTO donors(name,phone,email,blood_group,city,area,lat,lng,availability,
               total_donations,alerts_accepted,alerts_total,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, f"90000{i + 1:05d}", f"donor{i + 1}@example.com", g, "Bhopal", area, lat, lng,
             avail_map[av], 3 + i % 5, round(sc / 10), 10, now()),
        )
    con.execute("DELETE FROM sqlite_sequence WHERE name='requests'")
    con.execute("INSERT INTO sqlite_sequence(name, seq) VALUES('requests', 2041)")  # first request = LL-2042


@asynccontextmanager
async def lifespan(_app):
    with db() as con:
        con.executescript(SCHEMA)
        seed(con)
    yield


# ---------------------------------------------------------------- app
app = FastAPI(title="LifeLink API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(RequestValidationError)
async def on_validation_error(_request, exc: RequestValidationError):
    e = exc.errors()[0]
    field = ".".join(str(x) for x in e["loc"] if x != "body")
    msg = e["msg"].replace("Value error, ", "")
    return JSONResponse(status_code=422, content={"detail": f"{field}: {msg}" if field else msg})


def _plain(v: str) -> str:
    """Reject markup so names/addresses can never carry HTML into another user's screen."""
    if re.search(r"[<>]", v or ""):
        raise ValueError("Please remove < and > characters")
    return v


# ---- models
class DonorIn(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    phone: str
    email: str
    blood_group: str
    city: str = Field(min_length=2, max_length=60)
    area: str = ""
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)
    availability: str = "Available now"
    preferred_time: str = "Any time"
    last_donated: Optional[str] = None
    total_donations: int = Field(0, ge=0, le=500)
    consent_emergency: bool
    consent_medical: bool

    @field_validator("name", "city", "area", "preferred_time")
    @classmethod
    def _text(cls, v):
        return _plain(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        if not re.fullmatch(r"[6-9]\d{9}", v):
            raise ValueError("Enter a 10-digit mobile number")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        if not re.fullmatch(r"\S+@\S+\.\S+", v):
            raise ValueError("Enter a valid email")
        return v.lower()

    @field_validator("blood_group")
    @classmethod
    def _bg(cls, v):
        if v not in GROUPS:
            raise ValueError("Select a valid blood group")
        return v

    @field_validator("availability")
    @classmethod
    def _av(cls, v):
        if v not in AVAILABILITY:
            raise ValueError("Invalid availability status")
        return v

    @field_validator("last_donated", mode="before")
    @classmethod
    def _last(cls, v):
        if v in (None, ""):
            return None
        try:
            d = date.fromisoformat(v)
        except ValueError:
            raise ValueError("Use YYYY-MM-DD")
        if d > date.today():
            raise ValueError("Last donation date cannot be in the future")
        return v

    @field_validator("consent_emergency", "consent_medical")
    @classmethod
    def _consent(cls, v):
        if not v:
            raise ValueError("Consent is required")
        return v


class AvailabilityIn(BaseModel):
    availability: str

    @field_validator("availability")
    @classmethod
    def _av(cls, v):
        if v not in AVAILABILITY:
            raise ValueError("Invalid availability status")
        return v


class HospitalIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    email: str
    password: str = Field(min_length=8, max_length=100)
    phone: str = ""
    address: str = ""
    city: str = Field(min_length=2, max_length=60)
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)

    @field_validator("name", "city", "address", "phone")
    @classmethod
    def _text(cls, v):
        return _plain(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        if not re.fullmatch(r"\S+@\S+\.\S+", v):
            raise ValueError("Enter a valid email")
        return v.lower()


class LoginIn(BaseModel):
    email: str
    password: str


class RequestIn(BaseModel):
    blood_group: str
    units: int = Field(1, ge=1, le=20)
    urgency: Literal["CRITICAL", "URGENT", "SCHEDULED"] = "URGENT"
    radius_km: int = Field(10, ge=1, le=MAX_SEARCH_KM)
    context: str = Field("", max_length=80)
    notes: str = Field("", max_length=500)
    required_by: Optional[str] = Field(None, max_length=40)
    rare_mode: bool = False

    @field_validator("context", "notes", "required_by")
    @classmethod
    def _text(cls, v):
        return _plain(v) if v else v

    @field_validator("blood_group")
    @classmethod
    def _bg(cls, v):
        if v not in GROUPS:
            raise ValueError("Select a valid blood group")
        return v


class RespondIn(BaseModel):
    donor_id: int
    response: Literal["accepted", "declined"]


class StatusIn(BaseModel):
    status: Literal["Fulfilled", "Cancelled"]


class ExpandIn(BaseModel):
    radius_km: Optional[int] = Field(None, ge=1, le=MAX_SEARCH_KM)


# ---- routes
@app.get("/api/health")
def health():
    return {"ok": True, "time": now()}


@app.get("/api/compatibility")
def compatibility():
    return {"compatible_donors_for_patient": COMPAT, "rare_groups": sorted(RARE)}


@app.post("/api/donors", status_code=201)
def register_donor(body: DonorIn):
    with db() as con:
        try:
            cur = con.execute(
                """INSERT INTO donors(name,phone,email,blood_group,city,area,lat,lng,availability,
                   preferred_time,last_donated,total_donations,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (body.name.strip(), body.phone, body.email, body.blood_group, body.city.strip(), body.area.strip(),
                 body.lat, body.lng, body.availability, body.preferred_time, body.last_donated,
                 body.total_donations, now()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A donor with this mobile number is already registered")
        did = cur.lastrowid
    return {
        "id": donor_code(did), "donor_id": did,
        "readiness": readiness(5.0, body.availability, body.last_donated, 0, 0),
        "eligible": is_eligible(body.last_donated),
    }


@app.get("/api/donors")
def list_donors(blood_group: Optional[str] = None, city: Optional[str] = None,
                available_only: bool = False, limit: int = Query(50, ge=1, le=200)):
    sql, args = "SELECT * FROM donors WHERE 1=1", []
    if blood_group:
        sql += " AND blood_group=?"
        args.append(blood_group)
    if city:
        sql += " AND lower(city)=lower(?)"
        args.append(city)
    if available_only:
        sql += " AND availability='Available now'"
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db() as con:
        rows = con.execute(sql, args).fetchall()
    return [  # contact details are never exposed here
        {"id": donor_code(r["id"]), "name": r["name"], "blood_group": r["blood_group"], "city": r["city"],
         "area": r["area"], "availability": r["availability"], "eligible": is_eligible(r["last_donated"])}
        for r in rows
    ]


@app.put("/api/donors/{donor_id}/availability")
def set_availability(donor_id: str, body: AvailabilityIn):
    with db() as con:
        cur = con.execute("UPDATE donors SET availability=? WHERE id=?",
                          (body.availability, parse_id(donor_id, "LL-D-", 4200)))
        if cur.rowcount == 0:
            raise HTTPException(404, "Donor not found")
    return {"id": donor_id, "availability": body.availability}


@app.post("/api/hospitals", status_code=201)
def register_hospital(body: HospitalIn):
    salt = secrets.token_hex(8)
    with db() as con:
        try:
            cur = con.execute(
                "INSERT INTO hospitals(name,email,password_hash,salt,phone,address,city,lat,lng,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (body.name.strip(), body.email, hash_pw(body.password, salt), salt, body.phone, body.address,
                 body.city.strip(), body.lat, body.lng, now()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A hospital with this email already exists")
    return {"id": cur.lastrowid, "name": body.name}


@app.post("/api/hospital/login")
def hospital_login(body: LoginIn):
    with db() as con:
        h = con.execute("SELECT * FROM hospitals WHERE email=?", (body.email.strip().lower(),)).fetchone()
        if not h or not hmac.compare_digest(hash_pw(body.password, h["salt"]), h["password_hash"]):
            raise HTTPException(401, "Email or password is incorrect")
        token = secrets.token_urlsafe(32)
        con.execute("INSERT INTO sessions(token,hospital_id,created_at) VALUES(?,?,?)", (token, h["id"], now()))
    return {"ok": True, "token": token, "hospital": {"id": h["id"], "name": h["name"], "city": h["city"]}}


@app.post("/api/requests", status_code=201)
def create_request(body: RequestIn, authorization: Optional[str] = Header(None)):
    with db() as con:
        h = current_hospital(con, authorization)
        matches = match_donors(con, body.blood_group, h["lat"], h["lng"], h["city"], body.rare_mode)
        alerting = [m for m in matches if m[3] and m[1] <= body.radius_km]
        cur = con.execute(
            """INSERT INTO requests(hospital_id,blood_group,units,urgency,radius_km,context,notes,required_by,
               rare_mode,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (h["id"], body.blood_group, body.units, body.urgency, body.radius_km, body.context, body.notes,
             body.required_by, int(body.rare_mode), "Donors Responding" if alerting else "Searching", now()),
        )
        rid, ts = cur.lastrowid, now()
        for d, km, sc, can_alert in matches:
            go = can_alert and km <= body.radius_km
            con.execute(
                "INSERT INTO request_donors(request_id,donor_id,distance_km,readiness,can_alert,status,alerted_at) VALUES(?,?,?,?,?,?,?)",
                (rid, d["id"], km, sc, int(can_alert), "pending" if go else "idle", ts if go else None),
            )
        req = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        return request_out(con, req)


@app.get("/api/requests")
def list_requests(active_only: bool = False, authorization: Optional[str] = Header(None)):
    with db() as con:
        h = current_hospital(con, authorization)
        sql = "SELECT * FROM requests WHERE hospital_id=?"
        if active_only:
            sql += " AND status IN ('Searching','Donors Responding')"
        rows = con.execute(sql + " ORDER BY id DESC", (h["id"],)).fetchall()
        return [request_out(con, r, with_donors=False) for r in rows]


@app.get("/api/requests/{request_id}")
def get_request(request_id: str, authorization: Optional[str] = Header(None)):
    with db() as con:
        h = current_hospital(con, authorization)
        return request_out(con, load_request(con, request_id, h))


@app.post("/api/requests/{request_id}/respond")
def respond(request_id: str, body: RespondIn):
    """Called when a donor taps Accept / Decline on an alert."""
    rid = parse_id(request_id, "LL-")
    with db() as con:
        req = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not req:
            raise HTTPException(404, "Request not found")
        if req["status"] in ("Fulfilled", "Cancelled"):
            raise HTTPException(409, f"Request is already {req['status'].lower()}")
        cur = con.execute(
            "UPDATE request_donors SET status=?, responded_at=? WHERE request_id=? AND donor_id=? AND status='pending'",
            (body.response, now(), rid, body.donor_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "This donor has no pending alert for the request")
        con.execute(
            "UPDATE donors SET alerts_total=alerts_total+1, alerts_accepted=alerts_accepted+? WHERE id=?",
            (1 if body.response == "accepted" else 0, body.donor_id),
        )
        # Donor-facing endpoint (no login): return only the outcome, never donor contact details.
        counts = counts_out(donors_out(con, rid))
        return {"id": request_code(rid), "response": body.response, "status": req["status"], "counts": counts}


@app.post("/api/requests/{request_id}/expand")
def expand_radius(request_id: str, body: ExpandIn = ExpandIn(), authorization: Optional[str] = Header(None)):
    with db() as con:
        h = current_hospital(con, authorization)
        req = load_request(con, request_id, h)
        if req["status"] in ("Fulfilled", "Cancelled"):
            raise HTTPException(409, f"Request is already {req['status'].lower()}")
        new_r = max(body.radius_km or 0, min(MAX_SEARCH_KM, req["radius_km"] + 5))
        new_r = min(new_r, MAX_SEARCH_KM)
        con.execute(
            """UPDATE request_donors SET status='pending', alerted_at=?
               WHERE request_id=? AND status='idle' AND can_alert=1 AND distance_km<=?""",
            (now(), req["id"], new_r),
        )
        con.execute("UPDATE requests SET radius_km=?, status='Donors Responding' WHERE id=?", (new_r, req["id"]))
        return request_out(con, con.execute("SELECT * FROM requests WHERE id=?", (req["id"],)).fetchone())


@app.patch("/api/requests/{request_id}/status")
def set_status(request_id: str, body: StatusIn, authorization: Optional[str] = Header(None)):
    with db() as con:
        h = current_hospital(con, authorization)
        req = load_request(con, request_id, h)
        con.execute("UPDATE requests SET status=? WHERE id=?", (body.status, req["id"]))
        con.execute("UPDATE request_donors SET status='closed' WHERE request_id=? AND status='pending'", (req["id"],))
        return request_out(con, con.execute("SELECT * FROM requests WHERE id=?", (req["id"],)).fetchone())


@app.get("/api/stats")
def stats():
    with db() as con:
        one = lambda sql: con.execute(sql).fetchone()[0]
        avg = one("""SELECT AVG((julianday(responded_at) - julianday(alerted_at)) * 1440)
                     FROM request_donors WHERE responded_at IS NOT NULL AND alerted_at IS NOT NULL""")
        return {
            "donors": one("SELECT COUNT(*) FROM donors"),
            "hospitals": one("SELECT COUNT(*) FROM hospitals"),
            "requests": one("SELECT COUNT(*) FROM requests"),
            "fulfilled": one("SELECT COUNT(*) FROM requests WHERE status='Fulfilled'"),
            "avg_response_minutes": round(avg, 1) if avg is not None else None,
        }


# Serve the frontend from the same server (keep this LAST so /api routes win).
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
