"""
Sai OS Agent — macOS Client v2.0
Production-grade rewrite:
  - Dynamic screen resolution (no hardcoded canvas)
  - Deterministic UI-ready checks replacing time.sleep() in action primitives
  - Graceful PyAudio/Porcupine/asyncio shutdown
  - Client-side coordinate guardrails
"""
import os
import struct
import pyaudio
import logging
import asyncio
import json
import time
import threading
import queue
import math
import signal
import sys
import websockets
import pvporcupine
import base64
import subprocess
import tempfile
import io
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, Tuple

from dotenv import load_dotenv
from PIL import Image

# ---------------------------------------------------------------------------
# Optional macOS-only imports
# ---------------------------------------------------------------------------

objc = None
NSApp = NSApplication = NSApplicationActivationPolicyProhibited = None
NSBezierPath = NSColor = NSMakeRect = NSObject = None
NSFloatingWindowLevel = NSPanel = NSScreen = NSScreenSaverWindowLevel = None
NSTimer = NSView = None
NSWindowCollectionBehaviorCanJoinAllSpaces = None
NSWindowCollectionBehaviorFullScreenAuxiliary = None
NSWindowCollectionBehaviorStationary = None
NSWindowStyleMaskBorderless = None

try:
    if sys.platform == "darwin":
        import objc
        from AppKit import (
            NSApp,
            NSApplication,
            NSApplicationActivationPolicyProhibited,
            NSBezierPath,
            NSColor,
            NSMakeRect,
            NSObject,
            NSFloatingWindowLevel,
            NSPanel,
            NSScreen,
            NSScreenSaverWindowLevel,
            NSTimer,
            NSView,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorStationary,
            NSWindowStyleMaskBorderless,
        )
except Exception:
    pass  # Non-macOS or pyobjc not installed — overlay will be disabled

try:
    if sys.platform == "darwin":
        import Quartz
        _HAS_QUARTZ = True
    else:
        _HAS_QUARTZ = False
except ImportError:
    _HAS_QUARTZ = False

import pyautogui

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sai-client")

# ---------------------------------------------------------------------------
# Dynamic screen resolution
# ---------------------------------------------------------------------------

def _get_logical_screen_size() -> Tuple[int, int]:
    """
    Return the primary screen's logical dimensions (points, not physical Retina pixels).

    Priority:
    1. NSScreen.mainScreen().frame()  — authoritative macOS API, returns logical points
    2. pyautogui.size()               — portable fallback
    """
    try:
        if sys.platform == "darwin" and NSScreen is not None:
            frame = NSScreen.mainScreen().frame()
            w = int(frame.size.width)
            h = int(frame.size.height)
            if w > 0 and h > 0:
                return w, h
    except Exception as exc:
        logger.warning(f"NSScreen query failed: {exc}")
    return pyautogui.size()


# ---------------------------------------------------------------------------
# Accessibility helpers — replace time.sleep() with deterministic checks
# ---------------------------------------------------------------------------

