"""
Live CRUD Proof Server — runs actual DB operations against a real SQLite DB
and serves a beautiful visual dashboard at http://localhost:7788
"""

import os
import sys
import json
import sqlite3
import tempfile
import subprocess
import asyncio
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

import db.database as database
from src.user.user_service import get_caller_profile

# ── Use a dedicated demo database ──────────────────────────────────────────
DEMO_DB = os.path.join(os.path.dirname(__file__), "crud_demo.db")
database.DB_PATH = DEMO_DB
database.init_db()

app = FastAPI(title="CRUD Proof Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Helpers ────────────────────────────────────────────────────────────────

def _rows(table: str):
    with database.get_db_connection() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 20")]

def _reset_demo_db():
    """Wipe and re-seed demo data so the UI always has something to show."""
    with database.get_db_connection() as conn:
        conn.execute("DELETE FROM notification_outbox")
        conn.execute("DELETE FROM bookings")
        conn.execute("DELETE FROM calls")
        conn.execute("DELETE FROM whatsapp_preferences")
        conn.commit()

    # ── Seed calls ──
    database.save_call_start("call-001", "+911234567890")
    database.save_call_start("call-002", "+919876543210")
    database.save_call_end("call-001", "Patient asked about 9 AM slot for Dr. Sharma.", "Appointment inquiry", "completed")

    # ── Seed WhatsApp consent ──
    database.set_whatsapp_preference("clinic-a", "+911234567890", True, "voice_call", "v1")
    database.set_whatsapp_preference("clinic-a", "+919876543210", False, "voice_call", "v1")

    # ── Seed bookings ──
    database.save_booking(
        call_id="call-001",
        cal_booking_id="cal-7001",
        guest_name="Rahul Sharma",
        guest_email="rahul@example.com",
        start_time="2026-11-01T09:00:00Z",
        phone_number="+911234567890",
        tenant_id="clinic-a",
        notification_template={"name": "booking_confirmation", "language": "en_US", "parameters": []},
    )
    database.save_booking(
        call_id="call-002",
        cal_booking_id="cal-7002",
        guest_name="Priya Mehta",
        guest_email="priya@example.com",
        start_time="2026-11-02T11:00:00Z",
        phone_number="+919876543210",
        tenant_id="clinic-a",
        notification_template=None,
    )


_reset_demo_db()


# ── API Routes ─────────────────────────────────────────────────────────────

@app.get("/api/state")
async def get_state():
    calls = _rows("calls")
    bookings = _rows("bookings")
    prefs = _rows("whatsapp_preferences")
    notifs = _rows("notification_outbox")
    return JSONResponse({
        "calls": calls,
        "bookings": bookings,
        "whatsapp_preferences": prefs,
        "notification_outbox": notifs,
    })

@app.post("/api/demo/create-call")
async def demo_create_call(body: dict):
    call_id = f"call-{int(datetime.utcnow().timestamp())}"
    phone = body.get("phone", "+910000000000")
    database.save_call_start(call_id, phone)
    return {"ok": True, "call_id": call_id, "phone": phone, "status": "in-progress"}

@app.post("/api/demo/end-call")
async def demo_end_call(body: dict):
    call_id = body.get("call_id")
    if not call_id:
        return JSONResponse({"ok": False, "error": "call_id required"}, status_code=400)
    database.save_call_end(call_id, "Demo transcript — patient booked appointment.", "Appointment booked", "completed")
    return {"ok": True, "call_id": call_id, "status": "completed"}

@app.post("/api/demo/create-booking")
async def demo_create_booking(body: dict):
    call_id = body.get("call_id")
    name = body.get("name", "Demo Patient")
    email = body.get("email", "demo@clinic.test")
    phone = body.get("phone", "+911234567890")
    consent = body.get("consent", False)
    ts = int(datetime.utcnow().timestamp())
    booking_id = f"cal-{ts}"
    if consent:
        database.set_whatsapp_preference("clinic-a", phone, True, "voice_call", "v1")
    database.save_booking(
        call_id=call_id,
        cal_booking_id=booking_id,
        guest_name=name,
        guest_email=email,
        start_time=f"2026-12-01T{(ts%12)+8:02d}:00:00Z",
        phone_number=phone,
        tenant_id="clinic-a",
        notification_template={"name":"booking_confirmation","language":"en_US","parameters":[]} if consent else None,
    )
    return {"ok": True, "booking_id": booking_id, "whatsapp_queued": consent}

@app.get("/api/demo/caller-profile")
async def demo_caller_profile(phone: str = "+911234567890"):
    profile = get_caller_profile(phone)
    return profile

@app.post("/api/demo/notification-lifecycle")
async def demo_notification_lifecycle():
    notifs = _rows("notification_outbox")
    if not notifs:
        return {"ok": False, "error": "No notifications in queue. Create a booking with WhatsApp consent first."}
    claimed = database.claim_notification_batch(limit=1)
    if claimed:
        nid = claimed[0]["notification_id"]
        database.mark_notification_sent(nid, f"wamid.demo-{int(datetime.utcnow().timestamp())}")
        provider_id = _rows("notification_outbox")[0].get("provider_message_id","")
        if provider_id:
            database.update_notification_status(provider_id, "DELIVERED")
    return {"ok": True, "steps": ["QUEUED → PROCESSING", "PROCESSING → SENT", "SENT → DELIVERED"]}

@app.post("/api/demo/reset")
async def demo_reset():
    _reset_demo_db()
    return {"ok": True, "message": "Demo database reset to seed data."}

@app.post("/api/demo/run-tests")
async def demo_run_tests():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_doctor_crud.py", "-v", "--tb=short", "--no-header"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT)
    )
    lines = (result.stdout + result.stderr).splitlines()
    tests = []
    for line in lines:
        if "PASSED" in line:
            tests.append({"name": line.strip().replace(" PASSED", "").split("::")[-1], "status": "passed"})
        elif "FAILED" in line and "short test summary" not in line:
            tests.append({"name": line.strip().replace(" FAILED", "").split("::")[-1], "status": "failed"})
    summary_line = next((l for l in lines if "passed" in l or "failed" in l and "===" in l), "")
    return {"ok": True, "tests": tests, "summary": summary_line.strip("= ").strip(), "returncode": result.returncode}


