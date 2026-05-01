import os
import uuid
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

APP_NAME = "cane-fall-backend"

DB_PATH = os.getenv("DB_PATH", "/data/cane.db")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

ADMIN_TELEGRAM_IDS = os.getenv("ADMIN_TELEGRAM_IDS", "")
ADMIN_TELEGRAM_IDS = [x.strip() for x in ADMIN_TELEGRAM_IDS.split(",") if x.strip()]

ADMIN_KEY = os.getenv("ADMIN_KEY", "")

# Google Apps Script Email 中繼站
GAS_EMAIL_URL = "https://script.google.com/macros/s/AKfycbw4RQkzHVSGeAWsBar0xyB_Uv8wihlN-BCX3y_IzZoKspPMBu8hC9DautElY5MXkuR1/exec"


def send_demo_email(level: str, note: str, device_id: str):
    """透過 GAS 中繼站發送 Email"""
    print("⏳ 準備透過 GAS 發送 Email...", flush=True)

    payload = {
        "level": level,
        "note": note,
        "device_id": device_id
    }

    try:
        response = requests.post(GAS_EMAIL_URL, json=payload, timeout=10)

        if response.status_code == 200:
            print(f"✅ Email 已成功交由 Google 寄出！回傳: {response.text}", flush=True)
        else:
            print(f"❌ 交由 Google 寄件失敗，狀態碼: {response.status_code}", flush=True)

    except Exception as e:
        print(f"❌ 呼叫 GAS 中繼站發生錯誤: {e}", flush=True)


app = FastAPI(title=APP_NAME)

# 允許前端網頁跨網域呼叫
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------
# UTILS
# ----------------------

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ----------------------
# DB INIT
# ----------------------

def init_db():
    conn = db_conn()

    conn.execute("""
    CREATE TABLE IF NOT EXISTS devices(
      device_id TEXT PRIMARY KEY,
      api_key TEXT,
      alias TEXT,
      created_at TEXT,
      last_seen_at TEXT,
      last_battery_v REAL,
      last_rssi INTEGER,
      firmware TEXT,
      is_active INTEGER DEFAULT 1
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS events(
      event_id TEXT PRIMARY KEY,
      created_at TEXT,
      device_id TEXT,
      device_ts TEXT,
      level TEXT,
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
      PRIMARY KEY (telegram_id, device_id)
    )
    """)

    conn.commit()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


# ----------------------
# TELEGRAM / 通訊軟體
# ----------------------

def tg_send(chat_id, text):
    if not TG_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=10
        )
    except Exception as e:
        print(f"❌ Telegram 發送失敗: {e}", flush=True)


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


# ----------------------
# 通知內容
# ----------------------

def notify_event(conn, event_row):
    device_id = event_row["device_id"]
    level = event_row["level"]

    if level == "YELLOW":
        emoji = "🟡"
        status = "跌倒後已恢復站立"
    elif level == "ORANGE":
        emoji = "🟠"
        status = "跌倒後偵測到掙扎活動"
    elif level == "RED":
        emoji = "🔴"
        status = "可能無法起身"
    else:
        emoji = "⚪"
        status = "未知"

    device = conn.execute(
        "SELECT alias FROM devices WHERE device_id=?",
        (device_id,)
    ).fetchone()

    alias = device["alias"] if device and device["alias"] else device_id

    msg = (
        f"{emoji} 跌倒警示 {level}\n\n"
        f"狀態: {status}\n"
        f"設備: {alias} ({device_id})\n"
        f"time: {event_row['created_at']}\n"
        f"battery: {event_row['battery_v']}\n"
        f"rssi: {event_row['rssi']}\n"
        f"note: {event_row['note']}"
    )

    targets = get_notify_targets(conn, device_id)

    for chat_id in targets:
        tg_send(chat_id, msg)


# ----------------------
# MODELS
# ----------------------

class DeviceCreate(BaseModel):
    device_id: str
    api_key: str
    alias: Optional[str] = None


class EventIn(BaseModel):
    device_id: str
    level: str
    note: Optional[str] = None
    battery_v: Optional[float] = None
    rssi: Optional[int] = None


# ----------------------
# ROOT
# ----------------------

@app.get("/")
def root():
    return {
        "app": APP_NAME,
        "status": "ok"
    }


# ----------------------
# WEB DASHBOARD API
# ----------------------

@app.get("/api/dashboard")
def get_dashboard():
    conn = db_conn()

    try:
        last_event = conn.execute("""
            SELECT event_id, created_at, device_id, level, note, battery_v, rssi, notify_status
            FROM events
            WHERE level IN ('YELLOW', 'ORANGE', 'RED')
            ORDER BY created_at DESC
            LIMIT 1
        """).fetchone()

        today_count = conn.execute("""
            SELECT COUNT(*) AS count
            FROM events
            WHERE level IN ('YELLOW', 'ORANGE', 'RED')
              AND date(created_at, 'localtime') = date('now', 'localtime')
        """).fetchone()["count"]

        recent_rows = conn.execute("""
            SELECT event_id, created_at, device_id, level, note, battery_v, rssi, notify_status
            FROM events
            WHERE level IN ('YELLOW', 'ORANGE', 'RED')
            ORDER BY created_at DESC
            LIMIT 10
        """).fetchall()

        return {
            "today_count": today_count,
            "last_event": dict(last_event) if last_event else None,
            "recent_events": [dict(row) for row in recent_rows]
        }

    finally:
        conn.close()


