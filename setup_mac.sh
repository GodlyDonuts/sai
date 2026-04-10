#!/usr/bin/env bash
# setup_mac.sh — One-shot setup script for Sai on macOS
# Usage: bash setup_mac.sh

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'

info()    { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
error()   { echo -e "${RED}[✗]${RESET} $*" >&2; exit 1; }
section() { echo -e "\n${BOLD}$*${RESET}"; }

# ── 1. Python version check ──────────────────────────────────────────────────
section "Checking Python version..."

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c 'import sys; print(sys.version_info[:2])')
        major=$("$candidate" -c 'import sys; print(sys.version_info[0])')
        minor=$("$candidate" -c 'import sys; print(sys.version_info[1])')
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    error "Python 3.11+ is required but was not found.\nInstall it via: brew install python@3.11"
fi

info "Using $PYTHON_BIN ($(${PYTHON_BIN} --version))"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 2. Server virtual environment ────────────────────────────────────────────
section "Setting up server virtual environment..."

SERVER_DIR="$SCRIPT_DIR/server"
SERVER_VENV="$SERVER_DIR/venv"

if [ ! -d "$SERVER_VENV" ]; then
    "$PYTHON_BIN" -m venv "$SERVER_VENV"
    info "Created $SERVER_VENV"
else
    warn "Server venv already exists — skipping creation."
fi

info "Installing server dependencies..."
"$SERVER_VENV/bin/pip" install --quiet --upgrade pip
"$SERVER_VENV/bin/pip" install --quiet -r "$SERVER_DIR/requirements.txt"
info "Server dependencies installed."

# ── 3. Client virtual environment ────────────────────────────────────────────
section "Setting up client virtual environment..."

CLIENT_DIR="$SCRIPT_DIR/client"
CLIENT_VENV="$CLIENT_DIR/venv"

if [ ! -d "$CLIENT_VENV" ]; then
    "$PYTHON_BIN" -m venv "$CLIENT_VENV"
    info "Created $CLIENT_VENV"
else
    warn "Client venv already exists — skipping creation."
fi

info "Installing client dependencies..."
"$CLIENT_VENV/bin/pip" install --quiet --upgrade pip
"$CLIENT_VENV/bin/pip" install --quiet -r "$CLIENT_DIR/requirements.txt"
info "Client dependencies installed."

# ── 4. Environment files ──────────────────────────────────────────────────────
section "Configuring environment files..."

# Server .env
if [ ! -f "$SERVER_DIR/.env" ]; then
    cp "$SERVER_DIR/.env.example" "$SERVER_DIR/.env"
    info "Created server/.env from .env.example"
    warn "ACTION REQUIRED: Open server/.env and fill in your API keys:"
    warn "  → AMAZON_NOVA_API_KEY, NOVA_BASE_URL, OPENROUTER_API_KEY, ELEVENLABS_API_KEY"
else
    warn "server/.env already exists — skipping copy (your keys are safe)."
fi

# Client .env
if [ ! -f "$CLIENT_DIR/.env" ]; then
    cp "$CLIENT_DIR/.env.example" "$CLIENT_DIR/.env"
    info "Created client/.env from .env.example"
    warn "ACTION REQUIRED: Open client/.env and fill in your Picovoice key:"
    warn "  → PICOVOICE_ACCESS_KEY"
else
    warn "client/.env already exists — skipping copy (your keys are safe)."
fi

# ── 5. macOS permissions reminder ────────────────────────────────────────────
section "macOS Permissions Checklist"
echo ""
echo "  Sai requires three macOS permissions granted to your Terminal or IDE:"
echo ""
echo "  1. Accessibility    → System Settings → Privacy & Security → Accessibility"
echo "  2. Screen Recording → System Settings → Privacy & Security → Screen Recording"
echo "  3. Microphone       → System Settings → Privacy & Security → Microphone"
echo ""
echo "  If macOS does not prompt you automatically, reset permissions manually:"
echo ""
echo "    tccutil reset Accessibility"
echo "    tccutil reset ScreenCapture"
echo "    tccutil reset Microphone"
echo ""
echo "  Then re-launch your Terminal and run the app again."

# ── 6. Done ──────────────────────────────────────────────────────────────────
section "Setup complete!"
echo ""
echo "  To start the server:"
echo "    cd server && venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080"
echo ""
echo "  To start the client:"
echo "    cd client && venv/bin/python wake_word.py"
echo ""
echo "  Say ${BOLD}\"Hey Sai\"${RESET} to begin."
