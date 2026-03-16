import os
import struct
import pyaudio
import logging
import asyncio
import json
import time
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

# Global variables to store the exact dimensions of the last capture
latest_screenshot_width = 2560
latest_screenshot_height = 1600

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
                keyword_paths=[self.keyword_path]
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


# ==============================================================================
# Usage Example / Testing Script
# ==============================================================================

def capture_screen_sync() -> dict:
    """Captures screen natively, then resizes to logical display size for 1:1 brain mapping."""
    file_path = "/tmp/sai_capture.png"
    
    try:
        # 1. Native macOS capture (e.g., 2880x1800 on Retina)
        subprocess.run(["screencapture", "-x", "-C", file_path], check=True)
        
        # 2. Get logical size (e.g., 1440x900)
        logical_w, logical_h = pyautogui.size()
        
        with Image.open(file_path) as img:
            # 3. Downsample to logical size for 1:1 mapping
            # This makes the "Vision" identical to the "Execution" space.
            resized_img = img.resize((logical_w, logical_h), Image.Resampling.LANCZOS)
            
            buf = io.BytesIO()
            resized_img.convert("RGB").save(buf, format="JPEG", quality=85)
            b64_img = base64.b64encode(buf.getvalue()).decode('utf-8')
            
        logging.info(f"CAPTURED: Physical={img.size}, Resized to Logical={logical_w}x{logical_h}")
        return {
            "image_base64": b64_img,
            "width": logical_w,
            "height": logical_h
        }
    except Exception as e:
        logging.error(f"Native capture failed: {e}")
        return {"error": str(e)}

def perform_click_sync(x: int, y: int):
    """Click with 1:1 mapping (Resized capture ensures brain coords == logical coords)."""
    try:
        # No scaling needed! Resize to logical resolution on capture
        # ensures brain's (x, y) is already in PyAutoGUI's space.
        logging.warning(f"COORD 1:1 DEBUG: Brain=({x}, {y}), Clicking={x}, {y}")
        
        pyautogui.moveTo(x, y, duration=0.1)
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

def perform_open_url_sync(url: str):
    """Opens a URL directly in the default browser."""
    # Ensure URL looks valid enough
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    import subprocess
    logging.info(f"Navigating to: {url}")
    subprocess.run(["open", url])

async def receive_audio_from_websocket(websocket):
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
                    if command == "capture_screen":
                        capture_data = await loop.run_in_executor(None, capture_screen_sync)
                        response = {
                            "event": "screen_captured",
                            "image_base64": capture_data["image_base64"],
                            "width": capture_data["width"],
                            "height": capture_data["height"]
                        }
                        await websocket.send(json.dumps(response))
                        logging.info(f"Sent screen capture ({capture_data['width']}x{capture_data['height']}).")
                        
                    elif command == "click":
                        x, y = data.get("x"), data.get("y")
                        if x is not None and y is not None:
                            await loop.run_in_executor(None, perform_click_sync, int(x), int(y))
                        else:
                            logging.warning(f"Click command missing x or y: {data}")
                            
                    elif command == "type_text":
                        text = data.get("text", "")
                        await loop.run_in_executor(None, perform_type_sync, text)

                    elif command == "open_url":
                        url = data.get("url", "")
                        await loop.run_in_executor(None, perform_open_url_sync, url)

                    elif command == "keyboard_type":
                        text = data.get("text", "")
                        await loop.run_in_executor(None, perform_keyboard_type_sync, text)

                    elif command == "press_hotkey":
                        keys = data.get("keys", [])
                        if keys:
                            await loop.run_in_executor(None, perform_hotkey_sync, keys)

                    elif command == "scroll":
                        amount = data.get("amount", -10)
                        await loop.run_in_executor(None, perform_scroll_sync, int(amount))

                except json.JSONDecodeError:
                    logging.warning(f"Received unknown message: {message}")
    except websockets.ConnectionClosed:
        logging.warning("WebSocket connection closed.")
    except Exception as e:
        logging.error(f"Error in command listener: {e}")

async def stream_audio_to_websocket(detector: WakeWordDetector):
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
                receive_audio_from_websocket(websocket)
            )
    except Exception as e:
        logging.error(f"WebSocket session error: {e}")
    finally:
        logging.info("Cleaning up session resources...")
        detector.is_streaming = False
        detector.audio_queue = None

def on_wake_word(detector: WakeWordDetector):
    """Callback triggered when the wake word is detected."""
    print("\n" + "="*50)
    print(" >>> 'Sai' Wake Word Triggered! Starting bidirectional session...")
    print("="*50 + "\n")
    
    # Start the bidirectional streaming/playback task on the event loop
    asyncio.create_task(stream_audio_to_websocket(detector))

async def main():
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
        callback=lambda: on_wake_word(detector)
    )
    
    # Start the detector (runs the PyAudio loop in a separate thread)
    detector.start()
    
    logging.info("System is ready. Press Ctrl+C to terminate.")
    
    try:
        # Keep the main loop alive indefinitely
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        # Graceful shutdown Sequence
        logging.info("Initiating graceful shutdown...")
        detector.stop()
        
        # Allow time for threads to shut down and resources to be released
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    try:
        # Use asyncio.run to manage the lifecycle of the event loop
        asyncio.run(main())
    except KeyboardInterrupt:
        # Normally, asyncio catches this, but adding it here prevents verbose stack traces
        logging.info("Application terminated by user.")
