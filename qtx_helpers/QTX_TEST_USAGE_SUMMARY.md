# QTX Test Runner - Quick Usage Guide

## Available Scripts

1. **Python Script**: `python qtx_helpers/run_qtx_tests.py [options]`
2. **Shell Wrapper**: `./qtx_helpers/qtx_test.sh [options]` (same as Python script)

## Quick Commands

### List Available Tests
```bash
python qtx_helpers/run_qtx_tests.py --list
./qtx_helpers/qtx_test.sh --list
```

### Run All Tests
```bash
python qtx_helpers/run_qtx_tests.py
python qtx_helpers/run_qtx_tests.py all
./qtx_helpers/qtx_test.sh
```

### Run by Segment
```bash
# UDP tests (manager + edge cases)
python qtx_helpers/run_qtx_tests.py udp
./qtx_helpers/qtx_test.sh udp

# API tests
python qtx_helpers/run_qtx_tests.py api
./qtx_helpers/qtx_test.sh api

# Shared Memory tests
python qtx_helpers/run_qtx_tests.py shm
./qtx_helpers/qtx_test.sh shm

# Edge case tests only
python qtx_helpers/run_qtx_tests.py edge_cases
./qtx_helpers/qtx_test.sh edge_cases
```

### Run Specific Files
```bash
# UDP manager tests
python qtx_helpers/run_qtx_tests.py --file test_qtx_perpetual_udp_manager
./qtx_helpers/qtx_test.sh --file test_qtx_perpetual_udp_manager

# SHM manager tests
python qtx_helpers/run_qtx_tests.py --file test_qtx_perpetual_shm_manager
./qtx_helpers/qtx_test.sh --file test_qtx_perpetual_shm_manager
```

### Advanced Options
```bash
# Verbose output
python qtx_helpers/run_qtx_tests.py --verbose udp
./qtx_helpers/qtx_test.sh -v api

# Use pytest instead of unittest
python qtx_helpers/run_qtx_tests.py --pytest shm
./qtx_helpers/qtx_test.sh --pytest --verbose all
```

## Test Segments Mapping

| Command | Files Included |
|---------|----------------|
| `udp` | `test_qtx_perpetual_udp_manager.py` + `test_qtx_perpetual_udp_manager_edge_cases.py` |
| `api` | `test_qtx_perpetual_api_order_book_data_source.py` |
| `shm` | `test_qtx_perpetual_shm_manager.py` |
| `edge_cases` | `test_qtx_perpetual_udp_manager_edge_cases.py` |
| `all` | All test files |

## Common Development Workflows

```bash
# Quick UDP check during development
./qtx_helpers/qtx_test.sh udp

# Debug specific test with verbose output
./qtx_helpers/qtx_test.sh --verbose --file test_qtx_perpetual_udp_manager

# Full test suite before commit
./qtx_helpers/qtx_test.sh all

# Use pytest for better formatting
./qtx_helpers/qtx_test.sh --pytest --verbose all
```

## Original Command Equivalent

Your original command pattern:
```bash
python -m unittest @qtx_perpetual
```

Is now replaced with:
```bash
python qtx_helpers/run_qtx_tests.py all
# or simply
python qtx_helpers/run_qtx_tests.py
```

For specific segments:
```bash
# Instead of manually specifying files, use segments:
python qtx_helpers/run_qtx_tests.py udp    # for UDP-related tests
python qtx_helpers/run_qtx_tests.py api    # for API tests
python qtx_helpers/run_qtx_tests.py shm    # for shared memory tests
```

## Running from qtx_helpers Directory

If you're already in the `qtx_helpers` directory, you can use shorter commands:
```bash
cd qtx_helpers

# Then use:
python run_qtx_tests.py udp
./qtx_test.sh api
``` 