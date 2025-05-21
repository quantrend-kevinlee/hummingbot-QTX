import logging
import time
from decimal import Decimal

from hummingbot.core.data_type.common import OrderType, PositionMode, TradeType
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
    A simplified test strategy for QTX perpetual connector.

    This strategy will:
    1. Place a market buy order (creates LONG position)
    2. Place a distant limit buy order (should not get filled)
    3. Test completes after placing distant order

    This tests:
    - Market order placement
    - Position tracking
    - Multiple order placement
    """

    # Key Parameters - ADJUST THESE
    connector_name = "qtx_perpetual"  # The connector to test
    trading_pair = "ETH-USDT"  # Trading pair to use for testing
    order_amount = Decimal("0.015")  # Small amount to minimize risk
    distant_order_amount = Decimal("0.01")  # Amount for distant limit order
    leverage = 1  # Leverage to use for the test
    position_mode = PositionMode.ONEWAY  # Position mode to use: ONEWAY or HEDGE

    # Required class attribute for all script strategies
    markets = {"qtx_perpetual": {"ETH-USDT"}}

    # States for our test flow
    placed_buy_order = False
    buy_order_id = None
    buy_order_filled = False

    # Distant limit order tracking
    placed_distant_order = False
    distant_order_id = None
    test_complete = False

    # Position tracking
    long_position_amount = None
    position_entry_price = None

    # Order tracking for PnL calculation
    buy_fill_price = None

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
        if not self.placed_buy_order:
            self.place_market_buy_order()
            return

        # Step 2: Place a distant limit order (should not get filled)
        if self.placed_buy_order and not self.placed_distant_order and self.buy_order_filled:
            self.place_distant_limit_order()
            return

        # After distant order is placed, log positions and complete test
        if self.placed_distant_order and not self.test_complete:
            self.log_positions_status()
            self.log_with_clock(logging.INFO, "QTX test complete! Distant order placed and position opened.")
            self.test_complete = True

    def initialize_exchange(self):
        """Initialize the exchange settings required for testing"""
        try:
            # Set leverage
            connector = self.connectors[self.connector_name]
            self.log_with_clock(logging.INFO, f"Setting leverage to {self.leverage} for {self.trading_pair}")
            connector.set_leverage(trading_pair=self.trading_pair, leverage=self.leverage)

            # Set position mode to ONEWAY
            self.log_with_clock(logging.INFO, f"Setting position mode to {self.position_mode}")
            connector.set_position_mode(self.position_mode)

            self.log_with_clock(logging.INFO, "Exchange configuration complete. Beginning order test sequence...")
            self.initialized = True
            self.last_action_timestamp = time.time()
        except Exception as e:
            self.log_with_clock(logging.ERROR, f"Error initializing exchange: {str(e)}")

    def place_market_buy_order(self):
        """
        Place a limit buy order with aggressive price to simulate a market buy order.
        Using LIMIT order type at a price higher than current market for immediate execution.
        """
        try:
            connector = self.connectors[self.connector_name]
            # Get current market price
            current_price = connector.get_mid_price(self.trading_pair)

            # Calculate aggressive buy price (0.5% higher than current price)
            aggressive_price = current_price * Decimal("1.005")

            self.log_with_clock(
                logging.INFO,
                f"Simulating MARKET BUY using LIMIT order to create LONG position: {self.order_amount} {self.trading_pair} at aggressive price: {aggressive_price} (current price: {current_price})",
            )

            self.buy(
                connector_name=self.connector_name,
                trading_pair=self.trading_pair,
                amount=self.order_amount,
                order_type=OrderType.LIMIT,
                price=aggressive_price,  # Using aggressive price for immediate execution
            )

            self.placed_buy_order = True
            self.last_action_timestamp = time.time()
        except Exception as e:
            self.log_with_clock(logging.ERROR, f"Error placing simulated market buy order: {str(e)}")

    def place_distant_limit_order(self):
        """
        Place a limit buy order at a price unlikely to be filled.
        This tests the ability to place multiple orders and manage them.
        """
        try:
            connector = self.connectors[self.connector_name]
            # Get current market price
            current_price = connector.get_mid_price(self.trading_pair)

            # Calculate a very low buy price (20% below current price)
            distant_price = current_price * Decimal("0.80")

            self.log_with_clock(
                logging.INFO,
                f"Placing distant LIMIT BUY order (should not fill): {self.distant_order_amount} {self.trading_pair} at price: {distant_price} (current price: {current_price})",
            )

            order_id = self.buy(
                connector_name=self.connector_name,
                trading_pair=self.trading_pair,
                amount=self.distant_order_amount,
                order_type=OrderType.LIMIT,
                price=distant_price,
            )

            self.distant_order_id = order_id
            self.placed_distant_order = True
            self.last_action_timestamp = time.time()
            self.log_with_clock(logging.INFO, f"Placed distant limit order with ID: {order_id}")
        except Exception as e:
            self.log_with_clock(logging.ERROR, f"Error placing distant limit order: {str(e)}")
            # Even if the order fails, mark as placed to continue the test flow
            self.placed_distant_order = True

    # ---------------------------------------- Event handlers to track order lifecycle ----------------------------------------

    def did_fill_order(self, event: OrderFilledEvent):
        msg = f"Order filled: {event.trade_type.name} {event.amount} of {event.trading_pair} at {event.price}"
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

        # Track fill prices for PnL calculation
        if event.trade_type == TradeType.BUY:
            # Don't override buy_fill_price if it's already set - this would be for the main position
            if self.buy_fill_price is None:
                self.buy_fill_price = event.price

        # Log positions after fill
        self.log_positions_status()

    def did_create_buy_order(self, event: BuyOrderCreatedEvent):
        msg = f"Created BUY order {event.order_id}"
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

        # Track our order IDs
        if not self.buy_order_id and self.placed_buy_order and not self.placed_distant_order:
            self.buy_order_id = event.order_id
            self.log_with_clock(logging.INFO, f"Tracking main buy order ID: {event.order_id}")

        # If we already placed the main buy order and are now placing the distant order
        elif self.placed_buy_order and self.placed_distant_order and not self.distant_order_id:
            self.distant_order_id = event.order_id
            self.log_with_clock(logging.INFO, f"Tracking distant buy order ID: {event.order_id}")

    def did_create_sell_order(self, event: SellOrderCreatedEvent):
        msg = f"Created SELL order {event.order_id}"
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

    def did_cancel_order(self, event: OrderCancelledEvent):
        msg = f"Cancelled order {event.order_id}"
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

    def did_expire_order(self, event: OrderExpiredEvent):
        msg = f"Order {event.order_id} expired"
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

    def did_fail_order(self, event: MarketOrderFailureEvent):
        msg = f"Order {event.order_id} failed"
        self.log_with_clock(logging.ERROR, msg)
        self.notify_hb_app_with_timestamp(msg)

    def did_complete_buy_order(self, event: BuyOrderCompletedEvent):
        msg = f"Buy order {event.order_id} completed."
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

        # Check if this is our main buy order
        if event.order_id == self.buy_order_id:
            self.buy_order_filled = True
            self.log_with_clock(logging.INFO, "Main buy order filled. LONG position should be open.")

        # Check if this is our distant order (unlikely)
        elif event.order_id == self.distant_order_id:
            self.log_with_clock(logging.WARNING, "Distant limit order was filled! This was unexpected.")

        # Log positions
        self.log_positions_status()

    def did_complete_sell_order(self, event: SellOrderCompletedEvent):
        msg = f"Sell order {event.order_id} completed."
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

        # Log positions after position is closed
        self.log_positions_status()

    # ---------------------------------------- Helper functions ----------------------------------------

    def log_positions_status(self):
        """Log current positions status"""
        try:
            connector = self.connectors[self.connector_name]
            positions = connector.account_positions

            self.log_with_clock(logging.INFO, "Current Positions:")
            self.log_with_clock(logging.INFO, f"Position count: {len(positions)}")

            if len(positions) == 0:
                self.log_with_clock(logging.INFO, "  No open positions")
                return

            # Log each position with full details
            for key, position in positions.items():
                if position.trading_pair == self.trading_pair:
                    self.log_with_clock(
                        logging.INFO,
                        f"  Position Key: {key}\n"
                        f"  Trading Pair: {position.trading_pair}\n"
                        f"  Position Side: {position.position_side.name}\n"
                        f"  Amount: {position.amount}\n"
                        f"  Entry Price: {position.entry_price}\n"
                        f"  Leverage: {position.leverage}\n"
                        f"  Unrealized PnL: {position.unrealized_pnl}\n",
                    )

                    # Check if we can get the current price to calculate PnL percentage
                    try:
                        current_price = connector.get_mid_price(position.trading_pair)
                        if position.entry_price and position.entry_price > 0:
                            if position.amount > 0:  # LONG position
                                pnl_pct = ((current_price / position.entry_price) - 1) * 100
                                self.log_with_clock(logging.INFO, f"  PnL %: {pnl_pct:.2f}%")
                            elif position.amount < 0:  # SHORT position
                                pnl_pct = ((position.entry_price / current_price) - 1) * 100
                                self.log_with_clock(logging.INFO, f"  PnL %: {pnl_pct:.2f}%")
                    except Exception as price_error:
                        self.log_with_clock(logging.WARNING, f"  Could not calculate PnL %: {price_error}")

        except Exception as e:
            self.log_with_clock(logging.ERROR, f"Error logging positions: {str(e)}")
            import traceback

            self.log_with_clock(logging.ERROR, traceback.format_exc())
