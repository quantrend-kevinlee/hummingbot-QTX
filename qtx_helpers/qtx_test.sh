#!/bin/bash

# QTX Perpetual Test Runner - Shell Wrapper
# Simple wrapper around the Python test runner for convenience

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run the Python test runner with all arguments passed through
python "${SCRIPT_DIR}/run_qtx_tests.py" "$@" 