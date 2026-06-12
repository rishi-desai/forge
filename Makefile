.PHONY: install start stop restart status backend frontend tunnel \
        start-backend start-frontend start-tunnel \
        stop-backend stop-frontend stop-tunnel \
        restart-backend restart-frontend restart-tunnel

BACKEND_PID  := /tmp/forge-backend.pid
FRONTEND_PID := /tmp/forge-frontend.pid
TUNNEL_PID   := /tmp/forge-tunnel.pid
BACKEND_LOG  := /tmp/forge-backend.log
FRONTEND_LOG := /tmp/forge-frontend.log
TUNNEL_LOG   := /tmp/forge-tunnel.log
PIP          := $(HOME)/.local/bin/pip
UVICORN      := $(HOME)/.local/bin/uvicorn
TUNNEL_NAME  := forge

# Absorb service-selector targets so `make start backend` etc. are valid.
backend frontend tunnel: ;

# Services to act on: those named in the goal list, or all three if none.
_svcs = $(or $(filter backend frontend tunnel,$(MAKECMDGOALS)),backend frontend tunnel)

# True if something is listening on :PORT (port-based, no pgrep needed).
_port_up = ss -tlnp 2>/dev/null | grep -q ':$(1)'

# PID listening on :PORT (fallback when pid file is stale/missing).
_port_pid = ss -tlnp 2>/dev/null | grep ':$(1)' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2

# Elapsed time for PID from the system process table.
_etime = ps -o etime= -p "$(1)" 2>/dev/null | tr -d ' '

# ── status ────────────────────────────────────────────────────────────────────
status:
	@echo ""
	@if $(call _port_up,8000); then \
		pid=$$(cat $(BACKEND_PID) 2>/dev/null); \
		if [ -z "$$pid" ] || ! kill -0 "$$pid" 2>/dev/null; then \
			pid=$$($(call _port_pid,8000)); \
		fi; \
		up=$$($(call _etime,$$pid)); \
		printf "  backend   \033[32m●\033[0m running   http://localhost:8000   up %-9s pid=%s\n" "$$up" "$$pid"; \
	else \
		printf "  backend   \033[2m○\033[0m stopped\n"; \
	fi
	@if $(call _port_up,5173); then \
		pid=$$(cat $(FRONTEND_PID) 2>/dev/null); \
		if [ -z "$$pid" ] || ! kill -0 "$$pid" 2>/dev/null; then \
			pid=$$($(call _port_pid,5173)); \
		fi; \
		up=$$($(call _etime,$$pid)); \
		printf "  frontend  \033[32m●\033[0m running   http://localhost:5173    up %-9s pid=%s\n" "$$up" "$$pid"; \
	else \
		printf "  frontend  \033[2m○\033[0m stopped\n"; \
	fi
	@pid=$$(cat $(TUNNEL_PID) 2>/dev/null); \
	if [ -n "$$pid" ] && kill -0 "$$pid" 2>/dev/null; then \
		up=$$($(call _etime,$$pid)); \
		printf "  tunnel    \033[32m●\033[0m running   %-26s up %-9s pid=%s\n" "$(TUNNEL_NAME)" "$$up" "$$pid"; \
	else \
		printf "  tunnel    \033[2m○\033[0m stopped\n"; \
	fi
	@echo ""

# ── dependencies ──────────────────────────────────────────────────────────────
install:
	$(PIP) install -r requirements.txt --user -q
	cd frontend && npm install

# ── start ─────────────────────────────────────────────────────────────────────
start:
	@for svc in $(_svcs); do $(MAKE) --no-print-directory start-$$svc; done

# setsid puts each service in its own process group so kill -PGID tears down
# all descendants (uvicorn workers, npm→sh→node/vite, cloudflared children).
start-backend:
	@if $(call _port_up,8000); then \
		echo "backend already running (port 8000)"; \
	else \
		setsid sh -c 'cd backend && exec $(UVICORN) app:app --port 8000 --reload' \
			>>$(BACKEND_LOG) 2>&1 & \
		echo $$! > $(BACKEND_PID); \
		echo "backend  → http://localhost:8000   log: $(BACKEND_LOG)"; \
	fi

start-frontend:
	@if $(call _port_up,5173); then \
		echo "frontend already running (port 5173)"; \
	else \
		setsid sh -c 'cd frontend && exec npm run dev' >>$(FRONTEND_LOG) 2>&1 & \
		echo $$! > $(FRONTEND_PID); \
		echo "frontend → http://localhost:5173   log: $(FRONTEND_LOG)"; \
	fi

# Tunnel has no port to probe — use pid file + kill -0 to check liveness.
# (pgrep -f on the tunnel name self-matches the recipe shell and always
# returns "already running", so we avoid it entirely here.)
start-tunnel:
	@pid=$$(cat $(TUNNEL_PID) 2>/dev/null); \
	if [ -n "$$pid" ] && kill -0 "$$pid" 2>/dev/null; then \
		echo "tunnel already running (pid $$pid)"; \
	else \
		setsid sh -c 'exec cloudflared --config cloudflared.yml tunnel run $(TUNNEL_NAME)' \
			>>$(TUNNEL_LOG) 2>&1 & \
		echo $$! > $(TUNNEL_PID); \
		echo "tunnel   → cloudflared tunnel run $(TUNNEL_NAME)   log: $(TUNNEL_LOG)"; \
	fi

# ── stop ──────────────────────────────────────────────────────────────────────
stop:
	@for svc in $(_svcs); do $(MAKE) --no-print-directory stop-$$svc; done

stop-backend:
	@if $(call _port_up,8000); then \
		pid=$$(cat $(BACKEND_PID) 2>/dev/null); \
		[ -z "$$pid" ] && pid=$$($(call _port_pid,8000)); \
		kill -- -"$$pid" 2>/dev/null; rm -f $(BACKEND_PID); echo "backend stopped"; \
	else \
		rm -f $(BACKEND_PID); echo "backend not running"; \
	fi

stop-frontend:
	@if $(call _port_up,5173); then \
		pid=$$(cat $(FRONTEND_PID) 2>/dev/null); \
		[ -z "$$pid" ] && pid=$$($(call _port_pid,5173)); \
		kill -- -"$$pid" 2>/dev/null; rm -f $(FRONTEND_PID); echo "frontend stopped"; \
	else \
		rm -f $(FRONTEND_PID); echo "frontend not running"; \
	fi

stop-tunnel:
	@pid=$$(cat $(TUNNEL_PID) 2>/dev/null); \
	if [ -n "$$pid" ] && kill -0 "$$pid" 2>/dev/null; then \
		kill -- -"$$pid" 2>/dev/null; rm -f $(TUNNEL_PID); echo "tunnel stopped"; \
	else \
		rm -f $(TUNNEL_PID); echo "tunnel not running"; \
	fi

# ── restart ───────────────────────────────────────────────────────────────────
restart:
	@for svc in $(_svcs); do $(MAKE) --no-print-directory restart-$$svc; done

restart-backend:
	@$(MAKE) --no-print-directory stop-backend; \
	i=10; while $(call _port_up,8000) && [ $$i -gt 0 ]; do sleep 0.5; i=$$((i-1)); done; \
	$(MAKE) --no-print-directory start-backend

restart-frontend:
	@$(MAKE) --no-print-directory stop-frontend; \
	i=10; while $(call _port_up,5173) && [ $$i -gt 0 ]; do sleep 0.5; i=$$((i-1)); done; \
	$(MAKE) --no-print-directory start-frontend

restart-tunnel:
	@$(MAKE) --no-print-directory stop-tunnel
	@$(MAKE) --no-print-directory start-tunnel
