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
import functools
import base64
import mss
import mss.tools
import pyautogui
from dotenv import load_dotenv
import subprocess
from PIL import Image
import io
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

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
    objc = None

# Canonical vision canvas for Sai on M1 MacBook Pro.
# Nova always reasons on this 1440x900 logical space.
LOGICAL_CANVAS_WIDTH = 1440
LOGICAL_CANVAS_HEIGHT = 900

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class WakeWordDetector:
    """
    A robust, non-blocking wake word detector using Picovoice Porcupine and PyAudio.
    The audio capture and processing run in a separate daemon thread to ensure 
    the main application (e.g., handling WebSockets) is never blocked.
    """
    def __init__(self, keyword_path: str, access_key: str, callback: Callable[[], None]):
        """
        Initialize the detector.
        
        :param keyword_path: Path to the custom .ppn file (e.g., 'Sai_mac.ppn')
        :param access_key: Your Picovoice AccessKey
        :param callback: An asyncio-safe callback triggered when the wake word is heard
        """
        self.keyword_path = keyword_path
        self.access_key = access_key
        self.callback = callback
        
        self.porcupine: Optional[pvporcupine.Porcupine] = None
        self.pa: Optional[pyaudio.PyAudio] = None
        self.audio_stream = None
        self.is_listening = False
        self.is_streaming = False
        self.audio_queue: Optional[asyncio.Queue] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="WakeWordThread")
        self._loop = None

    def _listen_loop(self):
        """
        The blocking loop that continuously records audio and feeds it to Porcupine.
        Designed to run inside the ThreadPoolExecutor.
        """
        try:
            self.porcupine = pvporcupine.create(
                access_key=self.access_key,
                keyword_paths=[self.keyword_path],
                sensitivities=[0.8]
            )
            
            self.pa = pyaudio.PyAudio()
            self.audio_stream = self.pa.open(
                rate=self.porcupine.sample_rate,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=self.porcupine.frame_length
            )
            
            logging.info(f"Listening for custom wake word from '{self.keyword_path}'...")
            
            while self.is_listening:
                # Read audio frame (exception_on_overflow=False prevents crashes on slow systems like VM/Docker)
                pcm_bytes = self.audio_stream.read(self.porcupine.frame_length, exception_on_overflow=False)
                pcm_unpacked = struct.unpack_from("h" * self.porcupine.frame_length, pcm_bytes)
                
                keyword_index = self.porcupine.process(pcm_unpacked)
                
                if keyword_index >= 0:
                    logging.info("Wake word detected!")
                    # Securely schedule the callback on the main asyncio event loop
                    if self._loop and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(self.callback)
                
                # If streaming is active, push raw audio chunks to the queue
                if self.is_streaming and self.audio_queue is not None:
                    if self._loop and not self._loop.is_closed():
                        # Send raw bytes for the WebSocket stream
                        self._loop.call_soon_threadsafe(self.audio_queue.put_nowait, pcm_bytes)
                    
        except Exception as e:
            logging.error(f"Error in wake word detection: {e}")
        finally:
            self._cleanup()

    def _cleanup(self):
        """Releases PyAudio and Porcupine resources gracefully."""
        logging.info("Cleaning up audio and Porcupine resources...")
        if self.audio_stream is not None:
            if self.audio_stream.is_active():
                self.audio_stream.stop_stream()
            self.audio_stream.close()
            self.audio_stream = None
        
        if self.pa is not None:
            self.pa.terminate()
            self.pa = None
            
        if self.porcupine is not None:
            self.porcupine.delete()
            self.porcupine = None

    def start(self):
        """
        Starts the background thread that listens for the wake word.
        Must be called from a running asyncio event loop.
        """
        if self.is_listening:
            return
            
        self.is_listening = True
        self._loop = asyncio.get_running_loop()
        
        # Dispatch the blocking audio loop into the ThreadPoolExecutor
        self._loop.run_in_executor(self._executor, self._listen_loop)

    def stop(self):
        """Signals the background thread to stop listening."""
        self.is_listening = False
        # Do not wait for thread to join immediately to avoid blocking calling thread
        self._executor.shutdown(wait=False)

