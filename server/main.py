import logging
import json
import base64
import asyncio
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from datetime import datetime
import websockets
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sai-server")

app = FastAPI(title="Sai OS Agent Cloud Backend")


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_WS_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

@app.get("/")
async def root():
    return {"message": "Sai OS Agent Cloud Backend is running"}

@app.websocket("/ws/agent")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("New client connection established")
    
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

            # Send Setup Message
            setup_msg = {
                "setup": {
                    "model": "models/gemini-2.5-flash-native-audio-latest",
                    "systemInstruction": {
                        "role": "system",
                        "parts": [{
                            "text": "You are Sai, an ultra-fast, voice-native cybernetic OS co-pilot. You do not just chat; you physically control the user's computer via tools. \n\nCRITICAL EXECUTION LOOP:\n1. If the user asks you to interact with the screen (e.g., 'click the login button', 'play that video'), you MUST immediately call the `request_screen_capture` tool.\n2. Once you receive the screen capture image, visually scan it to locate the requested UI element.\n3. Calculate the exact [X, Y] pixel coordinates for the absolute center of that UI element.\n4. Call the `execute_click(x, y)` tool using those precise coordinates.\n\nRULES:\n- Be incredibly concise. Speak in short, conversational affirmations like 'Got it', 'Clicking now', or 'On it'. Do not explain your visual reasoning out loud.\n- If a UI is ambiguous, ask the user for clarification (e.g., 'There are two submit buttons, which one?').\n- Your primary directive is zero-click web navigation. Act definitively."
                        }]
                    },
                    "tools": [
                        {
                            "functionDeclarations": [
                                {
                                    "name": "request_screen_capture",
                                    "description": "Call this to see the user's current screen to locate UI elements."
                                },
                                {
                                    "name": "execute_click",
                                    "description": "Call this to physically click the mouse at specific coordinates on the screen.",
                                    "parameters": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "x": {
                                                "type": "INTEGER",
                                                "description": "The X coordinate to click."
                                            },
                                            "y": {
                                                "type": "INTEGER",
                                                "description": "The Y coordinate to click."
                                            }
                                        },
                                        "required": ["x", "y"]
                                    }
                                }
                            ]
                        }
                    ],
                    "generationConfig": {
                        "responseModalities": ["audio"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": "Aoede" # Example voice, can be adjusted
                                }
                            }
                        }
                    }
                }
            }
            # Note: The prompt asks for Pcm16000Hz. 
            # In the current spec, the output format is part of the speechConfig or implicit.
            # Gemini Multimodal Live API typically defaults to 16kHz PCM.
            
            await google_ws.send(json.dumps(setup_msg))
            logger.info("Sent setup message to Gemini")

            async def upstream():
                """Client -> Google"""
                try:
                    while True:
                        message = await websocket.receive()
                        if "bytes" in message:
                            # Audio bytes
                            audio_chunk = message["bytes"]
                            payload = {
                                "realtimeInput": {
                                    "mediaChunks": [{
                                        "data": base64.b64encode(audio_chunk).decode("utf-8"),
                                        "mimeType": "audio/pcm;rate=16000"
                                    }]
                                }
                            }
                            await google_ws.send(json.dumps(payload))
                        elif "text" in message:
                            # Text JSON from client (e.g. screen capture response)
                            try:
                                data = json.loads(message["text"])
                                if data.get("event") == "screen_captured":
                                    image_b64 = data.get("image_base64")
                                    width = data.get("width", 1920)
                                    height = data.get("height", 1080)
                                    logger.info(f"Received screen capture from client ({width}x{height}). Forwarding to Gemini.")
                                    # Forward as FunctionResponse
                                    function_response_payload = {
                                        "clientContent": {
                                            "turns": [{
                                                "role": "user",
                                                "parts": [{
                                                    "functionResponse": {
                                                        "name": "request_screen_capture",
                                                        "response": {
                                                            "image_b64": image_b64,
                                                            "message": f"Here is the screenshot. The resolution is {width}x{height}."
                                                        }
                                                    }
                                                }]
                                            }],
                                            "turnComplete": True
                                        }
                                    }
                                    await google_ws.send(json.dumps(function_response_payload))
                            except json.JSONDecodeError:
                                logger.error("Received malformed JSON text from client.")

                except WebSocketDisconnect:
                    logger.info("Client disconnected (upstream)")
                except Exception as e:
                    logger.error(f"Error in upstream: {e}")

            async def downstream():
                """Google -> Client"""
                try:
                    async for message in google_ws:
                        response = json.loads(message)
                        
                        # Process serverContent
                        if "serverContent" in response:
                            model_turn = response["serverContent"].get("modelTurn")
                            if model_turn:
                                for part in model_turn.get("parts", []):
                                    if "inlineData" in part:
                                        audio_data_b64 = part["inlineData"].get("data")
                                        if audio_data_b64:
                                            audio_binary = base64.b64decode(audio_data_b64)
                                            await websocket.send_bytes(audio_binary)
                                            logger.info(f"Forwarded {len(audio_binary)} bytes of audio to client")
                                    elif "functionCall" in part:
                                        function_call = part["functionCall"]
                                        fname = function_call.get("name")
                                        args = function_call.get("args", {})
                                        call_id = function_call.get("id")
                                        
                                        logger.info(f"Received function call from Gemini: {fname}")
                                        
                                        if fname == "request_screen_capture":
                                            # Instruct local client to capture screen
                                            await websocket.send_text(json.dumps({"command": "capture_screen"}))
                                            # Note: We don't send a functionResponse immediately. Wait for the client to reply in `upstream`.
                                            
                                        elif fname == "execute_click":
                                            # Instruct local client to click
                                            await websocket.send_text(json.dumps({
                                                "command": "click", 
                                                "x": args.get("x"), 
                                                "y": args.get("y")
                                            }))
                                            
                                            # Send immediate success functionResponse back to Gemini
                                            function_response_payload = {
                                                "clientContent": {
                                                    "turns": [{
                                                        "role": "user",
                                                        "parts": [{
                                                            "functionResponse": {
                                                                "name": "execute_click",
                                                                "response": {
                                                                    "status": "success",
                                                                    "message": f"Clicked at coordinates ({args.get('x')}, {args.get('y')})"
                                                                }
                                                            }
                                                        }]
                                                    }],
                                                    "turnComplete": True
                                                }
                                            }
                                            await google_ws.send(json.dumps(function_response_payload))
                                            logger.info("Acknowleged execute_click back to Gemini.")

                except Exception as e:
                    logger.error(f"Error in downstream: {e}")

            # Run both loops concurrently
            await asyncio.gather(upstream(), downstream())

    except WebSocketDisconnect:
        logger.info("Connection closed by client")
    except Exception as e:
        logger.error(f"Unexpected error in session: {e}")
    finally:
        logger.info("Session ended")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
