import logging
import json
import base64
import asyncio
import os
import collections
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from datetime import datetime
import websockets
from dotenv import load_dotenv
from openai import AsyncOpenAI

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sai-server")

app = FastAPI(title="Sai OS Agent Cloud Backend")


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_WS_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

cerebras_client = AsyncOpenAI(
    api_key=CEREBRAS_API_KEY,
    base_url="https://api.cerebras.ai/v1"
)

openrouter_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1"
)

@app.get("/")
async def root():
    return {"message": "Sai OS Agent Cloud Backend is running"}

@app.websocket("/ws/agent")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("New client connection established")
    
    # Shared state between upstream and downstream
    connection_state = {
        "last_call_id": None,
        "recent_model_replies": collections.deque(maxlen=3), # Tracks last few things Gemini said
        "latest_sight": "No visual context yet." # Tracks the last screen description from Gemini
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
                                    width = data.get("width", 1920)
                                    height = data.get("height", 1080)
                                    logger.info(f"Received screen capture from client ({width}x{height}). Forwarding to Gemini.")
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

            async def hybrid_reasoning(user_text, latest_sight):
                """Routes task to Llama (simple) or Gemini 3.1/OpenRouter (advanced)."""
                # STEP 1: Routing Decision (Llama 3.1-8b)
                # Heuristic: If it looks like a URL, it's likely advanced navigation
                url_keywords = [".com", ".org", ".net", ".io", "http", "www"]
                if any(k in user_text.lower() for k in url_keywords):
                    logger.info("URL-like input detected, auto-escalating to ADVANCED.")
                    complexity = "ADVANCED"
                else:
                    routing_prompt = f"""You are the strict Task Routing AI for a multi-modal agentic system. 
Your only objective is to analyze the user's command and the current screen context, then classify the task's complexity.

DEFINITIONS:
- SIMPLE: Basic OS-level commands, opening or closing local applications, simple system toggles, or greetings. 
- ADVANCED: Anything involving web navigation, URLs, research, multi-step actions, or specific interactions inside a website or application.

RULES:
1. You MUST output ONLY valid JSON. Do not include markdown code blocks (```json), preambles, or conversational text.
2. You MUST think step-by-step in a "reasoning" field BEFORE outputting the "complexity" field.

EXAMPLES:
User: "Open Safari"
Current Sight: "Desktop wallpaper"
Output: {{"reason": "The user wants to launch a local application, which is a basic OS-level command.", "complexity": "SIMPLE"}}

User: "Search for tide pods on Amazon"
Current Sight: "Safari homepage"
Output: {{"reason": "The user is asking to navigate a specific website and perform a search, requiring web navigation.", "complexity": "ADVANCED"}}

User: "Take a screenshot"
Current Sight: "A YouTube video playing"
Output: {{"reason": "Taking a screenshot is a basic system toggle.", "complexity": "SIMPLE"}}

User said: "{user_text}"
Current Sight: "{latest_sight}"
"""
                    
                    try:
                        routing_completion = await cerebras_client.chat.completions.create(
                            model="llama3.1-8b",
                            messages=[{"role": "system", "content": routing_prompt}],
                            response_format={"type": "json_object"}
                        )
                        routing_data = json.loads(routing_completion.choices[0].message.content)
                        complexity = routing_data.get("complexity", "SIMPLE")
                        logger.info(f"Routing Decision: {complexity}")
                    except Exception as e:
                        logger.error(f"Routing Error: {e}")
                        complexity = "SIMPLE" # Fallback

                # STEP 2: Execution
                if complexity == "SIMPLE":
                    # Use Llama for simple tool extraction
                    system_prompt = """You are the tool extractor for Sai. Output ONLY JSON.
Tools: type_text(text), capture_screen(), click(x,y), greet(message).
If User: "Open Safari" → {"command": "type_text", "text": "Safari"}"""
                    try:
                        completion = await cerebras_client.chat.completions.create(
                            model="llama3.1-8b",
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_text}
                            ],
                            response_format={"type": "json_object"}
                        )
                        return json.loads(completion.choices[0].message.content)
                    except: return None
                else:
                    # Use Seniors Brain (OpenRouter Gemini 3 Flash Preview)
                    senior_prompt = f"""You are Sai's Advanced Brain (Gemini 3). 
You have access to the user's screen through descriptions.
CURRENT SIGHT: "{latest_sight}"

TOOLS:
1. type_text(text) - To search or enter URLs. "text" should be the query or URL.
2. capture_screen() - If you need to see the screen AGAIN to verify an action.
3. click(x, y) - To click coordinates.

OUTPUT FORMAT (JSON ONLY):
{{"command": "type_text", "text": "URL or search query"}}
{{"command": "capture_screen"}}
{{"command": "click", "x": horizontal, "y": vertical}}

PLAN: Analyze the request and the screen. If you need to open a website, start by using type_text.
User: "{user_text}" """
                    try:
                        completion = await openrouter_client.chat.completions.create(
                            model="google/gemini-3-flash-preview", 
                            messages=[
                                {"role": "system", "content": senior_prompt},
                                {"role": "user", "content": user_text}
                            ],
                            response_format={"type": "json_object"}
                        )
                        content = completion.choices[0].message.content
                        logger.info(f"OpenRouter Response: {content}")
                        return json.loads(content)
                    except Exception as e:
                        logger.error(f"OpenRouter Error: {e}")
                        return None

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
                                            # logger.debug(f"Forwarded {len(audio_binary)} bytes to client")
                            
                            # Handle Input Transcriptions (user speech → text)
                            input_transcription = response["serverContent"].get("inputTranscription")
                            if input_transcription:
                                text = input_transcription.get("text", "")
                                if text.strip():
                                    logger.info(f"--- RAW GEMINI TRANSCRIPTION: '{text}' ---")
                                    
                                    # ECHO SUPPRESSION: Check if this matches something we just said
                                    # Normalize (lowercase, remove punctuation) for better matching
                                    normalized_input = "".join(filter(str.isalnum, text.lower()))
                                    is_echo = False
                                    for reply in list(connection_state["recent_model_replies"]):
                                        normalized_reply = "".join(filter(str.isalnum, reply.lower()))
                                        if normalized_reply and (normalized_reply in normalized_input or normalized_input in normalized_reply):
                                            is_echo = True
                                            break
                                    
                                    if is_echo:
                                        logger.info(f"Ignored echo transcription: '{text}'")
                                    else:
                                        logger.info(f"!!! TRIGGERING HYBRID BRAIN FOR: {text}")
                                        cerebras_action = await hybrid_reasoning(text, connection_state["latest_sight"])
                                        if cerebras_action and cerebras_action.get("command") != "none":
                                            # Logger for non-standard commands (hallucinations)
                                            cmd = cerebras_action.get("command")
                                            if cmd not in ["type_text", "capture_screen", "click"]:
                                                logger.warning(f"Cerebras returned unknown command/error: {cerebras_action}")
                                                # Still forward it so we can see it in client logs, 
                                                # but it won't be executed by the standard client commands
                                            
                                            # Guard against empty commands
                                            if cmd == "type_text" and not cerebras_action.get("text", "").strip():
                                                logger.info("Ignoring empty type_text command")
                                            else:
                                                logger.info(f"Forwarding Cerebras action to client: {cerebras_action}")
                                                await websocket.send_text(json.dumps(cerebras_action))
                                else:
                                    logger.debug("Received empty/whitespace transcription from Gemini")
                            
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
