# Sai OS Agent — Project Summary & Status

## Overview
Sai is a voice-native OS co-pilot for macOS. It uses a **Hybrid Brain** architecture:

| Layer | Technology | Role |
|---|---|---|
| **Senses** | Deepgram Nova-2 | Real-time audio transcription (upstream only) |
| **Routing Brain** | Amazon Nova 2 Lite (AWS Bedrock) | Classifies command as SIMPLE or ADVANCED |
| **Simple Brain** | Amazon Nova 2 Lite (AWS Bedrock) | Extracts single tool call for app launches |
| **Senior Brain** | Amazon Nova 2 Pro (AWS Bedrock) | Multi-step vision reasoning for complex tasks |
| **Client Executor** | Python + PyAutoGUI + mss | Executes physical Mac actions (Silent) |

---

## Current Status (March 15, 2026) — ✅ Functional

The full end-to-end pipeline is **working**. Wake word is detected, audio streams to the cloud, Deepgram transcribes, and the Amazon Nova brain issues real OS commands that execute on the Mac.

### Architecture Flow
1. **User says "Hey Sai..."** → `wake_word.py` detects wake word via Picovoice Porcupine
2. **Screenshot auto-captured** → Sent to server for visual context
3. **Audio streams** → `wake_word.py` → FastAPI WebSocket → Deepgram Live API
4. **Deepgram transcribes** → Sentences are processed immediately with a 1.5s debounce
5. **Router classifies** → SIMPLE (Nova Lite) or ADVANCED (multi-step agent loop)
6. **SIMPLE path** → Single tool command extracted and sent to client immediately
7. **ADVANCED path** → Multi-step agent loop (up to 10 steps):
   - Screenshot is sent to Nova Pro
   - Nova Pro reasons about the screenshot and outputs a command
   - Command is executed on Mac, then a **fresh screenshot** is captured
   - Loop continues until the model reports `"done": true`
8. **Silent Operation** → No audio is streamed back; the agent operates silently.

---

## Configuration

| Setting | Value |
|---|---|
| Wake Word Model | Picovoice Porcupine (`HeySai_mac.ppn`) |
| Senses (Transcription) | Deepgram Nova-2 |
| Routing / Simple Brain | Amazon Nova 2 Lite (AWS Bedrock) |
| Senior Brain (Vision) | Amazon Nova 2 Pro (AWS Bedrock) |
| Client OS Control | `pyautogui` + `mss` (screenshot) + `pbcopy` (clipboard paste) |
| Audio Output | Disabled (Silent Mode) |

---

## Supported Commands (Client)
| Command | Description |
|---|---|
| `type_text` | Opens Spotlight, types app name, presses Enter |
| `open_url` | Opens URL directly in default browser |
| `click` | Clicks at `(x, y)` pixel coordinates |
| `keyboard_type` | Pastes text via clipboard into focused element |
| `capture_screen` | Captures and returns screenshot to server |

---

## Known Issues / Limitations

- **Spotify search bar** — `keyboard_type` after `click` is inconsistent; Spotify may need a keyboard shortcut (`Cmd+L`) to reliably focus the search bar
- **Transcription fragments** — Deepgram sometimes fires intermediate transcripts, handled by a 1.5s debounce to ensure full sentence processing
- **SIMPLE routing too aggressive** — Some commands that need vision (e.g. "create a new document") are routed as SIMPLE and produce hallucinated commands

## Next Steps
- Improve routing accuracy by giving Nemotron more examples and a broader ADVANCED category
- Add `press_key` command (for `Enter`, `Escape`, arrow keys, `Cmd+L` etc.) to the client
- Add memory / context persistence so Sai remembers previous actions within a session
