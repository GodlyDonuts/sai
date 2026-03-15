import logging
import json
import base64
import asyncio
import os
import io
import collections
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from datetime import datetime
import websockets
from dotenv import load_dotenv
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sai-server")

app = FastAPI(title="Sai OS Agent Cloud Backend")


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_WS_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

# cerebras_client = AsyncOpenAI(
#     api_key=CEREBRAS_API_KEY,
#     base_url="https://api.cerebras.ai/v1"
# )

openrouter_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://api.cerebras.ai/v1" if not OPENROUTER_API_KEY else "https://openrouter.ai/api/v1"
)

MAX_AGENT_STEPS = 10
ACTION_SETTLE_TIME = 2.0  # seconds to wait after an action for the UI to update
SCREENSHOT_TIMEOUT = 5.0  # seconds to wait for a screenshot from client

def annotate_screenshot(image_b64: str) -> str:
    """Draw coordinate axis labels on the screenshot edges so the vision model can estimate click positions."""
    try:
        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size
        
        # Use a small default font
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
        except:
            font = ImageFont.load_default()
        
        # Draw X-axis labels along the top edge
        for x in range(0, w, 200):
            draw.text((x + 2, 2), str(x), fill="red", font=font)
            draw.line([(x, 0), (x, 15)], fill="red", width=1)
        
        # Draw Y-axis labels along the left edge
        for y in range(0, h, 200):
            draw.text((2, y + 2), str(y), fill="red", font=font)
            draw.line([(0, y), (15, y)], fill="red", width=1)
        
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
    
    # Shared state between upstream and downstream
    # Event to coordinate screenshot arrivals between upstream and the agent loop
    screenshot_event = asyncio.Event()
    
    connection_state = {
        "last_call_id": None,
        "recent_model_replies": collections.deque(maxlen=3),
        "latest_sight": "No visual context yet.",
        "latest_screenshot_b64": None,
        "transcription_buffer": [],
        "debounce_task": None,
        "agent_running": False  # Prevents concurrent agent loops
    }
    
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not found in environment")
        await websocket.close(code=1011) # Internal Error
        return

    try:
        # Initial Handshake with local client
        # Expecting: {"event": "wake_word_detected", "timestamp": <time>}
        initial_data = await websocket.receive_text()
        try:
            event_payload = json.loads(initial_data)
            if event_payload.get("event") == "wake_word_detected":
                timestamp = event_payload.get("timestamp")
                logger.info(f"Handshake successful: Wake word detected at {timestamp}")
                
                # Acknowledge handshake
                await websocket.send_json({"status": "handshake_complete"})
                
                # AUTO-TRIGGER SCREENSHOT: Ensure we have "Fresh Eyes" for the upcoming command
                logger.info("Auto-triggering initial screenshot for visual context.")
                await websocket.send_text(json.dumps({"command": "capture_screen"}))
            else:
                logger.warning(f"Unexpected initial event: {event_payload}")
                await websocket.close(code=1003) # Unsupported Data
                return
        except json.JSONDecodeError:
            logger.error("Malformed JSON during handshake")
            await websocket.close(code=1003)
            return

        # Connect to Google Gemini API
        async with websockets.connect(GEMINI_WS_URL) as google_ws:
            logger.info("Connected to Gemini API WebSocket")

            # Send Setup Message (BidiGenerateContentSetup format)
            # Reference: https://ai.google.dev/api/live#BidiGenerateContentSetup
            # - "setup" is the raw WebSocket wrapper key
            # - responseModalities/speechConfig go INSIDE generationConfig
            # - inputAudioTranscription/outputAudioTranscription go at setup ROOT
            setup_msg = {
                "setup": {
                    "model": "models/gemini-2.5-flash-native-audio-preview-12-2025",
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": "Aoede"
                                }
                            }
                        }
                    },
                    "systemInstruction": {
                        "parts": [{
                            "text": "You are the Voice and Sensory Interface for Sai. Your ONLY job is to transcribe audio from the user and speak short, confirming messages. \n\nRULES:\n- Do NOT provide advice. \n- Do NOT list plans.\n- If the user asks for something, JUST say 'Got it' or 'On it' or 'Launching Safari'. \n- Do NOT try to use tools yourself. The server handles logic via transcription."
                        }]
                    },
                    "inputAudioTranscription": {},
                    "outputAudioTranscription": {}
                }
            }
            
            await google_ws.send(json.dumps(setup_msg))
            logger.info("Setup message sent to Gemini")

            async def upstream():
                """Client -> Google"""
                try:
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            raise WebSocketDisconnect(message.get("code", 1000))
                            
                        if message.get("bytes"):
                            # Audio bytes
                            audio_chunk = message.get("bytes")
                            # logger.debug(f"Upstream: Sending {len(audio_chunk)} bytes to Google")
                            payload = {
                                "realtimeInput": {
                                    "audio": {
                                        "data": base64.b64encode(audio_chunk).decode("utf-8"),
                                        "mimeType": "audio/pcm;rate=16000"
                                    }
                                }
                            }
                            await google_ws.send(json.dumps(payload))
                        elif message.get("text"):
                            # Text JSON from client (e.g. screen capture response)
                            try:
                                data = json.loads(message["text"])
                                if data.get("event") == "screen_captured":
                                    image_b64 = data.get("image_base64")
                                    # Store locally for Senior Brain
                                    connection_state["latest_screenshot_b64"] = image_b64
                                    # Signal any waiting agent loop that a new screenshot is ready
                                    screenshot_event.set()
                                    
                                    width = data.get("width", 1920)
                                    height = data.get("height", 1080)
                                    logger.info(f"Received screen capture from client ({width}x{height}). Stored and forwarding to Gemini.")
                                    # Forward as user turn with an image part
                                    # This avoids "Invalid Argument" for tool IDs and allows Gemini to "see"
                                    client_content_payload = {
                                        "clientContent": {
                                            "turns": [{
                                                "role": "user",
                                                "parts": [
                                                    {
                                                        "text": "Here is the screenshot of my screen."
                                                    },
                                                    {
                                                        "inlineData": {
                                                            "mimeType": "image/jpeg",
                                                            "data": image_b64
                                                        }
                                                    }
                                                ]
                                            }],
                                            "turnComplete": True
                                        }
                                    }
                                    await google_ws.send(json.dumps(client_content_payload))
                            except json.JSONDecodeError:
                                logger.error("Received malformed JSON text from client.")

                except websockets.exceptions.ConnectionClosed as e:
                    logger.info(f"Gemini disconnected (upstream) code={e.code} reason='{e.reason}'")
                except WebSocketDisconnect:
                    logger.info("Client disconnected (upstream)")
                except Exception as e:
                    logger.error(f"Error in upstream: {e}")
                finally:
                    # If upstream exits, ensure downstream also exits
                    if not websocket.client_state.name == "DISCONNECTED":
                        try:
                            await websocket.close()
                        except RuntimeError:
                            pass

            async def hybrid_reasoning(user_text, latest_sight, latest_image_b64):
                """Routes task to Llama (simple) or Gemini 3 (advanced + vision)."""
                # STEP 1: Routing Decision (Llama 3.3-70b)
                # Heuristic: If it looks like a URL or complex intent, ADVANCED.
                url_keywords = [".com", ".org", ".net", ".io", "http", "www", "search", "navigate", "go to", "find"]
                if any(k in user_text.lower() for k in url_keywords):
                    complexity = "ADVANCED"
                else:
                    routing_prompt = f"""You are the Task Router for Sai.
Analyze the user command. Determine if it can be handled by a simple app launch or if it needs the Senior Brain (Vision/Browsing/Complex Apps).

SIMPLE: "Open Spotify", "Launch Terminal", "Hi", "Volume up", "Screenshot".
ADVANCED: "Add this song to my likes", "Buy tide pods", "Check my physics homework on Canvas", "Translate the text on my screen".

User said: "{user_text}"
Output valid JSON: {{"complexity": "SIMPLE" | "ADVANCED"}}"""
                    
                    try:
                        routing_completion = await openrouter_client.chat.completions.create(
                            model="nvidia/nemotron-3-super-120b-a12b:free",
                            messages=[{"role": "system", "content": routing_prompt}],
                            response_format={"type": "json_object"}
                        )
                        routing_data = json.loads(routing_completion.choices[0].message.content)
                        complexity = routing_data.get("complexity", "SIMPLE").strip().upper()
                        logger.info(f"Routing Decision: {complexity}")
                    except Exception as e:
                        logger.error(f"Routing Error: {e}")
                        complexity = "SIMPLE"

                # STEP 2: Execution
                if complexity == "SIMPLE":
                    system_prompt = """You are the tool extractor. Output ONLY JSON.
Example: "Open Safari" -> {"command": "type_text", "text": "Safari"}"""
                    try:
                        completion = await openrouter_client.chat.completions.create(
                            model="nvidia/nemotron-3-super-120b-a12b:free",
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_text}
                            ],
                            response_format={"type": "json_object"}
                        )
                        return json.loads(completion.choices[0].message.content)
                    except: return None
                else:
                    # ADVANCED: Launch multi-step agent loop
                    asyncio.create_task(run_agent_loop(user_text))
                    return None  # Agent loop handles sending commands directly

            async def run_agent_loop(user_text):
                """Multi-step agentic loop: observe → think → act → repeat."""
                if connection_state["agent_running"]:
                    logger.warning("Agent loop already running, skipping.")
                    return
                connection_state["agent_running"] = True
                
                SENIOR_SYSTEM_PROMPT = """You are Sai's Advanced Brain. You control a macOS computer by looking at screenshots and executing actions step-by-step.

YOU CAN SEE THE SCREENSHOT. It has RED coordinate labels along the top (X-axis) and left (Y-axis) edges every 200 pixels. USE THESE to estimate click coordinates.

TOOLS (always use the "command" key):
1. open_url(url) - opens a URL in the default browser immediately. ALWAYS use this for web navigation.
2. click(x, y) - clicks at exact pixel coordinates. Read the RED labels to estimate x and y precisely.
3. keyboard_type(text) - types text into the CURRENTLY FOCUSED element on screen. Only works if something is already focused.
4. type_text(text) - opens macOS Spotlight, types text, and launches it. Use only to open apps by name.

CRITICAL RULES:
- Output a SINGLE JSON object per response. No extra text, no markdown, no code fences.
- Always include "explanation" and "done" in every response.
- "done": false means more steps are coming. "done": true ONLY when you can confirm in the current screenshot that the task has been completed successfully.
- NEVER set "done": true alongside a command that needs to be executed. If you still have an action to take, set "done": false.
- After each step you will receive a FRESH screenshot of the result. Use it to decide your next action.
- If keyboard_type fails (text doesn't appear), try clicking the target field first, then keyboard_type again.

Examples:
{"explanation": "Opening amazon.com directly", "command": "open_url", "url": "https://www.amazon.com", "done": false}
{"explanation": "Amazon has loaded successfully, task is complete", "command": "none", "done": true}
{"explanation": "Clicking search bar at ~(500, 130) based on grid", "command": "click", "x": 500, "y": 130, "done": false}"""
                
                conversation_history = [
                    {"role": "system", "content": SENIOR_SYSTEM_PROMPT}
                ]
                
                logger.info(f"========== AGENT LOOP STARTED for: '{user_text}' ==========")
                
                try:
                    for step in range(MAX_AGENT_STEPS):
                        # 1. Get current screenshot
                        current_screenshot = connection_state["latest_screenshot_b64"]
                        if not current_screenshot:
                            logger.error("No screenshot available for agent loop.")
                            break
                        
                        # 2. Annotate with grid
                        annotated = annotate_screenshot(current_screenshot)
                        
                        # 3. Build user message with screenshot
                        step_label = f"Step {step + 1}/{MAX_AGENT_STEPS}"
                        if step == 0:
                            user_content = [{"type": "text", "text": f"[{step_label}] Task: {user_text}"}, 
                                           {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{annotated}"}}]
                        else:
                            user_content = [{"type": "text", "text": f"[{step_label}] Here is the updated screenshot after the previous action. Continue the task."}, 
                                           {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{annotated}"}}]
                        
                        conversation_history.append({"role": "user", "content": user_content})
                        
                        # 4. Call Senior Brain
                        try:
                            completion = await openrouter_client.chat.completions.create(
                                model="google/gemini-3-flash-preview",
                                messages=conversation_history
                            )
                            raw_content = completion.choices[0].message.content
                            logger.info(f"[Step {step + 1}] Senior Brain Response: {raw_content}")
                            
                            # Add assistant response to history
                            conversation_history.append({"role": "assistant", "content": raw_content})
                            
                            # Parse — try to extract JSON from the response
                            # Strip markdown code fences if present
                            cleaned = raw_content.strip()
                            if cleaned.startswith("```"):
                                cleaned = cleaned.split("\n", 1)[-1]
                                cleaned = cleaned.rsplit("```", 1)[0]
                            
                            data = json.loads(cleaned)
                            if isinstance(data, list) and len(data) > 0:
                                data = data[0]
                            if "action" in data and "command" not in data:
                                data["command"] = data["action"]
                            
                        except Exception as e:
                            logger.error(f"[Step {step + 1}] Senior Brain Error: {e}")
                            break
                        
                        # 5. Execute the command first (even if done, command may still need to run)
                        cmd = data.get("command")
                        if cmd and cmd != "none":
                            logger.info(f"[Step {step + 1}] Executing: {data}")
                            await websocket.send_text(json.dumps(data))
                            # Wait for action to settle
                            await asyncio.sleep(ACTION_SETTLE_TIME)
                        
                        # 6. Check if done AFTER executing
                        if data.get("done") == True:
                            logger.info(f"[Step {step + 1}] Agent reports DONE: {data.get('explanation', '')}")
                            break
                        
                        if not cmd or cmd == "none":
                            logger.info(f"[Step {step + 1}] No command and not done — breaking to avoid infinite loop.")
                            break
                        
                        # 8. Request a fresh screenshot
                        screenshot_event.clear()
                        await websocket.send_text(json.dumps({"command": "capture_screen"}))
                        
                        # 9. Wait for the screenshot to arrive via upstream
                        try:
                            await asyncio.wait_for(screenshot_event.wait(), timeout=SCREENSHOT_TIMEOUT)
                            logger.info(f"[Step {step + 1}] Fresh screenshot received.")
                        except asyncio.TimeoutError:
                            logger.error(f"[Step {step + 1}] Timed out waiting for screenshot.")
                            break
                    else:
                        logger.warning(f"Agent loop hit max steps ({MAX_AGENT_STEPS}).")
                
                except Exception as e:
                    logger.error(f"Agent loop error: {e}")
                finally:
                    connection_state["agent_running"] = False
                    logger.info("========== AGENT LOOP ENDED ==========")

            async def process_complete_transcription():
                """Process the fully accumulated transcription buffer."""
                full_text = "".join(connection_state["transcription_buffer"]).strip()
                connection_state["transcription_buffer"] = []
                connection_state["debounce_task"] = None
                
                if not full_text:
                    return
                
                logger.info(f"=== COMPLETE TRANSCRIPTION: '{full_text}' ===")
                
                # ECHO SUPPRESSION
                normalized_input = "".join(filter(str.isalnum, full_text.lower()))
                is_echo = False
                for reply in list(connection_state["recent_model_replies"]):
                    normalized_reply = "".join(filter(str.isalnum, reply.lower()))
                    if normalized_reply and (normalized_reply in normalized_input or normalized_input in normalized_reply):
                        is_echo = True
                        break
                
                if is_echo:
                    logger.info(f"Ignored echo transcription: '{full_text}'")
                    return
                
                logger.info(f"!!! TRIGGERING HYBRID BRAIN FOR: {full_text}")
                try:
                    action = await hybrid_reasoning(
                        full_text, 
                        connection_state["latest_sight"],
                        connection_state["latest_screenshot_b64"]
                    )
                    if action and action.get("command") != "none":
                        cmd = action.get("command")
                        if cmd not in ["type_text", "capture_screen", "click", "open_url", "keyboard_type"]:
                            logger.warning(f"Brain returned unknown command: {action}")
                        
                        if cmd == "type_text" and not action.get("text", "").strip():
                            logger.info("Ignoring empty type_text command")
                        else:
                            logger.info(f"Forwarding action to client: {action}")
                            await websocket.send_text(json.dumps(action))
                except Exception as e:
                    logger.error(f"Failed to process hybrid brain action: {e}")

            async def debounce_and_process():
                """Wait 1.5s of silence, then process the accumulated buffer."""
                await asyncio.sleep(1.5)
                await process_complete_transcription()

            async def downstream():
                """Google -> Client"""
                try:
                    async for message in google_ws:
                        response = json.loads(message)
                        
                        if "setupComplete" in response:
                            logger.info(">>> GEMINI SETUP CONFIRMED")
                        
                        # Handle goAway (server warning before disconnect)
                        if "goAway" in response:
                            time_left = response["goAway"].get("timeLeft", "unknown")
                            logger.warning(f"Gemini goAway: session ending in {time_left}")
                        
                        if "serverContent" in response:
                            model_turn = response["serverContent"].get("modelTurn")
                            if model_turn:
                                for part in model_turn.get("parts", []):

                                    # Handle Audio
                                    if "inlineData" in part:
                                        audio_data_b64 = part["inlineData"].get("data")
                                        if audio_data_b64:
                                            audio_binary = base64.b64decode(audio_data_b64)
                                            await websocket.send_bytes(audio_binary)
                            
                            # Handle Input Transcriptions (user speech → text)
                            input_transcription = response["serverContent"].get("inputTranscription")
                            if input_transcription:
                                text = input_transcription.get("text", "")
                                if text.strip():
                                    logger.info(f"--- FRAGMENT: '{text}' (buffer: {''.join(connection_state['transcription_buffer'])}) ---")
                                    
                                    # Append fragment to buffer
                                    connection_state["transcription_buffer"].append(text)
                                    
                                    # Cancel any pending debounce timer
                                    if connection_state["debounce_task"]:
                                        connection_state["debounce_task"].cancel()
                                    
                                    # Start a new 1.5s debounce timer
                                    connection_state["debounce_task"] = asyncio.create_task(debounce_and_process())
                            
                            # Handle turnComplete — flush buffer immediately
                            turn_complete = response["serverContent"].get("turnComplete", False)
                            if turn_complete and connection_state["transcription_buffer"]:
                                logger.info("Turn complete signal received, flushing buffer.")
                                if connection_state["debounce_task"]:
                                    connection_state["debounce_task"].cancel()
                                await process_complete_transcription()
                            
                            # Handle Output Transcriptions (model speech → text)
                            output_transcription = response["serverContent"].get("outputTranscription")
                            if output_transcription:
                                res_text = output_transcription.get("text", "")
                                logger.info(f"OUTPUT TRANSCRIPTION: '{res_text}'")
                                if res_text.strip():
                                    connection_state["recent_model_replies"].append(res_text)
                                    # If Gemini is describing the screen, update latest_sight
                                    keywords = ["screen", "visible", "browser", "windows", "desktop", "here is", "i see"]
                                    if any(k in res_text.lower() for k in keywords):
                                        logger.info(f"Updating Visual Context: {res_text}")
                                        connection_state["latest_sight"] = res_text
                            
                            # Log tool calls if model tries them anyway
                            if model_turn:
                                for part in model_turn.get("parts", []):
                                    if "functionCall" in part:
                                        logger.warning(f"MODEL TRIED ToolCall (Ignoring): {part['functionCall']}")
                            
                            interruption = response["serverContent"].get("interruption")
                            if interruption:
                                logger.info("User interrupted Gemini, stopping audio...")
                                # Optional: Clear audio queue on client if implemented

                except websockets.exceptions.ConnectionClosed as e:
                    logger.info(f"Gemini disconnected (downstream) code={e.code} reason='{e.reason}'")
                except WebSocketDisconnect:
                    logger.info("Client disconnected (downstream)")
                except Exception as e:
                    logger.error(f"Error in downstream: {e}")
                finally:
                    if not websocket.client_state.name == "DISCONNECTED":
                        try:
                            await websocket.close()
                        except RuntimeError:
                            pass

            # Run both loops concurrently. If one exits, the other should be cancelled.
            upstream_task = asyncio.create_task(upstream())
            downstream_task = asyncio.create_task(downstream())
            
            done, pending = await asyncio.wait(
                [upstream_task, downstream_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for task in pending:
                task.cancel()
                
    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"Google Gemini connection closed: code={e.code} reason='{e.reason}'")
    except WebSocketDisconnect:
        logger.info("Connection closed by client")
    except Exception as e:
        logger.error(f"Unexpected error in session: {e}")

    finally:
        logger.info("Session ended")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
