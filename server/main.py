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
                    "model": "models/gemini-2.5-flash-tts",
                    "systemInstruction": {
                        "role": "system",
                        "parts": [{
                            "text": "You are Sai, a voice-native cybernetic co-pilot. You control the user's operating system. Keep responses incredibly concise, conversational, and action-oriented."
                        }]
                    },
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
                        audio_chunk = await websocket.receive_bytes()
                        # Wrap in realtimeInput JSON structure
                        payload = {
                            "realtimeInput": {
                                "mediaChunks": [{
                                    "data": base64.b64encode(audio_chunk).decode("utf-8"),
                                    "mimeType": "audio/pcm;rate=16000"
                                }]
                            }
                        }
                        await google_ws.send(json.dumps(payload))
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
                        
                        # Optional: handle tool calls or other message types here

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
