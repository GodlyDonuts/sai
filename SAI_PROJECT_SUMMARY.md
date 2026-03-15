# Sai OS Agent - Project Summary & Status

## Overview
Sai is a voice-native OS co-pilot designed for Mac. It uses a **Hybrid Brain** architecture:
- **Gemini 2.5 Flash Native Audio:** Handles the "Senses" (Voice input, Audio output, and Transcription).
- **Cerebras (Qwen 3 / Llama 3.1):** Handles the "Cortex" (Logic, Decision making, and Tool generation).
- **Python Client:** Executes physical Mac commands via `pyautogui` (Clicking, Typing, Spotlight).

## Current Status (March 15, 2026)
We are currently facing an **Instant Disconnection** issue with the Gemini Live API. The connection to the Google WebSocket closes immediately after sending the `setup` payload.

### The Problem
- **Endpoint:** Using `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent`.
- **Symptom:** Disconnects after `setup`. No JSON error received.
- **Hypothesis:** The `setup` JSON schema for Gemini 2.5 Native Audio might have changed or requires specific flags (like `inputAudioTranscription`) located differently in the payload.

### Current Implementation Flow:
1. **User speaks** -> `wake_word.py` (Hey Sai) detected.
2. **Audio streams** to `main.py` -> Proxied to **Gemini**.
3. **Gemini Transcribes** -> Server receives text.
4. **Server triggers Cerebras** -> Receives tool JSON.
5. **Server sends JSON to Client** -> Client clicks/types.
6. **Gemini speaks** -> "Got it, opening Safari."

## Configuration Details
- **Gemini Model:** `models/gemini-2.5-flash-native-audio-latest`
- **Cerebras Model:** `qwen-3-235b-a22b-instruct-2507`
- **Tool Logic:** Spotlight is used for app launching (Cmd+Space + typing + Enter).

## Next Steps
- Solve the Bidi WebSocket handshake for the Native Audio model.
- Enable `inputAudioTranscription` correctly.
- Debug the client-side audio playback (user currently cannot hear model).