@app.get("/api/events/recent")
def get_recent_events(limit: int = 20):
    conn = db_conn()

    try:
        limit = max(1, min(limit, 100))

        rows = conn.execute("""
            SELECT event_id, created_at, device_id, level, note, battery_v, rssi, notify_status
            FROM events
            WHERE level IN ('YELLOW', 'ORANGE', 'RED')
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


@app.get("/api/stats/today")
def get_today_stats():
    conn = db_conn()

    try:
        rows = conn.execute("""
            SELECT level, COUNT(*) AS count
            FROM events
            WHERE level IN ('YELLOW', 'ORANGE', 'RED')
              AND date(created_at, 'localtime') = date('now', 'localtime')
            GROUP BY level
            ORDER BY level
        """).fetchall()

        total = sum(row["count"] for row in rows)
        by_level = {row["level"]: row["count"] for row in rows}

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "total": total,
            "by_level": by_level
        }

    finally:
        conn.close()


# ----------------------
# ADMIN API
# ----------------------

@app.post("/admin/devices")
def create_device(payload: DeviceCreate, x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")

    conn = db_conn()

    try:
        conn.execute(
            "INSERT INTO devices VALUES(?,?,?,?,?,?,?, ?,1)",
            (
                payload.device_id,
                payload.api_key,
                payload.alias,
                utc_now_iso(),
                None,
                None,
                None,
                None
            )
        )

        conn.commit()

        return {
            "device_id": payload.device_id,
            "message": "device created"
        }

    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Device already exists")

    finally:
        conn.close()


@app.get("/admin/devices")
def list_devices(x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")

    conn = db_conn()

    try:
        rows = conn.execute(
            "SELECT * FROM devices"
        ).fetchall()

        return [dict(r) for r in rows]

    finally:
        conn.close()


@app.get("/admin/events")
def list_events(x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")

    conn = db_conn()

    try:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY created_at DESC LIMIT 50"
        ).fetchall()

        return [dict(r) for r in rows]

    finally:
        conn.close()


# ----------------------
# DEVICE API
# ----------------------

@app.post("/api/v1/events")
def create_event(payload: EventIn, x_api_key: str = Header(default="")):
    conn = db_conn()

    try:
        device = conn.execute(
            "SELECT * FROM devices WHERE device_id=?",
            (payload.device_id,)
        ).fetchone()

        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        if device["api_key"] != x_api_key:
            raise HTTPException(status_code=403, detail="Invalid API key")

        level = payload.level.upper().strip()

        if level not in ["YELLOW", "ORANGE", "RED"]:
            raise HTTPException(
                status_code=400,
                detail="Invalid level. Use YELLOW, ORANGE, or RED."
            )

        event_id = str(uuid.uuid4())

        conn.execute(
            """INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                utc_now_iso(),
                payload.device_id,
                None,
                level,
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

        print(f"🔍 檢查到的 Level 為: {level}", flush=True)

        # 這裡已改成 YELLOW / ORANGE / RED 全部通知 Telegram 與 Email
        notify_event(conn, event)
        send_demo_email(level, payload.note, payload.device_id)

        conn.execute(
            "UPDATE events SET notify_status=? WHERE event_id=?",
            ("SENT", event_id)
        )
        conn.commit()

        return {
            "event_id": event_id,
            "level": level,
            "notified": True,
            "message": "event saved and notification sent"
        }

    except HTTPException:
        raise

    except Exception as e:
        print(f"❌ 建立事件或通知失敗: {e}", flush=True)

        try:
            if "event_id" in locals():
                conn.execute(
                    "UPDATE events SET notify_status=?, notify_error=? WHERE event_id=?",
                    ("ERROR", str(e), event_id)
                )
                conn.commit()
        except Exception:
            pass

        raise HTTPException(status_code=500, detail=str(e))

    finally:
        conn.close()


# ----------------------
# TELEGRAM BOT
# ----------------------

@app.post("/tg/webhook")
def telegram_webhook(update: dict):
    if "message" not in update:
        return {"ok": True}

    msg = update["message"]
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()

    conn = db_conn()

    try:
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
            tg_send(
                chat_id,
                "智能拐杖系統\n\n"
                "/pair 配貼拐杖\n"
                "/mydevices 查看我的拐杖\n"
                "/unbind cane-001 解除配對"
            )

        elif text == "/pair":
            tg_send(chat_id, "請輸入拐杖序號 (例如 cane-001)")

            conn.execute(
                "UPDATE users SET state='waiting_cane' WHERE telegram_id=?",
                (chat_id,)
            )

            conn.commit()

        elif text.startswith("/unbind"):
            parts = text.split()

            if len(parts) < 2:
                tg_send(chat_id, "請輸入要解除的拐杖序號，例如 /unbind cane-001")
            else:
                device_id = parts[1]

                conn.execute(
                    "DELETE FROM user_devices WHERE telegram_id=? AND device_id=?",
                    (chat_id, device_id)
                )

                conn.commit()

                tg_send(chat_id, f"已解除配對 {device_id}")

        elif text == "/mydevices":
            rows = conn.execute(
                "SELECT device_id FROM user_devices WHERE telegram_id=?",
                (chat_id,)
            ).fetchall()

            if not rows:
                tg_send(chat_id, "目前尚未配對任何拐杖")
            else:
                devices = "\n".join([r["device_id"] for r in rows])
                tg_send(chat_id, f"你的拐杖：\n{devices}")

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

        return {"ok": True}

    finally:
        conn.close()
