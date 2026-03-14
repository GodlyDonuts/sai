# Eyes, Hands, and the Brain (Server-Client Agent Architecture)

This document explains how the Gemini Cloud Server (`main.py`) orchestrates actions on the User's Local OS (`wake_word.py`) via the Multimodal Live API and WebSockets.

## Overview
The local `wake_word.py` client streams raw microphone audio to the `main.py` FastAPI server, which proxies that audio to the Google Gemini Multimodal Live API. The integration adds **Tool Calling** (Eyes and Hands) so the AI can perceive and manipulate the desktop natively.

## 1. Tool Declarations (The Setup Phase)
When a connection with Gemini is forged, the FastAPI server sends a `setup` payload. In this payload, we now pass a `tools` array to explicitly give Gemini new faculties via `functionDeclarations`. 

```json
{
    "name": "request_screen_capture",
    "description": "Call this to see the user's current screen..."
},
{
    "name": "execute_click",
    "description": "Call this to physically click the mouse...",
    "parameters": { "x": "INTEGER", "y": "INTEGER"}
}
```

### The Cognitive System Prompt
To ensure the model successfully bridges the gap between seeing an image and calculating exact pixel coordinates, we inject a highly rigorous `systemInstruction` in the setup payload:
*   It explicitly mandates a **"Perceive -> Calculate -> Execute"** loop.
*   The AI is instructed to request a screen capture, visually scan for the UI element, calculate the exact `[X, Y]` coordinate of its center, and use the `execute_click` tool. 
*   It is strictly commanded to act definitively and to remain incredibly concise ("zero-click web navigation").

## 2. Server Downstream: Receiving AI Commands
The `downstream` coroutine constantly listens to the Gemini WebSocket. Normally, it receives `inlineData` chunks (which are converted to binary and sent to the local speakers).
If the AI decides it needs to see or interact:
1.  **Intercept**: It receives a `functionCall` payload.
2.  **Translate**: Based on the function name (`request_screen_capture` or `execute_click`), the FastAPI server translates the AI request into a text-based JSON `{"command": ...}` message.
3.  **Forward**: This JSON command is sent down the client-server WebSocket to the local app.

## 3. Server Upstream: Proxying Client Replies
The `upstream` coroutine constantly listens to the local client WebSocket. Normally it expects raw `bytes` (microphone audio) and routes it to `realtimeInput` messages for Gemini.
If the client responds with JSON `text` instead:
1.  **Parse**: It reads the event type (e.g. `{"event": "screen_captured", "image_base64": "...", "width": 1920, "height": 1080}`).
2.  **Package**: It takes the data (the base64 screenshot) and packages it into the exact format Gemini expects: `clientContent` > `functionResponse`. It also appends a critical helper message telling Gemini the exact width and height of the captured screen: `Here is the screenshot. The resolution is 1920x1080.`
3.  **Forward**: It sends this payload back to Gemini. This allows the AI's complex spatial reasoning engine to calculate exact [X, Y] pixel geometry.

## 4. Key Takeaways regarding Asynchronous IO Management
Because the `upstream` and `downstream` coroutines run independently (`asyncio.gather`), one does not block the other!
*   **Audio Reliability**: While the server waits for the client to capture, compress, base64 encode, and upload the heavy screenshot in `upstream`, the `downstream` loop remains free to continue streaming Gemini's voice down to the speakers.
*   **Immediate ACKs**: When sending a minor command like `execute_click`, the server immediately fires a generic `status: "success"` `functionResponse` back to Google to move the conversation along rather than waiting for a round-trip confirmation from the client OS.
