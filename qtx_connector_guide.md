# QTX Connector Implementation Guide

## Overview

This document outlines the implementation of the QTX connector for Hummingbot, focusing on:

1. Current implementation with simulated trading features
2. Integration with Hummingbot's paper trading system
3. Fee handling similar to Binance's approach
4. Migration path when QTX adds more API features

## Current Implementation

The QTX connector is currently implemented with real market data integration but simulated trading functionality. This hybrid approach allows strategies to be tested using real-time QTX market data but without actual order execution.

### Key Components

1. **Market Data**: Connected to real QTX UDP feed
2. **Order Execution**: Currently simulated
3. **Account Management**: Currently simulated
4. **Fee Structure**: Using Binance-like fee structure (0.1% maker/taker)

## Paper Trading Integration

The connector works with Hummingbot's built-in paper trading system in two ways:

1. **Direct Usage**: The connector can be used directly with simulated order execution
2. **Paper Trading Wrapper**: The connector can be wrapped with Hummingbot's `PaperTradeExchange` wrapper

When using the official paper trade mode (`qtx_paper_trade`), Hummingbot:

1. Creates a non-trading QTX connector instance
2. Wraps it with `PaperTradeExchange`
3. Uses real QTX market data but simulates all trading operations

## Fee Implementation

We've implemented fee handling that directly uses Binance's fee structure with a simplified code structure:

1. **Fee Schema**: Directly using Binance's fee structure
   ```python
   # In Binance's binance_utils.py
   DEFAULT_FEES = TradeFeeSchema(
       maker_percent_fee_decimal=Decimal("0.001"),
       taker_percent_fee_decimal=Decimal("0.001"),
       buy_percent_fee_deducted_from_returns=True,
   )
   ```

2. **Fee Methods**: Implemented in `QtxExchange` class to use Binance's fees
   ```python
   def trade_fee_schema(self) -> TradeFeeSchema:
       # Use Binance's fee structure
       from hummingbot.connector.exchange.binance.binance_utils import DEFAULT_FEES as BINANCE_DEFAULT_FEES
       
       # When QTX implements its own fee structure, this method would be updated to use QTX-specific fees
       # For now, we're using Binance's fee structure as requested
       return BINANCE_DEFAULT_FEES
       
   def _get_fee(self, ...):
       # Directly calculate fees using the fee schema
       is_maker = order_type is OrderType.LIMIT_MAKER
       fee_schema = self.trade_fee_schema()
       fee_pct = fee_schema.maker_percent_fee_decimal if is_maker else fee_schema.taker_percent_fee_decimal
       return DeductedFromReturnsTradeFee(percent=fee_pct)
   ```

3. **Trading Fee Updates**: Implemented `_update_trading_fees` to use Binance's fee structure while preserving the migration path
   ```python
   async def _update_trading_fees(self):
       # FUTURE IMPLEMENTATION:
       # When QTX implements fee endpoints, the code would look like this:
       # try:
       #     response = await self._api_request(
       #         method=RESTMethod.GET,
       #         path_url="api/v1/trading-fee",  # QTX endpoint for trading fees
       #         is_auth_required=True
       #     )
       #     # Process response and update trading fees...
       
       # CURRENT IMPLEMENTATION:
       # For now, we're directly using Binance's fee structure
       from hummingbot.connector.exchange.binance.binance_utils import DEFAULT_FEES as BINANCE_DEFAULT_FEES
       
       # Apply Binance's fee structure to all trading pairs
       for trading_pair in self.trading_pairs:
           self._trading_fees[trading_pair] = BINANCE_DEFAULT_FEES
   ```

## Migration Path for Full QTX Integration

When QTX implements additional API features, the following methods should be updated:

### 1. Order Execution

| Current Method | Status | Future Implementation |
|----------------|--------|----------------------|
| `_place_order` | Simulation | Connect to QTX API order endpoint |
| `_place_cancel` | Simulation | Connect to QTX API cancel endpoint |
| `_execute_buy`/`_execute_sell` | Simulation | Use `_place_order` with real API |
| `_simulate_order_fills` | Complete simulation | Remove and use real order status |

### 2. Account Management

| Current Method | Status | Future Implementation |
|----------------|--------|----------------------|
| `_update_balances` | Simulation with fixed values | Fetch real balances from QTX API |
| `_update_order_status` | Empty placeholder | Implement to poll order status from API |
| `_user_stream_event_listener` | Empty placeholder | Connect to websocket/real-time events |

### 3. Fee Structure

| Current Method | Status | Future Implementation |
|----------------|--------|----------------------|
| `_update_trading_fees` | Uses DEFAULT_FEES | Fetch actual fee structure from QTX API |
| `trade_fee_schema` | Returns static schema | May need update if QTX has dynamic fees |

## Implementation Details

### Fee Calculation Flow

1. `estimate_fee_pct(is_maker)` determines the fee percentage based on order type
2. `_get_fee()` uses this to create the appropriate fee object
3. These fees are applied in:
   - Order simulation
   - Trade fill records
   - Balance updates

### Order Matching Logic

The current simulation matches orders based on the following criteria:

```python
# For buy orders
if order.trade_type == TradeType.BUY:
    lowest_ask = Decimal(str(order_book.get_price(False)))
    if lowest_ask <= order.price:
        price_condition_met = True
        
# For sell orders
elif order.trade_type == TradeType.SELL:
    highest_bid = Decimal(str(order_book.get_price(True)))
    if highest_bid >= order.price:
        price_condition_met = True
```

This logic is compatible with how Hummingbot's official paper trading works.

## Testing

1. Test the connector in both direct mode and with paper trading wrapper
2. Verify fee calculations match expectations
3. Ensure order simulation works correctly with real market data

## Future Development

1. **API Integration**: When QTX provides full API documentation, update methods to use real endpoints
2. **Real-time Events**: Implement proper websocket/event handling when available
3. **Dynamic Fees**: Integrate with QTX fee structure when available

## Conclusion

The current implementation provides a solid foundation with real market data and simulated trading. The fee structure follows industry standards and will be easy to update when QTX provides more API capabilities.