class ActivityOverlay:
    """
    Fullscreen animated border to indicate Sai is active.
    Runs in a dedicated Tk thread so it never blocks the asyncio loop.
    """
    def __init__(self):
        self._cmd_queue: "queue.Queue[tuple[str, Optional[bool]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._root = None
        self._enabled = sys.platform == "darwin" and objc is not None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="SaiActivityOverlay", daemon=True)
        self._thread.start()

    def run_forever(self):
        if not self._enabled:
            logging.warning("Activity overlay disabled: requires macOS with pyobjc installed.")
            # Block to keep behavior consistent with main-thread UI loop.
            while True:
                time.sleep(1)
        self._run()

    def set_active(self, active: bool):
        self._cmd_queue.put(("active", active))

    def suspend(self):
        self._cmd_queue.put(("suspend", True))

    def resume(self):
        self._cmd_queue.put(("suspend", False))

    def shutdown(self):
        self._cmd_queue.put(("shutdown", None))

    def _run(self):
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
                inset = 10.0
                corner = 18.0
                path_rect = NSMakeRect(
                    bounds.origin.x + inset,
                    bounds.origin.y + inset,
                    bounds.size.width - (inset * 2),
                    bounds.size.height - (inset * 2),
                )
                path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(path_rect, corner, corner)

                # Animated color blend
                t = (math.sin(self._phase) + 1.0) / 2.0
                base = (0.15 + 0.45 * t, 0.75 + 0.2 * (1 - t), 1.0, 0.9)
                accent = (1.0, 0.35 + 0.3 * t, 0.85, 0.9)
                mix = (
                    base[0] * (1 - t) + accent[0] * t,
                    base[1] * (1 - t) + accent[1] * t,
                    base[2] * (1 - t) + accent[2] * t,
                    0.9,
                )

                # Glow
                glow = NSColor.colorWithCalibratedRed_green_blue_alpha_(mix[0], mix[1], mix[2], 0.25)
                glow.setStroke()
                glow_path = path.copy()
                glow_path.setLineWidth_(12.0)
                glow_path.stroke()

                # Main stroke with dashed motion
                stroke = NSColor.colorWithCalibratedRed_green_blue_alpha_(mix[0], mix[1], mix[2], 0.95)
                stroke.setStroke()
                path.setLineWidth_(6.0 + 1.5 * math.sin(self._phase + 1.3))
                dash = (14.0, 8.0)
                path.setLineDash_count_phase_(dash, 2, self._phase * 10.0)
                path.stroke()

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

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)

        screen = NSScreen.mainScreen()
        frame = screen.frame()
        panel = _NonActivatingPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless,
            2,
            False,
        )
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setLevel_(NSScreenSaverWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary
        )

        view = _OverlayView.alloc().initWithFrame_(frame)
        panel.setContentView_(view)
        panel.orderOut_(None)

        state = {"active": False, "suspended": False}

        def _poll_commands(_timer):
            try:
                while True:
                    cmd, val = self._cmd_queue.get_nowait()
                    if cmd == "active":
                        state["active"] = bool(val)
                        if state["active"]:
                            panel.orderFrontRegardless_(None)
                        else:
                            panel.orderOut_(None)
                    elif cmd == "suspend":
                        state["suspended"] = bool(val)
                    elif cmd == "shutdown":
                        panel.orderOut_(None)
                        app.stop_(None)
                        return
            except queue.Empty:
                pass
            view.update_state(state)

        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, view, "step:", None, True
        )

        poller = _TimerTarget.alloc().init()
        poller.update_callback(lambda: _poll_commands(None))
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, poller, "tick:", None, True
        )

        app.run()

def _run_with_overlay_suspended(overlay: ActivityOverlay, func, *args, **kwargs):
    overlay.suspend()
    try:
        return func(*args, **kwargs)
    finally:
        overlay.resume()


def _capture_screen_with_context(overlay: ActivityOverlay) -> dict:
    """Capture screenshot AND app context in a single overlay-suspended window."""
    overlay.suspend()
    try:
        app_ctx = get_active_app_context_sync()
        capture_data = capture_screen_sync()
        return {"capture": capture_data, "app_context": app_ctx}
    finally:
        overlay.resume()


# ==============================================================================
# Usage Example / Testing Script
# ==============================================================================

