# Gemini Multimodal Live API Bidirectional Proxy

This document provides a detailed breakdown of the changes made to the FastAPI server to implement a bidirectional audio proxy for the Gemini Multimodal Live API.

## 1. Architectural Overview

The server acts as a middleman (proxy) between the local Sai client and Google's Gemini Multimodal Live API. This architecture allows the local client to remain lightweight while leveraging the high-performance cloud processing of Gemini.

### The Connection Flow:
1.  **Local Client Handshake**: The client connects to `ws://localhost:8080/ws/agent` and sends a `wake_word_detected` event.
2.  **Google WebSocket Initialization**: Upon successful handshake, the server opens a secondary WebSocket to Google's endpoint: `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent`.
3.  **Setup Phase**: The server immediately sends a configuration payload to Gemini to define its personality ("Sai") and output audio format.
4.  **Bidirectional Streaming**: The server runs two concurrent loops to manage data flow in both directions simultaneously.

## 2. Technical Implementation Details

### Dependencies
- **`python-dotenv`**: Added to manage the `GEMINI_API_KEY` securely via a [.env](file:///Users/sairamen/projects/sai/server/.env) file.
- **`websockets`**: Used for the outbound connection from the FastAPI server to Google.

### Model Selection
The integration uses **`models/gemini-2.5-flash-native-audio-latest`**. This specific model is part of the "Live API" family and is optimized for the `bidiGenerateContent` method which supports the sub-second latency required for voice conversation.

### The Bidirectional Loops
We use `asyncio.gather` to run two asynchronous functions concurrently:

#### Upstream Loop (`Client -> Google`)
- **Action**: Receives raw binary audio chunks from the local client.
- **Transformation**: Encodes the binary data in `base64`.
- **Packaging**: Wraps the data in the `realtimeInput` JSON structure required by the Gemini API:
  ```json
  {
    "realtimeInput": {
      "mediaChunks": [{
        "data": "...base64...",
        "mimeType": "audio/pcm;rate=16000"
      }]
    }
  }
  ```

#### Downstream Loop (`Google -> Client`)
- **Action**: Listens for responses from Google.
- **Parsing**: Looks for messages containing `serverContent` and `modelTurn`.
- **Extraction**: Extracts `inlineData` (base64 audio) and decodes it back to raw binary.
- **Forwarding**: Sends the raw binary audio directly back to the client's WebSocket for playback.

### System Instruction
The personality of the agent is defined in the initial `setup` message:
> "You are Sai, a voice-native cybernetic co-pilot. You control the user's operating system. Keep responses incredibly concise, conversational, and action-oriented."

## 3. Configuration

A [.env](file:///Users/sairamen/projects/sai/server/.env) file in the `server/` directory is required with the following content:
```env
GEMINI_API_KEY=your_google_api_key
```

## 4. Error Handling
- **Graceful Disconnects**: Detects when either side (client or Google) closes the connection and shuts down the associated loops to prevent resource leaks.
- **Validation**: Ensures the `GEMINI_API_KEY` is present before attempting a connection.
- **Handshake Security**: Closes the connection immediately if the initial handshake payload is malformed or unexpected.
