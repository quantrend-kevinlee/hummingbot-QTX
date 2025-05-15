# QTX Perpetual Delegation Architecture Notes

## Initial Issue: Error Handling in Order Cancellation

The issue was discovered when examining the error logs from the QTX perpetual connector. When attempting to cancel an order that doesn't exist on Binance, the error propagated to the upper layers instead of being handled properly:

```
Error in _place_cancel: Error executing request DELETE https://fapi.binance.com/fapi/v1/order. HTTP status is 400. Error: {"code":-2011,"msg":"Unknown order sent."}
```

The original implementation in `QtxPerpetualDerivative._place_cancel()` delegated to Binance's implementation but didn't handle the errors properly:

```python
async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
    binance_instance = self.binance_connector
    if binance_instance is None:
        raise ValueError("Binance connector not available: _place_cancel failed")

    try:
        return await binance_instance._place_cancel(order_id=order_id, tracked_order=tracked_order)
    except Exception as e:
        self.logger().error(f"Error in _place_cancel: {e}", exc_info=True)
        raise
```

This contrasted with Binance's implementation which had specific handling for "Unknown order" errors:

```python
if cancel_result.get("code") == -2011 and "Unknown order sent." == cancel_result.get("msg", ""):
    self.logger().debug(f"The order {order_id} does not exist on Binance Perpetuals. "
                         f"No cancelation needed.")
    await self._order_tracker.process_order_not_found(order_id)
    raise IOError(f"{cancel_result.get('code')} - {cancel_result['msg']}")
```

## Analysis of the Delegation Pattern Flaw

The current delegation pattern in QTX perpetual has a fundamental flaw: while delegating execution operations to another connector (Binance) simplifies implementation, it creates a dependency on exchange-specific details, particularly in error handling and event processing.

This contradicts the purpose of delegation, which should abstract away exchange-specific details. With the current pattern:

1. QTX must implement exchange-specific error handling methods like `_is_order_not_found_during_cancelation_error`
2. QTX becomes tightly coupled to Binance's error response format
3. Supporting a different execution platform would require reimplementing multiple error handling methods
4. QTX must understand and parse exchange-specific message formats in user stream events

The delegation approach should allow QTX to focus on market data while remaining agnostic about execution details.

## Current Solution: The "Delegate then Copy Back" Pattern

For the current implementation, we've adopted the "delegate then copy back" pattern as the most pragmatic immediate solution:

1. Each connector maintains its own internal state independently
2. After delegating operations, immediately copy relevant state back
3. Handle exchange-specific errors at the delegation boundary
4. Adapt exchange-specific message formats in event processing

```python
async def _place_cancel(self, order_id, tracked_order):
    try:
        result = await binance_instance._place_cancel(order_id, tracked_order)
        
        # Immediately copy back any state that might have changed
        binance_order = binance_instance._order_tracker.get_fillable_order(client_order_id=order_id)
        if binance_order is not None:
            our_order = self._order_tracker.get_fillable_order(client_order_id=order_id)
            if our_order is not None:
                our_order.current_state = binance_order.current_state
                
        return result
    except Exception as e:
        if self._is_order_not_found_during_cancelation_error(e):
            await self._order_tracker.process_order_not_found(order_id)
            return True
        raise
```

**Advantages:**
1. **Minimal changes needed** - Works with existing code structure
2. **Explicit state control** - Clear visibility of which state is being synchronized
3. **Compatible with current architecture** - Fits well with Hummingbot's inheritance hierarchy
4. **Immediate fixes** - Addresses critical issues without extensive refactoring

**Disadvantages:**
1. **Explicit error handling duplication** - Must implement exchange-specific error handling
2. **Redundant state** - Maintaining two copies of the same state
3. **Manual synchronization** - Easy to miss state updates
4. **Exchange coupling** - QTX still needs to understand exchange-specific formats

### Example: User Stream Event Handling Challenge

A clear example of the delegation pattern's limitations is in the user stream event handling. The QTX connector's `_user_stream_event_listener` method expects events in a specific format:

```python
async def _user_stream_event_listener(self):
    while True:
        try:
            channel, event_message = await self._user_stream_tracker.user_stream.get()
            
            # Process the same message types as Binance does
            event_type = event_message.get("e")
            
            # Process order updates, positions, etc.
            # ...
        except Exception as e:
            self.logger().error(f"Unexpected error in user stream listener loop: {e}", exc_info=True)
```

