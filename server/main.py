import logging
import json
import base64
import asyncio
import os
import io
import collections
import typing
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from datetime import datetime
from openai import OpenAI
import websockets as ws_client
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sai-server")

app = FastAPI(title="Sai OS Agent Cloud Backend (Nova + ElevenLabs)")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_STT_WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"

# Nova Setup
# Lite model stays on Amazon Nova endpoint
nova_client = OpenAI(
    api_key=os.getenv("AMAZON_NOVA_API_KEY"),
    base_url=os.getenv("NOVA_BASE_URL", "https://api.nova.amazon.com/v1")
)

NOVA_LITE_MODEL_ID = "nova-2-lite-v1"

# Pro model routes through OpenRouter to avoid Amazon RPM limits
nova_pro_client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
)

NOVA_PRO_MODEL_ID = "amazon/nova-pro-v1"

MAX_AGENT_STEPS = 25
ACTION_SETTLE_TIME = 2.0  # seconds to wait after an action for the UI to update
SCREENSHOT_TIMEOUT = 5.0  # seconds to wait for a screenshot from client

def extract_json(text: str) -> typing.Optional[dict]:
    """Robustly extract JSON from potentially verbose model output."""
    try:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            return json.loads(text[start:end+1])
    except Exception as e:
        logger.error(f"JSON extraction failed: {e} | Raw: {text[:100]}...")
    return None

def annotate_screenshot(image_b64: str, last_action: dict = None) -> str:
    """Draw minimal edge-only ruler ticks and optional last action marker on the screenshot.
    
    No full-screen grid lines — only small tick marks along the top and left edges
    every 200px so the LLM can use them as spatial references without cluttering the UI.
    """
    try:
        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size
        
        # Try to load a font, otherwise use default
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
        except:
            font = ImageFont.load_default()
        
        TICK_LEN = 12       # length of ruler tick marks in pixels
        LABEL_PAD = 2       # padding between tick and label text
        # We draw ticks at 0–1000 normalized coordinates mapped along each edge
        # so they match the model's coordinate system.
        NUM_STEPS = 5       # 0, 200, 400, 600, 800, 1000
        TICK_COLOR = (255, 60, 60)  # red ticks
        LABEL_COLOR = (255, 255, 255)  # white labels for readability
        
        step_norm = 1000 // NUM_STEPS
        # --- Top edge ruler (X-axis, 0–1000) ---
        for i in range(0, NUM_STEPS + 1):
            norm = i * step_norm
            x = int(round((norm / 1000) * w))
            if i == 0:
                continue  # skip origin to avoid overlap
            draw.line([(x, 0), (x, TICK_LEN)], fill=TICK_COLOR, width=2)
            draw.text((x + LABEL_PAD, LABEL_PAD), str(norm), fill=LABEL_COLOR, font=font)
        
        # --- Left edge ruler (Y-axis, 0–1000) ---
        for i in range(0, NUM_STEPS + 1):
            norm = i * step_norm
            y = int(round((norm / 1000) * h))
            if i == 0:
                continue
            draw.line([(0, y), (TICK_LEN, y)], fill=TICK_COLOR, width=2)
            draw.text((LABEL_PAD, y + LABEL_PAD), str(norm), fill=LABEL_COLOR, font=font)
        
        # --- Last-click crosshair (debug feedback) ---
        if last_action and last_action.get("command") == "click":
            lx, ly = int(last_action.get("x", 0)), int(last_action.get("y", 0))
            r = 20
            draw.ellipse([lx-r, ly-r, lx+r, ly+r], outline="lime", width=3)
            draw.line([lx-r*2, ly, lx+r*2, ly], fill="lime", width=2)
            draw.line([lx, ly-r*2, lx, ly+r*2], fill="lime", width=2)
            draw.text((lx + r + 5, ly - 10), f"CLICKED ({lx},{ly})", fill="lime", font=font)
        
        # Encode back to base64 PNG
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to annotate screenshot: {e}")
        return image_b64  # fallback to original

