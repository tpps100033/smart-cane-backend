import os
import uuid
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

APP_NAME = "cane-fall-backend"
DB_PATH = os.getenv("DB_PATH", "/data/cane.db")

print("DB_PATH =", DB_PATH, flush=True)

db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
ADMIN_TELEGRAM_IDS = os.getenv("ADMIN_TELEGRAM_IDS", "")
ADMIN_TELEGRAM_IDS = [x.strip() for x in ADMIN_TELEGRAM_IDS.split(",") if x.strip()]

ADMIN_KEY = os.getenv("ADMIN_KEY", "")

# ======================
# DB helpers
# ======================

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ======================
# DB init
# ======================

def init_db():
    conn = db_conn()

    conn.execute("""
    CREATE TABLE IF NOT EXISTS devices (
      device_id TEXT PRIMARY KEY,
      api_key TEXT NOT NULL,
      alias TEXT,
      created_at TEXT NOT NULL,
      last_seen_at TEXT,
      last_battery_v REAL,
      last_rssi INTEGER,
      firmware TEXT,
      is_active INTEGER NOT NULL DEFAULT 1
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS events (
      event_id TEXT PRIMARY KEY,
      created_at TEXT NOT NULL,
      device_id TEXT NOT NULL,
      device_ts TEXT,
      level TEXT NOT NULL,
      fsr INTEGER,
      acc_peak REAL,
      variance REAL,
      note TEXT,
      firmware TEXT,
      battery_v REAL,
      rssi INTEGER,
      ack_local INTEGER NOT NULL DEFAULT 0,
      notify_status TEXT NOT NULL,
      notify_error TEXT
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS notify_logs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_id TEXT NOT NULL,
      channel TEXT NOT NULL,
      attempt INTEGER NOT NULL,
      status_code INTEGER,
      ok INTEGER NOT NULL,
      error TEXT,
      created_at TEXT NOT NULL
    );
    """)

    # ===== 新增多用戶 =====

    conn.execute("""
    CREATE TABLE IF NOT EXISTS users(
      telegram_id TEXT PRIMARY KEY,
      username TEXT,
      role TEXT,
      state TEXT,
      created_at TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_devices(
      telegram_id TEXT,
      device_id TEXT,
      created_at TEXT,
      PRIMARY KEY (telegram_id,device_id)
    )
    """)

    conn.commit()
    conn.close()

# ======================
# auth
# ======================

def require_admin(x_admin_key: str):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="invalid_admin_key")

def auth_device(conn, device_id, x_api_key):
    row = conn.execute(
        "SELECT * FROM devices WHERE device_id=? AND is_active=1",
        (device_id,)
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404)

    if row["api_key"] != x_api_key:
        raise HTTPException(status_code=403)

    return row

# ======================
# telegram
# ======================

def tg_send(chat_id, text):

    if not TG_BOT_TOKEN:
        return {"ok": False}

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    try:

        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text
        }, timeout=8)

        ok = r.status_code == 200 and r.json().get("ok")

        return {"ok": ok}

    except Exception as e:

        return {"ok": False, "error": str(e)}

def get_notify_targets(conn, device_id):

    targets = set()

    if TG_CHAT_ID:
        targets.add(TG_CHAT_ID)

    for admin in ADMIN_TELEGRAM_IDS:
        targets.add(admin)

    rows = conn.execute(
        "SELECT telegram_id FROM user_devices WHERE device_id=?",
        (device_id,)
    ).fetchall()

    for r in rows:
        targets.add(r["telegram_id"])

    return list(targets)

def notify_event(conn, event_row, attempts=3):

    device_id = event_row["device_id"]
    level = event_row["level"]

    msg = f"""
跌倒警示 {level}

device: {device_id}
time: {event_row['created_at']}
"""

    targets = get_notify_targets(conn, device_id)

    last = {"ok": False}

    for chat_id in targets:

        last = tg_send(chat_id, msg)

    return last

# ======================
# models
# ======================

class DeviceCreate(BaseModel):
    device_id: str
    api_key: str
    alias: Optional[str] = None

class DeviceOut(BaseModel):
    device_id: str
    alias: Optional[str]
    created_at: str
    last_seen_at: Optional[str]
    last_battery_v: Optional[float]
    last_rssi: Optional[int]
    firmware: Optional[str]
    is_active: bool

class HeartbeatIn(BaseModel):
    device_id: str
    battery_v: Optional[float]
    rssi: Optional[int]
    firmware: Optional[str]

class EventIn(BaseModel):
    device_id: str
    level: str
    note: Optional[str] = None
    battery_v: Optional[float] = None
    rssi: Optional[int] = None

class EventOut(BaseModel):
    event_id: str
    created_at: str
    device_id: str
    level: str
    notify_status: str
    notify_error: Optional[str]

# ======================
# app
# ======================

app = FastAPI(title=APP_NAME)

@app.on_event("startup")
def startup():
    init_db()

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/")
def home():
    return {"service": APP_NAME}

# ======================
# admin API
# ======================

