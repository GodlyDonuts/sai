# 🏗️ Architecture & Technical Stack

## The Stack
* **The Brain:** Gemini 3.1 Pro (via Google AI Studio / Vertex AI) & Google Antigravity.
* **The Ears:** Gemini Live API (Native WebSocket audio streaming).
* **The Hands:** Local Desktop Client (Python/Rust) using OS accessibility APIs and PyAutoGUI.
* **The Infrastructure:** Google Cloud Run, Cloud Tasks, and Firebase.

## The Dual-Loop Cognitive Architecture
To solve the "Stop-and-Go" latency problem, we decouple hearing from seeing.

1. **The Fast Loop (Live Agent):** * Continuous WebSocket connection.
   * Maintains persona, handles conversational interruptions, and parses intent.
2. **The Slow Loop (UI Navigator):**
   * Triggered by the Fast Loop upon detecting a UI intent.
   * Captures screen, compresses, and prompts Gemini 3.1 Pro for exact `[X, Y]` coordinates of the target semantic element.
3. **The Proxy Tool Hand-off:**
   * Cloud model issues a tool call (e.g., `execute_click(x, y)`).
   * Intercepted in GCP, serialized over WebSocket to the local client, and executed instantly.