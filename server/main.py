import logging
import json
import base64
import asyncio
import os
import io
import collections
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from datetime import datetime
import boto3
from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveTranscriptionEvents,
    LiveOptions,
)
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sai-server")

app = FastAPI(title="Sai OS Agent Cloud Backend (Nova + Deepgram)")

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# Bedrock setup for Amazon Nova
session = boto3.Session(
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    region_name=os.getenv("AWS_REGION", "us-east-1")
)
bedrock_runtime = session.client(service_name='bedrock-runtime')

NOVA_LITE_MODEL_ID = "amazon.nova-lite-v1:0"
NOVA_PRO_MODEL_ID = "amazon.nova-pro-v1:0"

MAX_AGENT_STEPS = 10
ACTION_SETTLE_TIME = 2.0  # seconds to wait after an action for the UI to update
SCREENSHOT_TIMEOUT = 5.0  # seconds to wait for a screenshot from client

def annotate_screenshot(image_b64: str, last_action: dict = None) -> str:
    """Draw minimal edge-only ruler ticks and optional last action marker on the screenshot.
    
    No full-screen grid lines — only small tick marks along the top and left edges
    every 200px so the LLM can use them as spatial references without cluttering the UI.
    """
    try:
        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size
        
        # Try to load a font, otherwise use default
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
        except:
            font = ImageFont.load_default()
        
        TICK_LEN = 12       # length of ruler tick marks in pixels
        LABEL_PAD = 2       # padding between tick and label text
        STEP = 200          # ruler interval in pixels
        TICK_COLOR = (255, 60, 60)  # red ticks
        LABEL_COLOR = (255, 255, 255)  # white labels for readability
        
        # --- Top edge ruler (X-axis) ---
        for x in range(0, w, STEP):
            if x == 0:
                continue  # skip origin to avoid overlap
            draw.line([(x, 0), (x, TICK_LEN)], fill=TICK_COLOR, width=2)
            draw.text((x + LABEL_PAD, LABEL_PAD), str(x), fill=LABEL_COLOR, font=font)
        
        # --- Left edge ruler (Y-axis) ---
        for y in range(0, h, STEP):
            if y == 0:
                continue
            draw.line([(0, y), (TICK_LEN, y)], fill=TICK_COLOR, width=2)
            draw.text((LABEL_PAD, y + LABEL_PAD), str(y), fill=LABEL_COLOR, font=font)
        
        # --- Last-click crosshair (debug feedback) ---
        if last_action and last_action.get("command") == "click":
            lx, ly = int(last_action.get("x", 0)), int(last_action.get("y", 0))
            r = 20
            draw.ellipse([lx-r, ly-r, lx+r, ly+r], outline="lime", width=3)
            draw.line([lx-r*2, ly, lx+r*2, ly], fill="lime", width=2)
            draw.line([lx, ly-r*2, lx, ly+r*2], fill="lime", width=2)
            draw.text((lx + r + 5, ly - 10), f"CLICKED ({lx},{ly})", fill="lime", font=font)
        
        # Encode back to base64 PNG
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to annotate screenshot: {e}")
        return image_b64  # fallback to original

@app.get("/")
async def root():
    return {"message": "Sai OS Agent Cloud Backend is running"}

