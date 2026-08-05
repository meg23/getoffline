.PHONY: test integration-test integration-test-youtube integration-test-podcast registry-up registry-build registry-push registry-release compose-build compose-up test-compile test-ruff test-mccabe test-mypy test-bandit test-vulture test-wapiti test-wapiti-public test-wapiti-auth test-coverage clean venv migrate-db collectstatic run-app run-app-debug run-worker-updates run-worker-downloader-youtube run-worker-downloader-podcast run-worker-transcripts run-worker-cleanup run-scheduler

APP_NAME := GetOffline
BUILD_DIR := target
BUILD_OUTPUT := $(BUILD_DIR)/$(APP_NAME)
STATIC_ROOT := $(PWD)/.staticfiles
SRC_DIR := $(PWD)/src
REQ_FILE := $(SRC_DIR)/requirements.txt
CI_REQ_FILE := requirements-ci.txt
VENV_DIR := .venv
VENV_BIN := $(VENV_DIR)/bin
PYTHON := $(VENV_BIN)/python
PIP := $(VENV_BIN)/pip
PEX := $(VENV_BIN)/pex
RUFF := $(VENV_BIN)/ruff
RUFF_STRICT_SELECT := I,PIE810,PLR0402,TRY401,RUF013,RUF046,RUF100,B010,BLE001,EXE001,PLR1730,PYI034,PYI041,RUF012,SIM103,SIM117,UP012,UP037
VULTURE := $(VENV_BIN)/vulture
BANDIT := $(VENV_BIN)/bandit
MYPY := $(VENV_BIN)/mypy
WAPITI := $(VENV_BIN)/wapiti
COVERAGE := $(VENV_BIN)/coverage
MCCABE_MAX_COMPLEXITY := 60
MCCABE_MIN_COMPLEXITY := 61
CI_TOOLS := bandit coverage mccabe mypy pex vulture wapiti3
WAPITI_REPORT_DIR := $(BUILD_DIR)/wapiti
WAPITI_FRONTEND_URL ?= http://127.0.0.1:8080
WAPITI_API_URL ?= http://127.0.0.1:8081/api/library
WAPITI_PUBLIC_API_URL ?= http://127.0.0.1:8081/api/health
WAPITI_AUTH_URL ?= $(WAPITI_FRONTEND_URL)/login/
WAPITI_FORMAT ?= html
WAPITI_STATE_DIR ?= $(WAPITI_REPORT_DIR)/.state
WAPITI_OPTIONS ?= --no-bugreport --scope folder --flush-session --tasks 1 --depth 2 --timeout 10 --max-attack-time 5 --max-scan-time 120 --store-session $(WAPITI_STATE_DIR)/sessions --store-config $(WAPITI_STATE_DIR)/config
COMPOSE_RUNTIME := docker compose -f stacks/docker-compose.yml
COMPOSE_BUILD := $(COMPOSE_RUNTIME) -f stacks/docker-compose.build.yml
APP_SERVICES := frontend api worker-updates worker-downloader-youtube worker-downloader-podcast worker-ffmpeg worker-transcripts scheduler worker-cleanup

registry-up:
	$(COMPOSE_RUNTIME) up -d registry

registry-build: registry-up
	$(COMPOSE_BUILD) build $(APP_SERVICES)

registry-push: registry-build
	$(COMPOSE_BUILD) push $(APP_SERVICES)

registry-release: registry-push
	@echo "Published GetOffline images to $${GETOFFLINE_IMAGE_REGISTRY:-localhost:5000} with tag $${GETOFFLINE_IMAGE_TAG:-latest}."

compose-build: registry-release

compose-up:
	$(COMPOSE_RUNTIME) up -d
TEST_ENV := PYTHONPATH=$(SRC_DIR) GETOFFLINE_DB_ENGINE=sqlite GETOFFLINE_DB_NAME=":memory:" GETOFFLINE_MODEL_CACHE_DIR=$(PWD)/.test-model-cache GETOFFLINE_LOG_FILE=$(PWD)/.test-model-cache/youtube_batch_dl.log
PY_FILES := $(shell find src tests crons -type f -name '*.py' -not -path '*/build/*' -not -path '*/__pycache__/*')
SOURCE_PY_FILES := $(shell find src crons -type f -name '*.py' -not -path '*/build/*' -not -path '*/__pycache__/*')

