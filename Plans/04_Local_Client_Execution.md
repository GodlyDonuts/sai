# 💻 Local Desktop Client (The Hands)

## Responsibilities
* Maintain a secure WebSocket connection to the Cloud Run orchestrator.
* Capture and compress screenshots of the active OS window to send to the cloud.
* Receive `[X, Y]` coordinates and execute precise mouse/keyboard movements using PyAutoGUI.
* Stream system audio/microphone input to the Fast Loop.

## Setup Instructions
```bash
cd local-client
pip install -r requirements.txt

# Start client and connect to GCP
python main.py --ws-url wss://sai-orchestrator-[hash]-uc.a.run.app