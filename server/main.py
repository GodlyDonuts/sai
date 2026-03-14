import logging
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sai-server")

app = FastAPI(title="Sai OS Agent Cloud Backend")

@app.get("/")
async def root():
    return {"message": "Sai OS Agent Cloud Backend is running"}

@app.websocket("/ws/agent")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("New client connection established")
    
    try:
        # Initial Handshake
        # Expecting: {"event": "wake_word_detected", "timestamp": <time>}
        initial_data = await websocket.receive_text()
        try:
            event_payload = json.loads(initial_data)
            if event_payload.get("event") == "wake_word_detected":
                timestamp = event_payload.get("timestamp")
                logger.info(f"Handshake successful: Wake word detected at {timestamp}")
                
                # Acknowledge handshake (Optional, but good for stability)
                await websocket.send_json({"status": "handshake_complete"})
            else:
                logger.warning(f"Unexpected initial event: {event_payload}")
                await websocket.close(code=1003) # Unsupported Data
                return
        except json.JSONDecodeError:
            logger.error("Malformed JSON during handshake")
            await websocket.close(code=1003)
            return

        # Continuous Streaming Loop
        logger.info("Entering audio streaming loop...")
        while True:
            # Receive binary audio data
            audio_chunk = await websocket.receive_bytes()
            
            # Log the size of the received audio chunk
            chunk_size = len(audio_chunk)
            logger.info(f"Received audio chunk: {chunk_size} bytes")
            
            # Here is where we will hook up to the Gemini Live API later
            # For now, we just acknowledge or process it silently

    except WebSocketDisconnect:
        logger.info("Client disconnected gracefully")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        logger.info("Connection closed")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
