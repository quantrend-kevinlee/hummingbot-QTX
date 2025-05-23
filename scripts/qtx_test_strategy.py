import asyncio
import logging
import time
from decimal import Decimal
from typing import Dict, List

from hummingbot.core.data_type.common import OrderType, PositionMode
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class QtxTestStrategy(ScriptStrategyBase):
    """
    Test strategy for QTX to validate order placement, cancellation, and market order handling.

    This strategy will:
    1. Place and cancel a LIMIT BUY order
    2. Place and cancel a LIMIT SELL order
    3. Place a MARKET BUY order to open a long position (and leave it open)
    """

    # Define stages as class constants
    STAGE_INITIALIZE = "initialize"
    STAGE_TEST_BUY_CANCEL = "test_buy_cancel"
    STAGE_TEST_SELL_CANCEL = "test_sell_cancel"
    STAGE_OPEN_POSITION = "open_position"
    STAGE_DONE = "done"

    # Strategy parameters
    connector_name = "qtx_perpetual"
    trading_pair = "ETH-USDT"
    order_amount = Decimal("0.01")  # Small amount for testing
    limit_price_offset = Decimal("0.05")  # 5% offset for limit orders

    # Define markets as a class attribute
    markets = {connector_name: {trading_pair}}

    # Order tracking
    order_details: Dict[str, Dict] = {}  # Store all order details
    cancelled_order_ids: List[str] = []
    filled_order_ids: List[str] = []

    # Stage management
    current_stage = STAGE_INITIALIZE
    last_action_timestamp = None
    stage_timeout = 3  # seconds to wait between stages
    cancel_wait_time = 2  # seconds to wait before cancelling

    # Statistics
    orders_placed = 0
    orders_filled = 0
    orders_cancelled = 0

    # Test duration
    test_duration = 60  # 1 minute total test duration
    start_time = None

    # Track if we have a pending order
    pending_order_id = None
    pending_order_placed_time = None

    # Prevent multiple market orders
    market_order_placed = False

    def on_tick(self):
        """Main strategy logic executed on each tick."""
        # Initialize timestamps on first tick
        if self.start_time is None:
            self.start_time = time.time()
        if self.last_action_timestamp is None:
            self.last_action_timestamp = time.time()

        # Check if test duration exceeded
        if time.time() - self.start_time > self.test_duration:
            if self.current_stage != self.STAGE_DONE:
                self.finish_test()
            return

        # Don't process if already done
        if self.current_stage == self.STAGE_DONE:
            return

        # Handle pending order cancellation
        if self.pending_order_id and self.pending_order_placed_time:
            if time.time() - self.pending_order_placed_time >= self.cancel_wait_time:
                self.cancel_pending_order()
                return

        # Execute stage-specific logic
        if self.current_stage == self.STAGE_INITIALIZE:
            self.initialize_exchange()
        elif self.current_stage == self.STAGE_TEST_BUY_CANCEL:
            if self.check_stage_timeout() and not self.pending_order_id:
                self.test_buy_cancel()
        elif self.current_stage == self.STAGE_TEST_SELL_CANCEL:
            if self.check_stage_timeout() and not self.pending_order_id:
                self.test_sell_cancel()
        elif self.current_stage == self.STAGE_OPEN_POSITION:
            if self.check_stage_timeout() and not self.pending_order_id and not self.market_order_placed:
                self.open_position()

    def check_stage_timeout(self):
        """Check if enough time has passed since the last action."""
        return time.time() - self.last_action_timestamp >= self.stage_timeout

    def cancel_pending_order(self):
        """Cancel the pending order."""
        if self.pending_order_id:
            # Check if order is still active
            active_orders = self.get_active_orders(self.connector_name)
            is_still_active = any(order.client_order_id == self.pending_order_id for order in active_orders)

            if is_still_active:
                self.log_with_clock(logging.INFO, f"Cancelling order: {self.pending_order_id}")
                self.cancel(self.connector_name, self.trading_pair, self.pending_order_id)

                # Update order status
                if self.pending_order_id in self.order_details:
                    self.order_details[self.pending_order_id]["status"] = "CANCELLED"
                self.cancelled_order_ids.append(self.pending_order_id)
                self.orders_cancelled += 1
            else:
                # Order is no longer active (either filled or failed)
                if self.pending_order_id not in self.filled_order_ids:
                    # Must have failed
                    if self.pending_order_id in self.order_details:
                        self.order_details[self.pending_order_id]["status"] = "FAILED"

            # Reset pending order tracking
            self.pending_order_id = None
            self.pending_order_placed_time = None

            # Progress to next stage
            if self.current_stage == self.STAGE_TEST_BUY_CANCEL:
                self.current_stage = self.STAGE_TEST_SELL_CANCEL
                self.last_action_timestamp = time.time()
            elif self.current_stage == self.STAGE_TEST_SELL_CANCEL:
                self.current_stage = self.STAGE_OPEN_POSITION
                self.last_action_timestamp = time.time()

    def did_fill_order(self, event: OrderFilledEvent):
        """Handle order fill events."""
        self.orders_filled += 1
        self.filled_order_ids.append(event.order_id)

        # Update order details
        if event.order_id in self.order_details:
            self.order_details[event.order_id]["status"] = "FILLED"
            self.order_details[event.order_id]["fill_price"] = float(event.price)
            self.order_details[event.order_id]["fill_amount"] = float(event.amount)
            self.order_details[event.order_id]["fee"] = str(event.trade_fee)

        if event.order_id == self.pending_order_id:
            self.pending_order_id = None
            self.pending_order_placed_time = None

        # Progress to next stage if this was the final BUY order
        if self.current_stage == self.STAGE_OPEN_POSITION:
            self.log_with_clock(logging.INFO, "Final BUY order filled - Position opened")
            # Add a small delay to let all events process before finishing
            safe_ensure_future(self._delayed_finish_test())

    def did_create_buy_order(self, event: BuyOrderCreatedEvent):
        """Handle buy order creation events."""
        # Store order details
        self.order_details[event.order_id] = {
            "order_id": event.order_id,
            "type": "BUY",
            "order_type": event.type.name,
            "amount": float(event.amount),
            "price": (
                float(event.price)
                if event.type == OrderType.LIMIT and not str(event.price).lower() in ["nan", "none"]
                else "MARKET"
            ),
            "status": "CREATED",
            "created_time": time.time(),
        }

    def did_create_sell_order(self, event: SellOrderCreatedEvent):
        """Handle sell order creation events."""
        # Store order details
        self.order_details[event.order_id] = {
            "order_id": event.order_id,
            "type": "SELL",
            "order_type": event.type.name,
            "amount": float(event.amount),
            "price": (
                float(event.price)
                if event.type == OrderType.LIMIT and not str(event.price).lower() in ["nan", "none"]
                else "MARKET"
            ),
            "status": "CREATED",
            "created_time": time.time(),
        }

    def did_complete_buy_order(self, event: BuyOrderCompletedEvent):
        """Handle buy order completion events."""
        # This is called when an order is fully filled
        pass

    def did_complete_sell_order(self, event: SellOrderCompletedEvent):
        """Handle sell order completion events."""
        # This is called when an order is fully filled
        pass

    def initialize_exchange(self):
        """Set up exchange configuration"""
        connector = self.connectors[self.connector_name]

        # Wait for valid prices
        mid_price = connector.get_mid_price(self.trading_pair)
        if mid_price is None or mid_price <= 0:
            return

        # Configure exchange (silently)
        connector.set_leverage(self.trading_pair, 1)

        # Check position mode
        try:
            current_mode = connector.position_mode
            if current_mode != PositionMode.ONEWAY:
                self.log_with_clock(
                    logging.WARNING,
                    f"WARNING: Account is in {current_mode} mode. This test is designed for ONE-WAY mode!",
                )
        except Exception:
            pass

        self.log_with_clock(logging.INFO, "Starting QTX order test sequence...")
        self.current_stage = self.STAGE_TEST_BUY_CANCEL
        self.last_action_timestamp = time.time()

    def test_buy_cancel(self):
        """Place a LIMIT BUY order that will be cancelled."""
        connector = self.connectors[self.connector_name]
        current_price = connector.get_mid_price(self.trading_pair)

        if current_price is None or current_price <= 0:
            return

        # Place limit buy order below market price
        limit_price = current_price * (Decimal("1") - self.limit_price_offset)

        self.log_with_clock(
            logging.INFO, f"Placing LIMIT BUY order at {limit_price:.2f} (will cancel in {self.cancel_wait_time}s)"
        )

        order_id = self.buy(
            connector_name=self.connector_name,
            trading_pair=self.trading_pair,
            amount=self.order_amount,
            order_type=OrderType.LIMIT,
            price=limit_price,
        )

        self.pending_order_id = order_id
        self.pending_order_placed_time = time.time()
        self.orders_placed += 1

    def test_sell_cancel(self):
        """Place a LIMIT SELL order that will be cancelled."""
        connector = self.connectors[self.connector_name]
        current_price = connector.get_mid_price(self.trading_pair)

        if current_price is None or current_price <= 0:
            return

        # Place limit sell order above market price
        limit_price = current_price * (Decimal("1") + self.limit_price_offset)

        self.log_with_clock(
            logging.INFO, f"Placing LIMIT SELL order at {limit_price:.2f} (will cancel in {self.cancel_wait_time}s)"
        )

        order_id = self.sell(
            connector_name=self.connector_name,
            trading_pair=self.trading_pair,
            amount=self.order_amount,
            order_type=OrderType.LIMIT,
            price=limit_price,
        )

        self.pending_order_id = order_id
        self.pending_order_placed_time = time.time()
        self.orders_placed += 1

    def open_position(self):
        """Place a MARKET BUY order to open a position."""
        connector = self.connectors[self.connector_name]
        current_price = connector.get_mid_price(self.trading_pair)

        if current_price is None or current_price <= 0:
            return

        self.log_with_clock(logging.INFO, "Placing MARKET BUY order to open position")

        order_id = self.buy(
            connector_name=self.connector_name,
            trading_pair=self.trading_pair,
            amount=self.order_amount,
            order_type=OrderType.MARKET,
        )

        self.pending_order_id = order_id
        self.orders_placed += 1
        self.market_order_placed = True

    def finish_test(self):
        """Complete the test and show detailed statistics."""
        self.current_stage = self.STAGE_DONE

        # Get final position
        try:
            connector = self.connectors[self.connector_name]
            positions = connector.account_positions
            position_info = "No open positions"

            if positions:
                for key, position in positions.items():
                    if hasattr(position, "amount") and position.amount != 0:
                        side = "LONG" if position.amount > 0 else "SHORT"
                        position_info = (
                            f"{side} {abs(position.amount)} {key} @ {getattr(position, 'entry_price', 'N/A')}"
                        )
                        break
        except Exception:
            position_info = "Could not retrieve position"

        # Build detailed summary
        self.log_with_clock(
            logging.INFO,
            f"\n{'=' * 60}\n"
            f"QTX ORDER TEST COMPLETED\n"
            f"{'=' * 60}\n"
            f"Test Duration: {time.time() - self.start_time:.1f} seconds\n"
            f"Final Position: {position_info}\n"
            f"\nOrder Summary:\n"
            f"  Total Orders Placed: {self.orders_placed}\n"
            f"  Orders Filled: {self.orders_filled}\n"
            f"  Orders Cancelled: {self.orders_cancelled}\n"
            f"\nOrder Details:",
        )

        # Log each order's details
        for order_id, details in self.order_details.items():
            status_emoji = {"FILLED": "✅", "CANCELLED": "❌", "FAILED": "⚠️", "CREATED": "⏳"}.get(
                details["status"], "❓"
            )

            order_info = (
                f"\n  {status_emoji} Order #{order_id[-6:]}: {details['type']} {details['order_type']} "
                f"{details['amount']} ETH @ {details['price']}"
            )

            if details["status"] == "FILLED":
                order_info += f" -> Filled @ {details.get('fill_price', 'N/A')}"
                if "fee" in details:
                    order_info += f" (Fee: {details['fee']})"
            elif details["status"] == "CANCELLED":
                order_info += " -> Cancelled"
            elif details["status"] == "FAILED":
                order_info += " -> Failed"

            self.log_with_clock(logging.INFO, order_info)

        self.log_with_clock(logging.INFO, f"\n{'=' * 60}")

    async def _delayed_finish_test(self):
        """Finish the test with a small delay to allow all events to process."""
        await asyncio.sleep(0.5)  # Short delay to let events complete
        self.finish_test()