# ── Dashboard HTML ─────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CRUD Operations — Live Proof Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #060912;
    --surface: rgba(13,19,36,0.85);
    --surface2: rgba(20,28,52,0.7);
    --border: rgba(99,102,241,0.18);
    --border2: rgba(255,255,255,0.06);
    --accent: #6366f1;
    --accent2: #818cf8;
    --green: #10b981;
    --red: #f43f5e;
    --yellow: #f59e0b;
    --blue: #38bdf8;
    --text: #f1f5f9;
    --muted: #64748b;
    --pass: #10b981;
    --fail: #f43f5e;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    background-image: radial-gradient(ellipse at 20% 0%, rgba(99,102,241,0.12) 0%, transparent 60%),
                      radial-gradient(ellipse at 80% 100%, rgba(16,185,129,0.08) 0%, transparent 60%);
  }
  .topbar {
    background: rgba(6,9,18,0.9);
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(20px);
    padding: 1rem 2rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .logo { display: flex; align-items: center; gap: 0.75rem; }
  .logo-icon {
    width: 38px; height: 38px; border-radius: 10px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    display: flex; align-items: center; justify-content: center;
    font-size: 1.2rem; box-shadow: 0 0 20px rgba(99,102,241,0.4);
  }
  .logo-text { font-size: 1.1rem; font-weight: 700; }
  .logo-sub { font-size: 0.75rem; color: var(--muted); margin-top: 1px; }
  .status-pill {
    display: flex; align-items: center; gap: 0.4rem;
    background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3);
    padding: 0.35rem 0.9rem; border-radius: 100px;
    font-size: 0.78rem; font-weight: 600; color: var(--green);
  }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.4;} }

  .main { max-width: 1400px; margin: 0 auto; padding: 2rem; }

  /* HERO STATS */
  .hero { display: grid; grid-template-columns: repeat(4,1fr); gap: 1rem; margin-bottom: 2rem; }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 1.25rem 1.5rem;
    backdrop-filter: blur(12px);
    transition: transform 0.2s, border-color 0.2s;
    cursor: default;
  }
  .stat-card:hover { transform: translateY(-2px); border-color: var(--accent); }
  .stat-label { font-size: 0.75rem; font-weight: 500; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
  .stat-value { font-size: 2.2rem; font-weight: 800; margin: 0.25rem 0; background: linear-gradient(135deg,#fff,var(--accent2)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .stat-desc { font-size: 0.78rem; color: var(--muted); }

  /* LAYOUT */
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem; }
  .grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem; }
  .span2 { grid-column: span 2; }

  /* CARDS */
  .card {
    background: var(--surface);
    border: 1px solid var(--border2);
    border-radius: 16px;
    padding: 1.5rem;
    backdrop-filter: blur(12px);
  }
  .card-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 1.25rem;
  }
  .card-title {
    font-size: 0.95rem; font-weight: 600;
    display: flex; align-items: center; gap: 0.5rem;
  }
  .card-badge {
    font-size: 0.7rem; font-weight: 600;
    padding: 0.2rem 0.55rem; border-radius: 6px;
    background: rgba(99,102,241,0.15); color: var(--accent2);
  }

  /* BUTTONS */
  .btn {
    display: inline-flex; align-items: center; gap: 0.4rem;
    padding: 0.55rem 1.1rem; border-radius: 8px;
    font-size: 0.83rem; font-weight: 600;
    border: none; cursor: pointer;
    transition: all 0.18s;
  }
  .btn-primary { background: var(--accent); color: white; }
  .btn-primary:hover { background: var(--accent2); box-shadow: 0 0 20px rgba(99,102,241,0.4); }
  .btn-success { background: rgba(16,185,129,0.15); color: var(--green); border: 1px solid rgba(16,185,129,0.3); }
  .btn-success:hover { background: rgba(16,185,129,0.25); }
  .btn-danger { background: rgba(244,63,94,0.12); color: var(--red); border: 1px solid rgba(244,63,94,0.3); }
  .btn-danger:hover { background: rgba(244,63,94,0.22); }
  .btn-ghost { background: rgba(255,255,255,0.05); color: var(--text); border: 1px solid var(--border2); }
  .btn-ghost:hover { background: rgba(255,255,255,0.08); }
  .btn:disabled { opacity: 0.45; cursor: not-allowed; }

  /* FORM */
  .form-row { display: flex; gap: 0.6rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
  input[type=text], input[type=email] {
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border2);
    border-radius: 7px;
    color: var(--text);
    padding: 0.5rem 0.85rem;
    font-size: 0.83rem;
    font-family: inherit;
    outline: none;
    transition: border-color 0.2s;
    min-width: 0;
    flex: 1;
  }
  input:focus { border-color: var(--accent); }
  label { display: flex; align-items: center; gap: 0.4rem; font-size: 0.8rem; color: var(--muted); cursor: pointer; }
  input[type=checkbox] { accent-color: var(--accent); }

  /* TABLE */
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  th { text-align: left; color: var(--muted); font-weight: 500; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; padding: 0.4rem 0.75rem; border-bottom: 1px solid var(--border2); }
  td { padding: 0.55rem 0.75rem; border-bottom: 1px solid rgba(255,255,255,0.03); white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .empty { color: var(--muted); font-size: 0.82rem; padding: 1.5rem 0; text-align: center; }

  /* STATUS BADGES */
  .badge { display: inline-block; padding: 0.2rem 0.55rem; border-radius: 6px; font-size: 0.7rem; font-weight: 700; }
  .badge-green { background: rgba(16,185,129,0.15); color: var(--green); }
  .badge-yellow { background: rgba(245,158,11,0.15); color: var(--yellow); }
  .badge-blue { background: rgba(56,189,248,0.15); color: var(--blue); }
  .badge-red { background: rgba(244,63,94,0.15); color: var(--red); }
  .badge-purple { background: rgba(99,102,241,0.18); color: var(--accent2); }
  .badge-gray { background: rgba(100,116,139,0.2); color: var(--muted); }

  /* TEST RUNNER */
  .test-list { max-height: 340px; overflow-y: auto; padding-right: 4px; }
  .test-item {
    display: flex; align-items: center; gap: 0.6rem;
    padding: 0.45rem 0.6rem; border-radius: 8px;
    font-size: 0.8rem; margin-bottom: 3px;
    font-family: 'JetBrains Mono', monospace;
    transition: background 0.15s;
  }
  .test-item:hover { background: rgba(255,255,255,0.03); }
  .test-item.passed { color: #e2e8f0; }
  .test-item.failed { color: var(--red); background: rgba(244,63,94,0.05); }
  .test-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .test-dot.passed { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .test-dot.failed { background: var(--red); box-shadow: 0 0 6px var(--red); }
  .test-summary {
    margin-top: 1rem; padding: 0.75rem 1rem; border-radius: 10px;
    background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.25);
    font-size: 0.85rem; font-weight: 600; color: var(--green);
    font-family: 'JetBrains Mono', monospace;
    text-align: center;
  }
  .test-summary.fail { background: rgba(244,63,94,0.1); border-color: rgba(244,63,94,0.3); color: var(--red); }

  /* LIFECYCLE FLOW */
  .flow { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin: 1rem 0; }
  .flow-step {
    padding: 0.4rem 0.9rem; border-radius: 8px; font-size: 0.8rem; font-weight: 600;
    border: 1px solid var(--border2); color: var(--muted);
    transition: all 0.3s;
  }
  .flow-step.active { border-color: var(--accent); color: var(--accent2); background: rgba(99,102,241,0.12); }
  .flow-step.done { border-color: var(--green); color: var(--green); background: rgba(16,185,129,0.1); }
  .flow-arrow { color: var(--muted); font-size: 0.9rem; }

  /* TOAST */
  #toast-container { position: fixed; bottom: 1.5rem; right: 1.5rem; display: flex; flex-direction: column; gap: 0.5rem; z-index: 9999; }
  .toast {
    padding: 0.75rem 1.2rem; border-radius: 10px; font-size: 0.83rem; font-weight: 500;
    backdrop-filter: blur(16px); box-shadow: 0 8px 30px rgba(0,0,0,0.4);
    animation: slideIn 0.3s ease;
    max-width: 320px;
  }
  .toast.success { background: rgba(16,185,129,0.2); border: 1px solid rgba(16,185,129,0.4); color: #6ee7b7; }
  .toast.error { background: rgba(244,63,94,0.2); border: 1px solid rgba(244,63,94,0.4); color: #fda4af; }
  .toast.info { background: rgba(99,102,241,0.2); border: 1px solid rgba(99,102,241,0.4); color: #a5b4fc; }
  @keyframes slideIn { from { transform: translateX(120%); opacity:0; } to { transform: translateX(0); opacity:1; } }

  .mono { font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; }
  .text-green { color: var(--green); }
  .text-red { color: var(--red); }
  .text-muted { color: var(--muted); }
  .text-accent { color: var(--accent2); }
  .mt-1 { margin-top: 0.5rem; }
  .mt-2 { margin-top: 1rem; }
  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(99,102,241,0.3); border-radius: 99px; }
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">
    <div class="logo-icon">🧪</div>
    <div>
      <div class="logo-text">Doctor CRUD — Live Proof Dashboard</div>
      <div class="logo-sub">Automated Voice Agent · Real SQLite Operations</div>
    </div>
  </div>
  <div class="status-pill">
    <span class="dot"></span>
    Live Database Connected
  </div>
</div>

<div class="main">

  <!-- STATS -->
  <div class="hero" id="hero-stats">
    <div class="stat-card">
      <div class="stat-label">📞 Calls</div>
      <div class="stat-value" id="stat-calls">–</div>
      <div class="stat-desc">in calls table</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">📅 Bookings</div>
      <div class="stat-value" id="stat-bookings">–</div>
      <div class="stat-desc">appointments saved</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">💬 WhatsApp Prefs</div>
      <div class="stat-value" id="stat-prefs">–</div>
      <div class="stat-desc">consent records</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">📬 Notifications</div>
      <div class="stat-value" id="stat-notifs">–</div>
      <div class="stat-desc">outbox entries</div>
    </div>
  </div>

  <!-- ROW 1: Test Runner + Lifecycle -->
  <div class="grid2">

    <!-- TEST RUNNER -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">🧪 Run All 35 CRUD Tests <span class="card-badge">pytest</span></div>
        <div style="display:flex;gap:0.5rem">
          <button class="btn btn-primary" onclick="runTests()" id="run-btn">▶ Run Tests</button>
        </div>
      </div>
      <div id="test-area">
        <div class="empty">Click "Run Tests" to execute all 35 CRUD tests live against SQLite.</div>
      </div>
    </div>

    <!-- NOTIFICATION LIFECYCLE -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">📬 Notification Lifecycle <span class="card-badge">5 states</span></div>
        <button class="btn btn-success" onclick="runLifecycle()">▶ Simulate</button>
      </div>
      <p class="text-muted" style="font-size:0.82rem;margin-bottom:0.75rem">Watch a notification walk through all delivery states in real time.</p>
      <div class="flow">
        <div class="flow-step" id="step-queued">QUEUED</div>
        <div class="flow-arrow">→</div>
        <div class="flow-step" id="step-processing">PROCESSING</div>
        <div class="flow-arrow">→</div>
        <div class="flow-step" id="step-sent">SENT</div>
        <div class="flow-arrow">→</div>
        <div class="flow-step" id="step-delivered">DELIVERED</div>
      </div>
      <div id="lifecycle-log" style="font-size:0.78rem;color:var(--muted);margin-top:0.5rem;font-family:'JetBrains Mono',monospace;min-height:40px;"></div>
      <div class="table-wrap mt-2">
        <table>
          <thead><tr><th>notification_id</th><th>status</th><th>retry</th><th>event</th></tr></thead>
          <tbody id="notif-table-body"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ROW 2: CREATE Call + Caller Profile -->
  <div class="grid2">

    <!-- CREATE CALL -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">📞 CREATE Call <span class="card-badge">save_call_start</span></div>
      </div>
      <div class="form-row">
        <input type="text" id="new-phone" placeholder="+91XXXXXXXXXX" value="+917700000001">
        <button class="btn btn-primary" onclick="createCall()">Create Call</button>
      </div>
      <div id="call-result" class="mono text-muted mt-1"></div>
      <div id="call-update-area" style="display:none">
        <div class="form-row mt-1">
          <button class="btn btn-success" onclick="endCall()">✓ End Call (UPDATE)</button>
        </div>
        <div id="end-result" class="mono text-muted mt-1"></div>
      </div>
      <div class="table-wrap mt-2">
        <table>
          <thead><tr><th>call_id</th><th>caller</th><th>status</th><th>started_at</th></tr></thead>
          <tbody id="calls-table-body"></tbody>
        </table>
      </div>
    </div>

    <!-- CALLER PROFILE READ -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">👤 READ Caller Profile <span class="card-badge">get_caller_profile</span></div>
      </div>
      <div class="form-row">
        <input type="text" id="lookup-phone" placeholder="+91XXXXXXXXXX" value="+911234567890">
        <button class="btn btn-ghost" onclick="lookupProfile()">🔍 Lookup</button>
      </div>
      <div id="profile-result" style="margin-top:1rem;"></div>
    </div>
  </div>

  <!-- ROW 3: Create Booking + WhatsApp Prefs -->
  <div class="grid2">

    <!-- CREATE BOOKING -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">📅 CREATE Booking <span class="card-badge">save_booking</span></div>
      </div>
      <div class="form-row">
        <input type="text" id="b-name" placeholder="Patient name" value="Aman Patel" style="flex:2">
        <input type="email" id="b-email" placeholder="Email" value="aman@demo.test" style="flex:2">
      </div>
      <div class="form-row">
        <input type="text" id="b-phone" placeholder="+91..." value="+911234567890" style="flex:2">
        <input type="text" id="b-callid" placeholder="call_id" value="call-001" style="flex:2">
      </div>
      <div class="form-row">
        <label><input type="checkbox" id="b-consent" checked> WhatsApp consent (queue notification)</label>
      </div>
      <button class="btn btn-primary" onclick="createBooking()">📅 Create Booking</button>
      <div id="booking-result" class="mono text-muted mt-1"></div>
      <div class="table-wrap mt-2">
        <table>
          <thead><tr><th>booking_id</th><th>guest</th><th>start</th><th>tenant</th><th>status</th></tr></thead>
          <tbody id="bookings-table-body"></tbody>
        </table>
      </div>
    </div>

    <!-- WHATSAPP PREFS -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">💬 WhatsApp Preferences <span class="card-badge">CRUD</span></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>tenant</th><th>phone</th><th>enabled</th><th>source</th><th>updated</th></tr></thead>
          <tbody id="prefs-table-body"></tbody>
        </table>
      </div>
      <div style="margin-top:1rem;display:flex;gap:0.5rem;">
        <button class="btn btn-danger" onclick="resetDemo()">🔄 Reset Demo DB</button>
        <button class="btn btn-ghost" onclick="loadState()">↻ Refresh</button>
      </div>
    </div>
  </div>

</div><!-- /main -->

<div id="toast-container"></div>

<script>
let currentCallId = null;

// ── Toast ───────────────────────────────────────────────────────────
function toast(msg, type='info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// ── Fetch helpers ────────────────────────────────────────────────────
async function api(path, method='GET', body=null) {
  const opts = { method, headers: {'Content-Type':'application/json'} };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return res.json();
}

// ── Load full state ──────────────────────────────────────────────────
async function loadState() {
  const data = await api('/api/state');
  document.getElementById('stat-calls').textContent = data.calls.length;
  document.getElementById('stat-bookings').textContent = data.bookings.length;
  document.getElementById('stat-prefs').textContent = data.whatsapp_preferences.length;
  document.getElementById('stat-notifs').textContent = data.notification_outbox.length;

  renderCallsTable(data.calls);
  renderBookingsTable(data.bookings);
  renderPrefsTable(data.whatsapp_preferences);
  renderNotifTable(data.notification_outbox);
}

// ── Badge helper ─────────────────────────────────────────────────────
function statusBadge(s) {
  const map = {
    'in-progress':'badge-yellow','completed':'badge-green','abandoned':'badge-red',
    'confirmed':'badge-green',
    'QUEUED':'badge-yellow','PROCESSING':'badge-blue','SENT':'badge-purple',
    'DELIVERED':'badge-green','READ':'badge-green','FAILED':'badge-red',
  };
  return `<span class="badge ${map[s]||'badge-gray'}">${s}</span>`;
}

function truncate(s, n=22) { return s && s.length > n ? s.slice(0,n)+'…' : (s||'—'); }

// ── Tables ───────────────────────────────────────────────────────────
function renderCallsTable(rows) {
  const tb = document.getElementById('calls-table-body');
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="4" class="empty">No calls yet</td></tr>'; return; }
  tb.innerHTML = rows.map(r => `<tr>
    <td class="mono text-accent">${truncate(r.call_id,14)}</td>
    <td>${r.caller_number||'—'}</td>
    <td>${statusBadge(r.status)}</td>
    <td class="text-muted">${(r.started_at||'').slice(0,19)}</td>
  </tr>`).join('');
}

function renderBookingsTable(rows) {
  const tb = document.getElementById('bookings-table-body');
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="5" class="empty">No bookings yet</td></tr>'; return; }
  tb.innerHTML = rows.map(r => `<tr>
    <td class="mono text-accent">${truncate(r.cal_booking_id,10)}</td>
    <td>${r.guest_name||'—'}</td>
    <td class="text-muted">${(r.start_time||'').slice(0,16).replace('T',' ')}</td>
    <td>${r.tenant_id||'—'}</td>
    <td>${statusBadge(r.status)}</td>
  </tr>`).join('');
}

function renderPrefsTable(rows) {
  const tb = document.getElementById('prefs-table-body');
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="5" class="empty">No preferences</td></tr>'; return; }
  tb.innerHTML = rows.map(r => `<tr>
    <td>${r.tenant_id}</td>
    <td class="mono">${r.phone_number}</td>
    <td>${r.enabled ? '<span class="badge badge-green">✓ Yes</span>' : '<span class="badge badge-red">✗ No</span>'}</td>
    <td class="text-muted">${r.consent_source||'—'}</td>
    <td class="text-muted">${(r.updated_at||'').slice(0,16)}</td>
  </tr>`).join('');
}

function renderNotifTable(rows) {
  const tb = document.getElementById('notif-table-body');
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="4" class="empty">No notifications</td></tr>'; return; }
  tb.innerHTML = rows.map(r => `<tr>
    <td class="mono text-accent">${truncate(r.notification_id,20)}</td>
    <td>${statusBadge(r.delivery_status)}</td>
    <td class="text-muted">${r.retry_count}</td>
    <td class="text-muted">${r.event_type||'—'}</td>
  </tr>`).join('');
}

// ── CREATE Call ──────────────────────────────────────────────────────
async function createCall() {
  const phone = document.getElementById('new-phone').value.trim();
  const res = await api('/api/demo/create-call', 'POST', {phone});
  currentCallId = res.call_id;
  document.getElementById('call-result').innerHTML =
    `<span class="text-green">✓ Created</span>  call_id: <span class="text-accent">${res.call_id}</span>  status: <span class="text-muted">${res.status}</span>`;
  document.getElementById('call-update-area').style.display = 'block';
  toast(`Call created: ${res.call_id}`, 'success');
  loadState();
}

// ── END Call (UPDATE) ────────────────────────────────────────────────
async function endCall() {
  if (!currentCallId) return;
  const res = await api('/api/demo/end-call', 'POST', {call_id: currentCallId});
  document.getElementById('end-result').innerHTML =
    `<span class="text-green">✓ Updated</span>  ${res.call_id} → <span class="text-green">completed</span>`;
  toast(`Call ended → completed`, 'success');
  loadState();
}

// ── CREATE Booking ───────────────────────────────────────────────────
async function createBooking() {
  const body = {
    call_id: document.getElementById('b-callid').value,
    name: document.getElementById('b-name').value,
    email: document.getElementById('b-email').value,
    phone: document.getElementById('b-phone').value,
    consent: document.getElementById('b-consent').checked,
  };
  const res = await api('/api/demo/create-booking', 'POST', body);
  document.getElementById('booking-result').innerHTML =
    `<span class="text-green">✓ Booked</span>  id: <span class="text-accent">${res.booking_id}</span>  WhatsApp: <span class="${res.whatsapp_queued?'text-green':'text-muted'}">${res.whatsapp_queued?'queued':'not queued'}</span>`;
  toast(res.whatsapp_queued ? 'Booking + WhatsApp notification queued!' : 'Booking saved (no consent)', 'success');
  loadState();
}

// ── READ Caller Profile ──────────────────────────────────────────────
async function lookupProfile() {
  const phone = document.getElementById('lookup-phone').value.trim();
  const res = await api(`/api/demo/caller-profile?phone=${encodeURIComponent(phone)}`);
  const el = document.getElementById('profile-result');
  if (res.known_user) {
    el.innerHTML = `
      <div style="background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.25);border-radius:10px;padding:1rem;">
        <div style="font-weight:700;color:var(--green);margin-bottom:0.5rem;">✓ Known Caller</div>
        <div class="mono" style="display:grid;grid-template-columns:auto 1fr;gap:0.3rem 1rem;">
          <span class="text-muted">name</span><span>${res.name}</span>
          <span class="text-muted">email</span><span>${res.email}</span>
          <span class="text-muted">last_booking</span><span>${res.last_booking}</span>
          <span class="text-muted">last_status</span><span>${statusBadge(res.last_status)}</span>
        </div>
      </div>`;
    toast(`Found: ${res.name}`, 'success');
  } else {
    el.innerHTML = `<div style="color:var(--muted);font-size:0.85rem;">⚠ No record found for <span class="mono">${phone}</span></div>`;
    toast('Unknown caller', 'info');
  }
}

// ── Notification Lifecycle ───────────────────────────────────────────
async function runLifecycle() {
  ['queued','processing','sent','delivered'].forEach(s => {
    document.getElementById(`step-${s}`).className = 'flow-step';
  });
  document.getElementById('lifecycle-log').textContent = 'Simulating…';
  document.getElementById('step-queued').classList.add('done');

  await new Promise(r => setTimeout(r, 400));
  document.getElementById('step-processing').classList.add('active');
  document.getElementById('lifecycle-log').textContent = '→ Claiming batch (QUEUED → PROCESSING)…';

  await new Promise(r => setTimeout(r, 600));
  const res = await api('/api/demo/notification-lifecycle', 'POST');

  if (!res.ok) {
    document.getElementById('lifecycle-log').textContent = '⚠ ' + res.error;
    toast(res.error, 'error'); return;
  }

  document.getElementById('step-processing').classList.replace('active','done');
  document.getElementById('step-sent').classList.add('active');
  document.getElementById('lifecycle-log').textContent = '→ mark_notification_sent (PROCESSING → SENT)…';

  await new Promise(r => setTimeout(r, 500));
  document.getElementById('step-sent').classList.replace('active','done');
  document.getElementById('step-delivered').classList.add('active');
  document.getElementById('lifecycle-log').textContent = '→ update_notification_status DELIVERED via webhook…';

  await new Promise(r => setTimeout(r, 500));
  document.getElementById('step-delivered').classList.replace('active','done');
  document.getElementById('lifecycle-log').textContent = '✓ Full lifecycle complete: QUEUED → PROCESSING → SENT → DELIVERED';
  toast('Lifecycle simulated successfully!', 'success');
  loadState();
}

// ── Run Tests ─────────────────────────────────────────────────────────
async function runTests() {
  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Running…';
  document.getElementById('test-area').innerHTML = '<div class="empty">Executing 35 tests against SQLite…</div>';

  const res = await api('/api/demo/run-tests', 'POST');
  btn.disabled = false;
  btn.textContent = '▶ Run Tests';

  const allPass = res.returncode === 0;
  let html = `<div class="test-list">`;
  for (const t of res.tests) {
    html += `<div class="test-item ${t.status}"><span class="test-dot ${t.status}"></span>${t.name}</div>`;
  }
  html += `</div>`;
  if (res.summary) {
    html += `<div class="test-summary ${allPass?'':'fail'}">${allPass?'✅':''} ${res.summary}</div>`;
  }
  document.getElementById('test-area').innerHTML = html;
  toast(allPass ? `All ${res.tests.length} tests passed!` : 'Some tests failed', allPass?'success':'error');
}

// ── Reset ─────────────────────────────────────────────────────────────
async function resetDemo() {
  await api('/api/demo/reset', 'POST');
  ['queued','processing','sent','delivered'].forEach(s => {
    document.getElementById(`step-${s}`).className = 'flow-step';
  });
  document.getElementById('lifecycle-log').textContent = '';
  document.getElementById('call-result').textContent = '';
  document.getElementById('end-result').textContent = '';
  document.getElementById('call-update-area').style.display = 'none';
  document.getElementById('booking-result').textContent = '';
  document.getElementById('profile-result').innerHTML = '';
  currentCallId = null;
  toast('Demo database reset!', 'info');
  loadState();
}

// ── Auto-refresh ──────────────────────────────────────────────────────
loadState();
setInterval(loadState, 8000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


if __name__ == "__main__":
    print("\n" + "="*60)
    print("  [TEST]  Doctor CRUD Proof Dashboard")
    print("  [URL]   http://localhost:7788")
    print("="*60 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=7790, log_level="warning")