@app.get("/")
async def root():
    return {"message": "Sai OS Agent Cloud Backend is running"}

@app.websocket("/ws/agent")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("New client connection established")
    
    connection_state = {
        "latest_sight": "No visual context yet.",
        "latest_screenshot_b64": None,
        "latest_app_context": {},
        "transcription_buffer": [],
        "debounce_task": None,
        "active_agent_task": None,
        "command_triggered": False,
        "screenshot_event": asyncio.Event()
    }
    
    if not ELEVENLABS_API_KEY:
        logger.error("ELEVENLABS_API_KEY not found in environment")
        await websocket.close(code=1011)
        return

    try:
        # Initial Handshake with local client
        initial_data = await websocket.receive_text()
        event_payload = json.loads(initial_data)
        if event_payload.get("event") == "wake_word_detected":
            logger.info("Handshake successful")
            await websocket.send_json({"status": "handshake_complete"})
            # Trigger initial screenshot
            await websocket.send_text(json.dumps({"command": "capture_screen"}))
        else:
            await websocket.close(code=1003)
            return

        def _build_app_context_summary() -> str:
            """Summarize what the user currently has on screen."""
            ctx = connection_state.get("latest_app_context", {})
            app = ctx.get("app_name", "")
            url = ctx.get("tab_url", "")
            title = ctx.get("tab_title", "")
            if not app:
                return "No active app information available."
            parts = [f"Active app: {app}"]
            if url:
                parts.append(f"Browser tab URL: {url}")
            if title:
                parts.append(f"Tab title: {title}")
            return " | ".join(parts)

        async def interpret_intent(raw_transcription: str) -> str:
            """Fix garbled speech-to-text by interpreting the user's likely command intent."""
            app_ctx_summary = _build_app_context_summary()
            prompt = f"""You are a voice command interpreter for a macOS desktop assistant called Sai.
The user spoke a command after saying the wake word "Hey Sai". Speech-to-text may have introduced errors.
Your job: reconstruct the user's ACTUAL INTENDED COMMAND from the (possibly garbled) transcription.

CURRENT SCREEN CONTEXT:
{app_ctx_summary}

RAW TRANSCRIPTION: "{raw_transcription}"

RULES:
- The user is giving a COMMAND (an instruction to do something), never making a casual statement.
- Use the screen context to disambiguate. If the user is on twitter.com and the transcription mentions "twitter" or "sharing", they likely want to change a Twitter setting.
- Common speech-to-text errors:
  • "they're not" / "their not" → "turn off"
  • "they're on" → "turn on"
  • "clothes" → "close"
  • "right" → "write"
  • Homophones and near-homophones are extremely common.
- Output ONLY the corrected command as a short imperative sentence. No explanation, no JSON, no quotes.

Examples:
- Raw: "they're not data sharing on twitter" → Turn off data sharing on Twitter
- Raw: "clothes the window" → Close the window
- Raw: "open a new tabb" → Open a new tab
- Raw: "go to read it" → Go to Reddit"""

            try:
                response = nova_client.chat.completions.create(
                    model=NOVA_LITE_MODEL_ID,
                    messages=[
                        {"role": "system", "content": "Output only the corrected command. No explanation."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0
                )
                corrected = response.choices[0].message.content.strip().strip('"').strip("'")
                if corrected and len(corrected) > 2:
                    logger.info(f"Intent interpretation: '{raw_transcription}' → '{corrected}'")
                    return corrected
            except Exception as e:
                logger.error(f"Intent interpretation failed, using raw: {e}")
            return raw_transcription

        async def hybrid_reasoning(user_text, latest_sight, latest_image_b64):
            """Routes task to Nova Lite (simple/launch) or Nova Pro (advanced vision agent)."""
            app_ctx_summary = _build_app_context_summary()

            routing_prompt = f"""You are the Task Router for Sai, a macOS desktop assistant.
Your job: decide if the user's request is SIMPLE or ADVANCED.

CURRENT SCREEN CONTEXT:
{app_ctx_summary}

RULES:
- SIMPLE means a single fire-and-forget action that does NOT need to see the screen:
  • Launching an app ("Open Chrome", "Open Spotify")
  • Opening a brand-new URL that the user is NOT already on
  • A single global hotkey ("Maximize this window", "Take a screenshot")
- ADVANCED means ANYTHING that requires looking at the screen, navigating UI, clicking elements, or interacting with content that is already visible:
  • Any task involving a website or app the user is ALREADY on (e.g. they're on twitter.com and say "turn off data sharing")
  • Any multi-step workflow, settings navigation, form filling, reading content
  • Any task mentioning a specific site/service when the user is ALREADY on that site
  • Searching within a page, scrolling, clicking buttons, toggling settings
  • Answering questions about what's on screen

CRITICAL: If the user's request relates to the app or website they currently have open, it is ALWAYS ADVANCED — the agent must use the existing screen, NOT launch a new app or open Spotlight.

User said: "{user_text}"

Output JSON: {{"complexity": "SIMPLE" | "ADVANCED", "reason": "one sentence why"}}"""
            try:
                response = nova_client.chat.completions.create(
                    model=NOVA_LITE_MODEL_ID,
                    messages=[
                        {"role": "system", "content": "Output JSON only."},
                        {"role": "user", "content": routing_prompt}
                    ],
                    temperature=0
                )
                raw_routing = response.choices[0].message.content
                routing_data = extract_json(raw_routing)
                if not routing_data:
                    raise ValueError(f"No JSON found in routing response: {raw_routing}")
                
                complexity = routing_data.get("complexity", "SIMPLE").upper()
                reason = routing_data.get("reason", "")
                logger.info(f"Routing decision: {complexity} | Reason: {reason} | Screen: {app_ctx_summary}")
            except Exception as e: 
                logger.error(f"Routing failed, defaulting to ADVANCED for safety: {e}")
                complexity = "ADVANCED"

            if complexity == "SIMPLE":
                try:
                    resp = nova_client.chat.completions.create(
                        model=NOVA_LITE_MODEL_ID,
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Convert the user's request to a SINGLE tool-call JSON.\n"
                                    "AVAILABLE COMMANDS:\n"
                                    "- Launch an app via Spotlight: {\"command\": \"type_text\", \"text\": \"AppName\"}\n"
                                    "- Open a URL in the browser: {\"command\": \"open_url\", \"url\": \"https://...\"}\n"
                                    "- Press a hotkey: {\"command\": \"press_hotkey\", \"keys\": [\"command\", \"n\"]}\n"
                                    "RULES:\n"
                                    "- type_text is ONLY for launching apps via Spotlight. NEVER use it to interact with on-screen content.\n"
                                    "- If the task requires interacting with what's already on screen, output: {\"command\": \"escalate\"}\n"
                                    "ONLY output JSON."
                                )
                            },
                            {"role": "user", "content": user_text}
                        ],
                        temperature=0
                    )
                    raw_simple = resp.choices[0].message.content
                    result = extract_json(raw_simple)
                    if not result:
                        raise ValueError(f"No JSON found in simple generation: {raw_simple}")
                    
                    if result.get("command") == "escalate":
                        logger.info("SIMPLE handler escalated to ADVANCED agent loop.")
                        complexity = "ADVANCED"
                    else:
                        logger.info(f"SIMPLE action generated: {result}")
                        return result
                except Exception as e:
                    logger.error(f"SIMPLE generation failed, escalating to ADVANCED: {e}")
                    complexity = "ADVANCED"

            # ADVANCED path
            if connection_state["active_agent_task"] and not connection_state["active_agent_task"].done():
                connection_state["active_agent_task"].cancel()
            connection_state["active_agent_task"] = asyncio.create_task(run_agent_loop(user_text))
            return None

        async def run_agent_loop(user_text):
            logger.info(f"Starting Agent Loop for task: {user_text}")
            app_ctx_summary = _build_app_context_summary()
            SENIOR_SYSTEM_PROMPT = f"""You are the Senior Vision Specialist for Sai, a macOS desktop agent.

CURRENT SCREEN CONTEXT:
{app_ctx_summary}

STRATEGIC APPROACH — THINK FIRST, THEN ACT:
1. On your FIRST step, analyze the screenshot carefully and form a clear PLAN for the task.
   Include your high-level plan in the "explanation" field (e.g., "Plan: 1) click code editor, 2) select all, 3) type solution, 4) click Submit. Step 1: clicking the code editor area.").
2. Every subsequent "explanation" must describe WHY this action advances the plan — not just narrate what you're clicking.
3. If an action doesn't clearly move toward the goal, DO NOT take it.

CRITICAL RULES:

WORK WITH WHAT IS ON SCREEN:
- If the relevant app/website is ALREADY open, work directly within it.
- Do NOT open Spotlight, launch a new browser, or navigate away unless the task explicitly requires it.
- NEVER use type_text (Spotlight) when the target app/site is already the frontmost window.
- To navigate within the browser, use Command+L for the address bar or click links.

YOU CAN READ THE SCREENSHOT — DON'T CLICK TO READ:
- You can see and read ALL text visible in the screenshot. You do NOT need to click on elements just to read their content.
- Do NOT click on tabs, test cases, descriptions, or UI elements just to "view" information that is already visible on screen.
- If information is visible in the screenshot, use it immediately in your reasoning.

GOAL-FOCUSED EXECUTION:
- Every action must directly advance toward completing the task.
- Do NOT explore the UI, click on random elements, or gather information unnecessarily.
- If you find yourself clicking around without making progress, STOP and reconsider your approach.

FOR CODING / PROBLEM-SOLVING TASKS (e.g., LeetCode, HackerRank, coding challenges):
a) READ the problem statement directly from the screenshot — it is already visible, no clicking needed.
b) THINK about the solution and describe it in your "explanation" field.
c) CLICK on the code editor / text input area to place your cursor there.
d) Select all existing code (Command+A) and then type your complete solution using keyboard_type.
e) Do NOT click on test cases, examples, constraints, or description tabs — you can already see everything you need.
f) After typing the solution, click the Submit or Run button.

COORDINATE SYSTEM:
Normalized 2D coordinates [0, 1000] x [0, 1000]:
- (0, 0) = top-left corner of the screen
- (1000, 1000) = bottom-right corner of the screen
The client maps these normalized coordinates to actual screen pixels.

AGENTIC BEHAVIOR:
- Do ONE action per step (or "wait" if the UI is loading).
- After each action, VERIFY the result in the next screenshot before continuing.
- Only set done=true when the screenshot CONFIRMS the task is fully complete.

TEXT ENTRY RULES:
- Use {{"command": "keyboard_type", "text": "..."}} for typing into ANY on-screen field (code editors, search bars, forms, text areas).
- type_text is ONLY for Spotlight app launching — never for on-screen content.
- NEVER use paste hotkeys (Command+V / Cmd+V).
- You may use other hotkeys for navigation (Command+L, Command+T, etc.).

Output ONLY valid JSON: {{"explanation": "strategic reasoning", "command": "...", "x": number, "y": number, "done": bool}}

SUPPORTED COMMANDS:
- {{"command": "open_url", "url": "https://..."}}
- {{"command": "click", "x": <0-1000>, "y": <0-1000>}}
- {{"command": "type_text", "text": "App Name"}} (Spotlight ONLY)
- {{"command": "keyboard_type", "text": "content to type"}} (on-screen typing)
- {{"command": "press_hotkey", "keys": ["command", "a"]}}
- {{"command": "scroll", "amount": <positive=down, negative=up>}}
- {{"command": "wait"}}"""
            conversation_history = [{"role": "system", "content": SENIOR_SYSTEM_PROMPT}]
            last_action = None
            recent_actions = []   # track last N actions for stuck detection
            
            def _action_signature(action: dict) -> str:
                """Compact fingerprint of an action for repetition detection."""
                cmd = action.get("command", "")
                if cmd == "click":
                    return f"click({action.get('x')},{action.get('y')})"
                elif cmd in ("keyboard_type", "type_text"):
                    return f"{cmd}({action.get('text', '')[:30]})"
                elif cmd == "scroll":
                    return f"scroll({action.get('amount')})"
                elif cmd == "press_hotkey":
                    return f"hotkey({action.get('keys')})"
                elif cmd == "open_url":
                    return f"url({action.get('url', '')[:40]})"
                return cmd

            def _detect_cycle(actions: list) -> typing.Optional[int]:
                """Detect repeating cycles in action history. Returns cycle length or None."""
                if len(actions) < 4:
                    return None
                for cycle_len in range(1, len(actions) // 2 + 1):
                    if cycle_len > 6:
                        break
                    recent = actions[-cycle_len:]
                    prev = actions[-cycle_len * 2:-cycle_len]
                    if recent == prev:
                        return cycle_len
                return None

            try:
                if not connection_state["latest_screenshot_b64"]:
                    logger.info("Waiting for initial screenshot...")
                    try:
                        await asyncio.wait_for(connection_state["screenshot_event"].wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.error("Initial screenshot timeout.")
                        return

                for step in range(MAX_AGENT_STEPS):
                    current_screenshot = connection_state["latest_screenshot_b64"]
                    if not current_screenshot:
                        logger.error("No screenshot available at step start.")
                        break
                    
                    logger.info(f"Agent Step {step+1}/{MAX_AGENT_STEPS} starting...")
                    annotated = annotate_screenshot(current_screenshot, last_action)
                    
                    if step == 0:
                        user_text_msg = (
                            f"Task: {user_text}\n"
                            "Analyze the screenshot carefully. In your explanation, describe your HIGH-LEVEL PLAN "
                            "for completing this task (numbered steps), then take the FIRST action that begins executing that plan.\n"
                            "Remember: you can READ all text visible in the screenshot — do NOT click on things just to read them."
                        )
                    else:
                        user_text_msg = (
                            f"Task (reminder): {user_text}\n"
                            "Here is the current screenshot after your last action. "
                            "Evaluate whether your last action succeeded, then take the next step toward the goal."
                        )
                    
                    # Stuck detection: check for identical repeats or cyclic patterns
                    cycle_len = _detect_cycle(recent_actions)
                    if cycle_len is not None:
                        cycle_actions = recent_actions[-cycle_len:]
                        logger.warning(f"CYCLE DETECTED (length {cycle_len}): {cycle_actions}")
                        user_text_msg += (
                            f"\n\nCRITICAL WARNING: You are stuck in a repeating loop of {cycle_len} actions: "
                            f"{cycle_actions}. These actions are NOT making progress toward the goal.\n"
                            "You MUST completely change your approach. Ask yourself:\n"
                            "- What is the ACTUAL GOAL? (e.g., write code, not explore the UI)\n"
                            "- Am I clicking on things I should be READING from the screenshot instead?\n"
                            "- Should I be TYPING something instead of clicking?\n"
                            "- Do I need to scroll, use a hotkey, or interact with a completely different part of the screen?\n"
                            "Take a fundamentally different action NOW."
                        )
                    elif len(recent_actions) >= 2 and len(set(recent_actions[-2:])) == 1:
                        logger.warning(f"STUCK DETECTED: repeated '{recent_actions[-1]}' twice.")
                        user_text_msg += (
                            f"\n\nWARNING: You repeated the same action ({recent_actions[-1]}) twice. "
                            "It is not working. Try a completely different approach."
                        )

                    user_content = [
                        {"type": "text", "text": user_text_msg},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{annotated}"
                            }
                        }
                    ]
                    conversation_history.append({"role": "user", "content": user_content})
                    
                    # Trim conversation to keep context focused and avoid overwhelming the model.
                    # Keep: system prompt + first exchange (plan) + last 3 exchanges (6 messages).
                    if len(conversation_history) > 9:
                        conversation_history = (
                            conversation_history[:3]
                            + conversation_history[-6:]
                        )

                    response = nova_pro_client.chat.completions.create(
                        model=NOVA_PRO_MODEL_ID,
                        messages=conversation_history,
                        temperature=0.2
                    )
                    raw_content = response.choices[0].message.content
                    logger.info(f"Agent raw response: {raw_content[:200]}...")
                    conversation_history.append({"role": "assistant", "content": raw_content})
                    
                    data = extract_json(raw_content)
                    if not data:
                        logger.error(f"Agent failed to produce JSON: {raw_content}")
                        break

                    logger.info(f"Agent parsed data: {data}")
                    cmd = data.get("command")
                    performed_action = False
                    if cmd in {"open_url", "click", "type_text", "keyboard_type", "press_hotkey", "scroll", "wait"}:
                        last_action = data
                        recent_actions.append(_action_signature(data))
                        logger.info(f"Sending command to client: {cmd}")
                        await websocket.send_text(json.dumps(data))
                        await asyncio.sleep(ACTION_SETTLE_TIME)
                        performed_action = True
                        
                    # Guardrail: never allow "done" on the same step we performed an action.
                    if data.get("done") and performed_action:
                        logger.info("Agent requested done immediately after action; forcing verification step.")
                        data["done"] = False

                    if data.get("done"):
                        logger.info("Agent signaled completion (done=True).")
                        break
                    
                    # Hard bail if stuck even after the cycle warning was injected
                    if len(recent_actions) >= 6:
                        cycle_len_check = _detect_cycle(recent_actions)
                        if cycle_len_check is not None:
                            # Check if the cycle has repeated 3+ times
                            total_cycle_actions = cycle_len_check * 3
                            if len(recent_actions) >= total_cycle_actions:
                                tail = recent_actions[-total_cycle_actions:]
                                chunks = [
                                    tuple(tail[i * cycle_len_check:(i + 1) * cycle_len_check])
                                    for i in range(3)
                                ]
                                if len(set(chunks)) == 1:
                                    logger.error(
                                        f"Agent hopelessly stuck in cycle of length {cycle_len_check}: "
                                        f"{list(chunks[0])}, aborting loop."
                                    )
                                    break

                    connection_state["screenshot_event"].clear()
                    await websocket.send_text(json.dumps({"command": "capture_screen"}))
                    await asyncio.wait_for(connection_state["screenshot_event"].wait(), timeout=SCREENSHOT_TIMEOUT)
            except Exception as e: logger.error(f"Agent error in run_agent_loop: {e}")

        async def process_complete_transcription():
            if connection_state["command_triggered"]: return
            full_text = " ".join(connection_state["transcription_buffer"]).strip()
            connection_state["transcription_buffer"] = []
            if not full_text: return
            
            connection_state["command_triggered"] = True
            logger.info(f"Raw transcription: {full_text}")
            
            # Cancel any pending debounce
            if connection_state["debounce_task"]: connection_state["debounce_task"].cancel()

            try:
                await websocket.send_text(json.dumps({"command": "set_activity", "state": "active"}))
            except Exception as e:
                logger.warning(f"Failed to send activity start: {e}")

            try:
                interpreted_text = await interpret_intent(full_text)
                logger.info(f"Processing (interpreted): {interpreted_text}")
                action = await hybrid_reasoning(interpreted_text, connection_state["latest_sight"], connection_state["latest_screenshot_b64"])
                if action: await websocket.send_text(json.dumps(action))
                
                # Wait for agent loop if it was started
                if connection_state["active_agent_task"]:
                    try: await connection_state["active_agent_task"]
                    except asyncio.CancelledError: pass
            finally:
                try:
                    await websocket.send_text(json.dumps({"command": "set_activity", "state": "idle"}))
                except Exception as e:
                    logger.warning(f"Failed to send activity stop: {e}")
                
                # Finalize session to return to wake-word mode
                logger.info("One-shot command complete. Closing session.")
                await websocket.close()

        async def debounce_and_process():
            await asyncio.sleep(1.5)
            await process_complete_transcription()

        # Build ElevenLabs WebSocket URL with query params
        el_params = (
            f"?model_id=scribe_v2_realtime"
            f"&language_code=en"
            f"&audio_format=pcm_16000"
            f"&commit_strategy=vad"
            f"&vad_silence_threshold_secs=1.2"
        )
        el_url = ELEVENLABS_STT_WS_URL + el_params
        el_headers = {"xi-api-key": ELEVENLABS_API_KEY}

        async with ws_client.connect(el_url, additional_headers=el_headers) as el_ws:
            logger.info("Connected to ElevenLabs Scribe v2 Realtime STT")

            async def listen_elevenlabs():
                """Receive transcription events from ElevenLabs."""
                try:
                    async for msg in el_ws:
                        if connection_state["command_triggered"]:
                            continue
                        try:
                            data = json.loads(msg)
                            msg_type = data.get("message_type", "")

                            if msg_type == "session_started":
                                logger.info(f"ElevenLabs session started: {data.get('session_id')}")

                            elif msg_type == "partial_transcript":
                                text = data.get("text", "").strip()
                                if text:
                                    logger.info(f"Partial: {text}")

                            elif msg_type in ("committed_transcript", "committed_transcript_with_timestamps"):
                                text = data.get("text", "").strip()
                                if text:
                                    logger.info(f"Committed transcript: {text}")
                                    connection_state["transcription_buffer"].append(text)

                                    if connection_state["debounce_task"]:
                                        connection_state["debounce_task"].cancel()
                                    await process_complete_transcription()

                            elif msg_type in ("error", "auth_error", "quota_exceeded",
                                              "rate_limited", "transcriber_error"):
                                logger.error(f"ElevenLabs STT error: {data}")

                        except json.JSONDecodeError:
                            logger.warning(f"Non-JSON from ElevenLabs: {msg[:100]}")
                except Exception as e:
                    logger.error(f"ElevenLabs listener error: {e}")

            el_listen_task = asyncio.create_task(listen_elevenlabs())

            try:
                while True:
                    try:
                        message = await websocket.receive()
                    except RuntimeError:
                        break
                    if message.get("bytes"):
                        if not connection_state["command_triggered"]:
                            pcm_bytes = message["bytes"]
                            audio_b64 = base64.b64encode(pcm_bytes).decode("utf-8")
                            chunk_msg = json.dumps({
                                "message_type": "input_audio_chunk",
                                "audio_base_64": audio_b64,
                                "commit": False,
                                "sample_rate": 16000
                            })
                            try:
                                await el_ws.send(chunk_msg)
                            except Exception as e:
                                logger.error(f"Failed to send audio to ElevenLabs: {e}")
                    elif message.get("text"):
                        data = json.loads(message["text"])
                        if data.get("event") == "screen_captured":
                            connection_state["latest_screenshot_b64"] = data.get("image_base64")
                            connection_state["latest_app_context"] = data.get("app_context", {})
                            connection_state["screenshot_event"].set()
            except WebSocketDisconnect:
                logger.info("Client disconnected")
            finally:
                el_listen_task.cancel()
    except Exception as e: 
        logger.error(f"WebSocket error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        logger.info("Session ended")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
