import os
import uuid
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List
import smtplib
from email.mime.text import MIMEText
from email.header import Header as EmailHeader

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

APP_NAME = "cane-fall-backend"

DB_PATH = os.getenv("DB_PATH", "/data/cane.db")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

ADMIN_TELEGRAM_IDS = os.getenv("ADMIN_TELEGRAM_IDS", "")
ADMIN_TELEGRAM_IDS = [x.strip() for x in ADMIN_TELEGRAM_IDS.split(",") if x.strip()]

ADMIN_KEY = os.getenv("ADMIN_KEY", "")

# ----------------------
# [新增 Email 功能] EMAIL 設定
# ----------------------
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
SENDER_EMAIL = "smartcanebackend@gmail.com"
DEMO_RECEIVER_EMAIL = "tpps100033@gmail.com"
SENDER_PASSWORD = "auqdikpsikfnhekw"

def send_demo_email(level: str, note: str, device_id: str):
    """直接執行：依照 PRD 發送緊急通報郵件"""
    now = datetime.now(timezone.utc).isoformat()
    subject = f"🚨 智能拐杖警報：{level} (技術演示)"
    
    body = f"""
    --- Smart-Cane 智能拐杖即時通報 ---
    發送時間 (UTC)：{now}
    設備編號：{device_id}
    警報等級：{level}
    狀況描述：{note}
    ----------------------------------
    此郵件由 Smart-Cane 系統自動發送，用於技術驗證。
    """

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = SENDER_EMAIL
    msg['To'] = DEMO_RECEIVER_EMAIL
    msg['Subject'] = EmailHeader(subject, 'utf-8')

    try:
        # 強制印出開始發信的 log
        print(f"⏳ 準備發送 Email 給 {DEMO_RECEIVER_EMAIL}...", flush=True)
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, DEMO_RECEIVER_EMAIL, msg.as_string())
        print(f"✅ Email 成功發送至 {DEMO_RECEIVER_EMAIL}", flush=True)
    except Exception as e:
        print(f"❌ Email 發送失敗: {e}", flush=True)

app = FastAPI(title=APP_NAME)

# ----------------------
# UTILS & DB INIT (保持不變)
# ----------------------

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS devices(device_id TEXT PRIMARY KEY, api_key TEXT, alias TEXT, created_at TEXT, last_seen_at TEXT, last_battery_v REAL, last_rssi INTEGER, firmware TEXT, is_active INTEGER DEFAULT 1)")
    conn.execute("CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, created_at TEXT, device_id TEXT, device_ts TEXT, level TEXT, fsr INTEGER, acc_peak REAL, variance REAL, note TEXT, firmware TEXT, battery_v REAL, rssi INTEGER, ack_local INTEGER, notify_status TEXT, notify_error TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS users(telegram_id TEXT PRIMARY KEY, username TEXT, role TEXT, state TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS user_devices(telegram_id TEXT, device_id TEXT, created_at TEXT, PRIMARY KEY (telegram_id, device_id))")
    conn.commit()
    conn.close()

@app.on_event("startup")
def startup():
    init_db()

# ----------------------
# TELEGRAM (保持不變)
# ----------------------

def tg_send(chat_id, text):
    if not TG_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

def get_notify_targets(conn, device_id):
    targets = set()
    if TG_CHAT_ID:
        targets.add(TG_CHAT_ID)
    for admin in ADMIN_TELEGRAM_IDS:
        targets.add(admin)
    rows = conn.execute("SELECT telegram_id FROM user_devices WHERE device_id=?", (device_id,)).fetchall()
    for r in rows:
        targets.add(r["telegram_id"])
    return list(targets)

def notify_event(conn, event_row):
    device_id = event_row["device_id"]
    level = event_row["level"]
    if level == "YELLOW":
        emoji, status = "🟡", "已恢復站立"
    elif level == "ORANGE":
        emoji, status = "🟠", "掙扎中"
    elif level == "RED":
        emoji, status = "🔴", "可能無法起身"
    else:
        emoji, status = "⚪", "未知"
        
    device = conn.execute("SELECT alias FROM devices WHERE device_id=?", (device_id,)).fetchone()
    alias = device["alias"] if device and device["alias"] else device_id
    msg = f"{emoji} 跌倒警示 {level}\n\n狀態: {status}\n設備: {alias} ({device_id})\ntime: {event_row['created_at']}\nbattery: {event_row['battery_v']}\nrssi: {event_row['rssi']}\nnote: {event_row['note']}"
    targets = get_notify_targets(conn, device_id)
    for chat_id in targets:
        tg_send(chat_id, msg)

