# main.py
import os, json, logging, asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    force=True,
)
log = logging.getLogger("bridge")
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# ---------- Secrets / Config ----------
def _get_secret(secret_id: str) -> str | None:
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
        if not project_id:
            return None
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        return resp.payload.data.decode("utf-8")
    except Exception as e:
        log.info(f"GSM not used for '{secret_id}': {e}")
        return None

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or _get_secret("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in env or Google Secret Manager")

OPENAI_REALTIME_URL = os.environ.get(
    "OPENAI_REALTIME_URL",
    "wss://api.openai.com/v1/realtime?model=gpt-realtime&voice=alloy&temperature=0.8",
)

PORT = int(os.environ.get("PORT", 8080))
SYSTEM_MESSAGE = os.environ.get(
    "SYSTEM_MESSAGE",
    "You are a helpful and friendly AI phone assistant. "
    "Keep responses concise and natural. Interrupt yourself when the caller starts speaking."
)

# Prefer the stable *.a.run.app host if you set WS_HOST; fall back to CLOUD_RUN_SERVICE_URL; then X-Forwarded-Host
WS_HOST = os.environ.get("WS_HOST")  # e.g. twilio-bridge-xxxx.a.run.app (RECOMMENDED)
CR_HOST = os.environ.get("CLOUD_RUN_SERVICE_URL")  # also fine if set

LOG_EVENT_TYPES = {
    "error",
    "response.content.done",
    "response.done",
    "rate_limits.updated",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "session.created",
    "session.updated",
}

app = FastAPI()

# ---------------- Health ----------------
@app.get("/", response_class=JSONResponse)
async def health():
    return {"status": "ok", "service": "Twilio ↔ OpenAI Realtime bridge"}

# Handy to confirm IAM/public access
@app.get("/_ping")
async def ping():
    return PlainTextResponse("pong", status_code=200)

# ---------------- Twilio Voice webhook → TwiML ----------------
@app.api_route("/twilio/voice", methods=["GET", "POST"])
async def twilio_voice(request: Request):
    http_host = (request.headers.get("X-Forwarded-Host") or request.url.hostname or "").strip()
    http_host = http_host.replace("https://", "").replace("http://", "")
    ws_host = (WS_HOST or CR_HOST or http_host).replace("https://", "").replace("http://", "").strip()

    ws_url = f"wss://{ws_host}/media-stream"
    status_cb = f"https://{http_host}/stream-status"

    # --- detect caller number ---
    try:
        form = await request.form()
    except Exception:
        form = {}
    caller = (form.get("From") or "").strip()

    if caller.startswith("+39"):
        # Italian
        intro = "Benvenuto in Lobbi del tuo condominio. Sto verificando l'accesso."
        ready = "Quando sei pronto, puoi iniziare a parlare."
        lang = "it-IT"
    else:
        # English default
        intro = "Welcome to your building Lobbi. Checking access."
        ready = "Okay, you can start talking."
        lang = "en-US"

    vr = VoiceResponse()
    vr.say(intro, voice="alice", language=lang)
    vr.pause(length=1)
    vr.say(ready, voice="alice", language=lang)

    connect = Connect()
    connect.stream(
        url=ws_url,
        status_callback=status_cb,
        status_callback_method="POST",
    )
    vr.append(connect)

    xml = str(vr)
    log.info(f"TwiML -> ws:{ws_url}  statusCb:{status_cb}  caller:{caller} lang:{lang}")
    return HTMLResponse(content=xml, media_type="application/xml")


# Twilio will POST here for stream lifecycle: start, mark, media, stop, errors
@app.post("/stream-status")
async def stream_status(request: Request):
    try:
        data = await request.form()
        payload = dict(data)
    except Exception:
        payload = {"raw": (await request.body()).decode("utf-8", "ignore")}
    log.info(f"Stream status callback: {payload}")
    return JSONResponse({"ok": True})

