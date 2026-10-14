"""
FastAPI Entrypoint for Automated Voice Agent.
Handles Vobiz Telephony Media Streams, Pipecat Voice Pipeline, and Cal.com Scheduling.
"""

import os
import json
import uuid
import asyncio
import hmac
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from dotenv import load_dotenv

from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams,
)
from src.utils.audio_helpers import VobizTelephonySerializer, BrowserAudioSerializer
from src.agent.pipeline import create_agent_pipeline
from src.tasks.cal_booking import get_available_slots, book_appointment, list_event_types
from src.utils.google_auth import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    get_authorization_url,
    decode_oauth_state,
    exchange_code_for_tokens,
    encrypt_token,
)
from src.tasks.google_calendar import (
    seed_slots_for_doctor,
    get_available_slots_google,
    book_appointment_google,
    cancel_appointment_google,
    reschedule_appointment_google,
    process_calendar_sync,
)
from db.database import (
    get_db_connection,
    save_call_end,
    list_doctors,
    get_doctor,
    upsert_doctor,
    get_clinic_config,
    get_appointment,
)
from src.utils.whatsapp import (
    WHATSAPP_ACCESS_TOKEN,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_VERIFY_TOKEN,
    WHATSAPP_HOSPITAL_NAME,
    WHATSAPP_HOSPITAL_ADDRESS,
    WHATSAPP_HOSPITAL_PHONE,
    WHATSAPP_TIMEZONE,
    log_whatsapp_configuration,
    process_notification_queue,
    process_delivery_webhook,
    verify_webhook_signature,
)

load_dotenv()

SCHEDULING_PROVIDER = os.getenv("SCHEDULING_PROVIDER", "calcom").lower()


async def _notification_worker() -> None:
    while True:
        try:
            await process_notification_queue()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WhatsApp notification queue processing failed")
        await asyncio.sleep(5)


