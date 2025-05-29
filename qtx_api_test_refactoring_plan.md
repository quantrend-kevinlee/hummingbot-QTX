# QTX API Integration Test Refactoring Plan

## Overview

This document outlines the plan to refactor the QTX perpetual API integration tests (`test_qtx_perpetual_api_order_book_data_source.py`) to focus on testing the dynamic inheritance mechanism without relying on real UDP network connections.

## Current Issues

1. **Tight Coupling**: The current test is tightly coupled to real UDP socket operations, making it fragile and difficult to test in isolation
2. **Mixed Concerns**: Tests are trying to verify both dynamic inheritance AND UDP functionality simultaneously
3. **Mock Complexity**: Attempting to mock low-level socket operations creates compatibility issues with `select.select()` and other system calls
4. **Test Failures**: Current approach leads to errors like "argument must be an int, or have a fileno() method"

## Proposed Solution

### 1. Split Test Responsibilities

#### A. Dynamic Inheritance Tests (New Focus)
Create focused unit tests for the core QTX innovation - dynamic class creation and method overriding:

```python
# File: test_qtx_perpetual_dynamic_inheritance.py
class TestQtxPerpetualDynamicInheritance(IsolatedAsyncioWrapperTestCase):
    """Test dynamic inheritance and method overriding without network dependencies"""
```

**What to Test:**
- Dynamic class creation for all supported parent exchanges (Binance, OKX, Bybit)
- Proper inheritance of parent exchange methods
- Correct overriding of data source methods
- Trading pair format conversions
- Connector instantiation without network dependencies
- Error handling for invalid configurations

#### B. UDP Integration Tests (Already Complete)
The existing tests already provide comprehensive coverage:
- `test_qtx_perpetual_udp_manager.py` - 14 tests covering basic UDP functionality
- `test_qtx_perpetual_udp_manager_edge_cases.py` - 13 tests covering edge cases and performance

### 2. Mock Strategy

Instead of mocking low-level socket operations, mock at the UDP Manager interface level:

```python
def setUp(self):
    super().setUp()
    
    # Create a mock UDP manager with predictable behavior
    self.mock_udp_manager = AsyncMock(spec=QtxPerpetualUDPManager)
    self.mock_udp_manager.is_connected = True
    self.mock_udp_manager.subscribe_to_trading_pairs = AsyncMock(
        return_value=(True, ["BTC-USDT", "ETH-USDT"])
    )
    self.mock_udp_manager.get_message_queue = MagicMock(
        return_value=asyncio.Queue()
    )
    self.mock_udp_manager.build_orderbook_snapshot = AsyncMock(
        return_value={
            "trading_pair": "BTC-USDT",
            "bids": [[50000.0, 1.0], [49999.0, 2.0]],
            "asks": [[50001.0, 1.0], [50002.0, 2.0]],
            "update_id": 12345,
            "timestamp": time.time()
        }
    )
    
    # Patch UDP manager creation
    self.udp_patch = patch(
        'hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_derivative'
        '.QtxPerpetualUDPManager.get_instance'
    )
    self.udp_patch.start().return_value = self.mock_udp_manager
```

### 3. Test Structure

#### Test 1: Dynamic Class Creation
```python
def test_dynamic_class_creation_for_all_exchanges(self):
    """Test that dynamic classes are created correctly for each parent exchange"""
    for exchange_name in ["binance", "okx", "bybit"]:
        connector = QtxPerpetualDerivative(
            client_config_map=self.mock_config,
            exchange_backend=exchange_name,
            trading_pairs=["BTC-USDT"],
            trading_required=False
        )
        
        # Verify correct parent class
        exchange_info = EXCHANGE_CONNECTOR_CLASSES[exchange_name]
        module = importlib.import_module(exchange_info["module"])
        parent_class = getattr(module, exchange_info["class"])
        
        self.assertIsInstance(connector, parent_class)
        self.assertEqual(connector.name, "qtx_perpetual")
        
        # Verify dynamic class name
        self.assertTrue(connector.__class__.__name__.startswith("QtxDynamicConnector"))
```

#### Test 2: Method Overriding Verification
```python
async def test_data_source_method_overriding(self):
    """Test that data source methods are properly overridden"""
    connector = self._create_test_connector()
    data_source = connector.order_book_tracker.data_source
    
    # Verify methods are overridden (not the parent's original methods)
    self.assertIn("qtx", str(data_source.listen_for_subscriptions))
    self.assertIn("qtx", str(data_source.listen_for_order_book_diffs))
    self.assertIn("qtx", str(data_source.listen_for_trades))
    
    # Test that overridden methods interact with UDP manager
    await data_source.listen_for_subscriptions()
    self.mock_udp_manager.get_message_queue.assert_called()
```