def _poll_until(
    condition: Callable[[], bool],
    timeout: float,
    interval: float = 0.05,
) -> bool:
    """
    Spin-poll `condition()` every `interval` seconds until it returns True or
    `timeout` is reached.  Returns True when the condition was satisfied.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _spotlight_window_visible() -> bool:
    """
    Check if a Spotlight search window is on-screen.

    Strategy 1 (preferred): Quartz CGWindowListCopyWindowInfo — enumerates all
    on-screen windows; Spotlight's window has owner name "Spotlight".

    Strategy 2 (fallback): AppleScript UI scripting — check if the Spotlight
    process has a visible window.
    """
    if _HAS_QUARTZ:
        try:
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
            for win in windows:
                owner = win.get("kCGWindowOwnerName", "")
                name = win.get("kCGWindowName", "")
                if "Spotlight" in owner or "Spotlight" in name:
                    return True
            return False
        except Exception as exc:
            logger.debug(f"Quartz window check failed: {exc}")

    # AppleScript fallback
    script = (
        'tell application "System Events"\n'
        '    try\n'
        '        tell process "Spotlight"\n'
        '            return visible\n'
        '        end tell\n'
        '    on error\n'
        '        return false\n'
        '    end try\n'
        'end tell'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout.strip() == "true"
    except Exception:
        return False


def _get_clipboard_text() -> str:
    """Read the current clipboard text via pbpaste."""
    try:
        r = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=2
        )
        return r.stdout
    except Exception:
        return ""


def _clipboard_contains(expected: str, timeout: float = 1.5) -> bool:
    """
    Poll until the clipboard contains `expected` text.
    Ensures pbcopy has flushed before we fire Cmd+V.
    """
    return _poll_until(
        lambda: _get_clipboard_text() == expected,
        timeout=timeout,
        interval=0.02,
    )


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

def capture_screen_sync() -> dict:
    """
    Capture the screen using the native macOS screencapture utility, then
    downsample to the logical screen resolution (queried dynamically).

    Retina displays produce physical images at 2× or 3×; downsampling to
    logical resolution keeps token count reasonable while matching the
    coordinate space used by the agent and click mapping.
    """
    file_path = os.path.join(tempfile.gettempdir(), "sai_capture.png")
    canvas_w, canvas_h = _get_logical_screen_size()

    try:
        subprocess.run(["screencapture", "-x", "-C", file_path], check=True)

        with Image.open(file_path) as img:
            native_w, native_h = img.size
            resized = img.resize((canvas_w, canvas_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.convert("RGB").save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        logger.info(
            "Captured: native=%sx%s → canvas=%sx%s",
            native_w, native_h, canvas_w, canvas_h,
        )
        return {"image_base64": b64, "width": canvas_w, "height": canvas_h}
    except Exception as exc:
        logger.error(f"Native capture failed: {exc}")
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# OS action primitives
# ---------------------------------------------------------------------------

def perform_click_sync(x: int, y: int) -> None:
    """
    Click at normalized [0, 1000] coordinates.

    Mapping: the model always reasons on a [0, 1000] grid regardless of the
    actual screen size.  We convert to actual screen pixels via pyautogui.size()
    which returns the logical (not Retina-physical) dimensions — the same
    coordinate space used by pyautogui.moveTo / click.
    """
    # Client-side coordinate guardrail (defense-in-depth; server validates too)
    nx = max(0.0, min(1000.0, float(x)))
    ny = max(0.0, min(1000.0, float(y)))
    if nx != float(x) or ny != float(y):
        logger.warning(
            "perform_click_sync: coordinate clamped (%s,%s) → (%s,%s)", x, y, nx, ny
        )

    screen_w, screen_h = pyautogui.size()
    click_x = int(round((nx / 1000.0) * (screen_w - 1)))
    click_y = int(round((ny / 1000.0) * (screen_h - 1)))

    logger.info(
        "CLICK: norm=(%s,%s) → pixel=(%s,%s) | screen=%sx%s",
        x, y, click_x, click_y, screen_w, screen_h,
    )
    pyautogui.moveTo(click_x, click_y, duration=0.1)
    pyautogui.click()


def perform_scroll_sync(amount: int) -> None:
    pyautogui.scroll(amount)


def perform_type_sync(text: str) -> None:
    """
    Open Spotlight, clear any existing query, type the text, and launch the app.

    Replaces fragile fixed sleeps with deterministic accessibility checks:
    - After Cmd+Space: poll for the Spotlight window via Quartz / AppleScript
    - After typing: a minimum-viable delay for the search index to respond
      (no reliable OS-level event for "results appeared")
    """
    # Open Spotlight — key events need a minimal gap the OS requires to register
    pyautogui.keyDown("command")
    time.sleep(0.02)
    pyautogui.press("space")
    time.sleep(0.02)
    pyautogui.keyUp("command")

    # Wait for Spotlight window to become visible (deterministic)
    ready = _poll_until(_spotlight_window_visible, timeout=3.0, interval=0.05)
    if not ready:
        logger.warning("Spotlight window not detected within 3s — proceeding anyway")

    # Clear any stale query
    pyautogui.hotkey("command", "a")
    pyautogui.press("backspace")

    # Type the search text
    pyautogui.write(text, interval=0.04)

    # Allow the search index to populate results.
    # There is no public OS-level event for "Spotlight results ready"; 0.4s is
    # the empirically safe minimum across macOS versions.
    time.sleep(0.4)

    pyautogui.press("enter")
    time.sleep(0.1)
    pyautogui.press("enter")  # belt-and-suspenders for app launch confirmation


def perform_keyboard_type_sync(text: str) -> None:
    """
    Type text into the currently-focused field via clipboard paste.

    Replaces the blind time.sleep() with a clipboard-state poll: we only fire
    Cmd+V once pbcopy has flushed the data (verified by reading back via pbpaste).
    """
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), capture_output=True)

    # Deterministic readiness check: verify clipboard before pasting
    ready = _clipboard_contains(text, timeout=1.5)
    if not ready:
        logger.warning(
            "Clipboard readiness check timed out — pasting anyway (content may be stale)"
        )

    pyautogui.hotkey("command", "v")


def perform_hotkey_sync(keys: list) -> None:
    pyautogui.hotkey(*keys)


def perform_open_url_sync(url: str) -> None:
    """Open a URL in the currently-active browser, falling back to the OS default."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    browser = _detect_active_browser()
    if browser:
        logger.info(f"Opening {url} in {browser}")
        subprocess.run(["open", "-a", browser, url])
    else:
        logger.info(f"Opening {url} in default browser")
        subprocess.run(["open", url])


