.PHONY: install start stop restart backend frontend tunnel \
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
# Name given to `cloudflared tunnel create`
TUNNEL_NAME  := forge

# Absorb service-selector targets so `make start backend` etc. are valid.
backend frontend tunnel: ;

# Services to act on: those named in the goal list, or all three if none.
_svcs = $(or $(filter backend frontend tunnel,$(MAKECMDGOALS)),backend frontend tunnel)

# Port-based running check — immune to pgrep/pkill self-match issues.
_port_up = ss -tlnp 2>/dev/null | grep -q ':$(1)'

# ── status ────────────────────────────────────────────────────────────────────
status:
	@echo ""
	@if $(call _port_up,8000); then \
		pid=$$(cat $(BACKEND_PID) 2>/dev/null || echo "?"); \
		echo "  backend   ● running   http://localhost:8000   pid=$$pid"; \
	else \
		echo "  backend   ○ stopped"; \
	fi
	@if $(call _port_up,5173); then \
		pid=$$(cat $(FRONTEND_PID) 2>/dev/null || echo "?"); \
		echo "  frontend  ● running   http://localhost:5173   pid=$$pid"; \
	else \
		echo "  frontend  ○ stopped"; \
	fi
	@if pgrep -f "[c]loudflared tunnel run" >/dev/null 2>&1; then \
		pid=$$(cat $(TUNNEL_PID) 2>/dev/null || echo "?"); \
		echo "  tunnel    ● running   $(TUNNEL_NAME)   pid=$$pid"; \
	else \
		echo "  tunnel    ○ stopped"; \
	fi
	@echo ""

# ── dependencies ──────────────────────────────────────────────────────────────
install:
	$(PIP) install -r requirements.txt --user -q
	cd frontend && npm install

# ── start ─────────────────────────────────────────────────────────────────────
start:
	@for svc in $(_svcs); do $(MAKE) --no-print-directory start-$$svc; done

# setsid puts each service in its own process group (PGID = PID).
# Killing -PGID on stop propagates to every descendant (uvicorn workers, node/vite).
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

start-tunnel:
	@if pgrep -f "[c]loudflared tunnel run" >/dev/null 2>&1; then \
		echo "tunnel already running (pid $$(pgrep -f '[c]loudflared tunnel run' | head -1))"; \
	else \
		setsid sh -c 'exec cloudflared tunnel run $(TUNNEL_NAME)' \
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
		kill -- -"$$pid" 2>/dev/null; rm -f $(BACKEND_PID); echo "backend stopped"; \
	else \
		rm -f $(BACKEND_PID); echo "backend not running"; \
	fi

stop-frontend:
	@if $(call _port_up,5173); then \
		pid=$$(cat $(FRONTEND_PID) 2>/dev/null); \
		kill -- -"$$pid" 2>/dev/null; rm -f $(FRONTEND_PID); echo "frontend stopped"; \
	else \
		rm -f $(FRONTEND_PID); echo "frontend not running"; \
	fi

stop-tunnel:
	@if pgrep -f "[c]loudflared tunnel run" >/dev/null 2>&1; then \
		pid=$$(cat $(TUNNEL_PID) 2>/dev/null); \
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
