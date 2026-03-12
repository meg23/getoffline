.PHONY: build run test clean

BUILD_DIR := target
BUILD_OUTPUT := $(BUILD_DIR)/getoffline
SRC_DIR := $(PWD)/src
REQ_FILE := $(SRC_DIR)/requirements.txt

build:
	@echo "Building $(BUILD_OUTPUT) with pex (verbose)..."
	@mkdir -p $(BUILD_DIR)
	pex --sources-directory=$(SRC_DIR) \
	    -r $(REQ_FILE) \
	    -o $(BUILD_OUTPUT) \
	    -m main \
	    --venv append \
	    -v

run: build
	@echo "Running $(BUILD_OUTPUT)..."
	./$(BUILD_OUTPUT)


test:
	@echo "Running unit tests..."
	PYTHONPATH=$(SRC_DIR) python -m unittest discover -s tests -p "test_*.py" -v

clean:
	@echo "Removing generated target directory and Python bytecode..."
	rm -rf $(BUILD_DIR)
	find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