@app.websocket("/ws/agent")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("New client connection established")
    
    connection_state = {
        "latest_sight": "No visual context yet.",
        "latest_screenshot_b64": None,
        "transcription_buffer": [],
        "debounce_task": None,
        "active_agent_task": None,
        "screenshot_event": asyncio.Event()
    }
    
    if not DEEPGRAM_API_KEY:
        logger.error("DEEPGRAM_API_KEY not found in environment")
        await websocket.close(code=1011)
        return

    try:
        # Initial Handshake with local client
        initial_data = await websocket.receive_text()
        event_payload = json.loads(initial_data)
        if event_payload.get("event") == "wake_word_detected":
            logger.info("Handshake successful")
            await websocket.send_json({"status": "handshake_complete"})
            # Trigger initial screenshot
            await websocket.send_text(json.dumps({"command": "capture_screen"}))
        else:
            await websocket.close(code=1003)
            return

        # Define reasoning inside so it has access to websocket and state
        async def hybrid_reasoning(user_text, latest_sight, latest_image_b64):
            """Routes task to Nova Lite (simple/routing) or Nova Pro (advanced + vision)."""
            routing_prompt = f"""You are the Task Router for Sai. Determine complexity: SIMPLE or ADVANCED.\nUser said: "{user_text}"\nOutput JSON: {{"complexity": "SIMPLE" | "ADVANCED"}}"""
            try:
                body = json.dumps({
                    "system": [{"text": "Output JSON only."}],
                    "messages": [{"role": "user", "content": [{"text": routing_prompt}]}],
                    "inferenceConfig": {"max_tokens": 64, "temperature": 0}
                })
                response = bedrock_runtime.invoke_model(modelId=NOVA_LITE_MODEL_ID, body=body)
                routing_data = json.loads(json.loads(response.get('body').read())['output']['message']['content'][0]['text'])
                complexity = routing_data.get("complexity", "SIMPLE").upper()
            except: complexity = "SIMPLE"

            if complexity == "SIMPLE":
                try:
                    body = json.dumps({
                        "system": [{"text": "Convert to tool JSON. e.g. 'Open Safari' -> {\"command\": \"type_text\", \"text\": \"Safari\"}"}],
                        "messages": [{"role": "user", "content": [{"text": user_text}]}],
                        "inferenceConfig": {"max_tokens": 128, "temperature": 0}
                    })
                    resp = bedrock_runtime.invoke_model(modelId=NOVA_LITE_MODEL_ID, body=body)
                    return json.loads(json.loads(resp.get('body').read())['output']['message']['content'][0]['text'])
                except: return None
            else:
                if connection_state["active_agent_task"] and not connection_state["active_agent_task"].done():
                    connection_state["active_agent_task"].cancel()
                connection_state["active_agent_task"] = asyncio.create_task(run_agent_loop(user_text))
                return None

        async def run_agent_loop(user_text):
            SENIOR_SYSTEM_PROMPT = "You control macOS via screenshots. Tools: open_url, click(x,y), keyboard_type, type_text, wait, press_hotkey, scroll. Output JSON: {'explanation': '...', 'command': '...', 'done': bool}"
            conversation_history = [{"role": "system", "content": SENIOR_SYSTEM_PROMPT}]
            try:
                for step in range(MAX_AGENT_STEPS):
                    current_screenshot = connection_state["latest_screenshot_b64"]
                    if not current_screenshot: break
                    annotated = annotate_screenshot(current_screenshot, last_data)
                    user_content = [{"text": f"Task: {user_text}" if step == 0 else "Continue task."}, {"image": {"format": "png", "source": {"bytes": annotated}}}]
                    conversation_history.append({"role": "user", "content": user_content})
                    
                    # Bedrock call
                    body = json.dumps({
                        "system": [{"text": SENIOR_SYSTEM_PROMPT}],
                        "messages": [h for h in conversation_history if h["role"] != "system"],
                        "inferenceConfig": {"max_tokens": 1024, "temperature": 0.2}
                    })
                    response = bedrock_runtime.invoke_model(modelId=NOVA_PRO_MODEL_ID, body=body)
                    raw_content = json.loads(response.get('body').read())['output']['message']['content'][0]['text']
                    conversation_history.append({"role": "assistant", "content": [{"text": raw_content}]})
                    
                    data = json.loads(raw_content[raw_content.find('{'):raw_content.rfind('}')+1])
                    cmd = data.get("command")
                    if cmd and cmd != "none":
                        await websocket.send_text(json.dumps(data))
                        await asyncio.sleep(ACTION_SETTLE_TIME)
                    if data.get("done"): break
                    
                    connection_state["screenshot_event"].clear()
                    await websocket.send_text(json.dumps({"command": "capture_screen"}))
                    await asyncio.wait_for(connection_state["screenshot_event"].wait(), timeout=SCREENSHOT_TIMEOUT)
            except Exception as e: logger.error(f"Agent error: {e}")

        async def process_complete_transcription():
            full_text = " ".join(connection_state["transcription_buffer"]).strip()
            connection_state["transcription_buffer"] = []
            if not full_text: return
            logger.info(f"Processing: {full_text}")
            action = await hybrid_reasoning(full_text, connection_state["latest_sight"], connection_state["latest_screenshot_b64"])
            if action: await websocket.send_text(json.dumps(action))

        async def debounce_and_process():
            await asyncio.sleep(1.5)
            await process_complete_transcription()

        def on_message(self, result, **kwargs):
            sentence = result.channel.alternatives[0].transcript
            if sentence.strip():
                connection_state["transcription_buffer"].append(sentence)
                loop = asyncio.get_event_loop()
                if connection_state["debounce_task"]: connection_state["debounce_task"].cancel()
                connection_state["debounce_task"] = loop.create_task(debounce_and_process())

        # Initialize Deepgram
        deepgram = DeepgramClient(DEEPGRAM_API_KEY)
        dg_connection = deepgram.listen.live.v("1")
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
        dg_connection.start(LiveOptions(model="nova-2-general", language="en-US", encoding="linear16", channels=1, sample_rate=16000))

        while True:
            message = await websocket.receive()
            if message.get("bytes"):
                dg_connection.send(message.get("bytes"))
            elif message.get("text"):
                data = json.loads(message["text"])
                if data.get("event") == "screen_captured":
                    connection_state["latest_screenshot_b64"] = data.get("image_base64")
                    connection_state["screenshot_event"].set()
    except Exception as e: logger.error(f"WebSocket error: {e}")
    finally:
        if 'dg_connection' in locals(): dg_connection.finish()
        logger.info("Session ended")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
