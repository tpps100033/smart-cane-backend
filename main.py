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

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
ADMIN_TELEGRAM_IDS = os.getenv("ADMIN_TELEGRAM_IDS", "")
ADMIN_TELEGRAM_IDS = [x.strip() for x in ADMIN_TELEGRAM_IDS.split(",") if x.strip()]

ADMIN_KEY = os.getenv("ADMIN_KEY", "")


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# =========================
# DATABASE
# =========================

def init_db():

    conn = db_conn()

    conn.execute("""
    CREATE TABLE IF NOT EXISTS devices(
      device_id TEXT PRIMARY KEY,
      api_key TEXT NOT NULL,
      alias TEXT,
      created_at TEXT NOT NULL,
      last_seen_at TEXT,
      last_battery_v REAL,
      last_rssi INTEGER,
      firmware TEXT,
      is_active INTEGER NOT NULL DEFAULT 1
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS events(
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
      ack_local INTEGER,
      notify_status TEXT,
      notify_error TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS notify_logs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_id TEXT,
      channel TEXT,
      attempt INTEGER,
      status_code INTEGER,
      ok INTEGER,
      error TEXT,
      created_at TEXT
    )
    """)

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


# =========================
# TELEGRAM
# =========================

def tg_send(chat_id: str, text: str):

    if not TG_BOT_TOKEN:
        return {"ok": False}

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    r = requests.post(url, json={
        "chat_id": chat_id,
        "text": text
    })

    return {"ok": r.status_code == 200}


def get_notify_targets(conn, device_id):

    targets = set()

    for admin in ADMIN_TELEGRAM_IDS:
        targets.add(admin)

    rows = conn.execute(
        "SELECT telegram_id FROM user_devices WHERE device_id=?",
        (device_id,)
    ).fetchall()

    for r in rows:
        targets.add(r["telegram_id"])

    return list(targets)


def notify_event(conn, event):

    device_id = event["device_id"]

    targets = get_notify_targets(conn, device_id)

    msg = f"""
跌倒警示 {event['level']}
device: {device_id}
time: {event['created_at']}
"""

    for chat_id in targets:
        tg_send(chat_id, msg)


# =========================
# API MODELS
# =========================

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


app = FastAPI()


@app.on_event("startup")
def startup():
    init_db()


# =========================
# DEVICE API
# =========================

@app.post("/api/v1/events")
def create_event(payload: EventIn, x_api_key: str = Header(default="")):

    conn = db_conn()

    event_id = str(uuid.uuid4())

    created_at = utc_now_iso()

    conn.execute(
        "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            created_at,
            payload.device_id,
            None,
            payload.level,
            None,
            None,
            None,
            payload.note,
            None,
            payload.battery_v,
            payload.rssi,
            0,
            "PENDING",
            None
        )
    )

    conn.commit()

    event = conn.execute(
        "SELECT * FROM events WHERE event_id=?",
        (event_id,)
    ).fetchone()

    notify_event(conn, event)

    conn.close()

    return EventOut(
        event_id=event_id,
        created_at=created_at,
        device_id=payload.device_id,
        level=payload.level
    )


# =========================
# TELEGRAM BOT
# =========================

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

    # /start

    if text == "/start":

        if user["role"] == "admin":

            tg_send(chat_id, "管理者模式")

        else:

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

    elif text == "/mydevices":

        rows = conn.execute(
            "SELECT device_id FROM user_devices WHERE telegram_id=?",
            (chat_id,)
        ).fetchall()

        if not rows:

            tg_send(chat_id, "沒有配對設備")

        else:

            msg = "\n".join([r["device_id"] for r in rows])

            tg_send(chat_id, "你的拐杖:\n" + msg)

    conn.close()

    return {"ok": True}