def capture_screen_sync() -> dict:
    """Captures screen natively, then resizes to the fixed 1440x900 canvas used by the agent."""
    file_path = "/tmp/sai_capture.png"
    
    try:
        # 1. Native macOS capture (e.g., 2560x1600 on Retina)
        subprocess.run(["screencapture", "-x", "-C", file_path], check=True)

        with Image.open(file_path) as img:
            native_w, native_h = img.size

            # 2. Downsample to the fixed logical canvas (what Nova sees).
            resized_img = img.resize(
                (LOGICAL_CANVAS_WIDTH, LOGICAL_CANVAS_HEIGHT),
                Image.Resampling.LANCZOS,
            )
            
            buf = io.BytesIO()
            resized_img.convert("RGB").save(buf, format="JPEG", quality=85)
            b64_img = base64.b64encode(buf.getvalue()).decode('utf-8')
            
        logging.info(
            "CAPTURED: Native=%sx%s, Canvas=%sx%s",
            native_w,
            native_h,
            LOGICAL_CANVAS_WIDTH,
            LOGICAL_CANVAS_HEIGHT,
        )
        return {
            "image_base64": b64_img,
            "width": LOGICAL_CANVAS_WIDTH,
            "height": LOGICAL_CANVAS_HEIGHT
        }
    except Exception as e:
        logging.error(f"Native capture failed: {e}")
        return {"error": str(e)}

def perform_click_sync(x: int, y: int):
    """
    Clicks at a point chosen on the fixed 1440x900 canvas.

    The model outputs normalized coordinates in [0, 1000] for both X and Y,
    where (0,0) is top-left and (1000,1000) is bottom-right. We map those
    directly into whatever coordinate space PyAutoGUI reports
    (logical or physical) so it “just works” on this M1 MacBook Pro.
    """
    try:
        screen_w, screen_h = pyautogui.size()

        # Interpret x, y as normalized [0, 1000] coordinates.
        nx = max(0.0, min(1000.0, float(x)))
        ny = max(0.0, min(1000.0, float(y)))

        click_x = int(max(0, min(screen_w - 1, round((nx / 1000.0) * (screen_w - 1)))))
        click_y = int(max(0, min(screen_h - 1, round((ny / 1000.0) * (screen_h - 1)))))

        logging.warning(
            "CLICK DEBUG: Brain(norm)=(%s,%s) -> Click=(%s,%s) | screen=%sx%s",
            x,
            y,
            click_x,
            click_y,
            screen_w,
            screen_h,
        )

        pyautogui.moveTo(click_x, click_y, duration=0.1)
        pyautogui.click()
        
    except Exception as e:
        logging.error(f"Perform click failed: {e}")

def perform_scroll_sync(amount: int):
    """Scrolls the screen. Positive amount = up, negative = down."""
    # Note: macOS scrolling can be sensitive.
    pyautogui.scroll(amount)

def perform_type_sync(text: str):
    """Presses Cmd+Space to open Spotlight, clears it, types, and launches."""
    # Use explicit keyDown/Up for better reliability on macOS
    pyautogui.keyDown('command')
    time.sleep(0.1)
    pyautogui.press('space')
    time.sleep(0.1)
    pyautogui.keyUp('command')
    time.sleep(1.0)  # Wait for Spotlight to fully focus
    # Clear any existing text in Spotlight
    pyautogui.hotkey('command', 'a')
    pyautogui.press('backspace')
    time.sleep(0.1)
    # Type exactly the text provided
    pyautogui.write(text, interval=0.04)
    time.sleep(0.5)  # Wait for search results
    pyautogui.press('enter')
    time.sleep(0.2)
    pyautogui.press('enter')  # Redundant enter to be sure

def perform_keyboard_type_sync(text: str):
    """Types text directly into the currently focused field using clipboard paste (reliable on macOS)."""
    import subprocess
    # Handle newlines as Enter keypresses
    parts = text.split("\n")
    for i, part in enumerate(parts):
        if part:
            # Copy to clipboard and paste — much more reliable than pyautogui.write on macOS
            process = subprocess.run(["pbcopy"], input=part.encode("utf-8"), capture_output=True)
            pyautogui.hotkey("command", "v")
            time.sleep(0.1)
        if i < len(parts) - 1:
            pyautogui.press("enter")
            time.sleep(0.1)

def perform_hotkey_sync(keys: list):
    """Presses a combination of keys together (e.g., ['command', 'n'])."""
    pyautogui.hotkey(*keys)