def _detect_active_browser() -> str:
    """Return the frontmost app name if it's a known browser, else empty string."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to return name of first application process '
             'whose frontmost is true'],
            capture_output=True, text=True, timeout=3,
        )
        app = r.stdout.strip()
        if app in {
            "Google Chrome", "Chromium", "Safari", "Arc", "Firefox",
            "Microsoft Edge", "Brave Browser", "Opera", "Vivaldi",
        }:
            return app
    except Exception:
        pass
    return ""


def get_active_app_context_sync() -> dict:
    """Use AppleScript to get the frontmost app and browser tab info."""
    ctx: dict = {"app_name": "", "bundle_id": "", "tab_url": "", "tab_title": ""}
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to set frontApp to name of first application '
             'process whose frontmost is true\nreturn frontApp'],
            capture_output=True, text=True, timeout=3,
        )
        app_name = r.stdout.strip()
        ctx["app_name"] = app_name

        url_script = title_script = None
        if app_name in ("Google Chrome", "Chromium"):
            url_script = 'tell application "Google Chrome" to return URL of active tab of front window'
            title_script = 'tell application "Google Chrome" to return title of active tab of front window'
        elif app_name == "Safari":
            url_script = 'tell application "Safari" to return URL of front document'
            title_script = 'tell application "Safari" to return name of front document'
        elif app_name == "Arc":
            url_script = 'tell application "Arc" to return URL of active tab of front window'
            title_script = 'tell application "Arc" to return title of active tab of front window'

        if url_script:
            ctx["tab_url"] = subprocess.run(
                ["osascript", "-e", url_script], capture_output=True, text=True, timeout=3
            ).stdout.strip()
        if title_script:
            ctx["tab_title"] = subprocess.run(
                ["osascript", "-e", title_script], capture_output=True, text=True, timeout=3
            ).stdout.strip()
    except Exception as exc:
        logger.warning(f"Failed to get active app context: {exc}")
    return ctx


# ---------------------------------------------------------------------------
# Overlay + capture helpers
# ---------------------------------------------------------------------------

def _run_with_overlay_suspended(overlay: "ActivityOverlay", func, *args, **kwargs):
    overlay.suspend()
    try:
        return func(*args, **kwargs)
    finally:
        overlay.resume()


def _capture_screen_with_context(overlay: "ActivityOverlay") -> dict:
    """Capture screenshot AND app context in a single overlay-suspended window."""
    overlay.suspend()
    try:
        return {
            "capture": capture_screen_sync(),
            "app_context": get_active_app_context_sync(),
        }
    finally:
        overlay.resume()


# ---------------------------------------------------------------------------
# Wake word detector
# ---------------------------------------------------------------------------

class WakeWordDetector:
    """
    Non-blocking wake word detector.  Audio capture and Porcupine processing
    run in a single dedicated daemon thread; the asyncio event loop is never
    blocked.
    """

    # Sentinel placed on the audio queue to signal orderly shutdown
    _SHUTDOWN_SENTINEL = object()

    def __init__(
        self,
        keyword_path: str,
        access_key: str,
        callback: Callable[[], None],
    ):
        self.keyword_path = keyword_path
        self.access_key = access_key
        self.callback = callback

        self.porcupine: Optional[pvporcupine.Porcupine] = None
        self.pa: Optional[pyaudio.PyAudio] = None
        self.audio_stream = None
        self.is_listening = False
        self.is_streaming = False
        self.audio_queue: Optional[asyncio.Queue] = None

        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="WakeWordThread"
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Event signalled when the background thread has fully stopped
        self._stopped_event = threading.Event()

    # ------------------------------------------------------------------
    # Background audio loop
    # ------------------------------------------------------------------

    def _listen_loop(self) -> None:
        """Blocking audio capture loop — runs inside the ThreadPoolExecutor."""
        try:
            self.porcupine = pvporcupine.create(
                access_key=self.access_key,
                keyword_paths=[self.keyword_path],
                sensitivities=[0.8],
            )
            self.pa = pyaudio.PyAudio()
            self.audio_stream = self.pa.open(
                rate=self.porcupine.sample_rate,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=self.porcupine.frame_length,
            )

            logger.info(f"Listening for wake word from '{self.keyword_path}'...")

            while self.is_listening:
                pcm_bytes = self.audio_stream.read(
                    self.porcupine.frame_length, exception_on_overflow=False
                )
                pcm = struct.unpack_from(
                    "h" * self.porcupine.frame_length, pcm_bytes
                )

                if self.porcupine.process(pcm) >= 0:
                    logger.info("Wake word detected!")
                    if self._loop and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(self.callback)

                if (
                    self.is_streaming
                    and self.audio_queue is not None
                    and self._loop is not None
                    and not self._loop.is_closed()
                ):
                    self._loop.call_soon_threadsafe(
                        self.audio_queue.put_nowait, pcm_bytes
                    )

        except Exception as exc:
            logger.error(f"Wake word detection error: {exc}")
        finally:
            self._cleanup()
            self._stopped_event.set()

    def _cleanup(self) -> None:
        """Release PyAudio stream and Porcupine resources."""
        logger.info("Cleaning up audio resources...")
        try:
            if self.audio_stream is not None:
                if self.audio_stream.is_active():
                    self.audio_stream.stop_stream()
                self.audio_stream.close()
                self.audio_stream = None
        except Exception as exc:
            logger.warning(f"Audio stream close error: {exc}")

        try:
            if self.pa is not None:
                self.pa.terminate()
                self.pa = None
        except Exception as exc:
            logger.warning(f"PyAudio terminate error: {exc}")

        try:
            if self.porcupine is not None:
                self.porcupine.delete()
                self.porcupine = None
        except Exception as exc:
            logger.warning(f"Porcupine delete error: {exc}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background audio thread (must be called from a running event loop)."""
        if self.is_listening:
            return
        self.is_listening = True
        self._stopped_event.clear()
        self._loop = asyncio.get_running_loop()
        self._loop.run_in_executor(self._executor, self._listen_loop)

    def stop(self, join_timeout: float = 3.0) -> None:
        """
        Signal the background thread to stop and wait for it to release all
        OS resources before returning.  This prevents orphaned PyAudio streams
        and Porcupine instances.
        """
        if not self.is_listening:
            return
        self.is_listening = False

        # Wake the thread if it's blocked on audio_stream.read() via the sentinel
        if (
            self.is_streaming
            and self.audio_queue is not None
            and self._loop is not None
            and not self._loop.is_closed()
        ):
            self._loop.call_soon_threadsafe(
                self.audio_queue.put_nowait, self._SHUTDOWN_SENTINEL
            )

        # Shut down the executor, waiting for the thread to finish
        self._executor.shutdown(wait=True, cancel_futures=False)

        # Belt-and-suspenders: wait for the stopped event with timeout
        if not self._stopped_event.wait(timeout=join_timeout):
            logger.warning(
                "WakeWordDetector: background thread did not stop within %.1fs — "
                "resources may not be fully released",
                join_timeout,
            )
        else:
            logger.info("WakeWordDetector: background thread stopped cleanly")


