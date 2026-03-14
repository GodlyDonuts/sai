# ☁️ Cloud Backend & Orchestration (Google Cloud Run)

## Responsibilities
* Host the Google Antigravity framework.
* Manage the WebSocket server for the Fast Loop (audio).
* Process the Slow Loop visual reasoning requests.
* Dispatch serialized tool-call commands to the local client.

## Setup Instructions
```bash
# Authenticate and set project
gcloud auth login
gcloud config set project [YOUR_PROJECT_ID]

# Deploy Orchestrator to Cloud Run
gcloud run deploy sai-orchestrator \
  --source . \
  --region us-central1 \
  --set-env-vars="GEMINI_API_KEY=your_key,PROJECT_ID=your_project" \
  --allow-unauthenticated