def get_active_app_context_sync() -> dict:
    """Uses AppleScript to get the frontmost app name and, if it's a browser, the active tab URL."""
    ctx: dict = {"app_name": "", "bundle_id": "", "tab_url": "", "tab_title": ""}
    try:
        app_script = (
            'tell application "System Events" to set frontApp to name of first application process whose frontmost is true\n'
            'return frontApp'
        )
        result = subprocess.run(
            ["osascript", "-e", app_script],
            capture_output=True, text=True, timeout=3
        )
        app_name = result.stdout.strip()
        ctx["app_name"] = app_name

        if app_name in ("Google Chrome", "Chromium"):
            url_script = 'tell application "Google Chrome" to return URL of active tab of front window'
            title_script = 'tell application "Google Chrome" to return title of active tab of front window'
        elif app_name == "Safari":
            url_script = 'tell application "Safari" to return URL of front document'
            title_script = 'tell application "Safari" to return name of front document'
        elif app_name in ("Arc",):
            url_script = 'tell application "Arc" to return URL of active tab of front window'
            title_script = 'tell application "Arc" to return title of active tab of front window'
        elif app_name == "Firefox":
            url_script = None
            title_script = None
        else:
            url_script = None
            title_script = None

        if url_script:
            r = subprocess.run(["osascript", "-e", url_script], capture_output=True, text=True, timeout=3)
            ctx["tab_url"] = r.stdout.strip()
        if title_script:
            r = subprocess.run(["osascript", "-e", title_script], capture_output=True, text=True, timeout=3)
            ctx["tab_title"] = r.stdout.strip()
    except Exception as e:
        logging.warning(f"Failed to get active app context: {e}")
    return ctx


def perform_open_url_sync(url: str):
    """Opens a URL in the currently active browser, falling back to the default."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    active_browser = _detect_active_browser()

    if active_browser:
        logging.info(f"Opening {url} in active browser: {active_browser}")
        subprocess.run(["open", "-a", active_browser, url])
    else:
        logging.info(f"No active browser detected, opening {url} in default browser")
        subprocess.run(["open", url])


def _detect_active_browser() -> str:
    """Return the frontmost app name if it's a known browser, else empty string."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to return name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=3
        )
        app = result.stdout.strip()
        if app in ("Google Chrome", "Chromium", "Safari", "Arc", "Firefox",
                    "Microsoft Edge", "Brave Browser", "Opera", "Vivaldi"):
            return app
    except Exception:
        pass
    return ""

async def receive_audio_from_websocket(websocket, overlay: ActivityOverlay):
    """
    Listens for text (JSON) commands from the server for OS control.
    Silent operation: no binary audio playback.
    """
    try:
        logging.info("Started server command listener.")
        loop = asyncio.get_running_loop()
        
        async for message in websocket:
            if not isinstance(message, bytes):
                try:
                    data = json.loads(message)
                    logging.info(f"SERVER COMMAND RECEIVED: {json.dumps(data, indent=2)}")
                    
                    command = data.get("command")
                    if command == "set_activity":
                        state = data.get("state", "idle")
                        overlay.set_active(state == "active")
                        continue

                    if command == "capture_screen":
                        capture_result = await loop.run_in_executor(
                            None, _capture_screen_with_context, overlay
                        )
                        capture_data = capture_result["capture"]
                        app_ctx = capture_result["app_context"]
                        response = {
                            "event": "screen_captured",
                            "image_base64": capture_data["image_base64"],
                            "width": capture_data["width"],
                            "height": capture_data["height"],
                            "app_context": app_ctx,
                        }
                        await websocket.send(json.dumps(response))
                        logging.info(
                            "Sent screen capture (%sx%s) | app=%s url=%s",
                            capture_data['width'], capture_data['height'],
                            app_ctx.get('app_name', '?'), app_ctx.get('tab_url', ''),
                        )
                        
                    elif command == "click":
                        x, y = data.get("x"), data.get("y")
                        if x is not None and y is not None:
                            await loop.run_in_executor(
                                None, _run_with_overlay_suspended, overlay, perform_click_sync, int(x), int(y)
                            )
                        else:
                            logging.warning(f"Click command missing x or y: {data}")
                            
                    elif command == "type_text":
                        text = data.get("text", "")
                        await loop.run_in_executor(
                            None, _run_with_overlay_suspended, overlay, perform_type_sync, text
                        )

                    elif command == "open_url":
                        url = data.get("url", "")
                        await loop.run_in_executor(
                            None, _run_with_overlay_suspended, overlay, perform_open_url_sync, url
                        )

                    elif command == "keyboard_type":
                        text = data.get("text", "")
                        await loop.run_in_executor(
                            None, _run_with_overlay_suspended, overlay, perform_keyboard_type_sync, text
                        )

                    elif command == "press_hotkey":
                        keys = data.get("keys", [])
                        if keys:
                            await loop.run_in_executor(
                                None, _run_with_overlay_suspended, overlay, perform_hotkey_sync, keys
                            )

                    elif command == "scroll":
                        amount = data.get("amount", -10)
                        await loop.run_in_executor(
                            None, _run_with_overlay_suspended, overlay, perform_scroll_sync, int(amount)
                        )

                except json.JSONDecodeError:
                    logging.warning(f"Received unknown message: {message}")
    except websockets.ConnectionClosed:
        logging.warning("WebSocket connection closed.")
    except Exception as e:
        logging.error(f"Error in command listener: {e}")

