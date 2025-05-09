# Hummingbot Testing Guide

This guide summarizes the official documentation and community best‑practices for adding **unit**, **integration**, and **QA** tests to the [Hummingbot](https://github.com/hummingbot/hummingbot) code‑base.  
It is aimed at Python connectors/strategies as well as TypeScript Gateway connectors.

---

## 1. Why Tests Matter

- Code contributions must ship with **≥ 80 % unit‑test coverage** before a pull‑request is reviewed.  
  Tests may **not** make real network calls; all HTTP/WebSocket traffic should be mocked.
- A comprehensive QA checklist ensures every connector works reliably across accounts, strategies, and network conditions.

## 2. Directory Layout

```
hummingbot/
│
├── hummingbot/         # Production source
└── test/
    ├── unit/           # Fast, mocked tests (pytest)
    ├── integration/    # Slow, optional tests that spin up services
    └── qa/             # Checklist‑driven manual/automated scripts
```

Create new test modules mirroring the package you are testing, e.g.:

```
hummingbot/connector/exchange/binance_perpetual/…
test/unit/connector/exchange/binance_perpetual/test_rest.py
```

## 3. Writing Python Unit Tests (Pytest)

1. **Install dev dependencies**

```bash
pip install -r requirements_dev.txt
```

2. **Use fixtures & mocks**

```python
import pytest
from aioresponses import aioresponses

@pytest.fixture
def binance_http_mock():
    with aioresponses() as m:
        yield m
```

3. **Keep it fast**

- Target one behaviour per test.
- Avoid `sleep()` / real delay; use `asyncio.AdvancedLoop` or pytest‑asyncio.

4. **Run and iterate**

```bash
pytest -q  # run all
pytest -k binance_perpetual  # filter
pytest --cov=hummingbot --cov-report=term-missing
```

## 4. Integration Tests

- Live‑network tests belong in `test/integration`.
- Use **sandbox** or **testnet** endpoints and guard with an env‑var:

```python
pytest.skip("set HB_INTEGRATION=1 to enable", allow_module_level=True)
```

- Spin up Docker services (Redis, database) via `docker-compose` in `conftest.py`.

## 5. Connector QA Checklist

For every new **spot/perp** connector, verify:

| Area            | Example step           | Expected                                |
| --------------- | ---------------------- | --------------------------------------- |
| API key connect | `connect binance`      | Valid key connects; invalid key errors  |
| Balance sync    | `balance`              | Matches exchange                        |
| Order flow      | market‑making strategy | Orders place / cancel without error     |
| Rate limits     | rapid refresh          | Warnings near limit, no bans            |
| Disconnection   | pull cable             | Client reconnects and reconciles orders |

(See complete checklist in docs for additional scenarios.)

## 6. Gateway (TypeScript) Tests

Gateway connectors use **Jest**:

```bash
yarn jest test/chains/ethereum/ethereum.controller.test.ts
```

- Place tests under `gateway/test`.
- Mock RPC/WebSocket with [nock](https://github.com/nock/nock) or `jest.fn()` wrappers.

## 7. Local Debugging

- **VS Code**: open the project, press <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>D</kbd>, create a _Python_ or _Attach_ launch configuration targeting `bin/hummingbot.py`.
- **aiopython console**: quick ad‑hoc async experiments.

## 8. Continuous Integration

- Every push triggers the `ci` GitHub Action which runs `pytest`, style‑lint, and packages Docker images.
- Workflow file lives at `.github/workflows/ci.yml`; reproduce locally with `act`.

## 9. Tips & Common Pitfalls

| Pitfall                  | Remedy                                             |
| ------------------------ | -------------------------------------------------- |
| Real HTTP calls sneak in | Patch `aiohttp.ClientSession` / use `aioresponses` |
| Time‑based logic flaky   | Freeze time with `freezegun` or inject clock       |
| Randomness               | Seed RNG (`random.seed(42)`)                       |
| Coverage drop            | Run `pytest --cov`, examine gaps, add tests        |

---

_Last updated: 2025‑05‑06 07:17 UTC._