venv: $(VENV_DIR)/.installed

$(VENV_DIR)/.installed: $(REQ_FILE) $(CI_REQ_FILE) Makefile
	@echo "Creating virtual environment for $(APP_NAME)..."
	python3 -m venv $(VENV_DIR)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r $(REQ_FILE) -r $(CI_REQ_FILE) $(CI_TOOLS)
	@touch $@

test: test-compile test-ruff test-mccabe test-mypy test-bandit test-vulture test-coverage test-wapiti
	@echo "All scans and unit tests passed."

integration-test: venv
	@echo "Running combined Docker Compose integration test..."
	GETOFFLINE_COMPOSE_VARIANT=original PYTHONPATH=$(SRC_DIR) $(PYTHON) tests/integration/test_compose_pipeline.py
	GETOFFLINE_COMPOSE_VARIANT=stacks PYTHONPATH=$(SRC_DIR) $(PYTHON) tests/integration/test_compose_pipeline.py

integration-test-youtube: venv
	@echo "Running Docker Compose YouTube integration test..."
	GETOFFLINE_COMPOSE_VARIANT=original PYTHONPATH=$(SRC_DIR) $(PYTHON) tests/integration/test_youtube_pipeline.py
	GETOFFLINE_COMPOSE_VARIANT=stacks PYTHONPATH=$(SRC_DIR) $(PYTHON) tests/integration/test_youtube_pipeline.py

integration-test-podcast: venv
	@echo "Running Docker Compose podcast integration test..."
	GETOFFLINE_COMPOSE_VARIANT=original PYTHONPATH=$(SRC_DIR) $(PYTHON) tests/integration/test_podcast_source_pipeline.py
	GETOFFLINE_COMPOSE_VARIANT=stacks PYTHONPATH=$(SRC_DIR) $(PYTHON) tests/integration/test_podcast_source_pipeline.py

test-compile: venv
	@echo "Compiling Python files..."
	$(PYTHON) -m py_compile $(PY_FILES)

test-ruff: venv
	@echo "Running Ruff linting..."
	$(RUFF) check --extend-select $(RUFF_STRICT_SELECT) src tests crons

test-mccabe: venv
	@echo "Running McCabe complexity checks..."
	@output="$$($(PYTHON) -m mccabe --min $(MCCABE_MIN_COMPLEXITY) $(PY_FILES))"; \
	if [ -n "$$output" ]; then \
		echo "$$output"; \
		echo "McCabe complexity exceeds $(MCCABE_MAX_COMPLEXITY)."; \
		exit 1; \
	fi

test-mypy: venv
	@echo "Running mypy static type checks..."
	PYTHONPATH=$(SRC_DIR) $(MYPY) src tests crons

test-bandit: venv
	@echo "Running Bandit security checks..."
	$(BANDIT) -c pyproject.toml $(SOURCE_PY_FILES)

test-vulture: venv
	@echo "Running Vulture dead-code analysis..."
	$(VULTURE) src tests crons --min-confidence 100

test-wapiti: test-wapiti-auth

test-wapiti-public: venv
	@echo "Starting frontend and API services for Wapiti..."
	$(COMPOSE_BUILD) up -d --build --wait frontend api
	@mkdir -p $(WAPITI_REPORT_DIR) $(WAPITI_STATE_DIR)/sessions $(WAPITI_STATE_DIR)/config
	@echo "Scanning frontend: $(WAPITI_FRONTEND_URL)"
	$(WAPITI) -u "$(WAPITI_FRONTEND_URL)" $(WAPITI_OPTIONS) --format $(WAPITI_FORMAT) -o "$(WAPITI_REPORT_DIR)/frontend-public.$(WAPITI_FORMAT)"
	@echo "Scanning API: $(WAPITI_PUBLIC_API_URL)"
	$(WAPITI) -u "$(WAPITI_PUBLIC_API_URL)" $(WAPITI_OPTIONS) --format $(WAPITI_FORMAT) -o "$(WAPITI_REPORT_DIR)/api-public.$(WAPITI_FORMAT)"
	@echo "Wapiti reports written to $(WAPITI_REPORT_DIR)/"

