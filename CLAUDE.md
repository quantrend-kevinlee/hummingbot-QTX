# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This repository contains Hummingbot, an open-source framework for creating and deploying automated trading strategies (bots) that can run on various centralized and decentralized exchanges. It includes a set of exchange connectors, utility functions, and strategy implementations.

Key focus of this repository is the QTX spot and the QTX perpetual connector implementation, a custom connector that enables Hummingbot to communicate with the QTX exchange platform for trading cryptocurrency perpetual futures.

For now, we are focusing on the QTX perpetual connector.

## QTX Perpetual Connector Architecture

The QTX perpetual connector is implemented using a hybrid approach:

- Market data from QTX is received through a **UDP feed** (real-time market data)
- Trading operations use a **Dynamic Runtime Inheritance** model, where QTX creates a subclass of a parent exchange connector

Key components:

1. `QtxPerpetualDerivative` - Main connector class that integrates QTX market data with the parent exchange operations
2. `QtxPerpetualUDPManager` - Centralized UDP socket management, subscriptions, and message parsing
3. `QtxPerpetualTradingPairUtils` - Centralizes all trading pair conversion functions
4. `QtxPerpetualUtils` - Utilities for format conversion and API interactions
5. `QtxPerpetualShmManager` - Manages shared memory interactions for the connector

## Dynamic Runtime Inheritance Architecture

The QTX connector implements a flexible architecture that enables dynamic runtime inheritance and method overriding:

- **Runtime Class Factory Pattern**: Creates dynamic subclasses at runtime using Python's metaprogramming capabilities
- **Selective Method Overriding**: Overrides specific methods from the parent exchange class (market data handling, order placement/cancellation)
- **Flexible Parent Exchange Selection**: Can inherit from any supported parent exchange (currently supports Binance, OKX, Bybit)
- **Protocol-Based Implementation**: Uses typing.Protocol to ensure proper interface adherence
- **Inheritance-Based Approach**: Inherits all behavior from the parent exchange except for explicitly overridden methods

For detailed technical implementation of the dynamic subclassing system, see `dynamic_inheritance_architecture.md`.

## Development Commands

### Building/Compiling

```bash
# Dependencies
conda activate hummingbot

# Compile Hummingbot
./compile
```

### Running

```bash
# Start Hummingbot
./start
```

### QTX Enhanced UDP Logger Tool

The `qtx_enhanced_udp_logger.py` script in the test directory is a standalone tool that helps understand and debug the QTX UDP feed protocol. It demonstrates the complete subscription-data-unsubscription flow and proper message parsing:

- **Subscription flow**: Shows how to subscribe by sending the symbol name and receiving a numeric index ACK
- **Message parsing**: Implements correct binary structure unpacking for all message types (ticker, depth, trade)
- **Data structure**: Demonstrates the header format and type-specific payload formats
- **Unsubscription flow**: Shows how to properly unsubscribe by sending the symbol name with a "-" prefix
- **Message statistics**: Tracks and displays detailed statistics for each message type
- **Sequence tracking**: Monitors for sequence gaps to identify potential message loss
- **Latency measurement**: Calculates and reports message latency

This tool is valuable for understanding exactly how the protocol works and can help diagnose issues with the UDP connection, subscription process, or message parsing.

```bash
# Test the QTX UDP feed connection
python test/hummingbot/connector/derivative/qtx_perpetual/qtx_enhanced_udp_logger.py --host 172.30.2.221 --port 8080 --duration 30 --symbols binance-futures:btcusdt,binance-futures:ethusdt
```

### Testing

We use unittest, not pytest.

```bash
# Run all tests
python -m unittest discover -s test/hummingbot/connector/derivative/qtx_perpetual

# Run specific test file
python -m unittest test/hummingbot/connector/derivative/qtx_perpetual/test_qtx_perpetual_udp_manager.py
```

## QTX Connector Implementation Details