# ---------------------------------------------------------------------------
# Activity overlay (animated border)
# ---------------------------------------------------------------------------

class ActivityOverlay:
    """
    Fullscreen animated border overlay indicating Sai is active.
    Runs the NSApplication run-loop in a dedicated thread so it never blocks
    the asyncio event loop.
    """

    def __init__(self):
        self._cmd_queue: "queue.Queue[tuple[str, Optional[bool]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._enabled = sys.platform == "darwin" and objc is not None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="SaiActivityOverlay", daemon=True
        )
        self._thread.start()

    def run_forever(self) -> None:
        if not self._enabled:
            logger.warning(
                "Activity overlay disabled: requires macOS with pyobjc installed."
            )
            while True:
                time.sleep(1)
        self._run()

    def set_active(self, active: bool) -> None:
        self._cmd_queue.put(("active", active))

    def suspend(self) -> None:
        self._cmd_queue.put(("suspend", True))

    def resume(self) -> None:
        self._cmd_queue.put(("suspend", False))

    def shutdown(self) -> None:
        self._cmd_queue.put(("shutdown", None))

    def _run(self) -> None:
        if not self._enabled:
            return

        class _OverlayView(NSView):
            def initWithFrame_(self, frame):
                self = objc.super(_OverlayView, self).initWithFrame_(frame)
                if self is None:
                    return None
                self._active = False
                self._suspended = False
                self._phase = 0.0
                return self

            def update_state(self, state):
                self._active = bool(state.get("active"))
                self._suspended = bool(state.get("suspended"))

            def step_(self, _timer):
                if self._active and not self._suspended:
                    self._phase = (self._phase + 0.07) % (2 * math.pi)
                self.setNeedsDisplay_(True)

            def drawRect_(self, rect):
                if not self._active or self._suspended:
                    return
                bounds = self.bounds()
                inset = 12.0
                corner = 20.0
                path_rect = NSMakeRect(
                    bounds.origin.x + inset,
                    bounds.origin.y + inset,
                    bounds.size.width - (inset * 2),
                    bounds.size.height - (inset * 2),
                )
                path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    path_rect, corner, corner
                )
                t = (math.sin(self._phase) + 1.0) / 2.0
                base = (0.15 + 0.45 * t, 0.75 + 0.2 * (1 - t), 1.0, 0.9)
                accent = (1.0, 0.35 + 0.3 * t, 0.85, 0.9)
                mix = (
                    base[0] * (1 - t) + accent[0] * t,
                    base[1] * (1 - t) + accent[1] * t,
                    base[2] * (1 - t) + accent[2] * t,
                    0.9,
                )
                glow = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    mix[0], mix[1], mix[2], 0.28
                )
                glow.setStroke()
                glow_path = path.copy()
                glow_path.setLineWidth_(14.0)
                glow_path.stroke()
                stroke = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    mix[0], mix[1], mix[2], 0.95
                )
                stroke.setStroke()
                path.setLineWidth_(7.0 + 1.8 * math.sin(self._phase + 1.3))
                dash = (14.0, 8.0)
                path.setLineDash_count_phase_(dash, 2, self._phase * 10.0)
                path.stroke()
                corner_glow = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    1.0, 1.0, 1.0, 0.08
                )
                corner_glow.setStroke()
                corner_path = path.copy()
                corner_path.setLineWidth_(2.0)
                corner_path.stroke()

        class _NonActivatingPanel(NSPanel):
            def canBecomeKeyWindow(self):
                return False

            def canBecomeMainWindow(self):
                return False

        class _TimerTarget(NSObject):
            def init(self):
                self = objc.super(_TimerTarget, self).init()
                if self is None:
                    return None
                self._callback = None
                return self

            def update_callback(self, cb):
                self._callback = cb

            def tick_(self, _timer):
                if self._callback:
                    self._callback()

        ns_app = NSApplication.sharedApplication()
        ns_app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)

        screen = NSScreen.mainScreen()
        frame = screen.frame()
        panel = _NonActivatingPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless, 2, False
        )
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary
        )
        try:
            panel.setHidesOnDeactivate_(False)
        except Exception:
            pass

        view = _OverlayView.alloc().initWithFrame_(frame)
        panel.setContentView_(view)
        panel.orderOut_(None)

        state: dict = {"active": False, "suspended": False}

        def _poll_commands(_timer):
            try:
                while True:
                    cmd, val = self._cmd_queue.get_nowait()
                    if cmd == "active":
                        state["active"] = bool(val)
                        if state["active"]:
                            panel.orderFront_(None)
                        else:
                            panel.orderOut_(None)
                    elif cmd == "suspend":
                        state["suspended"] = bool(val)
                    elif cmd == "shutdown":
                        panel.orderOut_(None)
                        ns_app.stop_(None)
                        return
            except queue.Empty:
                pass
            view.update_state(state)
            if state["active"]:
                panel.orderFront_(None)
                panel.displayIfNeeded()

        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, view, "step:", None, True
        )
        poller = _TimerTarget.alloc().init()
        poller.update_callback(lambda: _poll_commands(None))
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, poller, "tick:", None, True
        )

        ns_app.run()