When the QTX perpetual user stream data source forwards events from Binance, it preserves Binance's format, which doesn't include a channel component. This causes the error:

```
ValueError: too many values to unpack (expected 2)
```

This highlights that even beyond error handling, the delegation pattern requires the QTX connector to understand exchange-specific message formats. If integration with another exchange like OKX were needed, the user stream event parsing logic would need to be completely rewritten to match OKX's event format.

The required adapter code increases significantly beyond just error handling - it needs to handle:
1. Error formats and handling logic
2. Event message structures and parsing
3. Field name mappings and conversions
4. Exchange-specific state representation

### Key Insight: Immediate State Synchronization

An important insight for the current approach is that state synchronization doesn't need to occur on a regular schedule. Since Hummingbot's core only interacts with the QTX connector (not directly with Binance), all state updates in the Binance connector must be triggered by method calls that first go through QTX.

This means we can synchronize state immediately after each delegation, rather than periodically:

1. QTX receives a method call from Hummingbot core
2. QTX delegates to the Binance connector 
3. Binance connector updates its internal state
4. QTX copies the relevant updated state back from Binance
5. QTX returns the result to Hummingbot core

This approach is deterministic and ensures that state is always synchronized at the precise moment it changes, with no need for periodic syncing tasks.

## Explored Architectural Alternatives

### 1. Full Adapter Pattern

Create a comprehensive adapter for each execution platform that translates all operations, errors, and state:

```python
class ExecutionPlatformAdapter:
    async def place_cancel(self, order_id, tracked_order) -> bool:
        # Platform-specific implementation with error handling
        pass
    
    # ...many other methods
```

**Pros**: Clean separation, platform-agnostic QTX code
**Cons**: Requires extensive adapter code for each platform, negating delegation benefits

### 2. Shared State Approach

Share core data structures (like order tracker) between QTX and execution connector:

```python
# In QTX initialization
binance_instance._order_tracker = self._order_tracker
```

**Pros**: Minimal code, automatic state synchronization
**Cons**: Hidden dependencies, fragile coupling, unclear ownership

### 3. Thin Error Adapter

Create a minimal adapter just for error handling while sharing state:

```python
class ErrorAdapter:
    def handle_cancel_error(self, e, order_id, order_tracker):
        # Translate platform-specific errors
        pass
```

**Pros**: Focused solution to the specific problem
**Cons**: Still creates implicit coupling through shared state

### 4. Ground-Up QTX Connector with Exchange-Specific Modules

Build a QTX connector from scratch with proper abstraction for exchange integration:

```
QtxPerpetualConnector
├── MarketDataManager (UDP-based)
└── ExecutionManager (interface)
    ├── BinanceExecutionModule
    ├── BybitExecutionModule 
    └── OtherExchangeExecutionModule
```

**Advantages:**
1. **Complete ownership** - Full control over the entire connector architecture
2. **Clean design** - Proper abstraction layers designed from the start
3. **Clear boundaries** - Market data and execution clearly separated
4. **Explicit exchange integration** - Exchange-specific code isolated in modules

**Disadvantages:**
1. **More initial code** - Requires implementing common exchange functionality for each exchange
2. **Duplication** - Significant duplication of existing connector code across exchanges
3. **Updates & maintenance** - Need to maintain separate implementations of common patterns

While this approach provides the cleanest architecture theoretically, the duplication of effort makes it less practical for QTX's needs where we want to leverage existing connectors.

### 5. Instance Transplantation Pattern (Using `__dict__` Replacement)

A approach leveraging Python's dynamic nature to transform the QTX connector into a modified exchange connector at runtime:

