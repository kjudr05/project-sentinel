# Project Sentinel — Build System
#
# Targets:
#   make          → build the C++ engine (Linux/macOS)
#   make test     → run full Python test suite
#   make demo     → run agent in mock nominal mode (no engine, no API key)
#   make demo-critical → demo under simulated critical pressure
#   make clean    → remove build artefacts
#   make run-all  → start engine + agent together

CC       = g++
CFLAGS   = -O2 -std=c++17 -pthread -Wall -Wextra
SRC      = engine/sentinel_engine.cpp
BIN      = engine/sentinel_engine
LOGDIR   = logs
LOGFILE  = $(LOGDIR)/sentinel_redo.log

.PHONY: all build test demo demo-warn demo-critical run-engine \
        run-agent run-dashboard clean help

all: build

## ── C++ Engine ───────────────────────────────────────────────────────────────

build: $(LOGDIR)
	@echo "  Building sentinel engine..."
	$(CC) $(CFLAGS) $(SRC) -o $(BIN)
	@echo "  ✓ Built: $(BIN)"

$(LOGDIR):
	mkdir -p $(LOGDIR)

## ── Tests ────────────────────────────────────────────────────────────────────

test:
	@echo "  Running Sentinel test suite (56 tests)..."
	python3 -m pytest tests/test_sentinel.py -v --tb=short

test-quick:
	python3 -m pytest tests/test_sentinel.py -q

## ── Demo Modes (no engine required) ─────────────────────────────────────────

demo:
	@echo "  Starting Sentinel in NOMINAL mock mode..."
	@echo "  (No engine or API key needed — echo mode)"
	python3 agent/sentinel_agent.py --mock nominal

demo-warn:
	@echo "  Starting Sentinel in WARN mock mode..."
	python3 agent/sentinel_agent.py --mock warn

demo-critical:
	@echo "  Starting Sentinel in CRITICAL mock mode..."
	python3 agent/sentinel_agent.py --mock critical

dashboard:
	python3 dashboard/sentinel_dashboard.py --mock nominal

dashboard-critical:
	python3 dashboard/sentinel_dashboard.py --mock critical

## ── Live Mode (requires compiled engine) ─────────────────────────────────────

run-engine: build
	@echo "  Starting hardware telemetry engine..."
	$(BIN)

run-agent:
	@echo "  Starting AI agent (watching $(LOGFILE))..."
	@echo "  Set ANTHROPIC_API_KEY env var for live Claude responses."
	python3 agent/sentinel_agent.py --log $(LOGFILE)

## ── Utilities ────────────────────────────────────────────────────────────────

clean:
	rm -f $(BIN)
	rm -f $(LOGFILE)
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
	@echo "  ✓ Cleaned"

help:
	@echo ""
	@echo "  Project Sentinel — Make Targets"
	@echo "  ───────────────────────────────"
	@echo "  make              → build C++ engine"
	@echo "  make test         → run 45-test suite"
	@echo "  make demo         → REPL in nominal mock mode"
	@echo "  make demo-warn    → REPL in warn mock mode"
	@echo "  make demo-critical→ REPL in critical mock mode"
	@echo "  make dashboard    → live terminal dashboard (mock)"
	@echo "  make run-engine   → start live hardware poller"
	@echo "  make run-agent    → start live AI agent"
	@echo "  make clean        → remove artefacts"
	@echo ""

bench:
	python3 scripts/benchmark.py --iterations 10000

replay:
	python3 scripts/replay.py --scenario full_cycle --speed 2

api:
	@echo "  Starting REST API server on http://localhost:7474 (mock nominal)"
	python3 scripts/sentinel_api.py --mock nominal

test-stress:
	python3 -m pytest tests/test_stress.py -v --tb=short

test-unit:
	python3 -m pytest tests/test_sentinel.py -v --tb=short

open-dashboard:
	@echo "  Open dashboard/sentinel_web.html in your browser"
	@python3 -c "import webbrowser; webbrowser.open('dashboard/sentinel_web.html')" 2>/dev/null || true

monitor:
	@echo "  Starting all Sentinel components (engine + API + dashboard)..."
	python3 scripts/sentinel_monitor.py --mock nominal

monitor-critical:
	python3 scripts/sentinel_monitor.py --mock critical

monitor-live:
	@echo "  Starting with live hardware data..."
	python3 scripts/sentinel_monitor.py

test-skill:
	python3 -m pytest tests/test_openclaw_skill.py -v

evaluate:
	@echo "  Opening evaluation guide..."
	@cat docs/EVALUATION.md | head -40

setup:
	bash scripts/setup_unix.sh