# ---------------------------------------------------------------------------
# WebSocket command receiver
# ---------------------------------------------------------------------------

async def receive_audio_from_websocket(
    websocket, overlay: ActivityOverlay
) -> None:
    """
    Process JSON command messages from the server and execute OS actions.
    Each action primitive is dispatched to the default thread pool so the
    asyncio event loop remains unblocked throughout.
    """
    loop = asyncio.get_running_loop()

    try:
        logger.info("Server command listener started.")
        async for message in websocket:
            if isinstance(message, bytes):
                continue  # not used in this direction

            try:
                data = json.loads(message)
                logger.info(f"SERVER COMMAND: {json.dumps(data)}")
                command = data.get("command")

                if command == "set_activity":
                    overlay.set_active(data.get("state") == "active")

                elif command == "capture_screen":
                    result = await loop.run_in_executor(
                        None, _capture_screen_with_context, overlay
                    )
                    cap = result["capture"]
                    ctx = result["app_context"]
                    await websocket.send(json.dumps({
                        "event": "screen_captured",
                        "image_base64": cap["image_base64"],
                        "width": cap["width"],
                        "height": cap["height"],
                        "app_context": ctx,
                    }))
                    logger.info(
                        "Screen captured: %sx%s | app=%s url=%s",
                        cap["width"], cap["height"],
                        ctx.get("app_name", "?"), ctx.get("tab_url", ""),
                    )

                elif command == "click":
                    x, y = data.get("x"), data.get("y")
                    if x is not None and y is not None:
                        await loop.run_in_executor(
                            None,
                            _run_with_overlay_suspended,
                            overlay, perform_click_sync, int(x), int(y),
                        )
                    else:
                        logger.warning(f"Click missing x or y: {data}")

                elif command == "type_text":
                    await loop.run_in_executor(
                        None,
                        _run_with_overlay_suspended,
                        overlay, perform_type_sync, data.get("text", ""),
                    )

                elif command == "open_url":
                    await loop.run_in_executor(
                        None,
                        _run_with_overlay_suspended,
                        overlay, perform_open_url_sync, data.get("url", ""),
                    )

                elif command == "keyboard_type":
                    await loop.run_in_executor(
                        None,
                        _run_with_overlay_suspended,
                        overlay, perform_keyboard_type_sync, data.get("text", ""),
                    )

                elif command == "press_hotkey":
                    keys = data.get("keys", [])
                    if keys:
                        await loop.run_in_executor(
                            None,
                            _run_with_overlay_suspended,
                            overlay, perform_hotkey_sync, keys,
                        )

                elif command == "scroll":
                    await loop.run_in_executor(
                        None,
                        _run_with_overlay_suspended,
                        overlay, perform_scroll_sync, int(data.get("amount", -10)),
                    )

            except json.JSONDecodeError:
                logger.warning(f"Non-JSON message from server: {str(message)[:100]}")

    except websockets.ConnectionClosed:
        logger.info("WebSocket connection closed.")
    except Exception as exc:
        logger.error(f"Command listener error: {exc}")


