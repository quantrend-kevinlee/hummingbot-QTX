# Dynamic Inheritance Architecture for QTX Perpetual Connector

## 1. Introduction

The QTX Perpetual Connector implements a sophisticated dynamic inheritance architecture that creates hybrid exchange connectors at runtime. This architecture enables the connector to combine QTX's proprietary UDP market data feed with any supported exchange's trading API (Binance, OKX, Bybit), creating a best-of-both-worlds solution.

### What is QTX?

QTX is a quantitative trading infrastructure provider that offers:
- **Ultra-low latency market data** via UDP multicast
- **Smart order routing** through shared memory interfaces
- **Cross-exchange data normalization** for consistent trading

### Why Dynamic Inheritance?

Traditional approaches would require creating separate connector classes for each exchange combination (QtxBinanceConnector, QtxOkxConnector, etc.). Instead, our dynamic architecture:
- Creates the appropriate connector class at runtime based on configuration
- Inherits all functionality from the parent exchange
- Selectively overrides only the methods needed for QTX integration
- Maintains full compatibility with Hummingbot's connector interface

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    QTX Connector Factory                     │
│                                                              │
│  Input: exchange_backend = "binance"                         │
│         qtx_host = "172.30.2.221"                          │
│         qtx_port = 8080                                      │
│                                                              │
│  Process:                                                    │
│  1. Load BinancePerpetualDerivative class                  │
│  2. Create QtxBinancePerpetualConnector dynamically         │
│  3. Override market data methods → QTX UDP                  │
│  4. Override order methods → QTX SHM                        │
│  5. Keep all other Binance functionality                    │
│                                                              │
│  Output: Hybrid connector instance                          │
└─────────────────────────────────────────────────────────────┘
```

## 3. Technical Implementation

### 3.1 Dynamic Class Creation with `types.new_class()`

The connector uses Python's `types.new_class()` function for proper dynamic class creation:

```python
import types
from typing import Type, Dict, Any

def create_qtx_connector_class(
    config: QtxConnectorConfig,
    client_config_map: Any
) -> Type[QtxConnectorProtocol]:
    """
    Factory function that creates a dynamic QTX connector class.
    
    This function:
    1. Loads the parent exchange class (e.g., BinancePerpetualDerivative)
    2. Creates a new class that inherits from it
    3. Overrides specific methods to use QTX's infrastructure
    4. Returns the new class (not an instance)
    """
    
    # Get parent exchange class dynamically
    exchange_info = EXCHANGE_CONNECTOR_CLASSES[config.exchange_backend]
    module = importlib.import_module(exchange_info["module"])
    base_exchange_class = getattr(module, exchange_info["class"])
    
    # Generate meaningful class name
    class_name = f"Qtx{config.exchange_backend.capitalize()}PerpetualConnector"
    
    # Define namespace population function
    def exec_body(ns: Dict[str, Any]) -> None:
        """Populate the class namespace with methods and attributes."""
        
        # Set metadata
        ns['__module__'] = 'hummingbot.connector.derivative.qtx_perpetual'
        ns['__doc__'] = f"Dynamic QTX connector for {config.exchange_backend}"
        
        # Add methods in modular fashion
        _add_initialization_methods(ns, config, exchange_info)
        _add_market_data_overrides(ns, config)
        _add_order_management_overrides(ns, config)
        _add_lifecycle_methods(ns, config)
    
    # Create the class using types.new_class
    return types.new_class(
        class_name,
        (base_exchange_class,),  # Inherit from parent exchange
        {},                       # No special keyword arguments
        exec_body                 # Function to populate namespace
    )
```

### 3.2 Method Override Strategy

The dynamic class selectively overrides only the methods necessary for QTX integration:

#### Market Data Override
```python
def _add_market_data_overrides(ns: Dict[str, Any], config: QtxConnectorConfig):
    """Override market data methods to use QTX UDP feed."""
    
    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        """
        Override parent's market data source with QTX UDP feed.
        
        This method:
        1. Gets the parent's data source object
        2. Replaces its market data methods with QTX implementations
        3. Returns the modified data source
        """
        # Get parent data source
        parent_data_source = super(self.__class__, self)._create_order_book_data_source()
        
        # Replace with QTX methods
        parent_data_source.listen_for_order_book_diffs = self._qtx_listen_for_diffs
        parent_data_source.listen_for_trades = self._qtx_listen_for_trades
        parent_data_source._order_book_snapshot = self._qtx_build_snapshot
        
        return parent_data_source
    
    ns['_create_order_book_data_source'] = _create_order_book_data_source
