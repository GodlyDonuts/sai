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
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

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
    """Captures the primary screen using mss, compresses to PNG, and returns base64 and dimensions."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # Primary monitor
        sct_img = sct.grab(monitor)
        # Compress to PNG
        png_bytes = mss.tools.to_png(sct_img.rgb, sct_img.size)
        return {
            "image_base64": base64.b64encode(png_bytes).decode('utf-8'),
            "width": sct_img.size.width,
            "height": sct_img.size.height
        }

def perform_click_sync(x: int, y: int):
    """Performs a physical mouse click at the specified coordinates."""
    pyautogui.click(x, y)

async def receive_audio_from_websocket(websocket, output_stream):
    """
    Listens for binary audio data from the WebSocket and writes it to the PyAudio output stream.
    Also handles text (JSON) commands for OS control.
    """
    try:
        logging.info("Started audio reception worker.")
        loop = asyncio.get_running_loop()
        async for message in websocket:
            if isinstance(message, bytes):
                # Write binary PCM data directly to the output stream without blocking
                await loop.run_in_executor(
                    None, 
                    functools.partial(output_stream.write, message, exception_on_underflow=False)
                )
            else:
                try:
                    data = json.loads(message)
                    logging.info(f"Received JSON message: {data}")
                    
                    command = data.get("command")
                    if command == "capture_screen":
                        # Execute screen capture in a background thread to prevent blocking
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
                        x = data.get("x")
                        y = data.get("y")
                        if x is not None and y is not None:
                            await loop.run_in_executor(None, perform_click_sync, x, y)
                            logging.info(f"Performed click at ({x}, {y})")
                        else:
                            logging.warning(f"Click command missing x or y coordinates: {data}")

                except json.JSONDecodeError:
                    logging.warning(f"Received unknown message type: {message}")
    except websockets.ConnectionClosed:
        logging.warning("Output: WebSocket connection closed.")
    except Exception as e:
        logging.error(f"Error receiving audio: {e}")

async def stream_audio_to_websocket(detector: WakeWordDetector):
    """
    Connects to the server, sends a handshake, and manages concurrent audio 
    streaming (mic to cloud) and playback (cloud to local speakers).
    """
    uri = "ws://localhost:8080/ws/agent"
    detector.audio_queue = asyncio.Queue()
    detector.is_streaming = True
    
    # Initialize PyAudio for output (Playback)
    pa_output = pyaudio.PyAudio()
    output_stream = pa_output.open(
        rate=16000,
        channels=1,
        format=pyaudio.paInt16,
        output=True,
        frames_per_buffer=1024 # Small buffer for low latency
    )

    async def _send_audio(ws):
        logging.info("Handshake sent. Now streaming upstream audio...")
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
        logging.info(f"Connecting to WebSocket at {uri}...")
        async with websockets.connect(uri) as websocket:
            # Step 1: Handshake
            handshake = {
                "event": "wake_word_detected",
                "timestamp": time.time()
            }
            await websocket.send(json.dumps(handshake))
            
            # Step 2: Run upstream and downstream concurrently
            await asyncio.gather(
                _send_audio(websocket),
                receive_audio_from_websocket(websocket, output_stream)
            )
                    
    except Exception as e:
        logging.error(f"WebSocket session error: {e}")
    finally:
        logging.info("Cleaning up WebSocket and playback resources...")
        detector.is_streaming = False
        detector.audio_queue = None
        
        # Cleanup Playback
        if output_stream:
            output_stream.stop_stream()
            output_stream.close()
        pa_output.terminate()

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