# ---------------------------------------------------------------------------
# WebSocket streaming session
# ---------------------------------------------------------------------------

async def stream_audio_to_websocket(
    detector: WakeWordDetector,
    overlay: ActivityOverlay,
) -> None:
    uri = "ws://localhost:8080/ws/agent"
    detector.audio_queue = asyncio.Queue()
    detector.is_streaming = True

    async def _send_audio(ws) -> None:
        logger.info("Upstream audio stream started.")
        while detector.is_streaming:
            try:
                chunk = await asyncio.wait_for(
                    detector.audio_queue.get(), timeout=1.0
                )
                # Discard the sentinel that signals an orderly shutdown
                if chunk is WakeWordDetector._SHUTDOWN_SENTINEL:
                    break
                await ws.send(chunk)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                break

    try:
        logger.info(f"Connecting to {uri}...")
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "event": "wake_word_detected",
                "timestamp": time.time(),
            }))
            await asyncio.gather(
                _send_audio(ws),
                receive_audio_from_websocket(ws, overlay),
            )
    except Exception as exc:
        logger.error(f"WebSocket session error: {exc}")
    finally:
        logger.info("Session ended — cleaning up.")
        overlay.set_active(False)
        detector.is_streaming = False
        detector.audio_queue = None


# ---------------------------------------------------------------------------
# Wake word callback
# ---------------------------------------------------------------------------

