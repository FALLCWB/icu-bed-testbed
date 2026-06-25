# icu-bed-testbed — digital-twin + IEEE 11073 SDC testbed.
#
# Validated physiology (Pulse Physiology Engine) drives ventilator and patient
# monitor digital twins exposed as SDC providers; an unauthenticated SDC
# consumer performs the silent-kill attack. Prerequisites: Docker Engine 24+
# (compose v2), make, and uv (for the figure generator).

SHELL := /bin/bash

.PHONY: help up down demo figure smoke-test clean

help:
	@echo "icu-bed-testbed targets:"
	@echo "  make up           build + start the full stack"
	@echo "  make demo         reset, run the silent-kill over SDC, regenerate the figure"
	@echo "  make figure       regenerate the true-vs-displayed figure from the timeline log"
	@echo "  make smoke-test   check the stack is up and both device twins are reachable over SDC"
	@echo "  make down         stop the stack"
	@echo "  make clean        down + remove run artefacts"
	@echo ""
	@echo "  Views once up:  room     http://localhost:8082   (ground truth)"
	@echo "                  monitor  http://localhost:8083   (bedside monitor, via SDC)"
	@echo "                  attacker http://localhost:8090   (attack console)"

up:
	docker compose up -d --build
	@echo "Up. room=http://localhost:8082  monitor=http://localhost:8083  attacker=http://localhost:8090"

down:
	docker compose down

# Reset to baseline, launch the silent-kill via the SDC attacker, wait for the
# (validated) physiology to reach the irreversible state, then render the figure.
# Capture both canonical scenarios (silent-kill + reversibility control) into
# avaliacao/canonical/, then render the headline figure from the committed log.
demo:
	@python3 scripts/capture_runs.py --host localhost
	@$(MAKE) figure

figure:
	@uv run --with matplotlib python scripts/make_figure.py --log avaliacao/canonical/silent_kill.jsonl --out figures/timeline.pdf
	@echo "Figure written to figures/timeline.pdf"

smoke-test:
	@curl -sf http://localhost:8090/healthz | python3 -c "import sys,json;d=json.load(sys.stdin);assert d.get('vent'),'ventilator twin not reachable over SDC';print('OK: ventilator twin reachable over SDC; rogue_active=%s'%d.get('rogue_active'))"

clean:
	docker compose down -v --remove-orphans
	@echo "Stack down. To remove run artefacts: rm -f avaliacao/*.jsonl"
