import logging
import json
import base64
import asyncio
import os
import io
import collections
import typing
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from datetime import datetime
from openai import OpenAI
from deepgram import AsyncDeepgramClient
from deepgram.core.events import EventType
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sai-server")

app = FastAPI(title="Sai OS Agent Cloud Backend (Nova + Deepgram)")

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# Nova Setup via OpenAI Proxy
nova_client = OpenAI(
    api_key=os.getenv("AMAZON_NOVA_API_KEY"),
    base_url=os.getenv("NOVA_BASE_URL", "https://api.nova.amazon.com/v1")
)

NOVA_LITE_MODEL_ID = "nova-2-lite-v1"
NOVA_PRO_MODEL_ID = "nova-pro-v1"

MAX_AGENT_STEPS = 25
ACTION_SETTLE_TIME = 2.0  # seconds to wait after an action for the UI to update
SCREENSHOT_TIMEOUT = 5.0  # seconds to wait for a screenshot from client

def extract_json(text: str) -> typing.Optional[dict]:
    """Robustly extract JSON from potentially verbose model output."""
    try:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            return json.loads(text[start:end+1])
    except Exception as e:
        logger.error(f"JSON extraction failed: {e} | Raw: {text[:100]}...")
    return None

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
        # We draw ticks at 0–1000 normalized coordinates mapped along each edge
        # so they match the model's coordinate system.
        NUM_STEPS = 5       # 0, 200, 400, 600, 800, 1000
        TICK_COLOR = (255, 60, 60)  # red ticks
        LABEL_COLOR = (255, 255, 255)  # white labels for readability
        
        step_norm = 1000 // NUM_STEPS
        # --- Top edge ruler (X-axis, 0–1000) ---
        for i in range(0, NUM_STEPS + 1):
            norm = i * step_norm
            x = int(round((norm / 1000) * w))
            if i == 0:
                continue  # skip origin to avoid overlap
            draw.line([(x, 0), (x, TICK_LEN)], fill=TICK_COLOR, width=2)
            draw.text((x + LABEL_PAD, LABEL_PAD), str(norm), fill=LABEL_COLOR, font=font)
        
        # --- Left edge ruler (Y-axis, 0–1000) ---
        for i in range(0, NUM_STEPS + 1):
            norm = i * step_norm
            y = int(round((norm / 1000) * h))
            if i == 0:
                continue
            draw.line([(0, y), (TICK_LEN, y)], fill=TICK_COLOR, width=2)
            draw.text((LABEL_PAD, y + LABEL_PAD), str(norm), fill=LABEL_COLOR, font=font)
        
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
        "command_triggered": False,
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
            routing_prompt = f"""You are the Task Router for Sai, a macOS assistant. 
Determine if the task is SIMPLE or ADVANCED.
- SIMPLE: A single, one-off action that requires NO visual feedback or navigation. 
  Examples: "Open Chrome", "Type hello world", "Maximize this window".
- ADVANCED: Any task involving multiple steps, specific URLs (e.g. amazon.com, google.com), visual search, research, or answering questions. 
  Examples: "Open amazon.com on Safari", "Search for physics notes", "Click the red button", "Answer my concept check questions".
User said: "{user_text}"
If the user mentions a specific website, URL, or a task with logic, it is ADVANCED.
Output JSON: {{"complexity": "SIMPLE" | "ADVANCED"}}"""
            try:
                response = nova_client.chat.completions.create(
                    model=NOVA_LITE_MODEL_ID,
                    messages=[
                        {"role": "system", "content": "Output JSON only."},
                        {"role": "user", "content": routing_prompt}
                    ],
                    temperature=0
                )
                raw_routing = response.choices[0].message.content
                routing_data = extract_json(raw_routing)
                if not routing_data:
                    raise ValueError(f"No JSON found in routing response: {raw_routing}")
                
                complexity = routing_data.get("complexity", "SIMPLE").upper()
                logger.info(f"Routing logic (Nova Lite): {complexity}")
            except Exception as e: 
                logger.error(f"Routing failed, defaulting to SIMPLE: {e}")
                complexity = "SIMPLE"

            if complexity == "SIMPLE":
                try:
                    resp = nova_client.chat.completions.create(
                        model=NOVA_LITE_MODEL_ID,
                        messages=[
                            {"role": "system", "content": "Convert to tool JSON. For apps, use: {'command': 'type_text', 'text': 'AppName'}. For URLs, use: {'command': 'open_url', 'url': 'https://...'}. ONLY output JSON."},
                            {"role": "user", "content": user_text}
                        ],
                        temperature=0
                    )
                    raw_simple = resp.choices[0].message.content
                    result = extract_json(raw_simple)
                    if not result:
                        raise ValueError(f"No JSON found in simple generation: {raw_simple}")
                    
                    logger.info(f"SIMPLE action generated: {result}")
                    return result
                except Exception as e:
                    logger.error(f"SIMPLE generation failed: {e}")
                    return None
            else:
                if connection_state["active_agent_task"] and not connection_state["active_agent_task"].done():
                    connection_state["active_agent_task"].cancel()
                connection_state["active_agent_task"] = asyncio.create_task(run_agent_loop(user_text))
                return None

        async def run_agent_loop(user_text):
            logger.info(f"Starting Agent Loop for task: {user_text}")
            SENIOR_SYSTEM_PROMPT = """You are the Senior Vision Specialist for Sai.
Resolution: The image you see is the full macOS screen, but you MUST reason in a normalized 2D coordinate system:
- X and Y are always in the range [0, 1000], where:
  - (0, 0) is the top-left corner of the visible screen
  - (1000, 1000) is the bottom-right corner of the visible screen
When you output a click at the visual center, use approximately (500, 500).
The client maps these normalized coordinates directly into the real screen coordinate space.

You MUST be agentic:
- Do ONE action per step (or "wait" if needed).
- After an action, you MUST request another screenshot and VERIFY the result before declaring completion.
- Only set done=true when the screenshot CONFIRMS the task is complete. Do not guess.

Output ONLY JSON: {"explanation": "...", "command": "...", "x": number, "y": number, "done": bool}

SUPPORTED COMMANDS:
- {"command": "open_url", "url": "https://..."}
- {"command": "click", "x": number (0-1000), "y": number (0-1000)}
- {"command": "type_text", "text": "App Name"}
- {"command": "keyboard_type", "text": "string"}
- {"command": "press_hotkey", "keys": ["command", "c"]}
- {"command": "scroll", "amount": number}
- {"command": "wait"}"""
            conversation_history = [{"role": "system", "content": SENIOR_SYSTEM_PROMPT}]
            last_action = None
            
            try:
                # Ensure we have at least one screenshot before beginning
                if not connection_state["latest_screenshot_b64"]:
                    logger.info("Waiting for initial screenshot...")
                    try:
                        await asyncio.wait_for(connection_state["screenshot_event"].wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.error("Initial screenshot timeout.")
                        return

                for step in range(MAX_AGENT_STEPS):
                    current_screenshot = connection_state["latest_screenshot_b64"]
                    if not current_screenshot:
                        logger.error("No screenshot available at step start.")
                        break
                    
                    logger.info(f"Agent Step {step+1}/{MAX_AGENT_STEPS} starting...")
                    annotated = annotate_screenshot(current_screenshot, last_action)
                    
                    user_text_msg = f"Task: {user_text}" if step == 0 else "Continue task."
                    user_content = [
                        {"type": "text", "text": user_text_msg},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{annotated}"
                            }
                        }
                    ]
                    conversation_history.append({"role": "user", "content": user_content})
                    
                    response = nova_client.chat.completions.create(
                        model=NOVA_PRO_MODEL_ID,
                        messages=conversation_history,
                        temperature=0.2
                    )
                    raw_content = response.choices[0].message.content
                    logger.info(f"Agent raw response: {raw_content[:200]}...")
                    conversation_history.append({"role": "assistant", "content": raw_content})
                    
                    data = extract_json(raw_content)
                    if not data:
                        logger.error(f"Agent failed to produce JSON: {raw_content}")
                        break

                    logger.info(f"Agent parsed data: {data}")
                    cmd = data.get("command")
                    performed_action = False
                    # Only forward actual tool commands to the client.
                    if cmd in {"open_url", "click", "type_text", "keyboard_type", "press_hotkey", "scroll", "wait"}:
                        last_action = data
                        logger.info(f"Sending command to client: {cmd}")
                        await websocket.send_text(json.dumps(data))
                        await asyncio.sleep(ACTION_SETTLE_TIME)
                        performed_action = True
                        
                    # Guardrail: never allow "done" on the same step we performed an action.
                    # We always require at least one follow-up screenshot to verify completion.
                    if data.get("done") and performed_action:
                        logger.info("Agent requested done immediately after action; forcing verification step.")
                        data["done"] = False

                    if data.get("done"):
                        logger.info("Agent signaled completion (done=True).")
                        break
                    
                    connection_state["screenshot_event"].clear()
                    await websocket.send_text(json.dumps({"command": "capture_screen"}))
                    await asyncio.wait_for(connection_state["screenshot_event"].wait(), timeout=SCREENSHOT_TIMEOUT)
            except Exception as e: logger.error(f"Agent error in run_agent_loop: {e}")

        async def process_complete_transcription():
            if connection_state["command_triggered"]: return
            full_text = " ".join(connection_state["transcription_buffer"]).strip()
            connection_state["transcription_buffer"] = []
            if not full_text: return
            
            connection_state["command_triggered"] = True
            logger.info(f"Processing (One-Shot): {full_text}")
            
            # Cancel any pending debounce
            if connection_state["debounce_task"]: connection_state["debounce_task"].cancel()
            
            action = await hybrid_reasoning(full_text, connection_state["latest_sight"], connection_state["latest_screenshot_b64"])
            if action: await websocket.send_text(json.dumps(action))
            
            # Wait for agent loop if it was started
            if connection_state["active_agent_task"]:
                try: await connection_state["active_agent_task"]
                except asyncio.CancelledError: pass
            
            # Finalize session to return to wake-word mode
            logger.info("One-shot command complete. Closing session.")
            await websocket.close()

        async def debounce_and_process():
            await asyncio.sleep(1.5)
            await process_complete_transcription()

        # Initialize Deepgram
        deepgram = AsyncDeepgramClient(api_key=DEEPGRAM_API_KEY)
        
        async with deepgram.listen.v1.connect(
            model="nova-2-general", 
            language="en-US", 
            encoding="linear16", 
            channels="1", 
            sample_rate="16000"
        ) as dg_socket:
            
            async def on_message(result: typing.Any):
                if result.type == "Results":
                    # If we already triggered a command, ignore further audio chunks
                    if connection_state["command_triggered"]: return

                    # Only buffer 'is_final' transcripts to prevent duplicate partial merges
                    if result.is_final and result.channel.alternatives[0].transcript:
                        sentence = result.channel.alternatives[0].transcript
                        if sentence.strip():
                            connection_state["transcription_buffer"].append(sentence)
                            logger.info(f"Buffered (is_final): {sentence}")
                            
                            # If Deepgram thinks the speech is truly finished, process immediately
                            if result.speech_final:
                                if connection_state["debounce_task"]: connection_state["debounce_task"].cancel()
                                await process_complete_transcription()
                                return

                            # Reset debounce timer for cases where speech_final isn't triggered yet
                            if connection_state["debounce_task"]: connection_state["debounce_task"].cancel()
                            connection_state["debounce_task"] = asyncio.create_task(debounce_and_process())

            dg_socket.on(EventType.MESSAGE, on_message)
            listening_task = asyncio.create_task(dg_socket.start_listening())

            try:
                while True:
                    # We no longer break on command_triggered here.
                    # This loop must stay alive to receive 'screen_captured' events
                    # while the one-shot agent task is running.
                    # It will exit naturally when websocket.close() is called or the client disconnects.
                        
                    try:
                        message = await websocket.receive()
                    except RuntimeError:
                        # Session closed by one-shot handler or client
                        break
                    if message.get("bytes"):
                        await dg_socket.send_media(message.get("bytes"))
                    elif message.get("text"):
                        data = json.loads(message["text"])
                        if data.get("event") == "screen_captured":
                            connection_state["latest_screenshot_b64"] = data.get("image_base64")
                            connection_state["screenshot_event"].set()
            except WebSocketDisconnect:
                logger.info("Client disconnected")
            finally:
                listening_task.cancel()
                await dg_socket.send_finalize()
    except Exception as e: 
        logger.error(f"WebSocket error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        logger.info("Session ended")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
