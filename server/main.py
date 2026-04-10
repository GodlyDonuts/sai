"""
Sai OS Agent — Cloud Backend v2.0
Production-grade rewrite: Pydantic structured outputs, swarm sub-agents,
SQLite persistent memory, summarization chain, coordinate guardrails.
"""
import logging
import json
import base64
import asyncio
import os
import io
import sqlite3
import threading
import typing
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from openai import OpenAI
import websockets as ws_client
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field, model_validator

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sai-server")

app = FastAPI(title="Sai OS Agent Cloud Backend", version="2.0.0")

# ---------------------------------------------------------------------------
# LLM clients
# ---------------------------------------------------------------------------

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_STT_WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"

nova_client = OpenAI(
    api_key=os.getenv("AMAZON_NOVA_API_KEY"),
    base_url=os.getenv("NOVA_BASE_URL", "https://api.nova.amazon.com/v1"),
)
NOVA_LITE_MODEL_ID = "nova-2-lite-v1"

nova_pro_client = OpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
)
NOVA_PRO_MODEL_ID = "amazon/nova-pro-v1"

MAX_AGENT_STEPS = 25
ACTION_SETTLE_TIME = 2.0       # seconds to let the UI settle after an action
SCREENSHOT_TIMEOUT = 5.0       # seconds to wait for a screenshot from the client
HISTORY_COMPRESS_THRESHOLD = 9 # compress history when it exceeds this many messages
HISTORY_RECENT_KEEP_PAIRS = 4  # keep the last N user+assistant pairs after compression
ENABLE_CRITIC = os.getenv("SAI_CRITIC_ENABLED", "true").lower() == "true"

# ---------------------------------------------------------------------------
# Pydantic models — every LLM structured output is validated here
# ---------------------------------------------------------------------------

class RoutingDecision(BaseModel):
    complexity: typing.Literal["SIMPLE", "ADVANCED"]
    reason: str


class SimpleAction(BaseModel):
    command: typing.Literal["type_text", "open_url", "press_hotkey", "escalate"]
    text: typing.Optional[str] = None
    url: typing.Optional[str] = None
    keys: typing.Optional[list[str]] = None


class AgentAction(BaseModel):
    explanation: str
    command: typing.Literal[
        "click", "type_text", "keyboard_type", "open_url",
        "press_hotkey", "scroll", "wait",
    ]
    x: typing.Optional[int] = Field(default=None, ge=0, le=1000)
    y: typing.Optional[int] = Field(default=None, ge=0, le=1000)
    text: typing.Optional[str] = None
    url: typing.Optional[str] = None
    keys: typing.Optional[list[str]] = None
    amount: typing.Optional[int] = None
    done: bool = False

    @model_validator(mode="after")
    def click_requires_coords(self) -> "AgentAction":
        if self.command == "click" and (self.x is None or self.y is None):
            raise ValueError("click command requires both x and y coordinates in [0, 1000]")
        return self


class CriticVerdict(BaseModel):
    approved: bool
    reason: str
    corrected_x: typing.Optional[int] = Field(default=None, ge=0, le=1000)
    corrected_y: typing.Optional[int] = Field(default=None, ge=0, le=1000)


class MemoryContext(BaseModel):
    relevant_facts: list[str] = Field(default_factory=list)
    session_summary: str = ""


class ConversationSummary(BaseModel):
    summary: str


class IntentResult(BaseModel):
    corrected_command: str

# ---------------------------------------------------------------------------
# Persistent memory — SQLite at ~/.sai/memory.db
# ---------------------------------------------------------------------------

_MEMORY_DB_PATH = Path.home() / ".sai" / "memory.db"
_db_lock = threading.Lock()
_memory_db: typing.Optional[sqlite3.Connection] = None