# --------------- WebSocket Bridge core ---------------
async def _bridge_socket(ws: WebSocket):
    """
    Twilio <Stream>  ↔  OpenAI Realtime (PCMU in/out).
    We accept the WS, read Twilio’s initial “connected” frame, and immediately
    send a format ACK back (audio/x-mulaw, 8 kHz, mono). Then we connect to OAI.
    """
    await ws.accept()
    log.info("✅ WS accepted at /media-stream")

    stream_sid = None
    last_assistant_item = None
    mark_queue: list[str] = []
    openai_ws = None
    pump_task = None

    async def initialize_session():
        nonlocal openai_ws
        openai_ws = await websockets.connect(
            OPENAI_REALTIME_URL,
            extra_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
        # Configure PCMU in/out + server VAD
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": "gpt-realtime",
                "instructions": SYSTEM_MESSAGE,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "turn_detection": {"type": "server_vad"}
                    },
                    "output": {
                        "format": {"type": "audio/pcmu"}
                    }
                }
            }
        }
        await openai_ws.send(json.dumps(session_update))
        log.info("Sent OpenAI session.update")

    async def send_mark():
        if not stream_sid:
            return
        await ws.send_json({"event": "mark", "streamSid": stream_sid, "mark": {"name": "responsePart"}})
        mark_queue.append("responsePart")

    async def handle_barge_in():
        nonlocal last_assistant_item
        if not openai_ws or not last_assistant_item:
            return
        try:
            await openai_ws.send(json.dumps({
                "type": "conversation.item.truncate",
                "item_id": last_assistant_item,
                "content_index": 0
            }))
            if stream_sid:
                await ws.send_json({"event": "clear", "streamSid": stream_sid})
                mark_queue.clear()
            log.info(f"Barge-in: truncated item {last_assistant_item}")
            last_assistant_item = None
        except Exception as e:
            log.warning(f"Barge-in failed: {e}")

    async def pump_openai_to_twilio():
        nonlocal last_assistant_item
        try:
            async for raw in openai_ws:
                try:
                    resp = json.loads(raw)
                except Exception:
                    continue

                t = resp.get("type")
                if t in LOG_EVENT_TYPES:
                    log.info(f"OAI event: {t}")

                if t == "response.output_audio.delta" and "delta" in resp and stream_sid:
                    await ws.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": resp["delta"]}
                    })
                    if resp.get("item_id") and resp["item_id"] != last_assistant_item:
                        last_assistant_item = resp["item_id"]
                    await send_mark()

                if t == "input_audio_buffer.speech_started":
                    await handle_barge_in()

        except Exception as e:
            log.warning(f"OAI→Twilio pump error: {e}")

    # -------- Twilio → OpenAI loop w/ explicit handshake --------
    try:
        # 1) Read the very first frame (Twilio sends "connected")
        try:
            first = await ws.receive_text()
        except WebSocketDisconnect:
            log.info("Twilio disconnected before first frame")
            return

        try:
            hello = json.loads(first)
        except Exception:
            hello = {}

        log.info(f"Twilio first event: {hello.get('event')}")

        # 2) ACK the 'connected' frame with our media format
        if hello.get("event") == "connected":
            ack = {
                "event": "connected",
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
            }
            await ws.send_json(ack)
            log.info("Sent Twilio connected ACK (μ-law, 8kHz, mono)")

        # 3) Continue processing (including if the first was already 'start')
        pending = [hello] if hello else []
        while True:
            if pending:
                data = pending.pop(0)
            else:
                try:
                    data = json.loads(await ws.receive_text())
                except WebSocketDisconnect:
                    log.info("Twilio disconnected")
                    break
                except Exception as e:
                    log.info(f"WS receive err: {e}")
                    break

            evt = data.get("event")

            if evt == "start":
                stream_sid = data["start"].get("streamSid")
                log.info(f"Twilio stream start (sid={stream_sid})")
                if not openai_ws:
                    await initialize_session()
                    pump_task = asyncio.create_task(pump_openai_to_twilio())

            elif evt == "media":
                if not openai_ws:
                    await initialize_session()
                    pump_task = asyncio.create_task(pump_openai_to_twilio())
                if openai_ws and not openai_ws.closed:
                    payload_b64 = data["media"]["payload"]  # base64 μ-law (PCMU)
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": payload_b64
                    }))

            elif evt == "mark":
                if mark_queue:
                    mark_queue.pop(0)

            elif evt == "stop":
                log.info("Twilio stream stop")
                break

            else:
                # 'connected', 'clear', or unknown
                pass

    finally:
        try:
            if openai_ws and not openai_ws.closed:
                await openai_ws.close()
        except Exception:
            pass
        if pump_task:
            try:
                pump_task.cancel()
            except Exception:
                pass
        try:
            await ws.close()
        except Exception:
            pass
        log.info("media-stream closed")

# Expose both paths (some configs still point to /stream)
@app.websocket("/media-stream")
async def ws_media(ws: WebSocket):
    await _bridge_socket(ws)

@app.websocket("/stream")
async def ws_media_legacy(ws: WebSocket):
    await _bridge_socket(ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, workers=1)
