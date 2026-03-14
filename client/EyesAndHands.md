# Eyes and Hands Integration (OS Control & Screen Capture)

This document outlines the architecture and implementation details for the "Eyes and Hands" capability added to the local Python client.

## Objective
To allow the cloud server (acting as the brain) to view what is on the user's screen (Eyes) and interact with the operating system via mouse clicks (Hands) while maintaining a real-time, low-latency audio stream over a single WebSocket connection.

## Dependencies Introduced
*   **`mss`**: Used for ultra-fast, cross-platform screen capturing. It is highly optimized and much faster than alternatives like `Pillow`'s `ImageGrab`.
*   **`pyautogui`**: Used for cross-platform physical mouse and keyboard control.

These are added to `requirements.txt`.

## Architecture & Implementation

The core challenge was ensuring that OS-level blocking operations (taking a screenshot, moving the mouse) do not block the main `asyncio` event loop. If the event loop blocks, the bidirectional audio stream will stutter, drop packets, or disconnect.

### 1. WebSocket Protocol Update
The `receive_audio_from_websocket` function, which previously only expected binary PCM audio data, was updated to handle mixed content:
*   **Binary Data (`bytes`)**: Routed directly to the `pyaudio` output stream for real-time speaker playback.
*   **Text Data (`JSON text`)**: Parsed as commands from the server.

### 2. "Eyes": Screen Capture Command (`capture_screen`)
When the client receives the JSON payload `{"command": "capture_screen"}`:
1.  **Thread Delegation**: The `loop.run_in_executor(None, capture_screen_sync)` method is called. This offloads the synchronous screen capture to a background thread pool.
2.  **Capture & Compress**: The `capture_screen_sync` function uses `mss` to grab the primary monitor (`sct.monitors[1]`). It identifies the display geometry (`width` and `height`) and immediately compresses the raw RGB pixels into a PNG byte stream using `mss.tools.to_png` to minimize payload size.
3.  **Encoding**: The PNG bytes are Base64 encoded.
4.  **Response**: The client sends a JSON text message back up the WebSocket containing the image and the physical screen resolution: `{"event": "screen_captured", "image_base64": "<base64_string>", "width": 1920, "height": 1080}`.

### 3. "Hands": Mouse Click Command (`click`)
When the client receives the JSON payload `{"command": "click", "x": <int>, "y": <int>}`:
1.  **Thread Delegation**: Similar to screen capture, the click operation is offloaded via `loop.run_in_executor(None, perform_click_sync, x, y)`.
2.  **Execution**: `pyautogui.click(x, y)` takes control of the OS cursor, moves it to the specified `(x, y)` coordinate, and performs a native left-click.

## Future Considerations
*   **Keyboard Input**: `pyautogui` can be easily extended to support `typewrite` or `press` commands by adding new JSON command parsers.
*   **Multi-Monitor Support**: Currently hardcoded to `monitors[1]` (the primary monitor). This could be parameterized.
*   **Safety Failsafe**: `pyautogui` has a built-in failsafe (slamming the mouse to the corner of the screen aborts the script). Ensure this remains active during development to prevent the agent from taking hostile control of the machine.
