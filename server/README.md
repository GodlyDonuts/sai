# Sai OS Agent - Cloud Backend Server

This directory contains the cloud backend component for the Sai OS Agent. It is built using **FastAPI** to provide a lightweight, high-performance WebSocket server capable of handling continuous data streaming (such as audio).

## Core Components

### 1. `main.py`
This is the primary entry point for the FastAPI application. It defines the server logic and websocket endpoints:

- **Endpoint:** `GET /`
  A simple health check endpoint to confirm the server is running.
  
- **Endpoint:** `WS /ws/agent`
  The core WebSocket endpoint that the local client connects to. The connection lifecycle is as follows:
  1. **Connection Acceptance**: The server accepts the incoming WebSocket connection.
  2. **Initial Handshake**: The server waits for an initial JSON payload indicating that the wake word was detected. It expects a payload in the format: `{"event": "wake_word_detected", "timestamp": <time>}`. If the payload is invalid or the event mismatches, it gracefully closes the connection.
  3. **Streaming Loop**: Once the handshake succeeds, the server enters a continuous asynchronous loop waiting for binary audio data. It currently receives the data and logs its size. *Note: In the future, this is where the server will pipe the audio data directly into the Gemini Live API for processing.*
  4. **Graceful Disconnects**: If the client disconnects or an exception occurs, the server catches the exception and logs the event without crashing.

### 2. `requirements.txt`
This file contains the Python dependencies required to run the server:
- `fastapi`: The web framework.
- `uvicorn[standard]`: The ASGI web server implementation used to run FastAPI.
- `websockets`: Provides necessary WebSocket support.

### 3. `test_client.py`
A simple Python script used to simulate a client connecting to the server. Since standard HTTP GET requests (like those from a browser) return a `404 Not Found` on WebSocket endpoints, this script genuinely tests the WebSocket connection protocol.
- Connects to `ws://localhost:8080/ws/agent`.
- Sends the expected JSON handshake.
- Simulates streaming by sending 1KB chunks of dummy binary data repeatedly.

## How to Run

**1. Install Dependencies**
```bash
pip install -r requirements.txt
```

**2. Start the Server**
```bash
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```
*(The `--reload` flag is useful during development to auto-restart the server on file changes.)*

**3. Run the Test Client**
In a separate terminal window:
```bash
python3 test_client.py
```
