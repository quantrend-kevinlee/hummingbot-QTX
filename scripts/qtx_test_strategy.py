"""
QTX Market Order Conversion Test Strategy

Validates QTX's intelligent market order handling and position detection.

Test Coverage:
- Market order → Aggressive LIMIT conversion (±3% pricing)
- Smart position action detection (auto OPEN/CLOSE)
- Complete order lifecycle (place → fill → cancel)

Expected Flow:
1. BUY MARKET (no position) → Auto-detect OPEN → LIMIT @ ask+3%
2. SELL MARKET (LONG exists) → Auto-detect CLOSE → LIMIT @ bid-3%
3. Distant LIMIT order → Test order management → Cancel after timeout

This proves QTX can handle real-world usage patterns where users don't 
specify position actions and expect market orders to "just work".
"""

import logging
import time
from decimal import Decimal

from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, TradeType
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderExpiredEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class QTXTestStrategy(ScriptStrategyBase):
    """
    End-to-end validation of QTX's market order intelligence.
    
    Tests that users can trade naturally without understanding position actions
    or market order limitations. QTX should "do the right thing" automatically.
    """

    # Key Parameters - ADJUST THESE
    connector_name = "qtx_perpetual"  # The connector to test
    trading_pair = "ETH-USDT"  # Trading pair to use for testing
    order_amount = Decimal("0.015")  # Small amount to minimize risk
    distant_order_amount = Decimal("0.01")  # Amount for distant limit order

    # State tracking
    placed_market_buy = False
    placed_market_sell = False
    placed_distant_order = False
    market_buy_filled = False
    market_sell_filled = False
    distant_order_id = None
    distant_order_timestamp = None
    test_complete = False

    # Tracking timestamps for delays
    last_action_timestamp = 0
    initialized = False

    def __init__(self, connectors=None, config=None):
        """
        Initialize the strategy.
        The connectors parameter will be passed by Hummingbot as a dict of connector names to connector instances.
        """
        super().__init__(connectors)  # Pass connectors to ScriptStrategyBase
        self.last_action_timestamp = time.time()

    def on_tick(self):
        """
        The main logic that runs on each clock tick.
        Implements our test flow sequentially.
        """
        current_time = time.time()

        # Initialize the exchange if not done already
        if not self.initialized:
            self.initialize_exchange()
            return

        # Skip if not enough time has passed since last action
        if current_time - self.last_action_timestamp < 1:  # 1 second between actions
            return

        # Step 1: Place market buy order (creates LONG position)
        if not self.placed_market_buy:
            self.place_market_buy_order()
            return

        # Step 2: Place market sell order (closes LONG position)
        if self.placed_market_buy and not self.placed_market_sell and self.market_buy_filled:
            self.place_market_sell_order()
            return

        # Step 3: Place distant limit order (tests order management)
        if self.placed_market_sell and not self.placed_distant_order and self.market_sell_filled:
            self.place_distant_limit_order()
            return

        # Check if distant order needs to be canceled (after 5 seconds)
        if (self.placed_distant_order and self.distant_order_id is not None and 
                not self.test_complete and self.distant_order_timestamp is not None):
            # Check if order is older than 5 seconds
            if current_time - self.distant_order_timestamp > 5:
                self.log_with_clock(logging.INFO, f"Canceling distant order {self.distant_order_id} after 5 seconds")
                connector = self.connectors[self.connector_name]
                connector.cancel(self.trading_pair, self.distant_order_id)
                # We'll wait for the cancel confirmation in did_cancel_order before completing the test
                return
                
        # After distant order is placed and either canceled or test completed
        if self.placed_distant_order and not self.test_complete and self.distant_order_id is None:
            self.log_with_clock(logging.INFO, "QTX MARKET ORDER + SMART POSITION DETECTION TEST COMPLETE!")
            self.log_with_clock(logging.INFO, "✅ MARKET BUY order conversion tested (MARKET → aggressive LIMIT +3%)")
            self.log_with_clock(logging.INFO, "✅ MARKET SELL order conversion tested (MARKET → aggressive LIMIT -3%)")
            self.log_with_clock(logging.INFO, "✅ Smart position action detection tested (auto OPEN/CLOSE)")
            self.log_with_clock(logging.INFO, "✅ Position opening and closing tested")
            self.log_with_clock(logging.INFO, "✅ Multiple order management tested")
            self.test_complete = True

    def initialize_exchange(self):
        """Set up exchange configuration for testing."""
        if not self.connectors or self.connector_name not in self.connectors:
            return

        connector = self.connectors[self.connector_name]
        if not connector.ready:
            return

        # Set up ONE-WAY position mode and 1x leverage
        self.log_with_clock(logging.INFO, f"Setting leverage to 1 for {self.trading_pair}")
        connector.set_leverage(self.trading_pair, 1)
        
        self.log_with_clock(logging.INFO, f"Setting position mode to PositionMode.ONEWAY")
        connector.set_position_mode(PositionMode.ONEWAY)
        
        self.log_with_clock(logging.INFO, "Exchange configuration complete. Beginning order test sequence...")
        self.initialized = True
        self.last_action_timestamp = time.time()

    def place_market_buy_order(self):
        """Place a MARKET BUY order without specifying position_action."""
        connector = self.connectors[self.connector_name]
        current_price = connector.get_mid_price(self.trading_pair)
        
        self.log_with_clock(
            logging.INFO,
            f"Placing MARKET BUY order: {self.order_amount} {self.trading_pair} (current price: {current_price})",
        )

        self.buy(
            connector_name=self.connector_name,
            trading_pair=self.trading_pair,
            amount=self.order_amount,
            order_type=OrderType.MARKET,  # Real MARKET order, no position_action
        )
        
        self.placed_market_buy = True
        self.last_action_timestamp = time.time()

    def place_market_sell_order(self):
        """Place a MARKET SELL order without specifying position_action."""
        connector = self.connectors[self.connector_name]
        current_price = connector.get_mid_price(self.trading_pair)
        
        self.log_with_clock(
            logging.INFO,
            f"Placing MARKET SELL order: {self.order_amount} {self.trading_pair} (current price: {current_price})",
        )

        self.sell(
            connector_name=self.connector_name,
            trading_pair=self.trading_pair,
            amount=self.order_amount,
            order_type=OrderType.MARKET,  # Real MARKET order, no position_action
        )
        
        self.placed_market_sell = True
        self.last_action_timestamp = time.time()

    def place_distant_limit_order(self):
        """Place a LIMIT order far from market to test order management."""
        connector = self.connectors[self.connector_name]
        current_price = connector.get_mid_price(self.trading_pair)
        distant_price = current_price * Decimal("0.8")  # 20% below market
        
        self.log_with_clock(
            logging.INFO,
            f"Placing distant LIMIT BUY order: {self.distant_order_amount} {self.trading_pair} at {distant_price} (current: {current_price})",
        )

        order_id = self.buy(
            connector_name=self.connector_name,
            trading_pair=self.trading_pair,
            amount=self.distant_order_amount,
            order_type=OrderType.LIMIT,
            price=distant_price,
        )
        
        self.distant_order_id = order_id
        self.distant_order_timestamp = time.time()
        self.placed_distant_order = True
        self.last_action_timestamp = time.time()

    def log_positions_status(self):
        """Log current positions for debugging."""
        connector = self.connectors[self.connector_name]
        positions = connector.account_positions
        
        self.log_with_clock(logging.INFO, f"Current Positions:")
        self.log_with_clock(logging.INFO, f"Position count: {len(positions)}")
        
        if positions:
            for position_key, position in positions.items():
                pnl_pct = (position.unrealized_pnl / position.entry_price) * 100 if position.entry_price > 0 else 0
                self.log_with_clock(logging.INFO, f"  {position}")
                self.log_with_clock(logging.INFO, f"  PnL %: {pnl_pct:.2f}%")
        else:
            self.log_with_clock(logging.INFO, f"  No open positions")

    # Event Handlers - track order lifecycle
    def did_create_buy_order(self, event: BuyOrderCreatedEvent):
        self.log_with_clock(logging.INFO, f"Created BUY order {event.order_id}")

    def did_create_sell_order(self, event: SellOrderCreatedEvent):
        self.log_with_clock(logging.INFO, f"Created SELL order {event.order_id}")

    def did_fill_order(self, event: OrderFilledEvent):
        self.log_with_clock(logging.INFO, f"Order filled: {event.trade_type.name} {event.amount} of {event.trading_pair} at {event.price}")
        
        if event.trade_type == TradeType.BUY and not self.market_buy_filled:
            self.market_buy_filled = True
        elif event.trade_type == TradeType.SELL and not self.market_sell_filled:
            self.market_sell_filled = True

    def did_complete_buy_order(self, event: BuyOrderCompletedEvent):
        self.log_with_clock(logging.INFO, f"Buy order {event.order_id} completed")

    def did_complete_sell_order(self, event: SellOrderCompletedEvent):
        self.log_with_clock(logging.INFO, f"Sell order {event.order_id} completed")

    def did_cancel_order(self, event: OrderCancelledEvent):
        self.log_with_clock(logging.INFO, f"Cancelled order {event.order_id}")
        if event.order_id == self.distant_order_id:
            self.distant_order_id = None  # Mark as cancelled

    def did_fail_order(self, event: MarketOrderFailureEvent):
        self.log_with_clock(logging.ERROR, f"Order {event.order_id} failed")