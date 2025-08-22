# Development and testing control for SSD IMS Home Assistant integration
.PHONY: help install test test-unit test-integration test-coverage lint format clean docker-up docker-down docker-logs docker-shell docker-restart

# Default target
help:
	@echo "SSD IMS Home Assistant Integration - Development Commands"
	@echo ""
	@echo "Development:"
	@echo "  install          Install development dependencies"
	@echo "  clean            Clean up generated files and caches"
	@echo "  format           Format code with black and isort"
	@echo "  lint             Run linting checks (flake8, mypy)"
	@echo "  lint-pylint      Run pylint checks (more lenient)"
	@echo ""
	@echo "Testing:"
	@echo "  test             Run all tests"
	@echo "  test-unit        Run unit tests only"
	@echo "  test-integration Run integration tests only"
	@echo "  test-coverage    Run tests with coverage report"
	@echo "  test-watch       Run tests in watch mode"
	@echo ""
	@echo "Docker:"
	@echo "  docker-up        Start Home Assistant container"
	@echo "  docker-down      Stop Home Assistant container"
	@echo "  docker-logs      Show container logs"
	@echo "  docker-shell     Open shell in container"
	@echo "  docker-restart   Restart container"
	@echo "  docker-lint      Run linting checks in Docker"
	@echo "  docker-pylint    Run pylint checks in Docker"
	@echo ""
	@echo "Deployment:"
	@echo "  build            Build integration package"
	@echo "  deploy           Deploy to Home Assistant"
	@echo "  validate         Validate integration configuration"

# Variables
PYTHON := python3
PIP := pip3
PYTEST := pytest
DOCKER_COMPOSE := docker compose
PROJECT_NAME := ssd_ims
TEST_DIR := tests
COVERAGE_DIR := htmlcov
DIST_DIR := dist

# Development dependencies
install:
	@echo "Installing development dependencies..."
	$(PIP) install -r requirements-test.txt
	$(PIP) install -e .
	@echo "Development environment ready!"

# Code formatting
format:
	@echo "Formatting code..."
	black custom_components/$(PROJECT_NAME)/ tests/
	isort custom_components/$(PROJECT_NAME)/ tests/
	@echo "Code formatting complete!"

# Linting with flake8 and mypy (strict)
lint:
	@echo "Running linting checks..."
	flake8 custom_components/$(PROJECT_NAME)/ tests/ --max-line-length=88 --extend-ignore=E203,W503
	mypy custom_components/$(PROJECT_NAME)/ --ignore-missing-imports
	@echo "Linting complete!"

# Linting with pylint (more lenient)
lint-pylint:
	@echo "Running pylint checks..."
	pylint custom_components/$(PROJECT_NAME)/ --rcfile=.pylintrc
	@echo "Pylint complete!"

# Docker-based linting
docker-lint:
	@echo "Running linting checks in Docker..."
	$(DOCKER_COMPOSE) run --rm dev
	@echo "Docker linting complete!"

# Docker-based pylint
docker-pylint:
	@echo "Running pylint checks in Docker..."
	$(DOCKER_COMPOSE) run --rm pylint
	@echo "Docker pylint complete!"

# Testing
test: test-unit test-integration

test-unit:
	@echo "Running unit tests..."
	$(PYTEST) $(TEST_DIR)/test_api_client.py $(TEST_DIR)/test_models.py -v --asyncio-mode=auto

test-integration:
	@echo "Running integration tests..."
	$(PYTEST) $(TEST_DIR)/test_coordinator.py $(TEST_DIR)/test_config_flow.py $(TEST_DIR)/test_sensor.py -v --asyncio-mode=auto

test-coverage:
	@echo "Running tests with coverage..."
	$(PYTEST) $(TEST_DIR)/ --cov=custom_components/$(PROJECT_NAME) --cov-report=html --cov-report=term-missing -v --asyncio-mode=auto
	@echo "Coverage report generated in $(COVERAGE_DIR)/"

test-watch:
	@echo "Running tests in watch mode..."
	$(PYTEST) $(TEST_DIR)/ -v --asyncio-mode=auto -f

# Docker commands
docker-up:
	@echo "Starting Home Assistant container..."
	$(DOCKER_COMPOSE) up -d
	@echo "Container started. Access at http://localhost:8123"

docker-down:
	@echo "Stopping Home Assistant container..."
	$(DOCKER_COMPOSE) down
	@echo "Container stopped"

docker-logs:
	@echo "Showing container logs..."
	$(DOCKER_COMPOSE) logs -f homeassistant

docker-shell:
	@echo "Opening shell in container..."
	$(DOCKER_COMPOSE) exec homeassistant /bin/bash

docker-restart:
	@echo "Restarting container..."
	$(DOCKER_COMPOSE) restart homeassistant
	@echo "Container restarted"

# Cleanup
clean:
	@echo "Cleaning up..."
	rm -rf $(COVERAGE_DIR)/
	rm -rf $(DIST_DIR)/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf __pycache__/
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	@echo "Cleanup complete!"

# Build and deployment
build:
	@echo "Building integration package..."
	mkdir -p $(DIST_DIR)
	cp -r custom_components/$(PROJECT_NAME) $(DIST_DIR)/
	@echo "Package built in $(DIST_DIR)/"

deploy: build
	@echo "Deploying to Home Assistant..."
	cp -r $(DIST_DIR)/$(PROJECT_NAME) config/custom_components/
	@echo "Deployment complete! Restart Home Assistant to load changes."

validate:
	@echo "Validating integration configuration..."
	$(PYTHON) -m custom_components.$(PROJECT_NAME).__init__
	@echo "Validation complete!"

# Development workflow shortcuts
dev-setup: install docker-up
	@echo "Development environment setup complete!"

dev-test: format lint test
	@echo "Development testing complete!"

dev-deploy: dev-test deploy docker-restart
	@echo "Development deployment complete!"

# Quick development cycle
quick: format lint test-unit docker-restart
	@echo "Quick development cycle complete!"

# Full development cycle
full: clean install format lint test docker-up
	@echo "Full development cycle complete!"
