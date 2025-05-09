# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This repository contains Hummingbot, an open-source framework for creating and deploying automated trading strategies (bots) that can run on various centralized and decentralized exchanges. It includes a set of exchange connectors, utility functions, and strategy implementations.

Key focus of this repository is the QTX spot and the QTX perpetual connector implementation, a custom connector that enables Hummingbot to communicate with the QTX exchange platform for trading cryptocurrency perpetual futures.

For now, we are focusing on the QTX perpetual connector.

## QTX Perpetual Connector Architecture

The QTX perpetual connector is implemented using a hybrid approach:

- Market data from QTX is received through a **UDP feed** (real-time market data)
- Trading operations are delegated to **Binance Perpetual API** (execution)

Key components:

1. `QtxPerpetualDerivative` - Main connector class that integrates QTX market data with Binance trading operations
2. `QtxPerpetualAPIOrderBookDataSource` - Manages market data from the UDP feed
3. `QtxPerpetualUserStreamDataSource` - Delegates user stream updates to Binance
4. `QtxPerpetualUDPManager` - Centralized UDP socket management, subscriptions, and message parsing
5. `QtxPerpetualTradingPairUtils` - Centralizes all trading pair conversion functions
6. `QtxPerpetualWebUtils` and `QtxPerpetualUtils` - Utilities for format conversion and API interactions
7. `QtxPerpetualAuth` - Authentication with Binance API

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

# Running with specific script inside the Hummingbot CLI
./start --script scripts/simple_pmm.py
```

### QTX UDP Logger Tool

The `qtx_udp_logger.py` script is a standalone tool that helps understand and debug the QTX UDP feed protocol. It demonstrates the complete subscription-data-unsubscription flow and proper message parsing:

- **Subscription flow**: Shows how to subscribe by sending the symbol name and receiving a numeric index ACK
- **Message parsing**: Implements correct binary structure unpacking for all message types (ticker, depth, trade)
- **Data structure**: Demonstrates the header format and type-specific payload formats
- **Unsubscription flow**: Shows how to properly unsubscribe by sending the symbol name with a "-" prefix

This tool is valuable for understanding exactly how the protocol works and can help diagnose issues with the UDP connection, subscription process, or message parsing.

```bash
# Test the QTX UDP feed connection
python qtx_udp_logger.py --host 172.30.2.221 --port 8080 --duration 30 --symbols binance-futures:btcusdt,binance-futures:ethusdt
```

### Testing

```bash
# Run all tests
pytest test/

# Run specific test file
pytest test/hummingbot/connector/test_utils.py

# Run tests with coverage
pytest --cov=hummingbot test/
```

## QTX Connector Implementation Details

1. **UDP Market Data**:

   - QTX provides market data over a UDP feed using the `QtxPerpetualUDPManager`
   - Uses the singleton pattern for centralized socket management and resource sharing
   - Must subscribe to specific trading pairs in QTX format (`binance-futures:btcusdt`)
   - Three message types: ticker (type 1/-1), depth (type 2), trade (type 3/-3)
   - Binary data format with struct packing
   - UDP manager centralizes socket handling, automatic reconnection, and consumer tracking

2. **Binance Trading Integration**:

   - Uses Binance API credentials for order execution
   - Delegates all trading operations to Binance's API via the `QtxPerpetualBinanceDelegation` module
   - Shares resources between connectors (throttler, time synchronizer)
   - Implements proper rate limiting and error handling
   - Validates trading requirements before executing operations

   **Delegation Pattern Example**:

   ```python
   async def _update_trading_rules(self) -> None:
      """
      Update QTX trading rules by delegating to the Binance sub-connector and copying the updated data back.
      """
      if self.binance_connector is None:
         self.logger().warning(
               "Cannot update trading rules because binance_connector is not available. "
               "Skipping _update_trading_rules."
         )
         return

      try:
         # Ask the Binance sub-connector to update its own trading rules
         await self.binance_connector._update_trading_rules()

         # Copy the trading rules from Binance into QTX
         self._trading_rules = self.binance_connector._trading_rules.copy()

         # Copy the trading pari symbol map from the Binance connector with careful error handling
         symbol_map = await self.binance_connector.trading_pair_symbol_map()
         if symbol_map:
            self._set_trading_pair_symbol_map(symbol_map.copy())
            self.logger().debug(
               f"Successfully updated trading rules and symbol map from Binance. "
               f"QTX symbols_mapping_initialized={self.trading_pair_symbol_map_ready()}"
            )
         else:
            self.logger().info(
               "Binance connector returned an empty symbol map. Trading rules updated, "
               "but no symbol map to copy."
            )

      except Exception as e:
         self.logger().error(
               f"Error in QTX _update_trading_rules during delegation to binance_connector: {e}", exc_info=True
         )
   ```

   This example demonstrates how the QTX connector delegates the trading pair initialization to Binance's implementation, while still maintaining error handling and proper state management.

3. **Trading Pair Management**:

   - Dedicated `QtxPerpetualTradingPairUtils` module handles all format conversions

4. **Configuration**:

   - QTX UDP host and port settings
   - Binance API keys

## Key Files

- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_derivative.py` - Main connector implementation
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_api_order_book_data_source.py` - UDP data source
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_constants.py` - Constants and settings. All configutation constants should be in here.
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_utils.py` - Utility functions
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_web_utils.py` - Web utilities
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_udp_manager.py` - Centralized UDP connection management
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_trading_pair_utils.py` - All methods for trading pair format conversion
- `/hummingbot/connector/derivative/qtx_perpetual/qtx_perpetual_auth.py` - Authentication with Binance API
- `/test/hummingbot/connector/derivative/qtx_perpetual/` - All test files should be here.

## Working with the QTX Connector

1. **Trading Pair Format**:

   - Hummingbot format: `BTC-USDT`
   - QTX UDP format: `binance-futures:btcusdt`
   - Binance API format: `BTCUSDT`

2. **Configuration**:

   ```python
   qtx_perpetual_host: str = "172.30.2.221"  # QTX UDP host
   qtx_perpetual_port: int = 8080  # QTX UDP port
   binance_api_key: str = "your_api_key"  # Binance API key
   binance_api_secret: str = "your_api_secret"  # Binance API secret
   ```

3. **Trading Modes**:
   - **Live Trading**: Connects to QTX market data feed and executes trades via Binance API

## Best Practices

1. **Error Handling**:

   - UDP connections may fail - the `QtxPerpetualUDPManager` includes reconnection logic
   - Handle Binance API errors gracefully through the delegation pattern
   - Never fall back to default values (like trading pairs) - if Binance is down, trading operations will fail
   - Log all errors with appropriate context

2. **Performance**:

   - UDP processing should be efficient to avoid missing messages
   - Use non-blocking I/O for socket operations
   - Consider the latency between market data and order execution
   - Cache trading pair conversions to avoid repeated processing

3. **Trading Pair Management**:

   - Always use the trading pairs from Binance's API rather than hardcoded defaults
   - Use the `QtxPerpetualTradingPairUtils` consistently for all conversions
   - Verify trading pairs exist on Binance before attempting to trade

4. **Testing**:

   - Implmement test for all the files like `/test/hummingbot/connector/derivative/binance_perpetual/`.
   - Run QTX perpetual and the Binance perpetual side-by-side with taker orders to see if QTX got better prices.
