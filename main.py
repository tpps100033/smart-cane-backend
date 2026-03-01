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

# 印出目前 DB_PATH（用來確認 Railway 變數到底有沒有生效）
print("DB_PATH =", DB_PATH, flush=True)

# 確保資料夾存在（避免 /data 沒掛 volume 直接爆炸）
db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

# Telegram (optional but recommended)
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# Admin key to manage devices via API (keep secret)
ADMIN_KEY = os.getenv("ADMIN_KEY", "")

# ---- DB helpers ----
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
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
      level TEXT NOT NULL, -- YELLOW/ORANGE/RED
      fsr INTEGER,
      acc_peak REAL,
      variance REAL,
      note TEXT,
      firmware TEXT,
      battery_v REAL,
      rssi INTEGER,
      ack_local INTEGER NOT NULL DEFAULT 0,
      notify_status TEXT NOT NULL, -- PENDING/SENT/FAILED/SKIPPED
      notify_error TEXT,
      FOREIGN KEY(device_id) REFERENCES devices(device_id)
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS notify_logs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_id TEXT NOT NULL,
      channel TEXT NOT NULL, -- telegram
      attempt INTEGER NOT NULL,
      status_code INTEGER,
      ok INTEGER NOT NULL,
      error TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY(event_id) REFERENCES events(event_id)
    );
    """)
    conn.commit()
    conn.close()

def require_admin(x_admin_key: str) -> None:
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="invalid_admin_key")

def auth_device(conn: sqlite3.Connection, device_id: str, x_api_key: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM devices WHERE device_id=? AND is_active=1",
        (device_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="device_not_found_or_inactive")
    if row["api_key"] != x_api_key:
        raise HTTPException(status_code=403, detail="invalid_device_api_key")
    return row

# ---- Telegram ----
def tg_send(text: str) -> Dict[str, Any]:
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return {"ok": False, "status_code": None, "error": "telegram_not_configured"}

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=8)
        ok = (r.status_code == 200 and r.json().get("ok") is True)
        return {"ok": ok, "status_code": r.status_code, "error": None if ok else r.text[:300]}
    except Exception as e:
        return {"ok": False, "status_code": None, "error": f"{type(e).__name__}: {e}"}

def notify_event(conn: sqlite3.Connection, event_row: sqlite3.Row, attempts: int = 3) -> Dict[str, Any]:
    """
    Send telegram and log attempts.
    Simple retry w/ backoff: 1s, 2s, 4s.
    """
    event_id = event_row["event_id"]
    level = event_row["level"]
    device_id = event_row["device_id"]
    alias_row = conn.execute("SELECT alias FROM devices WHERE device_id=?", (device_id,)).fetchone()
    alias_name = alias_row["alias"] if alias_row and alias_row["alias"] else device_id

    batt = event_row["battery_v"] if event_row["battery_v"] is not None else "N/A"
    rssi = event_row["rssi"] if event_row["rssi"] is not None else "N/A"

    msg = (
        f"跌倒警示 {level}\n"
        f"device: {alias_name} ({device_id})\n"
        f"time: {event_row['device_ts'] or event_row['created_at']}\n"
        f"rssi: {rssi}\n"
        f"battery: {batt}\n"
        f"event_id: {event_id}\n"
        f"note: {event_row['note'] or '-'}"
    )

    import time
    last = {"ok": False, "status_code": None, "error": "not_sent"}
    for attempt in range(1, attempts + 1):
        last = tg_send(msg)
        conn.execute(
            "INSERT INTO notify_logs(event_id, channel, attempt, status_code, ok, error, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (event_id, "telegram", attempt, last["status_code"], 1 if last["ok"] else 0, last["error"], utc_now_iso())
        )
        conn.commit()
        if last["ok"]:
            return last
        time.sleep(2 ** (attempt - 1))

    return last

# ---- API models ----
class DeviceCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=8, max_length=128)
    alias: Optional[str] = Field(default=None, max_length=128)

class DeviceOut(BaseModel):
    device_id: str
    alias: Optional[str] = None
    created_at: str
    last_seen_at: Optional[str] = None
    last_battery_v: Optional[float] = None
    last_rssi: Optional[int] = None
    firmware: Optional[str] = None
    is_active: bool

class HeartbeatIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    device_ts: Optional[str] = None
    battery_v: Optional[float] = None
    rssi: Optional[int] = None
    firmware: Optional[str] = None

class HeartbeatOut(BaseModel):
    status: str
    server_time: str

class EventIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    device_ts: Optional[str] = None
    level: str = Field(pattern="^(YELLOW|ORANGE|RED)$")
    fsr: Optional[int] = None
    acc_peak: Optional[float] = None
    variance: Optional[float] = None
    note: Optional[str] = None
    firmware: Optional[str] = None
    battery_v: Optional[float] = None
    rssi: Optional[int] = None
    ack_local: Optional[bool] = False

class EventOut(BaseModel):
    event_id: str
    created_at: str
    device_id: str
    level: str
    notify_status: str
    notify_error: Optional[str] = None

app = FastAPI(title=APP_NAME)

@app.on_event("startup")
def _startup():
    init_db()

@app.get("/healthz")
def healthz():
    return {"ok": True, "time": utc_now_iso(), "app": APP_NAME}

@app.get("/")
def home():
    # Minimal debug page (no auth) for quick demo health.
    return {"service": APP_NAME, "health": "/healthz", "admin_devices": "/admin/devices", "admin_events": "/admin/events"}

# ---- Admin endpoints ----
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
    row = conn.execute("SELECT * FROM devices WHERE device_id=?", (payload.device_id,)).fetchone()
    conn.close()
    return DeviceOut(
        device_id=row["device_id"],
        alias=row["alias"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        last_battery_v=row["last_battery_v"],
        last_rssi=row["last_rssi"],
        firmware=row["firmware"],
        is_active=bool(row["is_active"]),
    )

@app.get("/admin/devices", response_model=List[DeviceOut])
def admin_list_devices(x_admin_key: str = Header(default="")):
    require_admin(x_admin_key)
    conn = db_conn()
    rows = conn.execute("SELECT * FROM devices ORDER BY device_id ASC").fetchall()
    conn.close()
    return [
        DeviceOut(
            device_id=r["device_id"],
            alias=r["alias"],
            created_at=r["created_at"],
            last_seen_at=r["last_seen_at"],
            last_battery_v=r["last_battery_v"],
            last_rssi=r["last_rssi"],
            firmware=r["firmware"],
            is_active=bool(r["is_active"]),
        )
        for r in rows
    ]

@app.post("/admin/devices/{device_id}/deactivate")
def admin_deactivate_device(device_id: str, x_admin_key: str = Header(default="")):
    require_admin(x_admin_key)
    conn = db_conn()
    conn.execute("UPDATE devices SET is_active=0 WHERE device_id=?", (device_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/admin/events", response_model=List[EventOut])
def admin_list_events(limit: int = 50, x_admin_key: str = Header(default="")):
    require_admin(x_admin_key)
    limit = max(1, min(limit, 200))
    conn = db_conn()
    rows = conn.execute(
        "SELECT event_id, created_at, device_id, level, notify_status, notify_error "
        "FROM events ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [
        EventOut(
            event_id=r["event_id"],
            created_at=r["created_at"],
            device_id=r["device_id"],
            level=r["level"],
            notify_status=r["notify_status"],
            notify_error=r["notify_error"]
        )
        for r in rows
    ]

# ---- Device endpoints ----
@app.post("/api/v1/heartbeat", response_model=HeartbeatOut)
def heartbeat(payload: HeartbeatIn, x_api_key: str = Header(default="")):
    conn = db_conn()
    auth_device(conn, payload.device_id, x_api_key)

    now = utc_now_iso()
    conn.execute(
        "UPDATE devices SET last_seen_at=?, last_battery_v=?, last_rssi=?, firmware=? WHERE device_id=?",
        (now, payload.battery_v, payload.rssi, payload.firmware, payload.device_id)
    )
    conn.commit()
    conn.close()
    return HeartbeatOut(status="ok", server_time=now)

@app.post("/api/v1/events", response_model=EventOut)
def create_event(payload: EventIn, x_api_key: str = Header(default="")):
    conn = db_conn()
    auth_device(conn, payload.device_id, x_api_key)

    event_id = str(uuid.uuid4())
    created_at = utc_now_iso()

    conn.execute(
        """INSERT INTO events(
             event_id, created_at, device_id, device_ts, level, fsr, acc_peak, variance, note,
             firmware, battery_v, rssi, ack_local, notify_status, notify_error
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id, created_at, payload.device_id, payload.device_ts, payload.level, payload.fsr,
            payload.acc_peak, payload.variance, payload.note, payload.firmware, payload.battery_v,
            payload.rssi, 1 if payload.ack_local else 0, "PENDING", None
        )
    )
    # update last_seen as well
    conn.execute(
        "UPDATE devices SET last_seen_at=?, last_battery_v=?, last_rssi=?, firmware=? WHERE device_id=?",
        (created_at, payload.battery_v, payload.rssi, payload.firmware, payload.device_id)
    )
    conn.commit()

    # Notify
    row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
    result = notify_event(conn, row, attempts=3)

    if result["ok"]:
        conn.execute("UPDATE events SET notify_status=?, notify_error=? WHERE event_id=?",
                     ("SENT", None, event_id))
        notify_status = "SENT"
        notify_error = None
    else:
        conn.execute("UPDATE events SET notify_status=?, notify_error=? WHERE event_id=?",
                     ("FAILED", result["error"], event_id))
        notify_status = "FAILED"
        notify_error = result["error"]

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
