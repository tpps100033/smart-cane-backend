# Cane Fall Backend (FastAPI + SQLite + Telegram) — Railway-ready

## What you get
- Device registry (<=10 devices) with per-device API keys
- Heartbeat endpoint (optional)
- Events endpoint (fall alerts)
- SQLite persistence (recommended to mount Railway Volume at /data)
- Telegram notification + retry + logging

## Local run
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export ADMIN_KEY="admin_secret_123"
export TG_BOT_TOKEN="123456:ABC..."
export TG_CHAT_ID="123456789"
export DB_PATH="./cane.db"

uvicorn main:app --host 0.0.0.0 --port 8000
```

Health:
```bash
curl http://127.0.0.1:8000/healthz
```

Create a device:
```bash
curl -X POST http://127.0.0.1:8000/admin/devices \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: admin_secret_123" \
  -d '{"device_id":"cane-001","api_key":"devkey_cane001_abcdefgh","alias":"阿公拐杖"}'
```

Send a test event:
```bash
curl -X POST http://127.0.0.1:8000/api/v1/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: devkey_cane001_abcdefgh" \
  -d '{"device_id":"cane-001","level":"RED","note":"test_from_curl","firmware":"0.1.0"}'
```

## Railway deploy checklist
1. Create Railway project → Deploy from GitHub.
2. Add **Volume** mounted at `/data`.
3. Set Variables:
   - `ADMIN_KEY`
   - `TG_BOT_TOKEN`
   - `TG_CHAT_ID`
   - `DB_PATH=/data/cane.db`
4. Done. Use your Railway URL as backend base.

## ESP32 notes
- Set `BACKEND_BASE` to your Railway URL, e.g. `https://xxxxx.up.railway.app`
- Set `DEVICE_ID` and `DEVICE_API_KEY` to match your registered device.