#### Test 3: Trading Pair Conversion
```python
def test_trading_pair_format_conversions(self):
    """Test conversion between Hummingbot, QTX, and parent exchange formats"""
    test_cases = [
        ("BTC-USDT", "binance", "binance-futures:btcusdt", "BTCUSDT"),
        ("ETH-USDT", "okx", "okx-futures:ethusdt", "ETH-USDT-SWAP"),
        ("SOL-USDT", "bybit", "bybit-futures:solusdt", "SOLUSDT"),
    ]
    
    for hb_pair, exchange, expected_qtx, expected_exchange in test_cases:
        connector = self._create_test_connector(exchange, [hb_pair])
        
        # Test conversions through the connector
        qtx_format = connector._convert_to_qtx_format(hb_pair)
        self.assertEqual(qtx_format, expected_qtx)
```

#### Test 4: Order Book Tracker Integration
```python
async def test_order_book_tracker_uses_qtx_data(self):
    """Test that order book tracker properly uses QTX data source"""
    connector = self._create_test_connector()
    
    # Simulate order book message
    test_message = OrderBookMessage(
        message_type=OrderBookMessageType.DIFF,
        content={
            "trading_pair": "BTC-USDT",
            "bids": [[50000.0, 1.0]],
            "asks": [[50001.0, 1.0]],
            "update_id": 123
        },
        timestamp=time.time()
    )
    
    # Put message in mock queue
    queue = self.mock_udp_manager.get_message_queue.return_value
    await queue.put(test_message)
    
    # Verify tracker processes QTX messages
    # ... verification logic ...
```

#### Test 5: Error Handling
```python
def test_invalid_parent_exchange_handling(self):
    """Test error handling for invalid parent exchange"""
    with self.assertRaises(ValueError):
        QtxPerpetualDerivative(
            client_config_map=self.mock_config,
            exchange_backend="invalid_exchange",
            trading_pairs=["BTC-USDT"],
            trading_required=False
        )

async def test_udp_connection_failure_handling(self):
    """Test graceful handling when UDP connection fails"""
    self.mock_udp_manager.is_connected = False
    self.mock_udp_manager.connect = AsyncMock(return_value=False)
    
    connector = self._create_test_connector()
    # Verify connector handles UDP failure gracefully
    # ... verification logic ...
```

### 4. Implementation Steps

1. **Create New Test File**: `test_qtx_perpetual_dynamic_inheritance.py`
2. **Move Inheritance Tests**: Extract dynamic inheritance tests from the current file
3. **Implement Mock Strategy**: Use high-level UDP manager mocking
4. **Add Comprehensive Tests**: Cover all parent exchanges and edge cases
5. **Simplify or Remove**: Current `test_qtx_perpetual_api_order_book_data_source.py`
6. **Update Documentation**: Document the test split and rationale

### 5. Benefits

1. **Clear Separation of Concerns**: Each test file has a single, clear purpose
2. **Reliable Tests**: No network dependencies means consistent test results
3. **Fast Execution**: Mock-based tests run in milliseconds
4. **Better Maintainability**: Changes to UDP protocol don't affect inheritance tests
5. **Easier Debugging**: Clear which component is failing when tests fail

### 6. Test Coverage Matrix

| Component | Test File | Coverage |
|-----------|----------|----------|
| Dynamic Inheritance | `test_qtx_perpetual_dynamic_inheritance.py` | Class creation, method overriding, multi-exchange support |
| UDP Manager | `test_qtx_perpetual_udp_manager.py` | Connection, subscription, message flow |
| UDP Edge Cases | `test_qtx_perpetual_udp_manager_edge_cases.py` | Error handling, performance, concurrency |
| Trading Pair Utils | `test_qtx_perpetual_dynamic_inheritance.py` | Format conversions |

### 7. Next Steps

1. Review this plan with the team
2. Create the new test file structure
3. Implement the mock-based tests
4. Verify complete test coverage
5. Remove redundant tests from the old file

## Notes

- The existing UDP tests provide excellent coverage of network functionality
- The new inheritance tests will focus purely on the dynamic class creation mechanism
- This separation aligns with the single responsibility principle for tests
- Consider adding integration tests that run against a real QTX test server as a separate test suite for CI/CD