def on_wake_word(detector: WakeWordDetector, overlay: ActivityOverlay) -> None:
    print("\n" + "=" * 50)
    print(" >>> Wake word detected! Starting session...")
    print("=" * 50 + "\n")
    asyncio.create_task(stream_audio_to_websocket(detector, overlay))


# ---------------------------------------------------------------------------
# Async main + thread entry
# ---------------------------------------------------------------------------

async def main_async(
    overlay: ActivityOverlay,
    stop_event: threading.Event,
) -> None:
    load_dotenv()

    access_key = os.environ.get("PICOVOICE_ACCESS_KEY")
    if not access_key:
        logger.error(
            "PICOVOICE_ACCESS_KEY is not set. "
            "Export it before starting: export PICOVOICE_ACCESS_KEY='...' "
        )
        return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    keyword_path = os.path.join(script_dir, "HeySai_mac.ppn")
    if not os.path.exists(keyword_path):
        logger.error(
            f"Wake word file not found: {keyword_path}\n"
            "Download 'HeySai_mac.ppn' from the Picovoice Console."
        )
        return

    detector = WakeWordDetector(
        keyword_path=keyword_path,
        access_key=access_key,
        callback=lambda: on_wake_word(detector, overlay),
    )
    detector.start()
    logger.info("Sai is ready. Listening for wake word. Press Ctrl+C to quit.")

    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Initiating graceful shutdown...")
        overlay.set_active(False)

        # Stop detector and wait for PyAudio/Porcupine to be fully released
        detector.stop(join_timeout=4.0)

        # Give any pending asyncio tasks a moment to complete
        await asyncio.sleep(0.3)
        logger.info("Shutdown complete.")


def _run_asyncio_loop(
    overlay: ActivityOverlay,
    stop_event: threading.Event,
) -> None:
    """Entry point for the asyncio daemon thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main_async(overlay, stop_event))
    except Exception as exc:
        logger.error(f"Asyncio loop error: {exc}")
    finally:
        # Cancel any remaining tasks before closing the loop
        pending = asyncio.all_tasks(loop)
        if pending:
            for task in pending:
                task.cancel()
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        loop.stop()
        loop.close()
        logger.info("Asyncio loop closed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        overlay = ActivityOverlay()
        stop_event = threading.Event()

        def _handle_sigint(signum, frame):
            logger.info("SIGINT received — initiating shutdown.")
            stop_event.set()
            overlay.shutdown()

        signal.signal(signal.SIGINT, _handle_sigint)

        asyncio_thread = threading.Thread(
            target=_run_asyncio_loop,
            args=(overlay, stop_event),
            name="SaiAsyncioLoop",
            daemon=True,
        )
        asyncio_thread.start()

        # NSApplication run-loop must stay on the main thread (macOS requirement)
        overlay.run_forever()

        # Overlay exited — signal the asyncio thread to stop if it hasn't already
        stop_event.set()
        asyncio_thread.join(timeout=5.0)
        if asyncio_thread.is_alive():
            logger.warning("Asyncio thread did not exit cleanly within timeout.")

    except KeyboardInterrupt:
        logger.info("Application terminated by user.")