1. **UDP Market Data**:

   - QTX provides market data over a UDP feed using the `QtxPerpetualUDPManager`
   - Uses the singleton pattern for centralized socket management and resource sharing
   - Must subscribe to specific trading pairs in QTX format (`exchange_name:formatted_symbol`)
   - Three message types: ticker (type 1/-1), depth (type 2), trade (type 3/-3)
   - Binary data format with struct packing
   - UDP manager centralizes socket handling, automatic reconnection, and consumer tracking

2. **Dynamic Runtime Inheritance**:

   - Creates a dynamic subclass of the selected parent exchange at runtime
   - Overrides specific methods including order placement/cancellation and market data source to use QTX's implementation
   - Uses the parent exchange's API credentials for authentication (QTX did not require these yet)
   - Uses the parent exchange's implementation for all non-overridden methods and properties

3. **Trading Pair Management**:

   - Dedicated `QtxPerpetualTradingPairUtils` module handles conversions between the standard Hummingbot format and the QTX format
   - The conversion between QTX format and the exchange-specific format is handled by converting to the Hummingbot format first in QTX's overridden code, then letting the parent exchange convert to its specific format

4. **Configuration**:
   - The parent exchange name
   - QTX UDP host and port settings
   - Parent exchange API keys and configuration
   - Shared memory segment name for order placement (optional)

## Key Files

- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_derivative.py` - Main connector implementation
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_constants.py` - Constants and settings. All configutation constants should be in here.
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_utils.py` - Utility functions
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_udp_manager.py` - Centralized UDP connection management
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_trading_pair_utils.py` - All methods for trading pair format conversion
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_shm_manager.py` - Shared memory management for the connector
- `/dynamic_inheritance_architecture.md` - Detailed explanation of the dynamic subclassing architecture
- `/test/hummingbot/connector/derivative/qtx_perpetual/` - All test files should be here.

## Working with the QTX Connector

1. **Trading Pair Format**:

   - Hummingbot format: `BTC-USDT`
   - QTX UDP format: `exchange_name:formatted_symbol` (e.g. `binance-futures:btcusdt`)
   - Parent exchange API format: Depends on the exchange (e.g., `BTCUSDT` for Binance)

2. **Configuration**:

   ```python
   # Required configuration fields
   exchange_backend: str  # Parent exchange to inherit from (binance, okx, bybit)
   qtx_perpetual_host: str  # QTX UDP host IP address (default: 172.30.2.221)
   qtx_perpetual_port: int  # QTX UDP port (default: 8080)

   # Optional configuration fields
   qtx_place_order_shared_memory_name: str  # Shared memory segment name (default: "/place_order_kevinlee")

   # Parent exchange credentials
   exchange_api_key: str  # API key for the parent exchange
   exchange_api_secret: str  # API secret for the parent exchange
   ```

## Best Practices

1. **Error Handling**:

   - UDP connections may fail - the `QtxPerpetualUDPManager` includes reconnection logic
   - Handle parent exchange API errors gracefully in the overridden methods
   - Never fall back to default values (like trading pairs) - if the parent exchange is down, trading operations will fail
   - Log all errors with appropriate context

2. **Performance**:

   - UDP processing should be efficient to avoid missing messages
   - Use non-blocking I/O for socket operations
   - Consider the latency between market data and order execution
   - Cache trading pair conversions to avoid repeated processing
   - Use caching for dynamically generated classes as described in the dynamic architecture doc

3. **Trading Pair Management**:

   - Always use the trading pairs from the parent exchange's API rather than hardcoded defaults
   - Use the `QtxPerpetualTradingPairUtils` consistently for all conversions
   - Verify trading pairs exist on the parent exchange before attempting to trade

4. **Testing**:

   - Implement tests using Python's unittest framework, following the patterns in `/test/hummingbot/connector/derivative/qtx_perpetual/`
   - Run QTX perpetual and the parent exchange perpetual side-by-side with taker orders to see if QTX got better prices
   - Use the enhanced UDP logger for debugging UDP feed issues