```

#### Order Management Override
```python
def _add_order_management_overrides(ns: Dict[str, Any], config: QtxConnectorConfig):
    """Override order placement to use QTX shared memory."""
    
    async def _place_order(
        self,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        trade_type: TradeType,
        order_type: OrderType,
        price: Decimal,
        **kwargs
    ) -> Tuple[str, float]:
        """
        Override order placement to route through QTX.
        
        Features:
        1. Converts MARKET orders to aggressive LIMIT orders
        2. Auto-detects position actions (OPEN/CLOSE)
        3. Routes through QTX shared memory for execution
        """
        # Use parent's symbol conversion
        exchange_symbol = await super(self.__class__, self).exchange_symbol_associated_to_pair(trading_pair)
        
        # Route through QTX shared memory
        if self.shm_manager:
            success, result = await self.shm_manager.place_order(
                client_order_id=order_id,
                symbol=exchange_symbol,
                trade_type=trade_type,
                order_type=order_type,
                price=price,
                size=amount
            )
            
            if success:
                return result["exchange_order_id"], result["transaction_time"]
            else:
                raise Exception(f"QTX order placement failed: {result.get('error')}")
    
    ns['_place_order'] = _place_order
```

### 3.3 Protocol-Based Type Safety

The architecture uses `typing.Protocol` to define the expected interface:

```python
from typing import Protocol, runtime_checkable, Optional, Tuple

@runtime_checkable
class QtxConnectorProtocol(Protocol):
    """
    Protocol defining the interface that all QTX connectors must implement.
    
    This ensures type safety and provides clear documentation of the
    expected methods and attributes.
    """
    
    # Required attributes
    _qtx_perpetual_host: str
    _qtx_perpetual_port: int
    _exchange_backend: str
    _udp_manager: Optional['QtxPerpetualUDPManager']
    _shm_manager: Optional['QtxPerpetualSharedMemoryManager']
    
    # Core trading operations
    async def _place_order(self, ...) -> Tuple[str, float]: ...
    async def _place_cancel(self, ...) -> bool: ...
    
    # Market data operations
    def _create_order_book_data_source(self) -> 'OrderBookTrackerDataSource': ...
    
    # Properties
    @property
    def udp_manager(self) -> 'QtxPerpetualUDPManager': ...
    
    @property
    def shm_manager(self) -> Optional['QtxPerpetualSharedMemoryManager']: ...
```

### 3.4 Configuration-Driven Architecture

The connector configuration determines the runtime behavior:

```python
from pydantic import BaseModel, validator, IPvAnyAddress

class QtxConnectorConfig(BaseModel):
    """
    Validated configuration for QTX connector creation.
    
    Uses Pydantic for:
    - Automatic validation
    - Type conversion
    - Clear error messages
    """
    
    # QTX Infrastructure
    qtx_perpetual_host: IPvAnyAddress  # Validates IP address format
    qtx_perpetual_port: int            # Must be 1-65535
    qtx_place_order_shared_memory_name: Optional[str] = None
    
    # Parent Exchange
    exchange_backend: str              # Must be "binance", "okx", or "bybit"
    exchange_api_key: Optional[str] = None
    exchange_api_secret: Optional[str] = None
    
    # Trading Configuration
    trading_pairs: Optional[List[str]] = None
    trading_required: bool = True
    
    @validator('exchange_backend')
    def validate_exchange_backend(cls, v):
        supported = ['binance', 'okx', 'bybit']
        if v.lower() not in supported:
            raise ValueError(f"Unsupported exchange: {v}. Must be one of: {supported}")
        return v.lower()
```

## 4. Key Components

### 4.1 QTX UDP Manager

Handles real-time market data via UDP:

```python
class QtxPerpetualUDPManager:
    """
    Singleton manager for QTX UDP market data feed.
    
    Features:
    - Centralized UDP socket management
    - Automatic reconnection on failure
    - Per-symbol message queues
    - Binary protocol parsing
    """
    
    async def subscribe_and_get_queues(self, trading_pair: str) -> Dict[str, asyncio.Queue]:
        """
        Subscribe to a trading pair and get its message queues.
        
        Returns:
            {
                "diff": asyncio.Queue,  # Order book updates
                "trade": asyncio.Queue  # Trade messages
            }
        """
