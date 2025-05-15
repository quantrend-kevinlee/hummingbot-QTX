import os
import time
import logging
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import Field, field_validator

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, TradeType
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.core.event.events import (
    OrderFilledEvent,
    BuyOrderCreatedEvent,
    SellOrderCreatedEvent,
    BuyOrderCompletedEvent,
    SellOrderCompletedEvent,
    OrderCancelledEvent,
    OrderExpiredEvent,
    MarketOrderFailureEvent,
)


class QTXTestStrategy(ScriptStrategyBase):
    """
    A test strategy specifically designed to verify QTX's hedge mode with multiple positions.

    This strategy will:
    1. Place a market buy order (creates LONG position)
    2. Place a market sell order (creates SHORT position in HEDGE mode)
    3. Allow time to check status command to see both positions existing simultaneously
    4. Cancel all positions and orders

    This tests:
    - Order placement
    - HEDGE mode with simultaneous LONG and SHORT positions
    - Position tracking
    - Proper cleanup
    """

    # Key Parameters - ADJUST THESE
    connector_name = "qtx_perpetual"  # The connector to test
    trading_pair = "ETH-USDT"  # Trading pair to use for testing
    order_amount = Decimal("0.015")  # Small amount to minimize risk
    market_order_spread = Decimal("0.002")  # 0.2% spread for "market-like" limit orders
    status_check_delay = 5  # Seconds to wait for user to check status
    leverage = 1  # Leverage to use for the test
    position_mode = PositionMode.HEDGE  # Position mode to use

    # Required class attribute for all script strategies
    markets = {"qtx_perpetual": {"ETH-USDT"}}

    # States for our test flow
    placed_buy_order = False
    buy_order_id = None
    placed_sell_order = False
    sell_order_id = None
    waiting_for_status_check = False
    status_check_complete = False
    performed_cleanup = False
    test_complete = False

    # Order status tracking
    buy_order_filled = False
    sell_order_filled = False

    # Position tracking  
    long_position_amount = None
    short_position_amount = None

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

        # Step 2: Place market sell order (creates SHORT position)
        if not self.placed_sell_order:
            self.place_market_sell_order()
            return

        # Check if we have both positions
        if self.placed_buy_order and self.placed_sell_order and not self.waiting_for_status_check:
            # Check current positions
            connector = self.connectors[self.connector_name]
            positions = connector.account_positions

            # Look for LONG and SHORT positions
            has_long = False
            has_short = False
            
            for key, position in positions.items():
                if position.trading_pair == self.trading_pair and position.amount != 0:
                    if position.position_side.name == "LONG":
                        has_long = True
                        self.long_position_amount = position.amount
                    elif position.position_side.name == "SHORT":
                        has_short = True
                        self.short_position_amount = position.amount

            # If we have both positions, start waiting for status check
            if has_long and has_short:
                self.log_with_clock(
                    logging.INFO,
                    f"Both LONG and SHORT positions detected! You can use the 'status' command to see both positions. " +
                    f"Waiting {self.status_check_delay} seconds before cleanup..."
                )
                self.waiting_for_status_check = True
                self.last_action_timestamp = time.time()
                return

        # Wait for status check period
        if self.waiting_for_status_check and not self.status_check_complete:
            if current_time - self.last_action_timestamp >= self.status_check_delay:
                self.status_check_complete = True
                self.log_with_clock(logging.INFO, "Status check period complete. Proceeding with cleanup...")
                self.last_action_timestamp = time.time()
            return

        # Step 3: Perform cleanup to ensure all orders are cancelled and positions closed
        if self.status_check_complete and not self.performed_cleanup:
            self.cleanup_orders_and_positions()
            return

        # Test complete
        if not self.test_complete and self.performed_cleanup:
            self.log_with_clock(logging.INFO, "QTX test strategy complete! Check the logs for test results.")
            self.test_complete = True

    def initialize_exchange(self):
        """Initialize the exchange settings required for testing"""
        try:
            # Set leverage
            connector = self.connectors[self.connector_name]
            self.log_with_clock(logging.INFO, f"Setting leverage to {self.leverage} for {self.trading_pair}")
            connector.set_leverage(trading_pair=self.trading_pair, leverage=self.leverage)

            # Set position mode to HEDGE
            self.log_with_clock(logging.INFO, f"Setting position mode to {self.position_mode}")
            connector.set_position_mode(self.position_mode)

            self.log_with_clock(logging.INFO, f"Exchange configuration complete. Beginning order test sequence...")
            self.initialized = True
            self.last_action_timestamp = time.time()
        except Exception as e:
            self.log_with_clock(logging.ERROR, f"Error initializing exchange: {str(e)}")

    def place_market_buy_order(self):
        """
        Place a market buy order to create a LONG position.
        Using LIMIT order type with aggressive pricing to simulate market order.
        """
        try:
            connector = self.connectors[self.connector_name]
            mid_price = connector.get_mid_price(self.trading_pair)
            # Using a slightly higher price than market to increase fill likelihood
            buy_price = mid_price * (Decimal("1") + self.market_order_spread)

            self.log_with_clock(
                logging.INFO,
                f"Placing market BUY order to create LONG position: {self.order_amount} {self.trading_pair} @ {buy_price}",
            )

            self.buy(
                connector_name=self.connector_name,
                trading_pair=self.trading_pair,
                amount=self.order_amount,
                order_type=OrderType.LIMIT,  # Using LIMIT with aggressive pricing to simulate market order
                price=buy_price,
            )

            self.placed_buy_order = True
            self.last_action_timestamp = time.time()
        except Exception as e:
            self.log_with_clock(logging.ERROR, f"Error placing market buy order: {str(e)}")

    def place_market_sell_order(self):
        """
        Place a market sell order to create a SHORT position.
        Using LIMIT order type with aggressive pricing to simulate market order.
        """
        try:
            connector = self.connectors[self.connector_name]
            mid_price = connector.get_mid_price(self.trading_pair)
            # Using a slightly lower price than market to increase fill likelihood
            sell_price = mid_price * (Decimal("1") - self.market_order_spread)

            self.log_with_clock(
                logging.INFO,
                f"Placing market SELL order to create SHORT position: {self.order_amount} {self.trading_pair} @ {sell_price}",
            )

            self.sell(
                connector_name=self.connector_name,
                trading_pair=self.trading_pair,
                amount=self.order_amount,
                order_type=OrderType.LIMIT,  # Using LIMIT with aggressive pricing to simulate market order
                price=sell_price,
            )

            self.placed_sell_order = True
            self.last_action_timestamp = time.time()
        except Exception as e:
            self.log_with_clock(logging.ERROR, f"Error placing market sell order: {str(e)}")

    def cleanup_orders_and_positions(self):
        """
        Final cleanup step to ensure all orders are cancelled and positions are closed.
        This is important to leave the exchange in a clean state after testing.
        """
        try:
            self.log_with_clock(logging.INFO, "Performing final cleanup of orders and positions...")
            connector = self.connectors[self.connector_name]

            # Cancel all active orders
            active_orders = self.get_active_orders(self.connector_name)
            if active_orders:
                self.log_with_clock(logging.INFO, f"Cancelling {len(active_orders)} remaining active orders...")
                for order in active_orders:
                    if order.trading_pair == self.trading_pair:
                        self.log_with_clock(logging.INFO, f"Cancelling order {order.client_order_id}...")
                        self.cancel(self.connector_name, self.trading_pair, order.client_order_id)

            # Close all open positions
            positions = connector.account_positions
            for key, position in positions.items():
                if position.trading_pair == self.trading_pair and position.amount != 0:
                    self.log_with_clock(
                        logging.INFO,
                        f"Closing open position: {position.trading_pair} {position.position_side.name} {position.amount}",
                    )

                    # Get current market price
                    mid_price = connector.get_mid_price(self.trading_pair)

                    # Place an aggressive LIMIT order to close the position
                    # IMPORTANT: Use PositionAction.CLOSE to properly close positions
                    if position.position_side.name == "LONG":  # Long position, place sell order to close
                        sell_price = mid_price * (Decimal("1") - self.market_order_spread * 2)  # More aggressive
                        self.sell(
                            connector_name=self.connector_name,
                            trading_pair=self.trading_pair,
                            amount=abs(position.amount),
                            order_type=OrderType.LIMIT,
                            price=sell_price,
                            position_action=PositionAction.CLOSE,  # CRITICAL: Mark as closing order
                        )
                    elif position.position_side.name == "SHORT":  # Short position, place buy order to close
                        buy_price = mid_price * (Decimal("1") + self.market_order_spread * 2)  # More aggressive
                        self.buy(
                            connector_name=self.connector_name,
                            trading_pair=self.trading_pair,
                            amount=abs(position.amount),
                            order_type=OrderType.LIMIT,
                            price=buy_price,
                            position_action=PositionAction.CLOSE,  # CRITICAL: Mark as closing order
                        )

            self.performed_cleanup = True
            self.log_with_clock(
                logging.INFO, "Cleanup complete - any remaining orders will be cancelled and positions closed"
            )
            self.last_action_timestamp = time.time()
        except Exception as e:
            self.log_with_clock(logging.ERROR, f"Error during cleanup: {str(e)}")

    # ---------------------------------------- Event handlers to track order lifecycle ----------------------------------------

    def did_fill_order(self, event: OrderFilledEvent):
        msg = f"Order filled: {event.trade_type.name} {event.amount} of {event.trading_pair} at {event.price}"
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

        # Log positions after fill
        self.log_positions_status()

    def did_create_buy_order(self, event: BuyOrderCreatedEvent):
        msg = f"Created BUY order {event.order_id}"
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

        # Track our order ID
        if not self.buy_order_id and self.placed_buy_order:
            self.buy_order_id = event.order_id
            self.log_with_clock(logging.INFO, f"Tracking buy order ID: {event.order_id}")

    def did_create_sell_order(self, event: SellOrderCreatedEvent):
        msg = f"Created SELL order {event.order_id}"
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

        # Track our order ID
        if not self.sell_order_id and self.placed_sell_order:
            self.sell_order_id = event.order_id
            self.log_with_clock(logging.INFO, f"Tracking sell order ID: {event.order_id}")

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

        # Check if this is our buy order
        if event.order_id == self.buy_order_id:
            self.buy_order_filled = True
            self.log_with_clock(logging.INFO, "Buy order filled. LONG position should be open.")

        # Log positions
        self.log_positions_status()

    def did_complete_sell_order(self, event: SellOrderCompletedEvent):
        msg = f"Sell order {event.order_id} completed."
        self.log_with_clock(logging.INFO, msg)
        self.notify_hb_app_with_timestamp(msg)

        # Check if this is our sell order
        if event.order_id == self.sell_order_id:
            self.sell_order_filled = True
            self.log_with_clock(logging.INFO, "Sell order filled. SHORT position should be open.")

        # Log positions
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
                        if position.position_side.name == "LONG":
                            pnl_pct = ((current_price / position.entry_price) - 1) * 100
                            self.log_with_clock(logging.INFO, f"  PnL %: {pnl_pct:.2f}%")
                        elif position.position_side.name == "SHORT":
                            pnl_pct = ((position.entry_price / current_price) - 1) * 100
                            self.log_with_clock(logging.INFO, f"  PnL %: {pnl_pct:.2f}%")
                except Exception as price_error:
                    self.log_with_clock(logging.WARNING, f"  Could not calculate PnL %: {price_error}")

        except Exception as e:
            self.log_with_clock(logging.ERROR, f"Error logging positions: {str(e)}")
            import traceback
            self.log_with_clock(logging.ERROR, traceback.format_exc())

    def format_status(self) -> str:
        """Format status display with detailed order tracking and test progress"""
        if not hasattr(self, "connector_name"):
            return "Strategy not initialized."

        lines = []
        lines.extend(["\n", "  QTX Test Strategy Status:"])
        lines.extend([f"    Connector: {self.connector_name}"])
        lines.extend([f"    Trading Pair: {self.trading_pair}"])
        lines.extend([f"    Order Amount: {self.order_amount}"])
        lines.extend([f"    Leverage: {self.leverage}"])
        lines.extend([f"    Position Mode: {self.position_mode}"])

        # Current market data
        if hasattr(self, "connectors") and self.connector_name in self.connectors:
            connector = self.connectors[self.connector_name]
            try:
                mid_price = connector.get_mid_price(self.trading_pair)
                lines.extend([f"    Current Mid Price: {mid_price}"])
            except:
                pass

        # Test progress
        lines.extend(["\n", "  Test Progress:"])
        lines.extend(
            [
                f"    1. Placed Buy Order (LONG): {'✓' if self.placed_buy_order else '✗'}"
                + (f" (ID: {self.buy_order_id})" if self.buy_order_id else "")
                + (f" [FILLED]" if self.buy_order_filled else "")
            ]
        )
        lines.extend(
            [
                f"    2. Placed Sell Order (SHORT): {'✓' if self.placed_sell_order else '✗'}"
                + (f" (ID: {self.sell_order_id})" if self.sell_order_id else "")
                + (f" [FILLED]" if self.sell_order_filled else "")
            ]
        )
        lines.extend(
            [
                f"    3. Status Check Period: {'✓' if self.status_check_complete else ('In Progress' if self.waiting_for_status_check else '✗')}"
            ]
        )
        lines.extend([f"    4. Performed Final Cleanup: {'✓' if self.performed_cleanup else '✗'}"])
        lines.extend([f"    Test Complete: {'✓' if self.test_complete else '✗'}"])


        # Active orders
        if hasattr(self, "connectors") and self.connector_name in self.connectors:
            connector = self.connectors[self.connector_name]
            try:
                active_orders = connector._order_tracker.active_orders
                lines.extend(["\n", "  Active Orders:"])
                if len(active_orders) == 0:
                    lines.extend(["    No active orders"])
                else:
                    for order in active_orders.values():
                        order_type = "BUY" if order.trade_type is TradeType.BUY else "SELL"
                        lines.extend(
                            [
                                f"    Order ID: {order.client_order_id}",
                                f"      Exchange Order ID: {order.exchange_order_id}",
                                f"      Type: {order_type} {order.order_type.name}",
                                f"      Trading Pair: {order.trading_pair}",
                                f"      Price: {order.price}",
                                f"      Amount: {order.amount}",
                                f"      Filled: {order.executed_amount_base}/{order.amount}",
                                f"      Status: {order.current_state.name}",
                                "",
                            ]
                        )
            except Exception as e:
                lines.extend([f"\n  Error getting active orders: {str(e)}"])

        # Account information
        if hasattr(self, "connectors") and self.connector_name in self.connectors:
            try:
                connector = self.connectors[self.connector_name]
                positions = connector.account_positions

                # Format positions with more detail
                lines.extend(["\n", "  Open Positions:"])
                if len(positions) == 0:
                    lines.extend(["    No open positions"])
                else:
                    for key, position in positions.items():
                        pnl_pct = "N/A"
                        if position.entry_price and position.entry_price > 0:
                            try:
                                current_price = connector.get_mid_price(position.trading_pair)
                                if position.position_side.name == "LONG":
                                    pnl_pct = f"{((current_price / position.entry_price) - 1) * 100:.2f}%"
                                else:
                                    pnl_pct = f"{((position.entry_price / current_price) - 1) * 100:.2f}%"
                            except:
                                pass

                        lines.extend(
                            [
                                f"    {position.trading_pair} {position.position_side.name}:",
                                f"      Amount: {position.amount}",
                                f"      Entry Price: {position.entry_price}",
                                f"      Leverage: {position.leverage}",
                                f"      Unrealized PnL: {position.unrealized_pnl} ({pnl_pct})",
                            ]
                        )

            except Exception as e:
                lines.extend([f"\n  Error getting account info: {str(e)}"])

        return "\n".join(lines)