@app.post("/admin/devices", response_model=DeviceOut)
def admin_create_device(payload: DeviceCreate, x_admin_key: str = Header(default="")):

    require_admin(x_admin_key)

    conn = db_conn()

    created_at = utc_now_iso()

    conn.execute(
        "INSERT OR REPLACE INTO devices(device_id, api_key, alias, created_at, is_active) VALUES(?,?,?,?,1)",
        (payload.device_id, payload.api_key, payload.alias, created_at)
    )

    conn.commit()

    row = conn.execute(
        "SELECT * FROM devices WHERE device_id=?",
        (payload.device_id,)
    ).fetchone()

    conn.close()

    return row

@app.get("/admin/devices", response_model=List[DeviceOut])
def admin_list_devices(x_admin_key: str = Header(default="")):

    require_admin(x_admin_key)

    conn = db_conn()

    rows = conn.execute(
        "SELECT * FROM devices"
    ).fetchall()

    conn.close()

    return rows

@app.post("/admin/devices/{device_id}/deactivate")
def admin_deactivate_device(device_id: str, x_admin_key: str = Header(default="")):

    require_admin(x_admin_key)

    conn = db_conn()

    conn.execute(
        "UPDATE devices SET is_active=0 WHERE device_id=?",
        (device_id,)
    )

    conn.commit()

    conn.close()

    return {"ok": True}

@app.get("/admin/events", response_model=List[EventOut])
def admin_list_events(x_admin_key: str = Header(default="")):

    require_admin(x_admin_key)

    conn = db_conn()

    rows = conn.execute(
        "SELECT * FROM events ORDER BY created_at DESC LIMIT 50"
    ).fetchall()

    conn.close()

    return rows

# ======================
# device API
# ======================

@app.post("/api/v1/heartbeat")
def heartbeat(payload: HeartbeatIn, x_api_key: str = Header(default="")):

    conn = db_conn()

    auth_device(conn, payload.device_id, x_api_key)

    conn.execute(
        "UPDATE devices SET last_seen_at=? WHERE device_id=?",
        (utc_now_iso(), payload.device_id)
    )

    conn.commit()

    conn.close()

    return {"ok": True}

@app.post("/api/v1/events", response_model=EventOut)
def create_event(payload: EventIn, x_api_key: str = Header(default="")):

    conn = db_conn()

    auth_device(conn, payload.device_id, x_api_key)

    event_id = str(uuid.uuid4())

    created_at = utc_now_iso()

    conn.execute(
        """INSERT INTO events
        (event_id, created_at, device_id, level, note, battery_v, rssi, notify_status)
        VALUES (?,?,?,?,?,?,?,?)""",
        (
            event_id,
            created_at,
            payload.device_id,
            payload.level,
            payload.note,
            payload.battery_v,
            payload.rssi,
            "PENDING"
        )
    )

    conn.commit()

    row = conn.execute(
        "SELECT * FROM events WHERE event_id=?",
        (event_id,)
    ).fetchone()

    result = notify_event(conn, row)

    if result["ok"]:

        notify_status = "SENT"
        notify_error = None

    else:

        notify_status = "FAILED"
        notify_error = "telegram_error"

    conn.execute(
        "UPDATE events SET notify_status=?, notify_error=? WHERE event_id=?",
        (notify_status, notify_error, event_id)
    )

    conn.commit()
    conn.close()

    return EventOut(
        event_id=event_id,
        created_at=created_at,
        device_id=payload.device_id,
        level=payload.level,
        notify_status=notify_status,
        notify_error=notify_error
    )

# ======================
# telegram bot
# ======================

@app.post("/tg/webhook")
def telegram_webhook(update: dict):

    if "message" not in update:
        return {"ok": True}

    msg = update["message"]

    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "")

    conn = db_conn()

    user = conn.execute(
        "SELECT * FROM users WHERE telegram_id=?",
        (chat_id,)
    ).fetchone()

    if not user:

        role = "admin" if chat_id in ADMIN_TELEGRAM_IDS else "user"

        conn.execute(
            "INSERT INTO users VALUES(?,?,?,?,?)",
            (chat_id, msg["from"].get("username"), role, "idle", utc_now_iso())
        )

        conn.commit()

        user = conn.execute(
            "SELECT * FROM users WHERE telegram_id=?",
            (chat_id,)
        ).fetchone()

    if text == "/start":

        tg_send(chat_id, "請輸入拐杖序號 (例如 cane-001)")

        conn.execute(
            "UPDATE users SET state='waiting_cane' WHERE telegram_id=?",
            (chat_id,)
        )

        conn.commit()

    elif user["state"] == "waiting_cane":

        device_id = text

        device = conn.execute(
            "SELECT * FROM devices WHERE device_id=?",
            (device_id,)
        ).fetchone()

        if not device:

            tg_send(chat_id, "找不到此拐杖")

        else:

            conn.execute(
                "INSERT OR IGNORE INTO user_devices VALUES(?,?,?)",
                (chat_id, device_id, utc_now_iso())
            )

            conn.execute(
                "UPDATE users SET state='idle' WHERE telegram_id=?",
                (chat_id,)
            )

            conn.commit()

            tg_send(chat_id, f"已配對 {device_id}")

    conn.close()

    return {"ok": True}