# ----------------------
# MODELS (保持不變)
# ----------------------

class DeviceCreate(BaseModel):
    device_id: str
    api_key: str
    alias: Optional[str]

class EventIn(BaseModel):
    device_id: str
    level: str
    note: Optional[str] = None
    battery_v: Optional[float] = None
    rssi: Optional[int] = None

# ----------------------
# ADMIN API (保持不變)
# ----------------------

@app.post("/admin/devices")
def create_device(payload: DeviceCreate, x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403)
    conn = db_conn()
    conn.execute("INSERT INTO devices VALUES(?,?,?,?,?,?,?, ?,1)", (payload.device_id, payload.api_key, payload.alias, utc_now_iso(), None, None, None, None))
    conn.commit()
    conn.close()
    return {"device_id": payload.device_id}

@app.get("/admin/devices")
def list_devices(x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403)
    conn = db_conn()
    rows = conn.execute("SELECT * FROM devices").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/admin/events")
def list_events(x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403)
    conn = db_conn()
    rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ----------------------
# DEVICE API (修改此處)
# ----------------------

@app.post("/api/v1/events")
def create_event(payload: EventIn, x_api_key: str = Header(default="")):
    conn = db_conn()
    device = conn.execute("SELECT * FROM devices WHERE device_id=?", (payload.device_id,)).fetchone()
    if not device:
        raise HTTPException(status_code=404)
    if device["api_key"] != x_api_key:
        raise HTTPException(status_code=403)

    event_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id, utc_now_iso(), payload.device_id, None, payload.level, None, None, None, payload.note, None, payload.battery_v, payload.rssi, 0, "PENDING", None)
    )
    conn.commit()
    event = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
    notify_event(conn, event)

    # [新增 Email 功能] 拔除背景執行，改為直接呼叫，確保能看到 Log
    print(f"🔍 檢查到的 Level 為: {payload.level}", flush=True)
    if payload.level in ["RED", "ORANGE"]:
        send_demo_email(payload.level, payload.note, payload.device_id)

    conn.close()
    return {"event_id": event_id}

# ----------------------
# TELEGRAM BOT (保持不變)
# ----------------------

@app.post("/tg/webhook")
def telegram_webhook(update: dict):
    if "message" not in update:
        return {"ok": True}
    msg = update["message"]
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()
    conn = db_conn()
    user = conn.execute("SELECT * FROM users WHERE telegram_id=?", (chat_id,)).fetchone()
    if not user:
        role = "admin" if chat_id in ADMIN_TELEGRAM_IDS else "user"
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?)", (chat_id, msg["from"].get("username"), role, "idle", utc_now_iso()))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE telegram_id=?", (chat_id,)).fetchone()
    if text == "/start":
        tg_send(chat_id, "智能拐杖系統\n\n/pair 配對拐杖\n/mydevices 查看我的拐杖\n/unbind cane-001 解除配對")
    elif text == "/pair":
        tg_send(chat_id, "請輸入拐杖序號 (例如 cane-001)")
        conn.execute("UPDATE users SET state='waiting_cane' WHERE telegram_id=?", (chat_id,))
        conn.commit()
    elif user["state"] == "waiting_cane":
        device_id = text
        device = conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not device:
            tg_send(chat_id, "找不到此拐杖")
        else:
            conn.execute("INSERT OR IGNORE INTO user_devices VALUES(?,?,?)", (chat_id, device_id, utc_now_iso()))
            conn.execute("UPDATE users SET state='idle' WHERE telegram_id=?", (chat_id,))
            conn.commit()
            tg_send(chat_id, f"已配對 {device_id}")
    conn.close()
    return {"ok": True}
