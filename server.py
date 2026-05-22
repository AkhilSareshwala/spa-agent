import asyncio
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse

from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from bot import bot

pcs: dict = {}

ICE_SERVERS = [
    "stun:stun.l.google.com:19302",
    "stun:stun1.l.google.com:19302",
    "turn:openrelay.metered.ca:80?transport=tcp",
    "turn:openrelay.metered.ca:443?transport=tcp",
    "turns:openrelay.metered.ca:443?transport=tcp",
]

CLIENT_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Spa-Agent</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding:1rem}
    .wrap{max-width:480px;margin:0 auto;display:flex;flex-direction:column;gap:12px;padding-top:1rem}
    .card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:1rem 1.25rem}
    .row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
    .dot{width:8px;height:8px;border-radius:50%;background:#64748b;flex-shrink:0;transition:background 0.3s}
    .dot.live{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,0.2)}
    .label{font-size:11px;color:#94a3b8;font-weight:600;text-transform:uppercase;letter-spacing:0.06em}
    .status-text{font-size:13px;color:#94a3b8;margin-left:auto}
    .metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
    .metric{background:#0f172a;border-radius:8px;padding:0.75rem;text-align:center}
    .metric-label{font-size:11px;color:#64748b;margin-bottom:4px}
    .metric-val{font-size:22px;font-weight:500;color:#e2e8f0;transition:color 0.3s}
    .metric-val.updated{color:#22c55e}
    .metric-unit{font-size:11px;color:#64748b}
    .transcript{display:flex;flex-direction:column;gap:8px;max-height:300px;overflow-y:auto;padding-right:4px}
    .transcript::-webkit-scrollbar{width:4px}
    .transcript::-webkit-scrollbar-track{background:transparent}
    .transcript::-webkit-scrollbar-thumb{background:#334155;border-radius:2px}
    .turn{padding:8px 12px;border-radius:10px;font-size:14px;line-height:1.5;max-width:88%}
    .turn.user{background:#1e3a5f;color:#bae6fd;align-self:flex-start;border-bottom-left-radius:3px}
    .turn.bot{background:#1d4ed8;color:#fff;align-self:flex-end;border-bottom-right-radius:3px}
    .turn .who{font-size:11px;font-weight:600;opacity:0.65;margin-bottom:3px}
    .btn-row{display:flex;gap:8px}
    button{flex:1;padding:11px;border-radius:10px;border:1px solid #334155;background:transparent;color:#e2e8f0;font-size:14px;cursor:pointer;font-family:inherit;transition:background 0.15s}
    button:hover{background:#334155}
    #startBtn{background:#22c55e;color:#fff;border-color:#22c55e}
    #startBtn:hover{background:#16a34a}
    #stopBtn{background:#ef4444;color:#fff;border-color:#ef4444;display:none}
    #stopBtn:hover{background:#dc2626}
    .empty{color:#475569;font-size:13px;text-align:center;padding:1rem 0}
    .debug-log{font-size:11px;color:#475569;font-family:monospace;max-height:100px;overflow-y:auto;padding-top:4px;border-top:1px solid #1e293b;margin-top:8px}
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="row">
      <div class="dot" id="statusDot"></div>
      <span style="font-weight:500;font-size:15px">Spa agent — Luna</span>
      <span class="status-text" id="statusText">Click Connect to start</span>
    </div>
    <div class="btn-row">
      <button id="startBtn" onclick="startCall()">Connect</button>
      <button id="stopBtn" onclick="stopCall()">End Call</button>
    </div>
  </div>

  <div class="card">
    <div class="label" style="margin-bottom:10px">Latency (TTFB)</div>
    <div class="metrics">
      <div class="metric">
        <div class="metric-label">STT</div>
        <div class="metric-val" id="sttMs">—</div>
        <div class="metric-unit">ms</div>
      </div>
      <div class="metric">
        <div class="metric-label">LLM</div>
        <div class="metric-val" id="llmMs">—</div>
        <div class="metric-unit">ms</div>
      </div>
      <div class="metric">
        <div class="metric-label">TTS</div>
        <div class="metric-val" id="ttsMs">—</div>
        <div class="metric-unit">ms</div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="label" style="margin-bottom:10px">Live Transcript</div>
    <div class="transcript" id="transcript">
      <div class="empty" id="emptyMsg">Transcript will appear here during the call</div>
    </div>
  </div>

  <div class="card" id="debugCard" style="display:none">
    <div class="label" style="margin-bottom:6px">Debug Log</div>
    <div class="debug-log" id="debugLog"></div>
  </div>
</div>

<audio id="audio" autoplay></audio>

<script>
let pc, localStream, dataChannel;

document.addEventListener("keydown", e => {
  if (e.shiftKey && e.key === "D") {
    const c = document.getElementById("debugCard");
    c.style.display = c.style.display === "none" ? "block" : "none";
  }
});

function dbg(msg) {
  const log = document.getElementById("debugLog");
  const line = document.createElement("div");
  line.textContent = new Date().toISOString().slice(11,23) + " " + msg;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
  console.log("[Luna]", msg);
}

function setStatus(text, live) {
  document.getElementById("statusText").textContent = text;
  document.getElementById("statusDot").className = "dot" + (live ? " live" : "");
}

function addTurn(role, text) {
  const box = document.getElementById("transcript");
  const empty = document.getElementById("emptyMsg");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = "turn " + role;
  div.innerHTML = '<div class="who">' + (role === "user" ? "You" : "Luna") + "</div>" + text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function setMetric(processor, ms) {
  const p = processor.toLowerCase();
  let elId = null;
  if (p.includes("stt") || p.includes("deepgram") || p.includes("whisper") || p.includes("assemblyai")) {
    elId = "sttMs";
  } else if (p.includes("llm") || p.includes("groq") || p.includes("openai") || p.includes("anthropic") || p.includes("gemini") || p.includes("google")) {
    elId = "llmMs";
  } else if (p.includes("tts") || p.includes("sarvam") || p.includes("cartesia") || p.includes("elevenlabs")) {
    elId = "ttsMs";
  }
  if (!elId) { dbg("unknown metric: " + processor); return; }
  const el = document.getElementById(elId);
  el.textContent = ms;
  el.classList.add("updated");
  setTimeout(() => el.classList.remove("updated"), 800);
}

async function startCall() {
  setStatus("Requesting microphone...", false);
  try {
    localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  } catch(e) {
    setStatus("Microphone error: " + e.message, false);
    return;
  }

  setStatus("Connecting...", false);
  pc = new RTCPeerConnection({
    iceServers: [
      { urls: "stun:stun.l.google.com:19302" },
      { urls: "turn:openrelay.metered.ca:80",   username: "openrelayproject", credential: "openrelayproject" },
      { urls: "turn:openrelay.metered.ca:443",  username: "openrelayproject", credential: "openrelayproject" },
      { urls: "turns:openrelay.metered.ca:443", username: "openrelayproject", credential: "openrelayproject" }
    ]
  });

  dataChannel = pc.createDataChannel("pipecat");
  dataChannel.onopen  = () => { dbg("data channel open"); setStatus("Connected — Luna is live", true); };
  dataChannel.onclose = () => dbg("data channel closed");
  dataChannel.onerror = (e) => dbg("data channel error: " + e);
  dataChannel.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "transcript") addTurn(msg.role, msg.text);
      else if (msg.type === "metric") setMetric(msg.processor, msg.ttfb_ms);
    } catch(err) { dbg("parse error: " + err); }
  };

  localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
  pc.ontrack = (e) => { document.getElementById("audio").srcObject = e.streams[0]; };
  pc.oniceconnectionstatechange = () => {
    const s = pc.iceConnectionState;
    dbg("ICE: " + s);
    if (["failed","disconnected","closed"].includes(s)) setStatus("Connection lost: " + s, false);
  };

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  await new Promise(r => {
    if (pc.iceGatheringState === "complete") return r();
    pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === "complete") r(); };
    setTimeout(r, 4000);
  });

  const res = await fetch("/offer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type })
  });
  if (!res.ok) { setStatus("Server error: " + res.status, false); return; }

  const answer = await res.json();
  await pc.setRemoteDescription(answer);
  document.getElementById("startBtn").style.display = "none";
  document.getElementById("stopBtn").style.display  = "block";
}

async function stopCall() {
  if (dataChannel) dataChannel.close();
  if (pc) pc.close();
  if (localStream) localStream.getTracks().forEach(t => t.stop());
  pc = null; localStream = null; dataChannel = null;
  document.getElementById("startBtn").style.display = "block";
  document.getElementById("stopBtn").style.display  = "none";
  setStatus("Call ended", false);
}
</script>
</body>
</html>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    for conn in list(pcs.values()):
        try:
            await conn.close()
        except Exception:
            pass
    pcs.clear()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
@app.get("/client", response_class=HTMLResponse)
@app.get("/client/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=CLIENT_HTML)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/offer")
async def offer(request: Request):
    body = await request.json()
    conn = SmallWebRTCConnection(ice_servers=ICE_SERVERS)
    pcs[conn.pc_id] = conn
    await conn.initialize(sdp=body["sdp"], type=body["type"])
    answer = conn.get_answer()
    asyncio.ensure_future(bot(conn))
    return JSONResponse(answer)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)