# 🪐 Sai: The Universal Multimodal OS Agent

**The keyboard is dead. The mouse is dead. Welcome to the Zero-Click Web.**

> **Award Targets:** Best of UI Navigators Grand Prize
> **Built for:** The 2026 Gemini Live Agent Challenge

---

## 👁️ The Vision

For the past decade, we have relied on brittle APIs and hard-coded RPA bots to automate our digital lives. When a company changes their UI, the automation breaks. Furthermore, voice assistants are entirely blind—they can tell you the weather, but they cannot *see* the complex software in front of you.

**Sai** is a fundamental paradigm shift. We are combining the real-time, interruptible conversation of the **Live Agent** track with the visual perception and physical execution of the **UI Navigator** track.

Sai does not use a single integration or API to interact with third-party software. It uses **Gemini 3.1 Pro’s advanced multimodal and agentic reasoning** alongside the **Google Agent Development Kit (ADK)** to look at your screen, listen to your voice, and operate your computer exactly like a human would.

---

## 🧠 What is Sai?

Sai is an OS-level, voice-native cybernetic co-pilot. You do not type prompts into a text box. You simply sit back, look at your monitor, and speak natively to your computer.

**Example Interaction:**

> **User:** *"Sai, I'm out of laundry detergent. Get me some Tide Pods on Amazon."* > *(Sai takes over the mouse, opens a new tab, navigates to Amazon, locates the search bar visually, and types 'Tide Pods'.)* > **User (Interrupting):** *"Wait, Sai, actually change that to the liquid detergent, not the pods."* > *(Sai instantly halts the current action trajectory, listens to the new constraint, deletes 'pods' from the search bar, types 'liquid', and proceeds to checkout.)*

---

## ✨ Core Disruptive Features

1. **True "Zero-API" UI Navigation:** Powered by Gemini 3.1 Pro's state-of-the-art visual understanding, Sai dynamically maps the DOM and pixels of any interface in real-time. It beats CAPTCHAs, bypasses anti-bot measures, and navigates "Dark Patterns" because it acts visually, not programmatically.
2. **Full-Duplex Interruption Architecture:** Utilizing the Gemini Live API via WebSockets, Sai’s cognitive loop can be interrupted mid-action. If you see the mouse moving toward the wrong button, you simply say "Stop," and the agent immediately halts and awaits redirection.
3. **Google Antigravity Orchestration:** We leverage Google's new agentic development platform, Antigravity, to manage Sai's memory, tool-calling state, and task trajectory across long-horizon OS operations.
4. **Transparent Thought Stream:** Sai features an elegant UI overlay that exposes its "internal monologue" (e.g., `[Vision: Target is Amazon checkout. Audio: User requested 2-day shipping. Action: Locating Prime toggle at X:450, Y:820]`), proving to users (and judges) exactly how the multimodal reasoning is occurring.

---

## 🏗️ Architecture & Technical Stack

To solve the "Stop-and-Go" problem of multimodal agents (where the agent pauses to think, ruining the UX), Sai utilizes a **Dual-Loop Cognitive Architecture** hosted entirely on Google Cloud.

### The Stack

* **The Brain (Orchestration):** Gemini 3.1 Pro (via Google AI Studio / Vertex AI) & Google Antigravity.
* **The Ears (Audio IO):** Gemini Live API (Native WebSocket audio streaming, bypassing legacy ASR/TTS pipelines).
* **The Hands (Execution):** Local Desktop Client (Python/Rust) running OS-level accessibility APIs and PyAutoGUI for precise X/Y coordinate mouse/keyboard execution.
* **The Infrastructure:** Google Cloud Run (Backend hosting), Cloud Tasks (Queueing), and Firebase (Real-time telemetry and state management).

### The Dual-Loop System

1. **The Fast Loop (The Live Agent):** A continuous WebSocket connection between the user's microphone/speaker and the Gemini Live API. This loop maintains the conversational persona, handles user interruptions, and determines *intent*.
2. **The Slow Loop (The UI Navigator):** When the Fast Loop detects a UI intent (e.g., "click the checkout button"), it triggers the Slow Loop. A screenshot of the active OS window is captured, compressed, and sent to **Gemini 3.1 Pro** with a specialized prompt to extract the exact `[X, Y]` coordinates of the requested semantic element.
3. **The Proxy Tool Hand-off:** Using ADK, the cloud-based Gemini model issues a tool call (e.g., `execute_click(x, y)`). A callback intercepts this in Google Cloud, serializes it over a secure WebSocket to the local desktop client, and executes the physical mouse movement instantly.

![Architecture Diagram Placeholder - *To be uploaded to repo*]

---

## 🚀 Setup & Reproducibility (For Judges)

### Prerequisites

* Python 3.11+
* Google Cloud Platform Account (with Billing Enabled for Gemini 3.1 Pro / Vertex AI)
* Google GenAI SDK & ADK installed

### 1. Cloud Infrastructure Deployment

We use Infrastructure-as-Code to make deployment one-click.

```bash
# Clone the repository
git clone https://github.com/yourusername/sai-agent.git
cd sai-agent/cloud-backend

# Authenticate with Google Cloud
gcloud auth login
gcloud config set project [YOUR_PROJECT_ID]

# Deploy the Gemini Live API & Orchestration Backend to Cloud Run
gcloud run deploy sai-orchestrator \
  --source . \
  --region us-central1 \
  --set-env-vars="GEMINI_API_KEY=your_key,PROJECT_ID=your_project" \
  --allow-unauthenticated

```

*Note: The deployment logs and Cloud Run dashboard view are included in our `proof_of_cloud.mp4` submission.*

### 2. Local Client Execution

Once the cloud backend is live, run the local eyes and hands:

```bash
cd ../local-client
pip install -r requirements.txt

# Start the local agent and connect to your Google Cloud Run WebSocket URL
python main.py --ws-url wss://sai-orchestrator-[hash]-uc.a.run.app

```

---

## 🧪 The "Do Anything" Hackathon Demos

We didn't build toy examples. In our submission video, you will see Sai execute the following flawlessly:

1. **The Commerce Run:** Ordering a physical product from an unstructured e-commerce site entirely via voice, including handling a mid-checkout voice interruption to change the shipping address.
2. **The Subscription Guillotine:** Pointing Sai at a notoriously difficult "Dark Pattern" gym cancellation portal. Sai visually identifies the guilt-trip UI, ignores the massive green "Keep Membership" button, and successfully clicks the low-contrast, hidden "Confirm Cancellation" text.
3. **Cross-App Data Synthesis:** "Sai, look at my open email, find the competitor pricing PDF attached, open it, and draft a Slack message to the sales team summarizing how we beat them."

---

## 🏆 Why Sai Wins

* **Maximum Innovation (40%):** We completely shatter the "text box" paradigm. This isn't a chatbot; it's a multimodal entity that commands the user's operating system.
* **Technical Excellence (30%):** We push the absolute limits of the newly released **Gemini 3.1 Pro** and its native agentic reasoning, overcoming latency issues by decoupling the audio-conversational loop from the visual-execution loop.
* **Zero Mockups:** Every pixel of mouse movement in our 4-minute demo is genuinely generated by the model's visual understanding of the screen in real-time.

**Stop typing. Start speaking. Sai is the future of human-computer interaction.**