```

### 4.2 QTX Shared Memory Manager

Handles order placement via shared memory:

```python
class QtxPerpetualSharedMemoryManager:
    """
    Manager for QTX shared memory order placement.
    
    Features:
    - Zero-copy order submission
    - Microsecond latency
    - Automatic parameter validation
    - Error handling with retries
    """
    
    async def place_order(
        self,
        client_order_id: str,
        symbol: str,
        trade_type: TradeType,
        order_type: OrderType,
        price: Decimal,
        size: Decimal
    ) -> Tuple[bool, Dict[str, Any]]:
        """Place order through shared memory segment."""
```

### 4.3 Trading Pair Utilities

Handles format conversions between systems:

```python
class QtxPerpetualTradingPairUtils:
    """
    Utilities for trading pair format conversion.
    
    Formats:
    - Hummingbot: "BTC-USDT"
    - QTX UDP: "binance-futures:btcusdt"
    - Binance API: "BTCUSDT"
    """
    
    @staticmethod
    def convert_to_qtx_trading_pair(
        hummingbot_pair: str,
        exchange_name: str
    ) -> str:
        """Convert Hummingbot format to QTX format."""
        # BTC-USDT → binance-futures:btcusdt
```

## 5. Runtime Behavior

### 5.1 Initialization Flow

```
1. User configures: exchange_backend="binance"
2. Factory loads BinancePerpetualDerivative class
3. Creates QtxBinancePerpetualConnector class dynamically
4. Overrides market data and order methods
5. Returns instance with combined functionality
```

### 5.2 Market Data Flow

```
QTX UDP Server → UDP Manager → Message Queues → Order Book Updates
     ↓                ↓              ↓                    ↓
Binary Data    Parse & Route   Per-Symbol Queue   Hummingbot Core
```

### 5.3 Order Execution Flow

```
Strategy → _place_order() → SHM Manager → QTX Infrastructure
    ↓           ↓              ↓               ↓
Decision   Override Method  Shared Memory  Parent Exchange
```

## 6. Performance Optimizations

### 6.1 Class Caching

Dynamic classes are cached to avoid recreation:

```python
from weakref import WeakValueDictionary
import threading

_CLASS_CACHE: WeakValueDictionary[tuple, Type] = WeakValueDictionary()
_CACHE_LOCK = threading.RLock()

def create_qtx_connector_class(config: QtxConnectorConfig) -> Type:
    cache_key = (
        config.exchange_backend,
        str(config.qtx_perpetual_host),
        config.qtx_perpetual_port,
        config.trading_required
    )
    
    with _CACHE_LOCK:
        if cache_key in _CLASS_CACHE:
            return _CLASS_CACHE[cache_key]
    
    # Create class...
    
    with _CACHE_LOCK:
        _CLASS_CACHE[cache_key] = new_class
    
    return new_class
```

### 6.2 Direct Queue Access

Market data uses direct queue access for minimal latency:

```python
# Instead of callbacks, we use direct queue access
self._udp_queues[trading_pair] = {
    "diff": asyncio.Queue(maxsize=1000),
    "trade": asyncio.Queue(maxsize=1000)
}

# Consumer directly reads from queue
async def _consume_diff_messages(self, output: asyncio.Queue):
    while True:
        message = await self._udp_queues[trading_pair]["diff"].get()
        await output.put(message)
```

## 7. Error Handling

### 7.1 Graceful Degradation

The connector handles failures gracefully:

```python
async def _place_order(self, ...):
    try:
        # Try QTX shared memory
        if self.shm_manager:
            return await self.shm_manager.place_order(...)
    except Exception as e:
        self.logger().error(f"QTX order placement failed: {e}")
        # Could fall back to parent exchange if configured
        raise
```

### 7.2 Connection Management

Automatic reconnection for UDP:

```python
async def _maintain_udp_connection(self):
    while self._running:
        try:
            if not self._udp_manager.is_connected:
                await self._udp_manager.reconnect()
        except Exception as e:
            self.logger().error(f"UDP reconnection failed: {e}")
        await asyncio.sleep(5)
```

## 8. Testing Strategy

### 8.1 Protocol Conformance Testing

```python
def test_protocol_conformance():
    """Verify generated classes implement QtxConnectorProtocol."""
    connector_class = create_qtx_connector_class(config)
    assert issubclass(connector_class, QtxConnectorProtocol)
