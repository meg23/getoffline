.PHONY: build run run-no-pex test clean check-system-deps venv migrate-db run-app run-app-debug run-worker-downloads run-worker-sync run-worker-summaries

APP_NAME := GetOffline
BUILD_DIR := target
BUILD_OUTPUT := $(BUILD_DIR)/$(APP_NAME)
SRC_DIR := $(PWD)/src
REQ_FILE := $(SRC_DIR)/requirements.txt
VENV_DIR := .venv
VENV_BIN := $(VENV_DIR)/bin
PYTHON := $(VENV_BIN)/python
PIP := $(VENV_BIN)/pip
PEX := $(VENV_BIN)/pex

venv: $(VENV_BIN)/activate

$(VENV_BIN)/activate: $(REQ_FILE)
	@echo "Creating virtual environment for $(APP_NAME)..."
	python3 -m venv $(VENV_DIR)
	$(PIP) install --upgrade pip
	$(PIP) install -r $(REQ_FILE) pex
	@touch $(VENV_BIN)/activate

check-system-deps:
	@echo "Checking required system dependencies..."
	@command -v ffmpeg >/dev/null 2>&1 || { echo "Error: ffmpeg is required but not installed."; exit 1; }
	@command -v deno >/dev/null 2>&1 || { echo "Error: deno is required but not installed."; exit 1; }
	@echo "ffmpeg and deno are installed."

build: venv check-system-deps
	@echo "Building $(APP_NAME) at $(BUILD_OUTPUT) with pex..."
	@mkdir -p $(BUILD_DIR)
	$(PEX) --sources-directory=$(SRC_DIR) \
	    -r $(REQ_FILE) \
	    -o $(BUILD_OUTPUT) \
	    -m main \
	    --venv append \
	    -v

run: build
	@echo "Running $(APP_NAME) from $(BUILD_OUTPUT)..."
	./$(BUILD_OUTPUT)

run-no-pex: venv check-system-deps
	@echo "Running $(APP_NAME) directly with Python (no pex)..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m main

test: clean build
	@echo "Running unit tests in virtual environment..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m unittest discover -s tests -p "test_*.py" -v

clean:
	@echo "Removing generated artifacts and Python bytecode..."
	rm -rf $(BUILD_DIR) $(VENV_DIR)
	find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +

migrate-db: venv
	@echo "Migrating split Django/MySQL database..."
	PYTHONPATH=$(SRC_DIR) DJANGO_SETTINGS_MODULE=app.settings $(PYTHON) -m django migrate --run-syncdb

run-app: venv
	@echo "Running Django frontend app..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m app

run-app-debug: venv
	@echo "Running Django frontend app in debug mode..."
	PYTHONPATH=$(SRC_DIR) GETOFFLINE_DJANGO_DEBUG=1 $(PYTHON) -m app

run-worker-downloads: venv
	@echo "Running single-concurrency downloads worker..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m workers downloads

run-worker-sync: venv
	@echo "Running sync worker..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m workers sync

run-worker-summaries: venv
	@echo "Running summaries worker..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m workers summaries
