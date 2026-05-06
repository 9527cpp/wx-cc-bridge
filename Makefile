.PHONY: sync echo run onboard install-service uninstall-service restart-service status logs

PLIST_LABEL    := com.wx-cc-bridge
PLIST_TEMPLATE := scripts/wx-cc-bridge.plist.template
PLIST_DST      := $(HOME)/Library/LaunchAgents/$(PLIST_LABEL).plist

SERVICE_LABEL  := wx-cc-bridge
SERVICE_TEMPLATE := scripts/wx-cc-bridge.service.template
SYSTEMD_USER_DIR := $(HOME)/.config/systemd/user
SERVICE_DST    := $(SYSTEMD_USER_DIR)/$(SERVICE_LABEL).service

LOG_DIR        := $(HOME)/.local/log/wx-cc-bridge

PYTHON := python3
PYTHONPATH := src

sync:
	pip3 install httpx qrcode

echo:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m wx_cc_bridge.echo

run:
	PYTHONPATH=$(PYTHONPATH) PYTHONUNBUFFERED=1 $(PYTHON) -m wx_cc_bridge.bridge --onboard

onboard:
	PYTHONPATH=$(PYTHONPATH) PYTHONUNBUFFERED=1 $(PYTHON) -m wx_cc_bridge.bridge --onboard

install-service:
	@mkdir -p $(LOG_DIR) 2>/dev/null || true
	@# macOS
	@if command -v launchctl >/dev/null 2>&1; then \
		mkdir -p $(HOME)/Library/LaunchAgents 2>/dev/null || true; \
		sed -e 's|{{REPO}}|$(CURDIR)|g' -e 's|{{HOME}}|$(HOME)|g' $(PLIST_TEMPLATE) > $(PLIST_DST) 2>/dev/null || true; \
		launchctl unload $(PLIST_DST) 2>/dev/null || true; \
		launchctl load -w $(PLIST_DST); \
		echo "✓ installed (launchd)"; \
	elif command -v systemctl >/dev/null 2>&1; then \
		CLAUDE_BIN_PATH="$$(command -v claude || true)"; \
		if [ -n "$$CLAUDE_BIN_PATH" ]; then CLAUDE_BIN_PATH="$$(readlink -f "$$CLAUDE_BIN_PATH")"; else CLAUDE_BIN_PATH="claude"; fi; \
		NODE_BIN_PATH="$$(command -v node || true)"; \
		if [ -n "$$NODE_BIN_PATH" ]; then NODE_BIN_PREFIX="$$(dirname "$$(readlink -f "$$NODE_BIN_PATH")"):"; else NODE_BIN_PREFIX=""; fi; \
		mkdir -p $(SYSTEMD_USER_DIR) 2>/dev/null || true; \
		sed -e 's|{{REPO}}|$(CURDIR)|g' -e 's|{{LOG_DIR}}|$(LOG_DIR)|g' -e 's|{{PYTHON}}|$(shell readlink -f $(shell which python3))|g' -e 's|{{HOME}}|$(HOME)|g' -e "s|{{CLAUDE_BIN}}|$$CLAUDE_BIN_PATH|g" -e "s|{{NODE_BIN_PREFIX}}|$$NODE_BIN_PREFIX|g" $(SERVICE_TEMPLATE) > $(SERVICE_DST); \
		test -s $(SERVICE_DST); \
		systemctl --user daemon-reload; \
		systemctl --user enable --now $(SERVICE_LABEL); \
		echo "✓ installed (systemd --user)"; \
		echo "  提示：若需退出登录后继续运行，请执行一次：sudo loginctl enable-linger $$USER"; \
	else \
		echo "✗ no service manager found (install-service requires launchctl or systemctl)"; \
	fi

uninstall-service:
	@# macOS
	@if command -v launchctl >/dev/null 2>&1; then \
		launchctl unload $(PLIST_DST) 2>/dev/null || true; \
		rm -f $(PLIST_DST); \
		echo "✓ uninstalled (launchd)"; \
	elif command -v systemctl >/dev/null 2>&1; then \
		systemctl --user disable --now $(SERVICE_LABEL) 2>/dev/null || true; \
		rm -f $(SERVICE_DST); \
		systemctl --user daemon-reload; \
		echo "✓ uninstalled (systemd --user)"; \
	else \
		echo "✗ no service manager found"; \
	fi

restart-service:
	@# macOS
	@if command -v launchctl >/dev/null 2>&1; then \
		launchctl kickstart -k gui/$(shell id -u)/$(PLIST_LABEL); \
		echo "✓ restarted (launchd)"; \
	elif command -v systemctl >/dev/null 2>&1; then \
		systemctl --user restart $(SERVICE_LABEL); \
		echo "✓ restarted (systemd --user)"; \
	else \
		echo "✗ no service manager found"; \
	fi

status:
	@# macOS
	@if command -v launchctl >/dev/null 2>&1; then \
		launchctl list | grep $(PLIST_LABEL) || echo "(not running)"; \
	elif command -v systemctl >/dev/null 2>&1; then \
		systemctl --user status $(SERVICE_LABEL) || echo "(not running)"; \
	else \
		echo "(no service manager available)"; \
	fi

logs:
	@tail -n 50 -f $(LOG_DIR)/bridge.log $(LOG_DIR)/bridge.err.log