def _get_db() -> sqlite3.Connection:
    global _memory_db
    if _memory_db is None:
        _MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_MEMORY_DB_PATH), check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT    NOT NULL,
                task       TEXT,
                outcome    TEXT,
                summary    TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT    NOT NULL,
                category   TEXT    NOT NULL,
                content    TEXT    NOT NULL UNIQUE
            )
        """)
        conn.commit()
        _memory_db = conn
    return _memory_db


def store_session(task: str, outcome: str, summary: str) -> None:
    with _db_lock:
        _get_db().execute(
            "INSERT INTO sessions (started_at, task, outcome, summary) VALUES (?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), task, outcome, summary),
        )
        _get_db().commit()


def fetch_recent_sessions(limit: int = 5) -> list[dict]:
    with _db_lock:
        rows = _get_db().execute(
            "SELECT task, outcome, summary FROM sessions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"task": r[0], "outcome": r[1], "summary": r[2]} for r in rows]


def store_fact(category: str, content: str) -> None:
    with _db_lock:
        _get_db().execute(
            "INSERT OR IGNORE INTO facts (created_at, category, content) VALUES (?, ?, ?)",
            (datetime.utcnow().isoformat(), category, content),
        )
        _get_db().commit()


def fetch_facts(limit: int = 20) -> list[dict]:
    with _db_lock:
        rows = _get_db().execute(
            "SELECT category, content FROM facts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"category": r[0], "content": r[1]} for r in rows]

# ---------------------------------------------------------------------------
# Structured LLM call helper
# ---------------------------------------------------------------------------

def _call_structured_sync(
    client: OpenAI,
    model: str,
    messages: list[dict],
    response_model: type[BaseModel],
    temperature: float = 0,
) -> BaseModel:
    """
    Synchronous wrapper: calls the LLM and validates the response into a Pydantic model.

    Strategy:
    1. Request JSON mode (response_format=json_object) when the endpoint supports it.
    2. Fall back to a plain call if JSON mode is unsupported.
    3. In both cases, extract the JSON substring from the raw content, then
       validate it strictly with Pydantic. No regex hacks — if parsing fails
       we raise so the caller can decide how to recover.
    """
    # Attempt JSON mode first
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
    except Exception:
        # Endpoint doesn't support response_format — plain call
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
        )

    raw = response.choices[0].message.content.strip()

    # Strip markdown fences if the model added them
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    # Locate the JSON object if the model added prose around it
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start : end + 1]

    try:
        return response_model.model_validate_json(raw)
    except Exception as exc:
        raise ValueError(
            f"Pydantic validation failed for {response_model.__name__}: {exc} | raw={raw[:300]}"
        ) from exc


async def _call_structured(
    client: OpenAI,
    model: str,
    messages: list[dict],
    response_model: type[BaseModel],
    temperature: float = 0,
) -> BaseModel:
    """Async shim: runs the blocking OpenAI call in the default thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _call_structured_sync(client, model, messages, response_model, temperature),
    )

# ---------------------------------------------------------------------------
# Screenshot annotation
# ---------------------------------------------------------------------------

def annotate_screenshot(
    image_b64: str,
    last_action: typing.Optional[dict] = None,
) -> str:
    """
    Draw ruler tick marks on the screenshot edges and optionally a crosshair
    at the last click position.  All coordinates are treated as normalized
    [0, 1000] values and mapped to actual image pixels — the image may be
    any resolution.
    """
    try:
        img = Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size

        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
        except Exception:
            font = ImageFont.load_default()

        TICK_LEN = 12
        LABEL_PAD = 2
        TICK_STEPS = 5          # marks at 0, 200, 400, 600, 800, 1000
        TICK_COLOR = (255, 60, 60)
        LABEL_COLOR = (255, 255, 255)
        step_norm = 1000 // TICK_STEPS

        for i in range(1, TICK_STEPS + 1):
            norm = i * step_norm
            # Top-edge (X-axis)
            px = int(round((norm / 1000) * w))
            draw.line([(px, 0), (px, TICK_LEN)], fill=TICK_COLOR, width=2)
            draw.text((px + LABEL_PAD, LABEL_PAD), str(norm), fill=LABEL_COLOR, font=font)
            # Left-edge (Y-axis)
            py = int(round((norm / 1000) * h))
            draw.line([(0, py), (TICK_LEN, py)], fill=TICK_COLOR, width=2)
            draw.text((LABEL_PAD, py + LABEL_PAD), str(norm), fill=LABEL_COLOR, font=font)

        # Last-click crosshair (normalized → pixel)
        if last_action and last_action.get("command") == "click":
            norm_x = float(last_action.get("x", 0))
            norm_y = float(last_action.get("y", 0))
            px = int(round((norm_x / 1000) * w))
            py = int(round((norm_y / 1000) * h))
            r = 20
            draw.ellipse([px - r, py - r, px + r, py + r], outline="lime", width=3)
            draw.line([px - r * 2, py, px + r * 2, py], fill="lime", width=2)
            draw.line([px, py - r * 2, px, py + r * 2], fill="lime", width=2)
            draw.text(
                (px + r + 5, py - 10),
                f"CLICKED ({int(norm_x)},{int(norm_y)})",
                fill="lime",
                font=font,
            )

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as exc:
        logger.error(f"annotate_screenshot failed: {exc}")
        return image_b64

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "message": "Sai OS Agent Cloud Backend is running",
        "version": "2.0.0",
        "critic_enabled": ENABLE_CRITIC,
        "memory_db": str(_MEMORY_DB_PATH),
    }