```python
class QtxPerpetualDerivative(PerpetualDerivativePyBase):
    def __init__(self, exchange_type="binance", **exchange_params):
        # Basic initialization
        self._name = self._exchange_name = "qtx_perpetual"
        
        # Store QTX-specific configurations
        self._qtx_perpetual_host = exchange_params.pop("qtx_perpetual_host")
        self._qtx_perpetual_port = exchange_params.pop("qtx_perpetual_port")
        
        # Initialize UDP manager for QTX market data
        self._udp_manager = QtxPerpetualUDPManager.get_instance(
            self._qtx_perpetual_host, self._qtx_perpetual_port
        )
        
        # Map exchange types to their connector classes
        exchange_map = {
            "binance": BinancePerpetualDerivative,
            "okx": OKXPerpetualDerivative,
            "bybit": BybitPerpetualDerivative
        }
        
        # Get the appropriate exchange class
        exchange_class = exchange_map.get(exchange_type.lower())
        if not exchange_class:
            raise ValueError(f"Unsupported exchange type: {exchange_type}")
        
        # Create exchange connector with converted trading pairs
        exchange_connector = exchange_class(**exchange_params)
        
        # Preserve QTX-specific attributes
        qtx_preserved_attrs = {
            "name": "qtx_perpetual",
            "exchange_name": "qtx_perpetual",
            "qtx_perpetual_host": self._qtx_perpetual_host,
            "qtx_perpetual_port": self._qtx_perpetual_port,
            "udp_manager": self._udp_manager
        }
        
        # TRANSFORM: Replace self's __dict__ with exchange connector's __dict__
        self.__dict__ = exchange_connector.__dict__
        
        # Restore QTX-specific attributes
        for attr_name, attr_value in qtx_preserved_attrs.items():
            setattr(self, f"_{attr_name}", attr_value)
        
        # Replace specific methods with QTX versions
        self._apply_qtx_specific_methods()
        
    def _apply_qtx_specific_methods(self):
        import types
        
        # Override methods with QTX versions
        self.get_orderbook = types.MethodType(self._qtx_get_orderbook, self)
        self.place_order = types.MethodType(self._qtx_place_order, self)
```

**Advantages:**
1. **Minimal adaptation code** - No need for explicit state synchronization
2. **Complete exchange functionality** - All exchange methods available immediately
3. **Performance** - No delegation overhead for most operations
4. **Flexibility** - Easy to switch between different execution exchanges

**Disadvantages:**
1. **Method binding issues** - Methods aren't in `__dict__`, requiring explicit rebinding
2. **Descriptor protocol violations** - Properties and descriptors won't transfer properly
3. **Initialization state inconsistency** - The `__init__` flow gets disrupted
4. **Debugging complexity** - Method origins can be harder to trace

### 6. Factory Function with Dynamic Subclass Creation

For cases where a clear return type is needed:

```python
def create_qtx_perpetual(exchange_type, qtx_host, qtx_port, **exchange_params):
    # Map exchange types to classes
    exchange_map = {
        "binance": BinancePerpetualDerivative,
        "okx": OKXPerpetualDerivative,
        "bybit": BybitPerpetualDerivative
    }
    
    base_class = exchange_map.get(exchange_type.lower())
    if not base_class:
        raise ValueError(f"Unsupported exchange: {exchange_type}")
    
    # Define QtxPerpetual class dynamically
    class QtxPerpetualDerivative(base_class):
        def __init__(self, **params):
            super().__init__(**params)
            self._name = self._exchange_name = "qtx_perpetual"
            self._qtx_host = qtx_host
            self._qtx_port = qtx_port
            self._udp_manager = QtxPerpetualUDPManager.get_instance(qtx_host, qtx_port)
            
        def get_orderbook(self, symbol, depth=10):
            return self._udp_manager.get_orderbook(symbol, depth)
    
    # Create and return instance
    return QtxPerpetualDerivative(**exchange_params)
```

**Advantages:**
1. **Explicit class definition** - Class is defined in a more readable way
2. **Clean initialization** - No `__new__` manipulation required
3. **Simple to understand** - More straightforward than other dynamic approaches

**Disadvantages:**
1. **Not a standard class** - Requires using a factory function
2. **Type confusion** - Each call creates a new class definition
3. **Not ideal for inheritance** - Other classes can't easily subclass the return type

### 7. Dynamic Class Creation with `__new__` (Recommended Approach)

An improved approach using dynamic class creation to better handle inheritance and method resolution:

```python
class QtxPerpetualDerivative:
    def __new__(cls, exchange_type, qtx_host, qtx_port, **exchange_params):
        # Map exchange types to classes
        exchange_map = {
            "binance": BinancePerpetualDerivative,
            "okx": OKXPerpetualDerivative,
            "bybit": BybitPerpetualDerivative
        }
        
        base_class = exchange_map.get(exchange_type.lower())
        if not base_class:
            raise ValueError(f"Unsupported exchange: {exchange_type}")
        
        # Create dynamic subclass that inherits from both QtxPerpetualDerivative and base_class
        dynamic_class = type(
            f"QtxPerpetual{base_class.__name__}", 
            (cls, base_class), 
            {}
        )
        
        # Create instance of dynamic class
        instance = super(QtxPerpetualDerivative, cls).__new__(dynamic_class)
        return instance
    
    def __init__(self, exchange_type, qtx_host, qtx_port, **exchange_params):
        # Initialize base class
        base_init = super().__init__
        base_init(**exchange_params)
        
        # Initialize QTX-specific attributes
        self._name = self._exchange_name = "qtx_perpetual"
        self._qtx_host = qtx_host
        self._qtx_port = qtx_port
        self._udp_manager = QtxPerpetualUDPManager.get_instance(qtx_host, qtx_port)
    
    # QTX-specific method overrides
    def get_orderbook(self, symbol, depth=10):
        return self._udp_manager.get_orderbook(symbol, depth)
```

**Advantages:**
1. **Proper inheritance** - Maintains correct method resolution order
2. **No attribute manipulation** - Avoids `__dict__` replacement issues
3. **Clean design** - Uses Python's class creation mechanisms properly
4. **Type consistency** - Instance is both a QtxPerpetualDerivative and the specific exchange type
5. **No explicit method binding** - Methods are properly bound through inheritance
6. **Proper descriptor handling** - Properties and other descriptors work correctly
7. **Works with standard constructor pattern** - No need for factory functions

**Disadvantages:**
1. **Python-specific approach** - Still relies on dynamic language features
2. **Complexity** - Dynamic class creation is a more advanced pattern
3. **Type confusion** - Created types are unique combinations not defined elsewhere

## Error Handling Considerations

After thorough review, we determined that **error handling is the primary area requiring exchange-specific logic** in the main connector. The reasons are:

1. Different exchanges use different error codes and formats
2. Error handling often requires specific state updates (e.g., order not found → update order tracker)
3. Some errors should be handled differently depending on the exchange

For the QTX perpetual connector, implementing `_is_order_not_found_during_cancelation_error` with proper delegation to Binance's implementation ensures correct handling while keeping the code maintainable.

## Conclusions and Recommendations

### Current Approach Assessment

The "delegate then copy back" pattern currently implemented provides a pragmatic short-term solution that:
- Solves immediate error handling issues
- Requires minimal changes to existing code
- Maintains compatibility with Hummingbot's architecture
- Introduces some state synchronization complexity

### Optimal Solution for Runtime Exchange Selection

**For QTX's requirement to dynamically select exchange backends at runtime, the Dynamic Class Creation with `__new__` approach is the optimal solution** when factory functions cannot be used. This conclusion is based on:

1. **Complete elimination of state synchronization issues** - The QTX instance *is* the exchange instance
2. **Proper inheritance and method resolution** - Unlike `__dict__` replacement, maintains proper Python type semantics
3. **Works with standard class instantiation pattern** - No need for factory functions or special constructors
4. **Proper method binding** - No need for manual method binding with `types.MethodType`
5. **All exchange functionality available** - Inherits all exchange methods without duplication
6. **Runtime flexibility** - Can select different exchange backends based on configuration

While the factory function approach offers similar benefits with a cleaner implementation, the `__new__` approach is the best option when a standard class constructor pattern is required.

The ground-up approach with exchange-specific modules, while architecturally clean, would require excessive duplication of existing connector code, making it less practical for QTX's needs.

### Implementation Recommendations

#### For Short-Term Improvement of Current Delegation Approach:
1. Implement exchange-specific error detection methods (like `_is_order_not_found_during_cancelation_error`)
2. Wrap delegate method calls in try-except blocks with specific error handling
3. Immediately copy back relevant state after each delegation call
4. Only copy the state that could have been affected by the specific operation

#### For Long-Term Solution:
1. **Implement the Dynamic Class Creation with `__new__` approach**:
   - Create a dynamic class that inherits from both QTX and the chosen exchange
   - Override only the methods that need QTX-specific behavior
   - Let Python's method resolution order handle the rest
2. Implement proper initialization flow in `__init__`
3. Create clean interfaces for market data vs. execution responsibilities
4. Add support for multiple exchange backends through configuration parameters

By adopting the `__new__` approach, QTX can achieve the flexibility of runtime exchange selection while maintaining proper inheritance semantics and eliminating the need for manual state synchronization.