async def _slot_seed_worker() -> None:
    while True:
        try:
            doctors = list_doctors()
            for doc in doctors:
                if doc.get("status") == "active":
                    await seed_slots_for_doctor(doc["doctor_id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Google Calendar slot seeding worker error")
        await asyncio.sleep(12 * 3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    log_whatsapp_configuration()
    notif_worker = asyncio.create_task(_notification_worker())
    seed_worker = asyncio.create_task(_slot_seed_worker())
    try:
        yield
    finally:
        notif_worker.cancel()
        seed_worker.cancel()
        for w in (notif_worker, seed_worker):
            try:
                await w
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="Automated Voice Receptionist",
    description="Pipecat + Vobiz + Cal.com AI Voice Agent Backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
@app.get("/")
async def root(request: Request):
    """Health check and environment status. Redirects browsers to /dashboard automatically."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept and request.url.path == "/":
        return RedirectResponse(url="/dashboard")

    doctors = list_doctors()
    active_doctors = [d for d in doctors if d.get("status") == "active"]
    return {
        "status": "online",
        "service": "Automated Voice Agent",
        "scheduling_provider": SCHEDULING_PROVIDER,
        "stt_provider": os.getenv("STT_PROVIDER", "sarvam").lower(),
        "tts_provider": os.getenv("TTS_PROVIDER", "sarvam").lower(),
        "integrations": {
            "google_calendar": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
            "google_doctors_count": len(doctors),
            "google_active_doctors": len(active_doctors),
            "cal_com": bool(os.getenv("CAL.COM_API_KEY") or os.getenv("CAL_COM_API_KEY")),
            "sarvam": bool(os.getenv("SARVAM_API_KEY")),
            "whatsapp": bool(WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID),
            "deepgram": bool(os.getenv("DEEPGRAM_API_KEY")),
            "llm": bool(os.getenv("OPEN_ROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")),
            "llm_provider": "OpenRouter (gpt-4o-mini)" if (os.getenv("OPEN_ROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY")) else ("OpenAI" if os.getenv("OPENAI_API_KEY") else "None"),
            "cartesia": bool(os.getenv("CARTESIA_API_KEY")),
        },
        "endpoints": {
            "dashboard_ui": "/dashboard",
            "vobiz_inbound_webhook": "/vobiz/inbound",
            "vobiz_audio_stream_ws": "/ws/vobiz-stream",
            "availability": "/availability?doctorId=&date=&specialty=",
            "appointments": "/appointments",
            "doctors": "/doctors",
            "clinic_info": "/clinic-info",
            "google_oauth_start": "/oauth/google/start?doctor_id=",
            "google_webhook": "/webhooks/google/calendar",
            "cal_slots": "/cal/slots?date=YYYY-MM-DD",
            "call_logs": "/calls",
            "whatsapp_webhook": "/webhooks/whatsapp",
        },
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Interactive visual dashboard for inspecting voice agent status, Cal.com slots, and call logs."""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Automated Voice Agent Console</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #090d16;
            --card-bg: rgba(22, 30, 49, 0.7);
            --border: rgba(255, 255, 255, 0.08);
            --accent: #6366f1;
            --accent-glow: rgba(99, 102, 241, 0.25);
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
            --success: #10b981;
            --warning: #f59e0b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Outfit', sans-serif; }
        body {
            background: radial-gradient(circle at 50% 0%, #171e38 0%, var(--bg) 75%);
            color: var(--text-main);
            min-height: 100vh;
            padding: 2.5rem 1.5rem;
        }
        .container { max-width: 1100px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2rem; }
        h1 { font-size: 2rem; font-weight: 700; background: linear-gradient(135deg, #fff 40%, #a5b4fc); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .badge { display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0.8rem; border-radius: 9999px; background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.3); color: #34d399; font-size: 0.85rem; font-weight: 500; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1.5rem; margin-bottom: 2rem; }
        .card {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 1rem;
            padding: 1.5rem;
            backdrop-filter: blur(12px);
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
            transition: transform 0.2s, border-color 0.2s;
        }
        .card:hover { border-color: rgba(99, 102, 241, 0.4); }
        .card h2 { font-size: 1.15rem; font-weight: 600; margin-bottom: 1rem; display: flex; align-items: center; gap: 0.5rem; }
        .status-row { display: flex; justify-content: space-between; align-items: center; padding: 0.6rem 0; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
        .status-row:last-child { border-bottom: none; }
        .pill { padding: 0.2rem 0.6rem; border-radius: 0.35rem; font-size: 0.75rem; font-weight: 600; }
        .pill.active { background: rgba(16, 185, 129, 0.2); color: #34d399; }
        .pill.pending { background: rgba(245, 158, 11, 0.2); color: #fbbf24; }
        button, input {
            background: rgba(255,255,255,0.05);
            border: 1px solid var(--border);
            color: white;
            padding: 0.6rem 1rem;
            border-radius: 0.5rem;
            font-size: 0.9rem;
            outline: none;
        }
        button { cursor: pointer; background: var(--accent); border: none; font-weight: 600; transition: opacity 0.2s; }
        button:hover { opacity: 0.9; }
        .slots-container { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1rem; max-height: 220px; overflow-y: auto; }
        .slot-pill { padding: 0.4rem 0.8rem; background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border); border-radius: 0.4rem; font-size: 0.8rem; }
        .code-block { background: rgba(0,0,0,0.4); padding: 0.8rem; border-radius: 0.5rem; font-family: monospace; font-size: 0.8rem; color: #a5b4fc; overflow-x: auto; margin-top: 0.5rem; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>Automated Voice Receptionist</h1>
                <p style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.2rem;">Pipecat 1.12.0 &bull; Vobiz Telephony &bull; Cal.com Scheduling</p>
            </div>
            <div class="badge"><span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#10b981;"></span> Engine Online</div>
        </header>

        <!-- Live Web Audio Tester -->
        <div class="card" style="border: 1px solid rgba(99, 102, 241, 0.4); background: radial-gradient(circle at 50% 0%, rgba(99, 102, 241, 0.12) 0%, var(--card-bg) 80%); margin-bottom: 2rem;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
                <div>
                    <h2>🎙️ Test Agent Live in Browser (Zero Cost)</h2>
                    <p style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.2rem;">
                        Test Jacqueline, Deepgram, OpenRouter, and Cal.com booking directly with your computer's mic and speakers!
                    </p>
                </div>
                <div style="display: flex; align-items: center; gap: 1rem;">
                    <button id="call-btn" onclick="toggleBrowserCall()" style="background: #10b981; padding: 0.75rem 1.6rem; font-size: 1rem; border-radius: 0.6rem; font-weight: 600; display: flex; align-items: center; gap: 0.5rem; box-shadow: 0 4px 15px rgba(16, 185, 129, 0.3);">
                        <span>📞</span> <span id="call-btn-text">Call Jacqueline</span>
                    </button>
                    <span id="call-status" style="font-size: 0.85rem; color: var(--text-muted);">Ready to test</span>
                </div>
            </div>
        </div>

        <div class="grid">
            <!-- Integration Status -->
            <div class="card">
                <h2>Service Pipeline Integrations</h2>
                <div class="status-row">
                    <span>Scheduling Provider</span>
                    <span class="pill active" id="provider-pill">Cal.com</span>
                </div>
                <div class="status-row">
                    <span>Google Calendar (per-doctor OAuth)</span>
                    <span class="pill pending" id="google-status">Configure OAuth</span>
                </div>
                <div class="status-row">
                    <span>Connected Doctors</span>
                    <span class="pill active" id="doctors-count-pill">0</span>
                </div>
                <div class="status-row">
                    <span>VAD Engine (Silero)</span>
                    <span class="pill active">Ready</span>
                </div>
                <div class="status-row">
                    <span id="stt-label">STT Engine (Sarvam Saaras)</span>
                    <span class="pill pending" id="stt-status">Configure in .env</span>
                </div>
                <div class="status-row">
                    <span id="llm-label">LLM Engine (OpenRouter / OpenAI)</span>
                    <span class="pill pending" id="llm-status">Configure in .env</span>
                </div>
                <div class="status-row">
                    <span id="tts-label">TTS Engine (Sarvam Bulbul)</span>
                    <span class="pill pending" id="tts-status">Configure in .env</span>
                </div>
            </div>

            <!-- Doctor Calendar Connection Card -->
            <div class="card">
                <h2>🩺 Doctor Google Calendar Connect</h2>
                <p style="color: var(--text-muted); font-size: 0.85rem; margin-bottom: 0.8rem;">
                    Authorize a doctor's personal Gmail account to seed green availability slots and manage live appointments:
                </p>
                <div style="display: flex; flex-direction: column; gap: 0.6rem;">
                    <div style="display: flex; gap: 0.5rem;">
                        <input type="text" id="oauth-doctor-id" placeholder="doctor_id (e.g. dr_smith)" style="flex: 1;">
                        <input type="text" id="oauth-doctor-name" placeholder="Name (e.g. Dr. John Smith)" style="flex: 1;">
                    </div>
                    <button onclick="startDoctorOAuth()" style="background: #4f46e5; padding: 0.6rem 1rem;">🔗 Connect Doctor Calendar (OAuth 2.0)</button>
                    <div id="doctor-list-summary" style="font-size: 0.8rem; color: #a5b4fc; margin-top: 0.3rem;"></div>
                </div>
            </div>

            <!-- Live Slot Checker -->
            <div class="card">
                <h2>Live Availability Viewer</h2>
                <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
                    <input type="date" id="slot-date">
                    <select id="slot-provider" style="background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: white; padding: 0.6rem; border-radius: 0.5rem;">
                        <option value="google">Google Calendar</option>
                        <option value="calcom">Cal.com</option>
                    </select>
                    <button onclick="fetchSlots()">Check Slots</button>
                </div>
                <div id="slots-list" class="slots-container">
                    <span style="color: var(--text-muted); font-size: 0.85rem;">Select date to query live availability slots.</span>
                </div>
            </div>

            <!-- Vobiz Webhook Setup -->
            <div class="card">
                <h2>Vobiz Telecom Setup</h2>
                <p style="color: var(--text-muted); font-size: 0.85rem; margin-bottom: 0.6rem;">Configure your Vobiz inbound phone number webhook to stream to this server:</p>
                <div style="font-size: 0.8rem; color: #d1d5db;">Inbound Webhook URL:</div>
                <div class="code-block" id="webhook-url">https://&lt;your-domain&gt;/vobiz/inbound</div>
                <div style="font-size: 0.8rem; color: #d1d5db; margin-top: 0.8rem;">WebSocket Audio URL:</div>
                <div class="code-block" id="ws-url">wss://&lt;your-domain&gt;/ws/vobiz-stream</div>
            </div>
        </div>

        <!-- Call Logs Table -->
        <div class="card">
            <h2>Recent Calls & Appointments</h2>
            <div id="calls-table" style="color: var(--text-muted); font-size: 0.9rem;">
                No phone calls recorded yet. Once Vobiz routes a call, real-time records and bookings will show here.
            </div>
        </div>
    </div>

    <script>
        document.getElementById('slot-date').valueAsDate = new Date(Date.now() + 86400000);
        const host = window.location.host;
        document.getElementById('webhook-url').innerText = `https://${host}/vobiz/inbound`;
        document.getElementById('ws-url').innerText = `wss://${host}/ws/vobiz-stream`;

        let callWs = null;
        let audioCtx = null;
        let micStream = null;
        let micProcessor = null;
        let isCalling = false;
        let nextPlayTime = 0;
        let activeSources = [];

        async function toggleBrowserCall() {
            if (isCalling) {
                endBrowserCall();
            } else {
                startBrowserCall();
            }
        }

        async function startBrowserCall() {
            const btn = document.getElementById('call-btn');
            const btnText = document.getElementById('call-btn-text');
            const status = document.getElementById('call-status');

            status.innerText = 'Connecting mic & audio...';
            btn.style.opacity = '0.7';

            try {
                audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
                micStream = await navigator.mediaDevices.getUserMedia({
                    audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true }
                });

                const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                callWs = new WebSocket(`${wsProto}//${host}/ws/browser-call`);
                callWs.binaryType = 'arraybuffer';

                callWs.onopen = () => {
                    isCalling = true;
                    btnText.innerText = 'End Call';
                    btn.style.background = '#ef4444';
                    btn.style.boxShadow = '0 4px 15px rgba(239, 68, 68, 0.4)';
                    btn.style.opacity = '1';
                    status.innerHTML = '<span style="color: #34d399;">🟢 In Call</span> &bull; <span id="mic-indicator" style="color: #a5b4fc;">Mic ready</span>';

                    // Stream microphone to WebSocket without speaker feedback
                    const source = audioCtx.createMediaStreamSource(micStream);
                    micProcessor = audioCtx.createScriptProcessor(2048, 1, 1);
                    const silentGain = audioCtx.createGain();
                    silentGain.gain.value = 0;
                    source.connect(micProcessor);
                    micProcessor.connect(silentGain);
                    silentGain.connect(audioCtx.destination);

                    micProcessor.onaudioprocess = (e) => {
                        if (!isCalling || !callWs || callWs.readyState !== WebSocket.OPEN) return;
                        const input = e.inputBuffer.getChannelData(0);
                        const pcm = new Int16Array(input.length);
                        let sum = 0;
                        for (let i = 0; i < input.length; i++) {
                            const s = Math.max(-1, Math.min(1, input[i]));
                            pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
                            sum += s * s;
                        }
                        const rms = Math.sqrt(sum / input.length);
                        const indicator = document.getElementById('mic-indicator');
                        if (indicator) {
                            if (rms > 0.02) {
                                indicator.innerText = '🎙️ Speaking...';
                                indicator.style.color = '#34d399';
                            } else {
                                indicator.innerText = 'Listening...';
                                indicator.style.color = '#9ca3af';
                            }
                        }
                        callWs.send(pcm.buffer);
                    };
                };

                callWs.onmessage = async (e) => {
                    if (typeof e.data === 'string') {
                        try {
                            const msg = JSON.parse(e.data);
                            if (msg.type === 'interrupt') {
                                stopAllPlayback();
                            }
                        } catch(err) {}
                        return;
                    }

                    // Received binary PCM from Cartesia
                    const pcm16 = new Int16Array(e.data);
                    const float32 = new Float32Array(pcm16.length);
                    for (let i = 0; i < pcm16.length; i++) {
                        float32[i] = pcm16[i] / 32768.0;
                    }

                    playAudioChunk(float32);
                };

                callWs.onclose = () => {
                    endBrowserCall();
                };

                callWs.onerror = () => {
                    endBrowserCall();
                };

            } catch (err) {
                console.error(err);
                alert('Microphone access denied or audio error: ' + err.message);
                endBrowserCall();
            }
        }

        function playAudioChunk(samples) {
            if (!audioCtx) return;
            const audioBuffer = audioCtx.createBuffer(1, samples.length, 16000);
            audioBuffer.getChannelData(0).set(samples);
            const source = audioCtx.createBufferSource();
            source.buffer = audioBuffer;
            source.connect(audioCtx.destination);

            const now = audioCtx.currentTime;
            if (nextPlayTime < now) {
                nextPlayTime = now;
            }
            source.start(nextPlayTime);
            nextPlayTime += audioBuffer.duration;
            activeSources.push(source);
            source.onended = () => {
                const idx = activeSources.indexOf(source);
                if (idx > -1) activeSources.splice(idx, 1);
            };
        }

        function stopAllPlayback() {
            activeSources.forEach(s => {
                try { s.stop(); } catch(e) {}
            });
            activeSources = [];
            if (audioCtx) nextPlayTime = audioCtx.currentTime;
        }

        function endBrowserCall() {
            isCalling = false;
            stopAllPlayback();

            if (micStream) {
                micStream.getTracks().forEach(t => t.stop());
                micStream = null;
            }
            if (micProcessor) {
                micProcessor.disconnect();
                micProcessor = null;
            }
            if (callWs) {
                try { callWs.close(); } catch(e) {}
                callWs = null;
            }
            if (audioCtx) {
                try { audioCtx.close(); } catch(e) {}
                audioCtx = null;
            }

            const btn = document.getElementById('call-btn');
            const btnText = document.getElementById('call-btn-text');
            const status = document.getElementById('call-status');

            btnText.innerText = 'Call Jacqueline';
            btn.style.background = '#10b981';
            btn.style.boxShadow = '0 4px 15px rgba(16, 185, 129, 0.3)';
            btn.style.opacity = '1';
            status.innerText = 'Call ended. Ready to test again.';
        }

        function startDoctorOAuth() {
            const docId = document.getElementById('oauth-doctor-id').value.trim();
            const docName = document.getElementById('oauth-doctor-name').value.trim();
            if (!docId) {
                alert('Please enter a doctor ID (e.g. dr_smith)');
                return;
            }
            window.location.href = `/oauth/google/start?doctor_id=${encodeURIComponent(docId)}&name=${encodeURIComponent(docName)}`;
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/');
                const data = await res.json();
                document.getElementById('provider-pill').innerText = (data.scheduling_provider || 'calcom').toUpperCase();
                if (data.integrations.google_calendar) {
                    document.getElementById('google-status').className = 'pill active';
                    document.getElementById('google-status').innerText = 'Ready';
                }
                document.getElementById('doctors-count-pill').innerText = `${data.integrations.google_active_doctors || 0} active (${data.integrations.google_doctors_count || 0} total)`;

                if (data.integrations.llm) {
                    document.getElementById('llm-status').className = 'pill active';
                    document.getElementById('llm-status').innerText = 'Ready';
                    document.getElementById('llm-label').innerText = `LLM Engine (${data.integrations.llm_provider})`;
                }
                const sttProv = (data.stt_provider || 'sarvam').toLowerCase();
                const ttsProv = (data.tts_provider || 'sarvam').toLowerCase();

                if (sttProv === 'sarvam') {
                    document.getElementById('stt-label').innerText = 'STT Engine (Sarvam Saaras)';
                    if (data.integrations.sarvam) {
                        document.getElementById('stt-status').className = 'pill active';
                        document.getElementById('stt-status').innerText = 'Ready (Sarvam)';
                    }
                } else {
                    document.getElementById('stt-label').innerText = 'STT Engine (Deepgram Nova-2)';
                    if (data.integrations.deepgram) {
                        document.getElementById('stt-status').className = 'pill active';
                        document.getElementById('stt-status').innerText = 'Ready (Deepgram)';
                    }
                }

                if (ttsProv === 'sarvam') {
                    document.getElementById('tts-label').innerText = 'TTS Engine (Sarvam Bulbul - kavya)';
                    if (data.integrations.sarvam) {
                        document.getElementById('tts-status').className = 'pill active';
                        document.getElementById('tts-status').innerText = 'Ready (Sarvam)';
                    }
                } else {
                    document.getElementById('tts-label').innerText = 'TTS Engine (Cartesia Sonic)';
                    if (data.integrations.cartesia) {
                        document.getElementById('tts-status').className = 'pill active';
                        document.getElementById('tts-status').innerText = 'Ready (Cartesia)';
                    }
                }

                // Fetch doctors list
                const docRes = await fetch('/doctors');
                const docData = await docRes.json();
                if (docData.doctors && docData.doctors.length > 0) {
                    document.getElementById('doctor-list-summary').innerText = 'Connected: ' + docData.doctors.map(d => `${d.name} (${d.doctor_id})`).join(', ');
                }
            } catch(e) {}
        }

        async function fetchSlots() {
            const date = document.getElementById('slot-date').value;
            const provider = document.getElementById('slot-provider').value;
            const container = document.getElementById('slots-list');
            container.innerHTML = `<span style="color: #9ca3af;">Checking ${provider.toUpperCase()} availability...</span>`;
            try {
                const res = await fetch(`/availability?date=${date}&provider=${provider}`);
                const data = await res.json();
                if (data.slots && data.slots.length > 0) {
                    container.innerHTML = data.slots.map(s => {
                        const time = typeof s === 'string' ? new Date(s).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'}) : (s.display_time || s.start_time);
                        const label = typeof s === 'object' && s.doctor_name ? `${s.doctor_name}: ${time}` : time;
                        return `<div class="slot-pill">${label}</div>`;
                    }).join('');
                } else {
                    container.innerHTML = '<span style="color: #f87171;">No slots available on this date.</span>';
                }
            } catch(e) {
                container.innerHTML = '<span style="color: #f87171;">Failed to fetch slots.</span>';
            }
        }

        fetchStatus();
        fetchSlots();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


# ==========================================
# CAL.COM REST ENDPOINTS
# ==========================================
@app.get("/cal/event-types")
async def get_event_types():
    events = await list_event_types()
    return {"event_types": events}


@app.get("/cal/slots")
async def check_slots(date: str = Query(..., description="Date formatted as YYYY-MM-DD")):
    slots = await get_available_slots(date)
    return {"date": date, "slots": slots, "count": len(slots)}


@app.post("/cal/book")
async def book_slot(request: Request):
    data = await request.json()
    result = await book_appointment(
        start_time=data.get("start_time"),
        name=data.get("name"),
        email=data.get("email"),
        phone=data.get("phone"),
        notes=data.get("notes", ""),
    )
    return result


@app.get("/webhooks/whatsapp")
async def verify_whatsapp_webhook(
    mode: Optional[str] = Query(None, alias="hub.mode"),
    verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    if not WHATSAPP_VERIFY_TOKEN or not mode or not verify_token or not challenge:
        raise HTTPException(status_code=503, detail="WhatsApp webhook verification is not configured")
    if mode != "subscribe" or not hmac.compare_digest(verify_token, WHATSAPP_VERIFY_TOKEN):
        raise HTTPException(status_code=403, detail="Webhook verification failed")
    return PlainTextResponse(challenge)


@app.post("/webhooks/whatsapp")
async def receive_whatsapp_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_webhook_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    process_delivery_webhook(payload)
    return JSONResponse({"status": "ok"})


# =========================================================
# GOOGLE CALENDAR PER-DOCTOR SCHEDULING & OAUTH ENDPOINTS
# =========================================================

@app.get("/oauth/google/start")
async def google_oauth_start(
    doctor_id: str = Query(..., description="Unique Doctor ID"),
    name: Optional[str] = Query(None, description="Doctor Name"),
    specialty: Optional[str] = Query(None, description="Doctor Specialty"),
    request: Request = None,
):
    """Initiates Google OAuth 2.0 authorization code flow for a doctor's personal calendar."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET is not configured in .env",
        )

    existing = get_doctor(doctor_id)
    if not existing:
        upsert_doctor({
            "doctor_id": doctor_id,
            "name": name or f"Dr. {doctor_id.title()}",
            "specialty": specialty or "General Medicine",
            "status": "needs_reconnect",
        })

    host = request.headers.get("host", "localhost:8000")
    scheme = request.url.scheme
    redirect_uri = f"{scheme}://{host}/oauth/google/callback"
    auth_url, _ = get_authorization_url(doctor_id=doctor_id, redirect_uri=redirect_uri)
    return RedirectResponse(url=auth_url)


@app.get("/oauth/google/callback")
async def google_oauth_callback(
    code: str = Query(..., description="Authorization code"),
    state: str = Query(..., description="State parameter from Google"),
    request: Request = None,
):
    """Exchanges Google OAuth code for refresh token, stores it securely, and triggers slot seeding."""
    state_data = decode_oauth_state(state)
    doctor_id = state_data.get("doctor_id")
    if not doctor_id:
        raise HTTPException(status_code=400, detail="Invalid state payload in OAuth callback")

    host = request.headers.get("host", "localhost:8000")
    scheme = request.url.scheme
    redirect_uri = f"{scheme}://{host}/oauth/google/callback"

    try:
        token_data = exchange_code_for_tokens(code=code, redirect_uri=redirect_uri)
    except Exception as exc:
        logger.error(f"Failed to exchange OAuth code for doctor {doctor_id}: {exc}")
        raise HTTPException(status_code=400, detail=f"OAuth token exchange failed: {exc}")

    refresh_token = token_data.get("refresh_token")
    encrypted_refresh = encrypt_token(refresh_token) if refresh_token else None

    doc = get_doctor(doctor_id) or {"doctor_id": doctor_id}
    upsert_doctor({
        "doctor_id": doctor_id,
        "name": doc.get("name", f"Dr. {doctor_id.title()}"),
        "email": doc.get("email", ""),
        "encrypted_refresh_token": encrypted_refresh,
        "status": "active",
    })

    # Trigger initial slot seeding
    asyncio.create_task(seed_slots_for_doctor(doctor_id))

    return HTMLResponse(content=f"""
    <html>
        <body style="font-family: system-ui; background: #0f172a; color: white; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
            <div style="background: #1e293b; padding: 2.5rem; border-radius: 1rem; text-align: center; max-width: 480px; box-shadow: 0 10px 25px rgba(0,0,0,0.5);">
                <div style="font-size: 3rem; margin-bottom: 1rem;">✅</div>
                <h2 style="margin: 0 0 0.5rem 0;">Google Calendar Connected!</h2>
                <p style="color: #94a3b8; font-size: 0.95rem;">Calendar for doctor <strong>{doctor_id}</strong> is successfully connected. Initial availability slots are being seeded.</p>
                <a href="/dashboard" style="display: inline-block; margin-top: 1.5rem; background: #6366f1; color: white; padding: 0.75rem 1.5rem; border-radius: 0.5rem; text-decoration: none; font-weight: 600;">Return to Dashboard</a>
            </div>
        </body>
    </html>
    """)


@app.post("/webhooks/google/calendar")
async def google_calendar_webhook(request: Request):
    """Receives push notifications from Google Calendar watch channels and processes changes."""
    channel_id = request.headers.get("X-Goog-Channel-ID")
    resource_state = request.headers.get("X-Goog-Resource-State")
    logger.info(f"Google Calendar webhook received. Channel: {channel_id}, State: {resource_state}")

    if resource_state == "sync":
        return JSONResponse({"status": "sync_acknowledged"})

    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT doctor_id FROM calendar_sync_state WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()

    target_doctor_id = row["doctor_id"] if row else None
    if not target_doctor_id and channel_id and channel_id.startswith("chan_"):
        parts = channel_id.split("_")
        if len(parts) >= 2:
            target_doctor_id = parts[1]

    if target_doctor_id:
        asyncio.create_task(process_calendar_sync(target_doctor_id))

    return JSONResponse({"status": "ok"})


@app.get("/availability")
async def check_availability_endpoint(
    doctorId: Optional[str] = Query(None, description="Doctor ID or name filter"),
    date: Optional[str] = Query(None, description="Target date in YYYY-MM-DD format"),
    specialty: Optional[str] = Query(None, description="Doctor specialty filter"),
    provider: Optional[str] = Query(None, description="Provider override (google or calcom)"),
):
    """Query live availability slots for a date."""
    chosen_provider = (provider or SCHEDULING_PROVIDER).lower()
    if chosen_provider == "google":
        slots = await get_available_slots_google(
            doctor_or_specialty=doctorId or specialty,
            date_str=date,
        )
        return {
            "provider": "google",
            "date": date,
            "doctorId": doctorId,
            "specialty": specialty,
            "slots": slots,
            "count": len(slots),
        }
    else:
        slots = await get_available_slots(date_str=date or "")
        return {
            "provider": "calcom",
            "date": date,
            "slots": slots,
            "count": len(slots),
        }


@app.post("/appointments")
async def create_appointment_endpoint(request: Request):
    """Book a new appointment against Google Calendar with conflict prevention."""
    data = await request.json()
    doctor_id = data.get("doctorId") or data.get("doctor_id")
    slot_start = data.get("slotStart") or data.get("slot_start") or data.get("start_time")
    patient_name = data.get("patientName") or data.get("patient_name") or data.get("name")
    phone = data.get("phone")
    email = data.get("email")
    reason = data.get("reason", "")
    call_id = data.get("callId") or data.get("call_id")

    if not doctor_id or not slot_start or not patient_name:
        raise HTTPException(status_code=400, detail="doctorId, slotStart, and patientName are required")

    result = await book_appointment_google(
        doctor_id=doctor_id,
        slot_start=slot_start,
        patient_name=patient_name,
        phone=phone or "",
        email=email,
        reason=reason,
        call_id=call_id,
    )

    if result.get("status") == "conflict" or result.get("code") == 409:
        return JSONResponse(status_code=409, content=result)
    if result.get("status") == "error":
        return JSONResponse(status_code=400, content=result)
    return JSONResponse(status_code=201, content=result)


@app.post("/appointments/{appointment_id}/cancel")
async def cancel_appointment_endpoint(appointment_id: str, request: Request):
    """Cancel an appointment and restore green slot availability."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    result = await cancel_appointment_google(appointment_id=appointment_id, reason=body.get("reason"))
    if result.get("status") == "error":
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@app.post("/appointments/{appointment_id}/reschedule")
async def reschedule_appointment_endpoint(appointment_id: str, request: Request):
    """Reschedule an existing appointment to a new time slot."""
    body = await request.json()
    new_slot = body.get("newSlotStart") or body.get("new_slot_start") or body.get("start_time")
    if not new_slot:
        raise HTTPException(status_code=400, detail="newSlotStart is required")
    result = await reschedule_appointment_google(appointment_id=appointment_id, new_slot_start=new_slot)
    if result.get("status") == "conflict" or result.get("code") == 409:
        return JSONResponse(status_code=409, content=result)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@app.get("/clinic-info")
async def get_clinic_info_endpoint():
    """Returns clinic timings, working hours, break windows, and address info."""
    config = get_clinic_config()
    doctors = list_doctors()
    return {
        "clinic_name": WHATSAPP_HOSPITAL_NAME or "City Care Medical Center",
        "address": WHATSAPP_HOSPITAL_ADDRESS or "100 Health Avenue, Suite 400",
        "phone": WHATSAPP_HOSPITAL_PHONE or "+1-800-555-CARE",
        "timezone": config.get("timezone", WHATSAPP_TIMEZONE),
        "working_days": config.get("working_days"),
        "open_time": config.get("open_time"),
        "close_time": config.get("close_time"),
        "break_windows": config.get("break_windows"),
        "holidays": config.get("holidays"),
        "doctors_count": len(doctors),
    }


@app.get("/doctors")
async def list_doctors_endpoint(specialty: Optional[str] = None):
    """Returns list of registered doctors with their status and specialty."""
    docs = list_doctors(specialty=specialty)
    sanitized = []
    for d in docs:
        sanitized.append({
            "doctor_id": d["doctor_id"],
            "name": d["name"],
            "email": d["email"],
            "specialty": d.get("specialty"),
            "slot_duration_minutes": d.get("slot_duration_minutes"),
            "timezone": d.get("timezone"),
            "status": d.get("status"),
        })
    return {"doctors": sanitized}


# ==========================================
# CALL LOGS & DATABASE ENDPOINTS
# ==========================================
@app.get("/calls")
async def get_calls():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM calls ORDER BY started_at DESC LIMIT 50")
        calls = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT * FROM bookings ORDER BY created_at DESC LIMIT 50")
        bookings = [dict(row) for row in cursor.fetchall()]
    return {"calls": calls, "bookings": bookings}


# ==========================================
# VOBIZ TELEPHONY WEBHOOK & WEBSOCKET STREAM
# ==========================================
@app.post("/vobiz/inbound")
async def vobiz_inbound_call(request: Request):
    """
    Called by Vobiz when an incoming phone call is received on your DID number.
    Returns the media stream instructions to connect to this server's WebSocket.
    """
    body = await request.json() if request.headers.get("content-type") == "application/json" else await request.form()
    caller = body.get("From", body.get("caller", "Unknown"))
    call_id = body.get("CallSid", body.get("call_id", str(uuid.uuid4())))

    host = request.headers.get("host", "localhost:8000")
    ws_url = f"wss://{host}/ws/vobiz-stream?call_id={call_id}&caller={caller}"

    logger.info(f"Inbound call from {caller} (Call ID: {call_id}). Directing to {ws_url}")

    # Standard XML / JSON media stream instruction for Vobiz
    response_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""
    return HTMLResponse(content=response_xml, media_type="application/xml")


@app.websocket("/ws/vobiz-stream")
async def vobiz_websocket_stream(
    websocket: WebSocket,
    call_id: Optional[str] = None,
    caller: Optional[str] = None,
):
    """
    Bidirectional audio WebSocket stream endpoint for Vobiz and Pipecat.
    """
    await websocket.accept()

    call_session_id = call_id or str(uuid.uuid4())
    caller_phone = caller or "Unknown"
    logger.info(f"WebSocket connected for Call ID: {call_session_id}, Caller: {caller_phone}")

    serializer = VobizTelephonySerializer()

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
            serializer=serializer,
        ),
    )

    task, runner, trigger_greeting = create_agent_pipeline(
        transport=transport,
        call_id=call_session_id,
        caller_phone=caller_phone,
    )

    try:
        # Launch Pipecat runner and send initial greeting
        runner_task = asyncio.create_task(runner.run(task))
        await trigger_greeting()
        await runner_task

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for call {call_session_id}")
    except Exception as e:
        logger.error(f"Error during call {call_session_id}: {e}")
    finally:
        save_call_end(call_id=call_session_id, transcript="Call ended", status="completed")
        logger.info(f"Cleaned up call session {call_session_id}")


@app.websocket("/ws/browser-call")
async def browser_websocket_stream(websocket: WebSocket):
    """
    Direct Web Audio WebSocket stream for instant testing from the browser dashboard.
    """
    await websocket.accept()
    call_session_id = f"browser-{uuid.uuid4().hex[:8]}"
    logger.info(f"Browser testing call connected: {call_session_id}")

    serializer = BrowserAudioSerializer()
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
            serializer=serializer,
        ),
    )

    task, runner, trigger_greeting = create_agent_pipeline(
        transport=transport,
        call_id=call_session_id,
        caller_phone="+91-Browser-User",
    )

    try:
        runner_task = asyncio.create_task(runner.run(task))
        await trigger_greeting()
        await runner_task
    except WebSocketDisconnect:
        logger.info(f"Browser call disconnected: {call_session_id}")
    except Exception as e:
        logger.error(f"Error during browser call: {e}")
    finally:
        save_call_end(call_id=call_session_id, transcript="Browser test completed", status="completed")
        logger.info(f"Cleaned up browser call {call_session_id}")
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