test-wapiti-auth: venv
	@echo "Starting frontend and API services for authenticated Wapiti scan..."
	$(COMPOSE_BUILD) up -d --build --wait frontend api
	@mkdir -p $(WAPITI_REPORT_DIR) $(WAPITI_STATE_DIR)/sessions $(WAPITI_STATE_DIR)/config
	WAPITI_BIN="$(WAPITI)" \
	REPORT_DIR="$(WAPITI_REPORT_DIR)" \
	FRONTEND_URL="$(WAPITI_FRONTEND_URL)" \
	API_URL="$(WAPITI_API_URL)" \
	AUTH_URL="$(WAPITI_AUTH_URL)" \
	REPORT_FORMAT="$(WAPITI_FORMAT)" \
	WAPITI_OPTIONS="$(WAPITI_OPTIONS)" \
	COMPOSE_FILE="$(PWD)/stacks/docker-compose.yml:$(PWD)/stacks/docker-compose.build.yml" \
	$(SHELL) scripts/wapiti-authenticated-scan.sh
	@echo "Authenticated Wapiti reports written to $(WAPITI_REPORT_DIR)/"

test-coverage: venv
	@echo "Running unit tests with coverage..."
	$(TEST_ENV) $(COVERAGE) run --source=src -m unittest discover -s tests -p 'test_*.py' -v
	$(COVERAGE) report --show-missing

clean:
	@echo "Removing generated artifacts, virtual environment, and Python bytecode..."
	rm -rf $(BUILD_DIR) $(VENV_DIR) $(STATIC_ROOT) .coverage .test-model-cache
	find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +

migrate-db: venv
	@echo "Migrating split Django/MySQL database..."
	PYTHONPATH=$(SRC_DIR) DJANGO_SETTINGS_MODULE=frontend.settings $(PYTHON) -m django migrate --run-syncdb
	PYTHONPATH=$(SRC_DIR) DJANGO_SETTINGS_MODULE=frontend.settings $(PYTHON) -m django sync_model_schema

collectstatic: venv
	@echo "Collecting cache-busted static assets..."
	PYTHONPATH=$(SRC_DIR) DJANGO_SETTINGS_MODULE=frontend.settings GETOFFLINE_STATIC_ROOT=$(STATIC_ROOT) GETOFFLINE_STATIC_MANIFEST=1 $(PYTHON) -m django collectstatic --noinput

run-app: venv collectstatic
	@echo "Running Django frontend..."
	PYTHONPATH=$(SRC_DIR) GETOFFLINE_STATIC_ROOT=$(STATIC_ROOT) GETOFFLINE_STATIC_MANIFEST=1 $(PYTHON) -m frontend

run-app-debug: venv collectstatic
	@echo "Running Django frontend app in debug mode..."
	PYTHONPATH=$(SRC_DIR) GETOFFLINE_STATIC_ROOT=$(STATIC_ROOT) GETOFFLINE_STATIC_MANIFEST=1 GETOFFLINE_DJANGO_DEBUG=1 $(PYTHON) -m frontend

run-worker-updates: venv
	@echo "Running single-concurrency updates worker..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m workers updates

run-worker-downloader-youtube: venv
	@echo "Running single-concurrency YouTube downloader worker..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m workers downloader-youtube

run-worker-downloader-podcast: venv
	@echo "Running podcast downloader worker..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m workers downloader-podcast

run-worker-transcripts: venv
	@echo "Running parallel transcript worker..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m workers transcripts --prefetch $${PREFETCH:-4}

run-worker-cleanup: venv
	@echo "Running cleanup worker..."
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m workers cleanup

run-scheduler: venv
	@echo "Running scheduler..."
	PYTHONPATH=$(SRC_DIR) DJANGO_SETTINGS_MODULE=frontend.settings $(PYTHON) -m django run_scheduler --loop --install-defaults