async def stream_audio_to_websocket(detector: WakeWordDetector, overlay: ActivityOverlay):
    uri = "ws://localhost:8080/ws/agent"
    detector.audio_queue = asyncio.Queue()
    detector.is_streaming = True
    
    async def _send_audio(ws):
        logging.info("Streaming upstream audio...")
        while detector.is_streaming:
            try:
                # Wait for audio data from the detector loop
                chunk = await asyncio.wait_for(detector.audio_queue.get(), timeout=1.0)
                await ws.send(chunk)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                break

    try:
        logging.info(f"Connecting to {uri}...")
        async with websockets.connect(uri) as websocket:
            # Step 1: Handshake
            await websocket.send(json.dumps({"event": "wake_word_detected", "timestamp": time.time()}))
            
            # Step 2: Run upstream and downstream concurrently
            await asyncio.gather(
                _send_audio(websocket),
                receive_audio_from_websocket(websocket, overlay)
            )
    except Exception as e:
        logging.error(f"WebSocket session error: {e}")
    finally:
        logging.info("Cleaning up session resources...")
        overlay.set_active(False)
        detector.is_streaming = False
        detector.audio_queue = None

def on_wake_word(detector: WakeWordDetector, overlay: ActivityOverlay):
    """Callback triggered when the wake word is detected."""
    print("\n" + "="*50)
    print(" >>> 'Sai' Wake Word Triggered! Starting bidirectional session...")
    print("="*50 + "\n")
    
    # Start the bidirectional streaming/playback task on the event loop
    asyncio.create_task(stream_audio_to_websocket(detector, overlay))

async def main_async(overlay: ActivityOverlay, stop_event: threading.Event):
    # Load environment variables from .env file
    load_dotenv()
    
    # 1. Provide your AccessKey from Picovoice Console
    access_key = os.environ.get("PICOVOICE_ACCESS_KEY")
    if not access_key:
        logging.error("PICOVOICE_ACCESS_KEY environment variable is missing.")
        logging.error("Please export it: export PICOVOICE_ACCESS_KEY='your_access_key_here'")
        # For local testing, you can uncomment and hardcode below (NOT RECOMMENDED for production)
        # access_key = "YOUR_HARDCODED_KEY_HERE"
        return

    # 2. Path to the custom .ppn file downloaded for Mac
    # Resolve absolute path relative to this script so it works from any CWD
    script_dir = os.path.dirname(os.path.abspath(__file__))
    keyword_path = os.path.join(script_dir, "HeySai_mac.ppn")
    
    # Optional check to ensure file exists before failing in the background thread
    if not os.path.exists(keyword_path):
        logging.error(f"Cannot find custom wake word file: {keyword_path}")
        logging.error("Please ensure you have generated and downloaded 'Sai' for Mac from Picovoice Console.")
        return

    # 3. Instantiate and start detector
    # We pass the detector instance to the callback using a lambda 
    detector = WakeWordDetector(
        keyword_path=keyword_path,
        access_key=access_key,
        callback=lambda: on_wake_word(detector, overlay)
    )
    
    # Start the detector (runs the PyAudio loop in a separate thread)
    detector.start()
    
    logging.info("System is ready. Press Ctrl+C to terminate.")
    
    try:
        # Keep the main loop alive indefinitely
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    finally:
        # Graceful shutdown Sequence
        logging.info("Initiating graceful shutdown...")
        detector.stop()
        overlay.set_active(False)
        
        # Allow time for threads to shut down and resources to be released
        await asyncio.sleep(0.5)

def _run_asyncio_loop(overlay: ActivityOverlay, stop_event: threading.Event):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main_async(overlay, stop_event))
    finally:
        loop.stop()
        loop.close()

if __name__ == "__main__":
    try:
        overlay = ActivityOverlay()
        stop_event = threading.Event()

        def _handle_sigint(signum, frame):
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

        # Tk must run on the main thread on macOS.
        overlay.run_forever()
        stop_event.set()
        asyncio_thread.join(timeout=2.0)
    except KeyboardInterrupt:
        # Normally, asyncio catches this, but adding it here prevents verbose stack traces
        logging.info("Application terminated by user.")
