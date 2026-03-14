# Wake Word Detection Implementation

## Overview
This directory contains the local client implementation for the OS agent's wake word detection system. It uses **Picovoice Porcupine** to listen for the custom wake word "Sai".

## Architecture
The system is designed with an asynchronous, non-blocking architecture to ensure that the main application thread (which will eventually handle WebSocket communication with the cloud backend) is never blocked by audio processing.
- **Audio Capture**: `PyAudio` captures the audio stream from the default system microphone.
- **Wake Word Engine**: `pvporcupine` processes the audio stream to detect the custom wake word `HeySai_mac.ppn`.
- **Concurrency**: The blocking audio read loop is executed in a background daemon thread using `ThreadPoolExecutor`.
- **Event Loop Integration**: When the wake word is detected, Porcupine triggers a callback that is safely dispatched back to the main `asyncio` event loop using `call_soon_threadsafe`.

## Prerequisites
- macOS (as the `.ppn` model is compiled specifically for Mac)
- Python 3.11+
- Homebrew (for dependencies)

## Setup Instructions

### 1. Install System Dependencies
PyAudio requires the `portaudio` package to build successfully. If you run into build errors during `pip install`, run this first:
```bash
brew install portaudio
```

### 2. Install Python Dependencies
Install the required packages from `requirements.txt`:
```bash
pip install -r requirements.txt
```
*(Dependencies: `pvporcupine`, `pyaudio`, `python-dotenv`)*

### 3. Environment Variables
The application uses `python-dotenv` to load the Picovoice Access Key. 
1. Create a `.env` file in the `client/` directory.
2. Add your Access Key:
   ```env
   PICOVOICE_ACCESS_KEY=your_access_key_here
   ```

### 4. Custom Wake Word Model
The script requires a custom `.ppn` file trained for the wake word "Sai" on Mac.
- The file must be named `HeySai_mac.ppn` and placed in the same directory as `wake_word.py`.
- The script dynamically resolves the absolute path to `HeySai_mac.ppn` using `__file__`, meaning `python wake_word.py` can be executed successfully from any working directory.

## Running the Application
To run the wake word detector:
```bash
python client/wake_word.py
```
You will see mocked "WebSocket pinging" logs every 5 seconds. This demonstrates that the main asynchronous loop is alive and non-blocked while the microphone actively listens in the background. Saying "Sai" will print an interception log.