@app.websocket("/ws/agent")
async def websocket_endpoint(websocket: WebSocket):  # noqa: C901
    await websocket.accept()
    logger.info("New client connection established")

    state: dict[str, typing.Any] = {
        "latest_screenshot_b64": None,
        "latest_app_context": {},
        "screen_width": None,           # physical pixel dimensions reported by client
        "screen_height": None,
        "transcription_buffer": [],
        "debounce_task": None,
        "active_agent_task": None,
        "command_triggered": False,
        "screenshot_event": asyncio.Event(),
    }

    if not ELEVENLABS_API_KEY:
        logger.error("ELEVENLABS_API_KEY not set")
        await websocket.close(code=1011)
        return

    try:
        # ------------------------------------------------------------------
        # Handshake
        # ------------------------------------------------------------------
        initial_data = await websocket.receive_text()
        event_payload = json.loads(initial_data)
        if event_payload.get("event") != "wake_word_detected":
            await websocket.close(code=1003)
            return

        logger.info("Handshake successful")
        await websocket.send_json({"status": "handshake_complete"})
        await websocket.send_text(json.dumps({"command": "capture_screen"}))

        # ------------------------------------------------------------------
        # Helpers
        # ------------------------------------------------------------------

        def _app_ctx_summary() -> str:
            ctx = state.get("latest_app_context", {})
            app_name = ctx.get("app_name", "")
            if not app_name:
                return "No active app information available."
            parts = [f"Active app: {app_name}"]
            if url := ctx.get("tab_url", ""):
                parts.append(f"Browser tab URL: {url}")
            if title := ctx.get("tab_title", ""):
                parts.append(f"Tab title: {title}")
            return " | ".join(parts)

        # ------------------------------------------------------------------
        # Sub-Agent 1 — Memory Fetcher
        # Runs once per task before the Senior Brain loop starts.
        # Retrieves and ranks relevant past sessions and stored facts.
        # ------------------------------------------------------------------

        async def memory_sub_agent(task: str) -> MemoryContext:
            recent = fetch_recent_sessions(limit=5)
            facts = fetch_facts(limit=20)
            if not recent and not facts:
                return MemoryContext()

            prompt = (
                f'You are the Memory Sub-Agent for Sai, a macOS desktop assistant.\n'
                f'Given the CURRENT TASK and past memory, identify what is RELEVANT.\n\n'
                f'CURRENT TASK: "{task}"\n\n'
                f'PAST SESSIONS:\n{json.dumps(recent, indent=2)}\n\n'
                f'STORED FACTS:\n{json.dumps(facts, indent=2)}\n\n'
                'Output JSON:\n'
                '- relevant_facts: list of strings (max 5) — concise, directly relevant facts\n'
                '- session_summary: one sentence of the most relevant past context, or ""\n'
                'If nothing is relevant, return empty values.'
            )
            try:
                result = await _call_structured(
                    nova_client,
                    NOVA_LITE_MODEL_ID,
                    [
                        {"role": "system", "content": "Output JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    MemoryContext,
                )
                logger.info(
                    f"Memory sub-agent: {len(result.relevant_facts)} facts, "
                    f"summary={'yes' if result.session_summary else 'none'}"
                )
                return result
            except Exception as exc:
                logger.error(f"Memory sub-agent failed: {exc}")
                return MemoryContext()

        # ------------------------------------------------------------------
        # Sub-Agent 2 — Critic
        # Intercepts every planned click and verifies the coordinate lands on
        # an interactive element in the annotated screenshot.  Uses Nova Lite
        # for minimal latency overhead.
        # ------------------------------------------------------------------

        async def critic_sub_agent(
            action: AgentAction,
            annotated_b64: str,
        ) -> CriticVerdict:
            if not ENABLE_CRITIC or action.command != "click":
                return CriticVerdict(approved=True, reason="Critic skipped.")

            prompt = (
                f"You are a UI Critic verifying a planned click.\n"
                f"Agent plans to click at normalized ({action.x}, {action.y}) on a 0-1000 grid.\n"
                f"Agent reasoning: {action.explanation}\n\n"
                "The screenshot has ruler tick marks on the top and left edges at 200, 400, 600, 800, 1000.\n"
                "Use them to estimate where the click will land.\n\n"
                "Does this coordinate land on an interactive element (button, link, input, tab, etc.)?\n\n"
                "Output JSON:\n"
                "- approved: true if the click looks correct\n"
                "- reason: brief justification (one sentence)\n"
                "- corrected_x / corrected_y: if approved=false and you can see the correct target, "
                "provide corrected coords in [0,1000]; otherwise null"
            )
            try:
                result = await _call_structured(
                    nova_client,
                    NOVA_LITE_MODEL_ID,
                    [
                        {"role": "system", "content": "Output JSON only."},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{annotated_b64}"},
                                },
                            ],
                        },
                    ],
                    CriticVerdict,
                )
                logger.info(
                    f"Critic: approved={result.approved} | {result.reason[:80]}"
                )
                return result
            except Exception as exc:
                logger.error(f"Critic sub-agent failed, approving by default: {exc}")
                return CriticVerdict(approved=True, reason="Critic unavailable.")

        # ------------------------------------------------------------------
        # Summarization chain
        # When conversation_history exceeds the threshold, older exchanges are
        # compressed by Nova Lite into a single context block.  This preserves
        # the overarching goal across long multi-step tasks while keeping the
        # context window lean.
        # ------------------------------------------------------------------

        async def _compress_history(
            history: list[dict],
            task: str,
        ) -> list[dict]:
            if len(history) <= HISTORY_COMPRESS_THRESHOLD:
                return history

            system_msgs = [m for m in history if m["role"] == "system"]
            non_system = [m for m in history if m["role"] != "system"]

            keep_count = HISTORY_RECENT_KEEP_PAIRS * 2
            to_summarize = non_system[:-keep_count] if len(non_system) > keep_count else []
            recent = non_system[-keep_count:]

            if not to_summarize:
                return history

            # Build text-only digest (drop image parts to keep prompt small)
            lines: list[str] = []
            for msg in to_summarize:
                role = msg["role"].upper()
                content = msg["content"]
                if isinstance(content, list):
                    text_parts = [
                        p["text"]
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    content = " ".join(text_parts)
                lines.append(f"{role}: {str(content)[:400]}")

            prompt = (
                f"Summarize the following agent steps as a compact context block.\n"
                f"Overarching task: '{task}'\n"
                f"Preserve: what was tried, what worked or failed, and the current progress.\n\n"
                + "\n".join(lines)
            )
            try:
                result = await _call_structured(
                    nova_client,
                    NOVA_LITE_MODEL_ID,
                    [
                        {"role": "system", "content": "Output JSON with a single 'summary' key."},
                        {"role": "user", "content": prompt},
                    ],
                    ConversationSummary,
                )
                summary_msg = {
                    "role": "user",
                    "content": f"[COMPRESSED HISTORY — {len(to_summarize)} steps]\n{result.summary}",
                }
                logger.info(
                    f"History compressed: {len(to_summarize)} messages → 1 summary block"
                )
                return system_msgs + [summary_msg] + recent
            except Exception as exc:
                logger.error(f"Summarization failed, falling back to naive trim: {exc}")
                return system_msgs + non_system[:3] + recent

        # ------------------------------------------------------------------
        # Coordinate safety guardrail
        # ------------------------------------------------------------------

        def _coords_valid(action: AgentAction) -> bool:
            if action.command != "click":
                return True
            return (
                action.x is not None
                and action.y is not None
                and 0 <= action.x <= 1000
                and 0 <= action.y <= 1000
            )

        # ------------------------------------------------------------------
        # Intent interpretation
        # ------------------------------------------------------------------

        async def interpret_intent(raw: str) -> str:
            prompt = (
                f"You are a voice command interpreter for Sai, a macOS desktop assistant.\n"
                f"SCREEN CONTEXT: {_app_ctx_summary()}\n"
                f'RAW TRANSCRIPTION: "{raw}"\n\n'
                "Reconstruct the user's ACTUAL INTENDED COMMAND from the (possibly garbled) transcription.\n"
                "Common STT errors: 'they\\'re not'→'turn off', 'clothes'→'close', 'right'→'write'.\n"
                "Output ONLY JSON: {\"corrected_command\": \"...\"}"
            )
            try:
                result = await _call_structured(
                    nova_client,
                    NOVA_LITE_MODEL_ID,
                    [
                        {"role": "system", "content": "Output JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    IntentResult,
                )
                corrected = result.corrected_command.strip().strip('"').strip("'")
                if len(corrected) > 2:
                    logger.info(f"Intent: '{raw}' → '{corrected}'")
                    return corrected
            except Exception as exc:
                logger.error(f"Intent interpretation failed, using raw: {exc}")
            return raw

        # ------------------------------------------------------------------
        # Hybrid router
        # ------------------------------------------------------------------

        async def hybrid_reasoning(user_text: str) -> typing.Optional[dict]:
            ctx = _app_ctx_summary()

            routing_prompt = (
                f"You are the Task Router for Sai, a macOS desktop assistant.\n"
                f"SCREEN CONTEXT: {ctx}\n\n"
                "SIMPLE = single fire-and-forget action that does NOT need to see the screen:\n"
                "  - Launch an app via Spotlight\n"
                "  - Open a brand-new URL the user is NOT already on\n"
                "  - A single global hotkey\n\n"
                "ADVANCED = anything requiring screen interaction, multi-step UI navigation,\n"
                "or any task relating to the app/site currently open.\n\n"
                f'User said: "{user_text}"\n\n'
                'Output JSON: {"complexity": "SIMPLE" | "ADVANCED", "reason": "one sentence"}'
            )

            complexity = "ADVANCED"
            try:
                decision = await _call_structured(
                    nova_client,
                    NOVA_LITE_MODEL_ID,
                    [
                        {"role": "system", "content": "Output JSON only."},
                        {"role": "user", "content": routing_prompt},
                    ],
                    RoutingDecision,
                )
                complexity = decision.complexity
                logger.info(f"Routing: {complexity} | {decision.reason}")
            except Exception as exc:
                logger.error(f"Routing failed, defaulting to ADVANCED: {exc}")

            if complexity == "SIMPLE":
                simple_prompt = (
                    "Convert the user's request to a SINGLE tool-call JSON.\n"
                    "AVAILABLE COMMANDS:\n"
                    '  {"command": "type_text", "text": "AppName"}  — Spotlight app launch only\n'
                    '  {"command": "open_url", "url": "https://..."}  — open a URL\n'
                    '  {"command": "press_hotkey", "keys": ["command", "n"]}  — keyboard shortcut\n'
                    '  {"command": "escalate"}  — if on-screen interaction is required\n\n'
                    f'User request: "{user_text}"\n'
                    "Output JSON only."
                )
                try:
                    action = await _call_structured(
                        nova_client,
                        NOVA_LITE_MODEL_ID,
                        [
                            {"role": "system", "content": "Output JSON only."},
                            {"role": "user", "content": simple_prompt},
                        ],
                        SimpleAction,
                    )
                    if action.command != "escalate":
                        logger.info(f"SIMPLE action: {action.model_dump(exclude_none=True)}")
                        return action.model_dump(exclude_none=True)
                    logger.info("SIMPLE handler escalated to ADVANCED.")
                except Exception as exc:
                    logger.error(f"SIMPLE generation failed, escalating: {exc}")

            # ADVANCED — start the swarm agent loop
            if (
                state["active_agent_task"]
                and not state["active_agent_task"].done()
            ):
                state["active_agent_task"].cancel()
            state["active_agent_task"] = asyncio.create_task(
                run_agent_loop(user_text)
            )
            return None

        # ------------------------------------------------------------------
        # Senior Brain — main agent loop with swarm integration
        # ------------------------------------------------------------------

        async def run_agent_loop(user_text: str) -> None:  # noqa: C901
            logger.info(f"Agent loop starting: {user_text}")
            ctx = _app_ctx_summary()

            # Step 0: Memory sub-agent injects relevant past context
            memory = await memory_sub_agent(user_text)
            memory_block = ""
            if memory.relevant_facts or memory.session_summary:
                parts: list[str] = []
                if memory.session_summary:
                    parts.append(f"Past context: {memory.session_summary}")
                if memory.relevant_facts:
                    parts.append("Relevant facts: " + "; ".join(memory.relevant_facts))
                memory_block = "\n\nMEMORY:\n" + "\n".join(parts)

            SYSTEM_PROMPT = f"""You are the Senior Vision Specialist for Sai, a macOS desktop agent.

SCREEN CONTEXT: {ctx}{memory_block}

STRATEGIC APPROACH:
1. On your FIRST step, analyze the screenshot and form a numbered PLAN in the "explanation" field.
2. Every subsequent "explanation" must say WHY the action advances the plan — not just what you clicked.
3. If an action doesn't clearly move toward the goal, do NOT take it.

RULES:
- If the relevant app/website is ALREADY open, work within it. Never open Spotlight or navigate away unless necessary.
- You can READ all text visible in the screenshot. Do NOT click elements just to read their content.
- Use keyboard_type for ALL on-screen text entry. type_text is ONLY for Spotlight app launching.
- NEVER use paste hotkeys (Command+V / Cmd+V).
- ONE action per step. After each action, verify the result in the next screenshot.
- Set done=true only when the screenshot CONFIRMS the task is fully complete.

COORDINATE SYSTEM: Normalized [0, 1000] x [0, 1000]. (0,0)=top-left, (1000,1000)=bottom-right.
The ruler tick marks on the screenshot edges are your spatial reference.

Output ONLY valid JSON:
{{"explanation":"...","command":"click|type_text|keyboard_type|open_url|press_hotkey|scroll|wait","x":0-1000,"y":0-1000,"text":"...","url":"...","keys":[...],"amount":0,"done":false}}"""

            conversation_history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
            last_action: typing.Optional[dict] = None
            recent_actions: list[str] = []
            task_outcome = "incomplete"

            def _sig(a: AgentAction) -> str:
                if a.command == "click":
                    return f"click({a.x},{a.y})"
                if a.command in ("keyboard_type", "type_text"):
                    return f"{a.command}({(a.text or '')[:30]})"
                if a.command == "scroll":
                    return f"scroll({a.amount})"
                if a.command == "press_hotkey":
                    return f"hotkey({a.keys})"
                if a.command == "open_url":
                    return f"url({(a.url or '')[:40]})"
                return a.command

            def _detect_cycle(actions: list[str]) -> typing.Optional[int]:
                n = len(actions)
                if n < 4:
                    return None
                for length in range(1, min(n // 2 + 1, 7)):
                    if actions[-length:] == actions[-length * 2 : -length]:
                        return length
                return None

            def _parse_action(raw: str) -> AgentAction:
                """Extract JSON from raw model output and validate with Pydantic."""
                clean = raw.strip()
                if clean.startswith("```"):
                    clean = "\n".join(
                        line for line in clean.splitlines()
                        if not line.startswith("```")
                    ).strip()
                if not clean.startswith("{"):
                    start, end = clean.find("{"), clean.rfind("}")
                    if start != -1 and end != -1:
                        clean = clean[start : end + 1]
                return AgentAction.model_validate_json(clean)

            try:
                # Wait for initial screenshot
                if not state["latest_screenshot_b64"]:
                    try:
                        await asyncio.wait_for(
                            state["screenshot_event"].wait(), timeout=5.0
                        )
                    except asyncio.TimeoutError:
                        logger.error("Initial screenshot timed out.")
                        return

                for step in range(MAX_AGENT_STEPS):
                    screenshot = state["latest_screenshot_b64"]
                    if not screenshot:
                        logger.error("No screenshot available at step start.")
                        break

                    logger.info(f"Step {step + 1}/{MAX_AGENT_STEPS}")
                    annotated = annotate_screenshot(screenshot, last_action)

                    # Build user message for this step
                    if step == 0:
                        user_msg = (
                            f"Task: {user_text}\n"
                            "Analyze the screenshot carefully. In 'explanation', describe your HIGH-LEVEL PLAN "
                            "(numbered steps), then take the FIRST action.\n"
                            "Remember: you can READ all visible text — do NOT click just to read."
                        )
                    else:
                        user_msg = (
                            f"Task (reminder): {user_text}\n"
                            "Evaluate whether your last action succeeded, then take the next step."
                        )

                    # Stuck detection
                    cycle = _detect_cycle(recent_actions)
                    if cycle is not None:
                        user_msg += (
                            f"\n\nCRITICAL: You are stuck in a loop of {cycle} repeating actions: "
                            f"{recent_actions[-cycle:]}. "
                            "MUST completely change approach. Ask: What is the ACTUAL GOAL? "
                            "Should I be TYPING instead of clicking? Take a fundamentally different action NOW."
                        )
                    elif len(recent_actions) >= 2 and len(set(recent_actions[-2:])) == 1:
                        user_msg += (
                            f"\n\nWARNING: Same action repeated twice ({recent_actions[-1]}). "
                            "It is not working. Try a completely different approach."
                        )

                    conversation_history.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_msg},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{annotated}"},
                            },
                        ],
                    })

                    # Compress history to prevent context overflow
                    conversation_history = await _compress_history(
                        conversation_history, user_text
                    )

                    # --- Senior Brain call (blocking sync → executor) ---
                    loop = asyncio.get_event_loop()
                    response = await loop.run_in_executor(
                        None,
                        lambda: nova_pro_client.chat.completions.create(
                            model=NOVA_PRO_MODEL_ID,
                            messages=conversation_history,
                            temperature=0.2,
                        ),
                    )
                    raw_content: str = response.choices[0].message.content
                    logger.info(f"Senior Brain: {raw_content[:200]}...")
                    conversation_history.append(
                        {"role": "assistant", "content": raw_content}
                    )

                    # --- Parse into Pydantic model ---
                    try:
                        action = _parse_action(raw_content)
                    except Exception as exc:
                        logger.error(f"AgentAction parse failed: {exc}")
                        conversation_history.append({
                            "role": "user",
                            "content": (
                                f"Your response was not valid JSON. Error: {exc}. "
                                "Output ONLY a valid JSON object matching the schema. No text outside the JSON."
                            ),
                        })
                        continue

                    # --- Coordinate guardrail ---
                    if not _coords_valid(action):
                        logger.warning(
                            f"Coordinate out of bounds: ({action.x}, {action.y}). Injecting correction."
                        )
                        conversation_history.append({
                            "role": "user",
                            "content": (
                                f"GUARDRAIL: click at ({action.x}, {action.y}) is outside [0, 1000]. "
                                "All coordinates MUST be in range [0, 1000]. "
                                "Re-examine the screenshot and output a corrected action."
                            ),
                        })
                        continue

                    # --- Critic sub-agent (click verification) ---
                    if action.command == "click":
                        verdict = await critic_sub_agent(action, annotated)
                        if not verdict.approved:
                            logger.warning(
                                f"Critic rejected click ({action.x},{action.y}): {verdict.reason}"
                            )
                            if (
                                verdict.corrected_x is not None
                                and verdict.corrected_y is not None
                            ):
                                action = action.model_copy(
                                    update={
                                        "x": verdict.corrected_x,
                                        "y": verdict.corrected_y,
                                    }
                                )
                                logger.info(
                                    f"Critic corrected click → ({action.x},{action.y})"
                                )
                            else:
                                conversation_history.append({
                                    "role": "user",
                                    "content": (
                                        f"CRITIC: The planned click at ({action.x},{action.y}) does not land "
                                        f"on an interactive element. Reason: {verdict.reason}. "
                                        "Re-examine the screenshot and choose a different target."
                                    ),
                                })
                                continue

                    # --- Guard: never mark done on the same step as an action ---
                    if action.done:
                        logger.info("Agent signaled done=true.")
                        task_outcome = "complete"
                        break

                    # --- Execute command ---
                    cmd = action.command
                    if cmd in {
                        "open_url", "click", "type_text", "keyboard_type",
                        "press_hotkey", "scroll", "wait",
                    }:
                        payload = action.model_dump(exclude_none=True)
                        last_action = payload
                        recent_actions.append(_sig(action))
                        logger.info(f"Executing: {cmd}")
                        await websocket.send_text(json.dumps(payload))
                        await asyncio.sleep(ACTION_SETTLE_TIME)

                        # Hard cycle bail (3 full repetitions after warnings)
                        if len(recent_actions) >= 6:
                            cyc = _detect_cycle(recent_actions)
                            if cyc is not None:
                                total = cyc * 3
                                if len(recent_actions) >= total:
                                    tail = recent_actions[-total:]
                                    chunks = [
                                        tuple(tail[i * cyc : (i + 1) * cyc])
                                        for i in range(3)
                                    ]
                                    if len(set(chunks)) == 1:
                                        logger.error(
                                            f"Hopelessly stuck in cycle of {cyc}, aborting."
                                        )
                                        break

                    # --- Request next screenshot ---
                    state["screenshot_event"].clear()
                    await websocket.send_text(json.dumps({"command": "capture_screen"}))
                    try:
                        await asyncio.wait_for(
                            state["screenshot_event"].wait(),
                            timeout=SCREENSHOT_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        logger.error("Screenshot timed out in agent loop.")
                        break

            except asyncio.CancelledError:
                logger.info("Agent loop cancelled.")
            except Exception as exc:
                import traceback
                logger.error(f"Agent loop error: {type(exc).__name__}: {exc}")
                logger.error(traceback.format_exc())
            finally:
                # Persist session outcome to memory DB
                try:
                    summary = (
                        f"Task: {user_text} | Steps: {len(recent_actions)} | "
                        f"Outcome: {task_outcome} | Last: {recent_actions[-3:]}"
                    )
                    store_session(user_text, task_outcome, summary)
                    logger.info(f"Session stored: {task_outcome}")
                except Exception as exc:
                    logger.warning(f"Failed to store session: {exc}")

        # ------------------------------------------------------------------
        # Transcription processing
        # ------------------------------------------------------------------

        async def process_complete_transcription() -> None:
            if state["command_triggered"]:
                return
            full_text = " ".join(state["transcription_buffer"]).strip()
            state["transcription_buffer"] = []
            if not full_text:
                return

            state["command_triggered"] = True
            logger.info(f"Raw transcription: {full_text}")

            if state["debounce_task"]:
                state["debounce_task"].cancel()

            try:
                await websocket.send_text(
                    json.dumps({"command": "set_activity", "state": "active"})
                )
            except Exception as exc:
                logger.warning(f"Failed to send activity start: {exc}")

            try:
                interpreted = await interpret_intent(full_text)
                logger.info(f"Processing: {interpreted}")
                action = await hybrid_reasoning(interpreted)
                if action:
                    await websocket.send_text(json.dumps(action))
                if state["active_agent_task"]:
                    try:
                        await state["active_agent_task"]
                    except asyncio.CancelledError:
                        pass
            finally:
                try:
                    await websocket.send_text(
                        json.dumps({"command": "set_activity", "state": "idle"})
                    )
                except Exception as exc:
                    logger.warning(f"Failed to send activity stop: {exc}")
                logger.info("Command complete. Closing session.")
                await websocket.close()

        # ------------------------------------------------------------------
        # ElevenLabs STT connection
        # ------------------------------------------------------------------

        el_url = (
            ELEVENLABS_STT_WS_URL
            + "?model_id=scribe_v2_realtime"
            + "&language_code=en"
            + "&audio_format=pcm_16000"
            + "&commit_strategy=vad"
            + "&vad_silence_threshold_secs=1.2"
        )

        async with ws_client.connect(
            el_url, additional_headers={"xi-api-key": ELEVENLABS_API_KEY}
        ) as el_ws:
            logger.info("Connected to ElevenLabs Scribe v2 Realtime STT")

            async def listen_elevenlabs() -> None:
                try:
                    async for msg in el_ws:
                        if state["command_triggered"]:
                            continue
                        try:
                            data = json.loads(msg)
                            mtype = data.get("message_type", "")
                            if mtype == "session_started":
                                logger.info(f"ElevenLabs session: {data.get('session_id')}")
                            elif mtype == "partial_transcript":
                                if text := data.get("text", "").strip():
                                    logger.info(f"Partial: {text}")
                            elif mtype in (
                                "committed_transcript",
                                "committed_transcript_with_timestamps",
                            ):
                                if text := data.get("text", "").strip():
                                    logger.info(f"Committed: {text}")
                                    state["transcription_buffer"].append(text)
                                    if state["debounce_task"]:
                                        state["debounce_task"].cancel()
                                    await process_complete_transcription()
                            elif mtype in (
                                "error", "auth_error", "quota_exceeded",
                                "rate_limited", "transcriber_error",
                            ):
                                logger.error(f"ElevenLabs error: {data}")
                        except json.JSONDecodeError:
                            logger.warning(f"Non-JSON from ElevenLabs: {str(msg)[:100]}")
                except Exception as exc:
                    logger.error(f"ElevenLabs listener error: {exc}")

            el_listen_task = asyncio.create_task(listen_elevenlabs())

            try:
                while True:
                    try:
                        message = await websocket.receive()
                    except RuntimeError:
                        break

                    if message.get("bytes"):
                        if not state["command_triggered"]:
                            pcm = message["bytes"]
                            chunk = json.dumps({
                                "message_type": "input_audio_chunk",
                                "audio_base_64": base64.b64encode(pcm).decode("utf-8"),
                                "commit": False,
                                "sample_rate": 16000,
                            })
                            try:
                                await el_ws.send(chunk)
                            except Exception as exc:
                                logger.error(f"Failed to forward audio: {exc}")

                    elif message.get("text"):
                        data = json.loads(message["text"])
                        if data.get("event") == "screen_captured":
                            state["latest_screenshot_b64"] = data.get("image_base64")
                            state["latest_app_context"] = data.get("app_context", {})
                            # Record physical screen dimensions on first capture
                            if state["screen_width"] is None:
                                state["screen_width"] = data.get("width")
                                state["screen_height"] = data.get("height")
                                logger.info(
                                    f"Screen dimensions: {state['screen_width']}x{state['screen_height']}"
                                )
                            state["screenshot_event"].set()

            except WebSocketDisconnect:
                logger.info("Client disconnected")
            finally:
                el_listen_task.cancel()

    except Exception as exc:
        import traceback
        logger.error(f"WebSocket handler error: {exc}")
        logger.error(traceback.format_exc())
    finally:
        logger.info("Session ended")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