```

### 8.2 Dynamic Behavior Testing

```python
def test_method_overrides():
    """Verify correct methods are overridden."""
    connector_class = create_qtx_connector_class(config)
    overrides = connector_class.describe_overrides()
    
    expected = ['_place_order', '_place_cancel', '_create_order_book_data_source']
    assert all(method in overrides for method in expected)
```

### 8.3 Integration Testing

```python
async def test_market_data_flow():
    """Test end-to-end market data flow."""
    connector = create_qtx_connector_instance(config)
    await connector.start_network()
    
    # Verify UDP subscriptions
    assert len(connector._udp_subscriptions) == len(connector.trading_pairs)
    
    # Verify data flow
    # ... test market data updates ...
```

## 9. Common Use Cases

### 9.1 Basic Setup

```python
# Create a QTX-Binance hybrid connector
connector = QtxPerpetualDerivative(
    client_config_map=config,
    exchange_backend="binance",
    qtx_perpetual_host="172.30.2.221",
    qtx_perpetual_port=8080,
    exchange_api_key="your_binance_key",
    exchange_api_secret="your_binance_secret",
    trading_pairs=["BTC-USDT", "ETH-USDT"]
)
```

### 9.2 Multiple Exchange Support

```python
# Same code works for different exchanges
okx_connector = QtxPerpetualDerivative(
    exchange_backend="okx",  # Just change this
    # ... rest remains the same
)

bybit_connector = QtxPerpetualDerivative(
    exchange_backend="bybit",  # Or this
    # ... rest remains the same
)
```

### 9.3 Custom Configuration

```python
# With shared memory for ultra-low latency
config = QtxConnectorConfig(
    exchange_backend="binance",
    qtx_perpetual_host="172.30.2.221",
    qtx_perpetual_port=8080,
    qtx_place_order_shared_memory_name="/qtx_orders",  # Enable SHM
    # ... other config
)
```

## 10. Debugging and Monitoring

### 10.1 Runtime Introspection

```python
# Inspect the dynamic class
connector_class = type(connector)
print(f"Class name: {connector_class.__name__}")
print(f"Parent class: {connector_class.__bases__[0].__name__}")
print(f"Overridden methods: {connector_class.describe_overrides()}")
```

### 10.2 Logging

The connector provides detailed logging:

```
INFO - Creating dynamic class: QtxBinancePerpetualConnector
INFO - Subscribed to QTX UDP: binance-futures:btcusdt → BTC-USDT
DEBUG - Order placement via SHM: client_id=123, exchange_id=456
INFO - Unsubscribing from 2 trading pairs
```

### 10.3 Performance Monitoring

```python
# Monitor message rates
stats = connector.udp_manager.get_statistics()
print(f"Messages received: {stats['total_messages']}")
print(f"Messages per second: {stats['message_rate']}")
print(f"Queue depths: {stats['queue_depths']}")
```

## 11. Best Practices

### 11.1 Configuration

- Always validate configuration before creating connectors
- Use environment variables for sensitive data
- Set appropriate queue sizes based on expected message rates

### 11.2 Resource Management

- Use async context managers for proper cleanup
- Monitor queue depths to prevent memory issues
- Implement circuit breakers for external dependencies

### 11.3 Error Handling

- Log all errors with context
- Implement retry logic with exponential backoff
- Provide clear error messages for configuration issues

## 12. Future Enhancements

### 12.1 Planned Features

- Support for more parent exchanges
- WebSocket fallback for UDP
- Advanced order types through SHM
- Performance metrics dashboard

### 12.2 Architecture Evolution

- Plugin system for custom overrides
- Dynamic feature detection
- Hot-swappable components
- Multi-region support

## 13. Conclusion

The QTX Dynamic Inheritance Architecture represents a sophisticated approach to combining multiple trading systems. By using Python's metaprogramming capabilities correctly with `types.new_class()`, we achieve:

- **Flexibility**: Support any exchange without code duplication
- **Performance**: Microsecond-latency market data and order routing
- **Maintainability**: Clear separation of concerns and modular design
- **Type Safety**: Full protocol-based type checking
- **Testability**: Comprehensive testing at all levels

This architecture demonstrates how dynamic class creation, when properly implemented, can solve complex integration challenges while maintaining code quality and performance.