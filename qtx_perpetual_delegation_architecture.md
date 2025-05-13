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
5. **More maintainable** - No dependency on other connectors' implementation details

**Disadvantages:**
1. **More initial code** - Implementation of common exchange functionality required
2. **Duplication** - Some logic from existing connectors would be duplicated
3. **Updates & maintenance** - Need to maintain own implementations of common patterns

This approach would allow:
- Receiving market data through a dedicated UDP implementation
- Defining a clean interface for execution operations
- Implementing exchange-specific modules that handle their own error cases
- Adding new exchanges without modifying existing code

Given the challenges with the delegation pattern, especially around error handling, this ground-up approach might provide a cleaner long-term solution, though it requires more upfront work.

### 5. Instance Transplantation Pattern

A new architectural approach leverages Python's dynamic nature to transform the QTX connector into a modified exchange connector instance at runtime:

```python
class QtxPerpetualDerivative(PerpetualDerivativePyBase):
    def __init__(self, ...):
        # Basic initialization
        self._name = self._exchange_name = "qtx_perpetual"
        
        # Store QTX-specific configurations
        self._qtx_perpetual_host = qtx_perpetual_host
        self._qtx_perpetual_port = qtx_perpetual_port
        
        # Initialize UDP manager for QTX market data
        self._udp_manager = QtxPerpetualUDPManager.get_instance(...)
        
        # Create exchange connector with converted trading pairs
        exchange_connector = BinancePerpetualDerivative(...)
        
        # Preserve QTX-specific attributes
        qtx_preserved_attrs = {...}
        
        # TRANSFORM: Replace self's __dict__ with exchange connector's __dict__
        self.__dict__ = exchange_connector.__dict__
        
        # Restore QTX-specific attributes
        for attr_name, attr_value in qtx_preserved_attrs.items():
            setattr(self, f"_{attr_name}", attr_value)
        
        # Replace specific methods with QTX versions
        self._apply_qtx_specific_methods()
```

**Advantages:**
1. **Minimal adaptation code** - No need for explicit state synchronization
2. **Complete exchange functionality** - All exchange methods available immediately
3. **Performance** - No delegation overhead for most operations
4. **Clear responsibilities** - QTX handles market data, exchange handles execution
5. **Flexibility** - Easy to switch between different execution exchanges

**Disadvantages:**
1. **Python-specific approach** - Relies on dynamic language features
2. **Less explicit** - Transformation happens "magically" at runtime
3. **Potential fragility** - May break if exchange connector implementation changes
4. **Debugging complexity** - Method origins can be harder to trace

This pattern completely avoids the need for delegation and state synchronization by making the QTX connector become the exchange connector at runtime, while selectively replacing only the methods that need QTX-specific behavior (order book, order execution).

## The "Delegate then Copy Back" Pattern

For the current implementation, the "delegate then copy back" pattern emerged as the most pragmatic solution:

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

## Key Insight: Immediate State Synchronization

An important insight is that state synchronization doesn't need to occur on a regular schedule. Since Hummingbot's core only interacts with the QTX connector (not directly with Binance), all state updates in the Binance connector must be triggered by method calls that first go through QTX.

This means we can synchronize state immediately after each delegation, rather than periodically:

1. QTX receives a method call from Hummingbot core
2. QTX delegates to the Binance connector 
3. Binance connector updates its internal state
4. QTX copies the relevant updated state back from Binance
5. QTX returns the result to Hummingbot core

This approach is deterministic and ensures that state is always synchronized at the precise moment it changes, with no need for periodic syncing tasks.

## Error Handling Considerations

After thorough review, we determined that **error handling is the primary area requiring exchange-specific logic** in the main connector. The reasons are:

1. Different exchanges use different error codes and formats
2. Error handling often requires specific state updates (e.g., order not found → update order tracker)
3. Some errors should be handled differently depending on the exchange

For the QTX perpetual connector, implementing `_is_order_not_found_during_cancelation_error` with proper delegation to Binance's implementation ensures correct handling while keeping the code maintainable.

## Conclusions

1. **Error handling is the main challenge** in the delegation pattern
2. The **"delegate then copy back"** pattern provides the best balance for the current implementation:
   - Clean separation of concerns
   - Explicit state synchronization
   - Minimal adaptation code
   - Compatibility with Hummingbot's inheritance hierarchy

3. For error handling specifically, a thin adaptation layer that:
   - Catches exceptions from the delegated connector
   - Translates them using connector-specific helper methods
   - Updates local state accordingly
   - Returns appropriate results or re-raises

4. State synchronization should be explicit and immediate after each delegation, not scheduled periodically

5. The **Instance Transplantation Pattern** offers the most elegant solution for QTX's unique requirements:
   - Eliminates state synchronization issues entirely
   - Minimizes code duplication
   - Maintains clear separation of responsibilities
   - Provides flexibility to change execution exchanges
   - Leverages Python's dynamic nature for efficient implementation

6. For long-term development, the ground-up approach with exchange-specific modules may offer a cleaner architecture, though it requires more initial development.

## Implementation Recommendations

### For Current Delegation Approach:
1. Implement exchange-specific error detection methods (like `_is_order_not_found_during_cancelation_error`)
2. Wrap delegate method calls in try-except blocks with specific error handling
3. Immediately copy back relevant state after each delegation call
4. Only copy the state that could have been affected by the specific operation
5. Consider creating an `ErrorAdapter` class if error handling becomes complex across multiple methods

### For Instance Transplantation Pattern:
1. Implement a clean transformation in the constructor that replaces `self.__dict__` with the exchange connector's `__dict__`
2. Preserve QTX-specific attributes by restoring them after transformation
3. Implement QTX-specific methods for order book data and order execution
4. Create proper interface boundaries where trading pair conversion happens
5. Ensure proper lifecycle management (startup, shutdown) for the QTX UDP connection
6. Add support for multiple exchange backends through a configuration parameter

### For Future Ground-Up Approach:
1. Define clean interfaces between market data and execution components
2. Implement a module system for exchange-specific execution logic
3. Centralize error handling within each exchange module
4. Maintain a consistent state model across all exchange integrations
5. Design for extensibility to make adding new exchanges straightforward

By following these guidelines, QTX can maintain a clean architecture that properly handles exchange-specific requirements while remaining maintainable and extensible.