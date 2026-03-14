import asyncio
import websockets
import json
import time

async def test_client():
    uri = "ws://localhost:8080/ws/agent"
    async with websockets.connect(uri) as websocket:
        # 1. Send Handshake
        handshake = {
            "event": "wake_word_detected",
            "timestamp": time.time()
        }
        await websocket.send(json.dumps(handshake))
        print(f"Sent handshake: {handshake}")
        
        # 2. Wait for confirmation
        response = await websocket.recv()
        print(f"Server response: {response}")
        
        # 3. Stream dummy audio data
        for i in range(5):
            dummy_audio = b"\x00\x01\x02\x03" * 256  # 1KB of dummy audio
            await websocket.send(dummy_audio)
            print(f"Sent audio chunk {i+1} (1024 bytes)")
            await asyncio.sleep(0.5)

if __name__ == "__main__":
    try:
        asyncio.run(test_client())
    except Exception as e:
        print(f"Failed to connect: {e}")
