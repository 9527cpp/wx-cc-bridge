.PHONY: sync echo run install-service uninstall-service restart-service status logs

PLIST_LABEL    := com.wx-cc-bridge
PLIST_TEMPLATE := scripts/wx-cc-bridge.plist.template
PLIST_DST      := $(HOME)/Library/LaunchAgents/$(PLIST_LABEL).plist

SERVICE_LABEL  := wx-cc-bridge
SERVICE_TEMPLATE := scripts/wx-cc-bridge.service.template
SERVICE_DST    := /etc/systemd/system/$(SERVICE_LABEL).service

LOG_DIR        := /home/rock5b/.local/log/wx-cc-bridge

PYTHON := python3
PYTHONPATH := src

sync:
	pip3 install httpx qrcode

echo:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m wx_cc_bridge.echo

run:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m wx_cc_bridge.bridge

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
		sed -e 's|{{REPO}}|$(CURDIR)|g' -e 's|{{LOG_DIR}}|$(LOG_DIR)|g' -e 's|{{PYTHON}}|$(shell readlink -f $(shell which python3))|g' $(SERVICE_TEMPLATE) > $(SERVICE_DST) 2>/dev/null || true; \
		systemctl daemon-reload; \
		systemctl enable --now $(SERVICE_LABEL); \
		echo "✓ installed (systemd)"; \
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
		systemctl disable --now $(SERVICE_LABEL) 2>/dev/null || true; \
		rm -f $(SERVICE_DST); \
		systemctl daemon-reload; \
		echo "✓ uninstalled (systemd)"; \
	else \
		echo "✗ no service manager found"; \
	fi

restart-service:
	@# macOS
	@if command -v launchctl >/dev/null 2>&1; then \
		launchctl kickstart -k gui/$(shell id -u)/$(PLIST_LABEL); \
		echo "✓ restarted (launchd)"; \
	elif command -v systemctl >/dev/null 2>&1; then \
		systemctl restart $(SERVICE_LABEL); \
		echo "✓ restarted (systemd)"; \
	else \
		echo "✗ no service manager found"; \
	fi

status:
	@# macOS
	@if command -v launchctl >/dev/null 2>&1; then \
		launchctl list | grep $(PLIST_LABEL) || echo "(not running)"; \
	elif command -v systemctl >/dev/null 2>&1; then \
		systemctl status $(SERVICE_LABEL) || echo "(not running)"; \
	else \
		echo "(no service manager available)"; \
	fi

logs:
	@tail -n 50 -f $(LOG_DIR)/bridge.log $(LOG_DIR)/bridge.err.log