#!/usr/bin/env bash
# ============================================================
#  Project Sentinel — Unix/macOS One-Click Setup
#  Samsung PRISM AX Hackathon
#  Usage: bash scripts/setup_unix.sh
# ============================================================

set -e

GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; RESET="\033[0m"
ok()   { echo -e "  ${GREEN}[OK]${RESET} $*"; }
warn() { echo -e "  ${YELLOW}[WARN]${RESET} $*"; }
err()  { echo -e "  ${RED}[ERROR]${RESET} $*"; exit 1; }

echo ""
echo "  ====================================================="
echo "   Project Sentinel | Samsung PRISM AX Hackathon"
echo "   Unix/macOS Setup Script"
echo "  ====================================================="
echo ""

# ── Python ────────────────────────────────────────────────────
if command -v python3 &>/dev/null; then
    PY=$(python3 --version)
    ok "$PY"
    PYTHON=python3
elif command -v python &>/dev/null; then
    PY=$(python --version)
    ok "$PY"
    PYTHON=python
else
    err "Python 3.10+ not found. Install from https://python.org/downloads"
fi

# Check version >= 3.10
$PYTHON -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" || \
    err "Python 3.10+ required. Found: $PY"

# ── Node.js ───────────────────────────────────────────────────
if command -v node &>/dev/null; then
    ok "Node.js $(node --version)"
else
    warn "Node.js not found. Install from https://nodejs.org (needed for OpenClaw)."
    warn "Continuing without OpenClaw — you can install it later."
    NODE_MISSING=1
fi

# ── Python packages ───────────────────────────────────────────
echo ""
echo "  Installing Python packages..."
$PYTHON -m pip install anthropic psutil pytest --quiet --upgrade
ok "anthropic, psutil, pytest installed."

# ── OpenClaw ──────────────────────────────────────────────────
if [ -z "$NODE_MISSING" ]; then
    echo ""
    echo "  Installing OpenClaw..."
    npm install -g openclaw@latest --silent 2>/dev/null && \
        ok "OpenClaw $(openclaw --version 2>/dev/null || echo 'installed')" || \
        warn "OpenClaw install failed. Run: npm install -g openclaw@latest"
fi

# ── Logs directory ────────────────────────────────────────────
mkdir -p logs
ok "logs/ directory ready."

# ── Tests ─────────────────────────────────────────────────────
echo ""
echo "  Running test suite (133 tests)..."
echo "  -------------------------------------------------------"
$PYTHON -m pytest tests/ -q --tb=short && ok "All 133 tests passed." || \
    warn "Some tests failed — see output above."

# ── Build C++ engine (optional) ───────────────────────────────
echo ""
if command -v g++ &>/dev/null; then
    echo "  Building C++ engine..."
    g++ -O2 -std=c++17 -pthread -Wall engine/sentinel_engine.cpp \
        -o engine/sentinel_engine 2>/dev/null && \
        ok "C++ engine built: engine/sentinel_engine" || \
        warn "C++ build failed — use Python engine instead."
else
    warn "g++ not found — C++ engine skipped. Python engine works fine."
fi

# ── Done ──────────────────────────────────────────────────────
echo ""
echo "  ====================================================="
echo "   Setup complete! Quick start:"
echo ""
echo "   Quick demo (no API key needed):"
echo "     python3 scripts/demo.py --quick"
echo ""
echo "   Web dashboard:"
echo "     open dashboard/sentinel_web.html"
echo ""
echo "   AI agent (mock mode):"
echo "     python3 agent/sentinel_agent.py --mock warn"
echo ""
echo "   Full OpenClaw mode:"
echo "     export ANTHROPIC_API_KEY=sk-ant-YOUR-KEY"
echo "     openclaw onboard --anthropic-api-key \$ANTHROPIC_API_KEY"
echo "     python3 scripts/sentinel_engine_py.py &"
echo "     openclaw gateway --config openclaw.json"
echo "  ====================================================="
echo ""
