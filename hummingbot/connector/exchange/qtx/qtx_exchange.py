#!/usr/bin/env python

import asyncio
import socket
import time
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.qtx import qtx_utils
from hummingbot.connector.exchange.qtx.qtx_api_order_book_data_source import QTXAPIOrderBookDataSource
from hummingbot.connector.exchange.qtx.qtx_auth import QtxAuth
from hummingbot.connector.exchange.qtx.qtx_user_stream_data_source import QTXAPIUserStreamDataSource
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.cancellation_result import CancellationResult
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TradeFeeBase, build_trade_fee
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.event.events import BuyOrderCompletedEvent, OrderFilledEvent, SellOrderCompletedEvent
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.model.trade_fill import TradeFill

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter


class QtxExchange(ExchangePyBase):
    """
    QtxExchange connects to the QTX UDP market data source for real-time market data.
    Trading functionality is currently simulated until QTX provides order execution endpoints.
    When QTX adds trading API endpoints, this connector can be updated to use them without changing the interface.
    """

    def __init__(
        self,
        client_config_map: "ClientConfigAdapter",
        qtx_api_key: str,
        qtx_secret_key: str,
        qtx_host: str,
        qtx_port: int,
        trading_pairs: List[str],
        trading_required: bool = True,
        **kwargs,  # Accept arbitrary keyword args to prevent TypeError
    ):
        self._qtx_host = qtx_host
        self._qtx_port = qtx_port
        self.api_key = qtx_api_key
        self.secret_key = qtx_secret_key

        # Assign the renamed parameter to the internal _trading_pairs attribute
        self._trading_pairs = trading_pairs

        # Initialize other attributes
        self._trading_required = trading_required
        self._web_assistants_factory = None
        self._api_factory = None
        # Internal state for orders and trades (simulated until API endpoints are available)
        self._orders_cache: Dict[str, Dict[str, Any]] = {}
        self._trades_cache: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        # Call parent constructor after initializing our fields
        super().__init__(client_config_map)

    async def _make_trading_pairs_request(self) -> Optional[Dict[str, Any]]:
        """
        Override the base class method to prevent REST API call for trading pairs.
        Returns None as the actual mapping is handled by
        _initialize_trading_pair_symbols_from_exchange_info based on configured pairs.
        """

        return None

    @property
    def name(self) -> str:
        return "qtx"

    @property
    def trigger_event(self, event_tag: int, event: any):
        """
        Triggers specified event to the hummingbot application
        """
        if self._events_listener is not None:
            self._events_listener(event_tag, event)
        # Note: Trade recording is handled by the paper trade layer when running in paper trade mode
        # We don't need to manually record trades here

    @property
    def trading_pairs(self) -> List[str]:
        """
        Return trading pairs, ensuring we have valid pairs.
        """
        if not hasattr(self, "_trading_pairs"):
            return []
        if not isinstance(self._trading_pairs, list):
            return []
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    @property
    def authenticator(self) -> Optional[AuthBase]:
        """Authentication for API calls."""
        return QtxAuth(api_key=self.api_key, secret_key=self.secret_key, time_provider=self._time_synchronizer)

    def supported_order_types(self) -> List[OrderType]:
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET]

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        """
        Creates the order book data source for QTX market data.
        """
        return QTXAPIOrderBookDataSource(trading_pairs=self.trading_pairs, connector=self)

    def _create_user_stream_data_source(self) -> Optional[UserStreamTrackerDataSource]:
        """
        Returns a simulated UserStreamTrackerDataSource to satisfy application requirements
        when running a trading strategy with this connector.
        """
        self.logger().info("Using simulated user stream data source.")
        return QTXAPIUserStreamDataSource()

    def _get_fee(
        self,
        base_currency: str,
        quote_currency: str,
        order_type: OrderType,
        order_side: TradeType,
        amount: Decimal,
        price: Decimal = s_decimal_NaN,
        is_maker: Optional[bool] = None,
    ) -> TradeFeeBase:
        """
        Return a flat zero fee for market data only connector.
        """
        return DeductedFromReturnsTradeFee(percent=Decimal("0"))

    async def get_last_traded_price(self, trading_pair: str) -> float:
        """
        Get the last traded price from the order book if available.

        :param trading_pair: The trading pair to get the price for
        :return: The last traded price or 0 if not available
        """
        orderbook = self.get_order_book(trading_pair)
        if orderbook is not None and orderbook.last_trade_price is not None:
            return float(orderbook.last_trade_price)
        return 0.0

    async def get_last_traded_prices(self, trading_pairs: List[str]) -> Dict[str, float]:
        """
        Gets the last traded price for multiple trading pairs.

        :param trading_pairs: List of trading pairs to get prices for
        :return: Dict mapping each trading pair to its last traded price
        """
        result = {}
        for trading_pair in trading_pairs:
            result[trading_pair] = await self.get_last_traded_price(trading_pair)
        return result

    async def exchange_symbol_associated_to_pair(self, trading_pair: str) -> str:
        """
        Formats a trading pair into the exchange symbol format.
        For QTX, we'll use a simplified format.

        :param trading_pair: The trading pair in Hummingbot format (e.g., BTC-USDT)
        :return: The trading pair in exchange format (e.g., btcusdt)
        """
        return qtx_utils.format_trading_pair(trading_pair)

    async def trading_pair_associated_to_exchange_symbol(self, symbol: str) -> str:
        """
        Converts an exchange symbol to Hummingbot trading pair format.

        :param symbol: Symbol in exchange format
        :return: Trading pair in Hummingbot format
        """
        return qtx_utils.convert_from_exchange_trading_pair(symbol)

    async def get_trading_rules(self) -> List[TradingRule]:
        """
        Returns placeholder trading rules for market data only.
        Uses the _trading_pairs list initialized in __init__.
        """
        results = []
        for trading_pair in self.trading_pairs:
            if not isinstance(trading_pair, str):
                self.logger().warning(f"Invalid trading_pair type found in list: {trading_pair}. Skipping.")
                continue
            try:
                min_price_increment = Decimal("0.00001")
                min_base_increment = Decimal("0.001")
                min_notional = Decimal("0.01")

                results.append(
                    TradingRule(
                        trading_pair=trading_pair,
                        min_order_size=Decimal("0.001"),
                        min_price_increment=min_price_increment,
                        min_base_amount_increment=min_base_increment,
                        min_notional_size=min_notional,
                    )
                )
            except Exception as e:
                self.logger().error(f"Error creating trading rule for {trading_pair}: {e}", exc_info=True)
        return results

    async def execute_buy(
        self, order_id: str, trading_pair: str, amount: Decimal, order_type: OrderType, price: Decimal
    ) -> str:
        """
        Simulated buy order execution for paper trading with QTX market data.
        Creates an in-flight order and simulates the order being placed.
        """
        self.logger().debug(f"Executing buy order {order_id} for {amount} {trading_pair} at {price}")

        # Create an in-flight order
        order = InFlightOrder(
            client_order_id=order_id,
            exchange_order_id=f"qtx_{order_id}",  # Simulated exchange order ID
            trading_pair=trading_pair,
            order_type=order_type,
            trade_type=TradeType.BUY,
            amount=amount,
            price=price,
            creation_timestamp=self.current_timestamp,
        )

        # Store the order
        self._order_tracker.start_tracking_order(order)

        # Simulate order being placed successfully
        # In a real implementation, this would make an API call
        self.logger().info(f"Simulated buy order {order_id} placed successfully")

        # Simulate order update
        order_update = OrderUpdate(
            client_order_id=order_id,
            exchange_order_id=f"qtx_{order_id}",
            trading_pair=trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=OrderState.OPEN,  # Order is now open
        )
        self._order_tracker.process_order_update(order_update)

        # Return the client order ID
        return order_id

    async def execute_sell(
        self, order_id: str, trading_pair: str, amount: Decimal, order_type: OrderType, price: Decimal
    ) -> str:
        """
        Simulated sell order execution for paper trading with QTX market data.
        Creates an in-flight order and simulates the order being placed.
        """
        self.logger().debug(f"Executing sell order {order_id} for {amount} {trading_pair} at {price}")

        # Create an in-flight order
        order = InFlightOrder(
            client_order_id=order_id,
            exchange_order_id=f"qtx_{order_id}",  # Simulated exchange order ID
            trading_pair=trading_pair,
            order_type=order_type,
            trade_type=TradeType.SELL,
            amount=amount,
            price=price,
            creation_timestamp=self.current_timestamp,
        )

        # Store the order
        self._order_tracker.start_tracking_order(order)

        # Simulate order being placed successfully
        # In a real implementation, this would make an API call
        self.logger().info(f"Simulated sell order {order_id} placed successfully")

        # Simulate order update
        order_update = OrderUpdate(
            client_order_id=order_id,
            exchange_order_id=f"qtx_{order_id}",
            trading_pair=trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=OrderState.OPEN,  # Order is now open
        )
        self._order_tracker.process_order_update(order_update)

        # Return the client order ID
        return order_id

    async def cancel_all(self, timeout_seconds: float) -> List[CancellationResult]:
        """
        Cancels all active orders.
        Returns a list of CancellationResult objects indicating success/failure per order.
        """
        self.logger().debug(f"Cancelling all active orders (timeout: {timeout_seconds}s)")

        # Get all active orders
        cancellation_results = []
        active_orders = self.in_flight_orders.copy()

        for client_order_id, order in active_orders.items():
            try:
                # Simulate cancellation
                await self.cancel(client_order_id, order.trading_pair)
                cancellation_results.append(CancellationResult(client_order_id, True))
            except Exception as e:
                self.logger().error(f"Failed to cancel order {client_order_id}: {e}")
                cancellation_results.append(CancellationResult(client_order_id, False))

        return cancellation_results

    def buy(
        self,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType = OrderType.LIMIT,
        price: Decimal = s_decimal_NaN,
        **kwargs,
    ) -> str:
        """
        Creates a buy order in the exchange using the parameters.
        This is a synchronous wrapper around the asyncio execute_buy method.
        """
        # Generate a client order ID
        client_order_id = self.generate_client_order_id(True, trading_pair)

        # Create asyncio task to execute the buy order
        safe_ensure_future(
            self.execute_buy(
                client_order_id,
                trading_pair,
                amount,
                order_type,
                price,
            )
        )

        # Return the client order ID
        return client_order_id

    def sell(
        self,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType = OrderType.LIMIT,
        price: Decimal = s_decimal_NaN,
        **kwargs,
    ) -> str:
        """
        Creates a sell order in the exchange using the parameters.
        This is a synchronous wrapper around the asyncio execute_sell method.
        """
        # Generate a client order ID
        client_order_id = self.generate_client_order_id(False, trading_pair)

        # Create asyncio task to execute the sell order
        safe_ensure_future(
            self.execute_sell(
                client_order_id,
                trading_pair,
                amount,
                order_type,
                price,
            )
        )

        # Return the client order ID
        return client_order_id

    def cancel(self, order_id: str, trading_pair: str = None):
        """
        Cancels an order in the exchange.
        This is a synchronous wrapper around the asyncio _execute_cancel method.
        """
        safe_ensure_future(self._execute_cancel(order_id, trading_pair))
        return order_id

    async def _execute_cancel(self, order_id: str, trading_pair: str):
        """
        Executes order cancellation process.
        """
        self.logger().debug(f"Cancelling order {order_id}")

        # Check if order exists
        if order_id not in self.in_flight_orders:
            self.logger().warning(f"Attempted to cancel order {order_id}, but order not found in tracking list")
            # Consider raising an exception here if needed
            return

        # Get the order
        tracked_order = self.in_flight_orders[order_id]

        # Simulate cancellation
        # In a real implementation, this would make an API call
        self.logger().info(f"Simulated cancellation of {order_id}")

        # Update order state
        order_update = OrderUpdate(
            client_order_id=order_id,
            exchange_order_id=tracked_order.exchange_order_id,
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=OrderState.CANCELED,  # Order is now cancelled
        )
        self._order_tracker.process_order_update(order_update)

        return order_update

    async def _update_balances(self):
        """
        Update account balances.
        Currently uses simulated values until API endpoints are available.
        """
        # Clear existing balances
        self._account_available_balances.clear()
        self._account_balances.clear()

        # For all configured pairs, create initial balances
        # Ensure we use the property to get the pairs correctly
        pairs_to_use = self.trading_pairs  # Use the property
        if not pairs_to_use:
            self.logger().warning("Cannot update balances: No trading pairs available.")
            return

        for trading_pair in pairs_to_use:
            try:
                base, quote = trading_pair.split("-")
                # Initial balance values (will be replaced with API calls in the future)
                self._account_balances[base] = Decimal("10.0")  # Base asset balance
                self._account_balances[quote] = Decimal("10000.0")  # Quote asset balance
                self._account_available_balances[base] = Decimal("10.0")  # Available base
                self._account_available_balances[quote] = Decimal("10000.0")  # Available quote
            except ValueError:
                self.logger().error(f"Could not split trading pair '{trading_pair}' to update balances.")

        self.logger().debug("Updated account balances.")

    async def _update_trading_rules(self):
        """
        Fetch and update trading rules for all trading pairs.
        """
        self._trading_rules = {
            trading_rule.trading_pair: trading_rule for trading_rule in await self.get_trading_rules()
        }

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        """
        Initialize trading pair symbols directly from the configured list.
        The exchange_info parameter is ignored since we're not querying an actual exchange.
        """
        self._trading_pair_symbol_map = {}
        self._symbol_trading_pair_map = {}

        # Use the trading_pairs property to ensure we have valid pairs
        pairs_to_map = self.trading_pairs

        for trading_pair in pairs_to_map:
            # Convert to exchange symbol format (lowercase, no separator)
            exchange_symbol = qtx_utils.format_trading_pair(trading_pair)
            self._trading_pair_symbol_map[trading_pair] = exchange_symbol
            self._symbol_trading_pair_map[exchange_symbol] = trading_pair

    async def _status_polling_loop_fetch_updates(self):
        """
        Simulated implementation for fetching status updates periodically.
        This will update orders, balances, and positions as needed.
        It also simulates order fills based on market data.
        """
        # Update the balances first
        await self._update_balances()

        # Update open orders
        await self._update_order_status()

        # Update lost orders if any
        await self._update_lost_orders_status()

        # Process any simulated fills for open orders
        await self._simulate_order_fills()

    async def _simulate_order_fills(self):
        """
        Simulates order fills based on current market data.
        This method is called periodically to check if any open orders should be filled.
        """
        if not self.in_flight_orders:
            return  # No open orders to process

        # Process each open order
        for order_id, order in list(self.in_flight_orders.items()):
            # Skip orders that are not in OPEN state
            if order.current_state != OrderState.OPEN:
                continue

            try:
                # Get the order book for this trading pair
                order_book = self.get_order_book(order.trading_pair)
                if order_book is None:
                    self.logger().warning(f"No order book available for {order.trading_pair}")
                    continue

                # For limit orders, check if the price conditions are met
                if order.order_type == OrderType.LIMIT:
                    # For buy orders, check if lowest ask <= order price
                    # For sell orders, check if highest bid >= order price
                    price_condition_met = False

                    if order.trade_type == TradeType.BUY:
                        lowest_ask = Decimal(str(order_book.get_price(False)))
                        if lowest_ask <= order.price:
                            price_condition_met = True
                    elif order.trade_type == TradeType.SELL:
                        highest_bid = Decimal(str(order_book.get_price(True)))
                        if highest_bid >= order.price:
                            price_condition_met = True

                    if not price_condition_met:
                        continue  # Price conditions not met

                # Simulate the fill using the order book
                base_asset, quote_asset = self.split_trading_pair(order.trading_pair)

                # Get current balances
                quote_balance = self._account_balances.get(quote_asset, Decimal("0"))
                base_balance = self._account_balances.get(base_asset, Decimal("0"))

                # Determine fill price and amount based on order book
                if order.trade_type == TradeType.BUY:
                    # Simulate buy using order book
                    fill_price = (
                        order.price
                        if order.order_type == OrderType.LIMIT
                        else Decimal(str(order_book.get_price(False)))
                    )
                    fill_amount = order.amount

                    # Check if we have enough quote balance
                    quote_amount_needed = fill_amount * fill_price
                    if quote_amount_needed > quote_balance:
                        self.logger().warning(f"Insufficient {quote_asset} balance for buy order {order_id}")
                        continue

                    # Update balances
                    fee_amount = fill_amount * Decimal("0.001")  # 0.1% fee
                    self._account_balances[quote_asset] = quote_balance - quote_amount_needed
                    self._account_balances[base_asset] = base_balance + fill_amount - fee_amount
                    self._account_available_balances[quote_asset] = self._account_balances[quote_asset]
                    self._account_available_balances[base_asset] = self._account_balances[base_asset]

                    fee_asset = base_asset

                else:  # SELL
                    # Simulate sell using order book
                    fill_price = (
                        order.price if order.order_type == OrderType.LIMIT else Decimal(str(order_book.get_price(True)))
                    )
                    fill_amount = order.amount

                    # Check if we have enough base balance
                    if fill_amount > base_balance:
                        self.logger().warning(f"Insufficient {base_asset} balance for sell order {order_id}")
                        continue

                    # Update balances
                    quote_amount_received = fill_amount * fill_price
                    fee_amount = quote_amount_received * Decimal("0.001")  # 0.1% fee
                    self._account_balances[base_asset] = base_balance - fill_amount
                    self._account_balances[quote_asset] = quote_balance + quote_amount_received - fee_amount
                    self._account_available_balances[base_asset] = self._account_balances[base_asset]
                    self._account_available_balances[quote_asset] = self._account_balances[quote_asset]

                    fee_asset = quote_asset

                # Create trade update and order filled event
                trade_id = f"qtx_trade_{order_id}_{int(time.time() * 1000)}"

                # Create and process the trade update
                trade_update = TradeUpdate(
                    trade_id=trade_id,
                    client_order_id=order_id,
                    exchange_order_id=order.exchange_order_id,
                    trading_pair=order.trading_pair,
                    fee=fee_amount,
                    fee_asset=fee_asset,
                    trade_type=order.trade_type,
                    order_type=order.order_type,
                    price=fill_price,
                    amount=fill_amount,
                    trade_timestamp=self.current_timestamp,
                )
                self._order_tracker.process_trade_update(trade_update)

                # Create and emit order filled event
                order_filled_event = OrderFilledEvent(
                    timestamp=self.current_timestamp,
                    order_id=order_id,
                    trading_pair=order.trading_pair,
                    trade_type=order.trade_type,
                    order_type=order.order_type,
                    price=fill_price,
                    amount=fill_amount,
                    trade_fee=build_trade_fee(
                        self.name,
                        is_maker=False,
                        base_currency=base_asset,
                        quote_currency=quote_asset,
                        order_type=order.order_type,
                        order_side=order.trade_type,
                        amount=fill_amount,
                        price=fill_price,
                        fee_percent=Decimal("0.001"),  # 0.1% fee
                    ),
                    exchange_trade_id=trade_id,
                )
                self.trigger_event(self.ORDER_FILLED_EVENT_TAG, order_filled_event)

                # Update order state to filled
                order_update = OrderUpdate(
                    client_order_id=order_id,
                    exchange_order_id=order.exchange_order_id,
                    trading_pair=order.trading_pair,
                    update_timestamp=self.current_timestamp,
                    new_state=OrderState.FILLED,
                )
                self._order_tracker.process_order_update(order_update)

                # Emit order completed event
                if order.trade_type == TradeType.BUY:
                    self.trigger_event(
                        self.BUY_ORDER_COMPLETED_EVENT_TAG,
                        BuyOrderCompletedEvent(
                            timestamp=self.current_timestamp,
                            order_id=order_id,
                            base_asset=base_asset,
                            quote_asset=quote_asset,
                            base_asset_amount=fill_amount - fee_amount if fee_asset == base_asset else fill_amount,
                            quote_asset_amount=quote_amount_needed,
                            order_type=order.order_type,
                        ),
                    )
                else:  # SELL
                    self.trigger_event(
                        self.SELL_ORDER_COMPLETED_EVENT_TAG,
                        SellOrderCompletedEvent(
                            timestamp=self.current_timestamp,
                            order_id=order_id,
                            base_asset=base_asset,
                            quote_asset=quote_asset,
                            base_asset_amount=fill_amount,
                            quote_asset_amount=(
                                quote_amount_received - fee_amount
                                if fee_asset == quote_asset
                                else quote_amount_received
                            ),
                            order_type=order.order_type,
                        ),
                    )

                self.logger().info(
                    f"Simulated fill for {order.trade_type.name} order {order_id}: {fill_amount} {base_asset} "
                    f"at {fill_price} {quote_asset}"
                )

            except Exception as e:
                self.logger().error(f"Error simulating fill for order {order_id}: {e}", exc_info=True)

    def _fill_order_from_event(self, event: OrderFilledEvent):
        """
        Record order fills to the database for the history command to work properly.
        This is called automatically by trigger_event when an OrderFilledEvent is emitted.
        """
        try:
            # Remove this condition so it always tries to record the trade
            # if not hasattr(self, "_order_tracker_task"):
            #    return
            self.logger().info(f"Attempting to record trade for order {event.order_id}")
            # Try to access the hummingbot application through connector hierarchy
            # This is a bit of a hack but needed to get to the trade_fill_db
            import hummingbot.client.hummingbot_application as hummingbot_app

            hb_instance = hummingbot_app.get_hummingbot_application()
            if hb_instance is not None:
                config_path = hb_instance.strategy_file_name
                strategy = hb_instance.strategy_name if hasattr(hb_instance, "strategy_name") else ""
                # Parse trading pair to get base and quote assets
                try:
                    base_asset, quote_asset = event.trading_pair.split("-")
                except Exception:  # Use explicit Exception instead of bare except
                    base_asset = ""
                    quote_asset = ""

                self.logger().info(f"Creating trade record for {event.trading_pair} - {event.order_id}")
                # Create a trade record
                trade = TradeFill(
                    config_file_path=config_path,
                    strategy=strategy,
                    market=self.name,
                    symbol=event.trading_pair,
                    base_asset=base_asset,
                    quote_asset=quote_asset,
                    timestamp=event.timestamp,
                    order_id=event.order_id,
                    trade_type=str(event.trade_type),
                    order_type=str(event.order_type),
                    price=Decimal(str(event.price)),
                    amount=Decimal(str(event.amount)),
                    trade_fee=event.trade_fee.to_json(),
                    exchange_trade_id=event.exchange_trade_id,
                    leverage=1,  # Default to spot trading leverage
                )

                # Save to database
                if hasattr(hb_instance, "trade_fill_db"):
                    self.logger().info(f"Saving trade to database: {trade}")
                    hb_instance.trade_fill_db.save_trade_fill(trade)
                else:
                    self.logger().error("Could not save trade: trade_fill_db not found")
            else:
                self.logger().error("Could not record trade: Hummingbot application instance not found")
        except Exception as e:
            self.logger().error(f"Error recording trade: {e}", exc_info=True)

    async def check_network(self) -> NetworkStatus:
        """
        Checks UDP connectivity to the QTX feed.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            sock.sendto(b"", (self._qtx_host, self._qtx_port))
            sock.close()
            return NetworkStatus.CONNECTED
        except Exception as e:
            self.logger().warning(f"UDP network check failed: {e}")
            return NetworkStatus.NOT_CONNECTED

    @property
    def rate_limits_rules(self) -> List[RateLimit]:
        """No defined rate limits for this connector."""
        return []

    @property
    def domain(self) -> str:
        """No specific REST domain for this connector."""
        return "qtx_domain"

    @property
    def client_order_id_max_length(self) -> Optional[int]:
        return None

    @property
    def client_order_id_prefix(self) -> Optional[str]:
        return None

    @property
    def trading_pairs_request_path(self) -> str:
        """Path not used due to override."""
        return "/not_used"

    @property
    def trading_rules_request_path(self) -> str:
        """Path not used, rules generated internally."""
        return "/not_used"

    @property
    def check_network_request_path(self) -> str:
        """Path not used, check_network overridden."""
        return "/not_used"

    def _create_web_assistants_factory(self) -> Optional[WebAssistantsFactory]:
        """
        Create a minimal WebAssistantsFactory instance to satisfy status checks.
        """
        from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
        from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

        # Create a throttler if it doesn't exist
        if self._throttler is None:
            self._throttler = AsyncThrottler(rate_limits=[])

        return WebAssistantsFactory(throttler=self._throttler, auth=self._auth)

    async def _format_trading_rules(self, exchange_info: Any) -> List[TradingRule]:
        """
        Override: Trading rules are generated directly in get_trading_rules.
        The exchange_info parameter is ignored.
        """
        return await self.get_trading_rules()

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception) -> bool:
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return False

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return False

    # TRADING METHODS (currently simulated, will be replaced with API calls when available)

    async def _place_order(
        self,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        trade_type: TradeType,
        order_type: OrderType,
        price: Decimal,
        **kwargs,
    ) -> Tuple[str, float]:
        # Create a simulated exchange order ID (will be replaced with real API response)
        simulated_exchange_order_id = f"qtx_{int(time.time() * 1e6)}_{order_id[-4:]}"
        timestamp = time.time()
        return simulated_exchange_order_id, timestamp

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
        # Return success for cancellation (will be replaced with actual API response)
        return True

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        # Return the current order status (will be replaced with API response)
        return OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=tracked_order.exchange_order_id,
            trading_pair=tracked_order.trading_pair,
            update_timestamp=time.time(),
            new_state=OrderState.PENDING_CREATE,
        )

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        # Return empty list as no actual trades happen
        return []

    async def _update_trading_fees(self):
        """
        Update trading fees.
        Currently uses simulated values until fee endpoints are available.
        """
        self.logger().debug("Updating trading fees")
        for trading_pair in self.trading_pairs:
            # Create zero fee entries for all trading pairs
            self._trading_fees[trading_pair] = {
                "maker": Decimal("0.0"),
                "taker": Decimal("0.0"),
            }

    def c_tick(self, timestamp: float):
        """
        Override the tick method from the base connector to handle any potential clock errors.
        This method is called by the strategy's tick method.
        """
        try:
            super().tick(timestamp)
        except Exception as e:
            self.logger().error(f"Error in QTX connector tick: {str(e)}", exc_info=True)
            # Continue execution to avoid stopping the strategy due to clock errors

    async def _user_stream_event_listener(self):
        """
        Listen for user account updates via WebSocket or other real-time protocol.
        Currently simulated - will be implemented when QTX provides user stream API.
        """
        while True:
            try:
                # Placeholder until user stream API is available
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in user stream listener: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _update_order_status(self):
        """
        Polls the REST API to get order status updates.
        Will be implemented when QTX provides order status API endpoints.
        """
        # Currently handled by _simulate_order_fills until API endpoints are available
        pass

    async def _update_lost_orders_status(self):
        """
        Checks for any orders that might have been lost or missed by normal status updates.
        Will be implemented when QTX provides order history API endpoints.
        """
        # Currently not needed with simulated orders, will be implemented with real API

    @property
    def status_dict(self) -> Dict[str, bool]:
        """
        Returns a dictionary with status indicators for the connector's readiness.
        """
        # Get base status from parent class
        status = super().status_dict

        # Log the status details for debugging
        self.logger().info(f"QTX connector status: {status}")
        self.logger().info(f"Order book tracker ready: {self.order_book_tracker.ready}")
        self.logger().info(f"Trading pairs: {self.trading_pairs}")

        # Update status with QTX-specific indicators
        custom_status = {
            "trading_rule_initialized": True,  # Trading rules are statically defined for now
            "user_stream_initialized": True,  # User stream is simulated for now
            "account_balance": True,  # Account balance is simulated for now
            "symbols_mapping_initialized": True,  # Always true for QTX internal mapping
        }

        # Return the combined status
        return {**status, **custom_status}

    async def check_order_book_status(self) -> Dict[str, Any]:
        """
        Diagnostic function to check the status of the order books for all trading pairs.
        """
        result = {
            "connector": self.name,
            "status": "operational" if self._order_book_initialized.is_set() else "initializing",
            "trading_pairs_configured": self._trading_pairs,
            "order_book_status": {},
        }

        # Check each trading pair's order book status
        for trading_pair in self._trading_pairs:
            ob_status = {"exists": trading_pair in self.order_book_tracker.order_books}

            if ob_status["exists"]:
                order_book = self.order_book_tracker.order_books[trading_pair]
                ob_status.update(
                    {
                        "bids": len(order_book.bid_entries()),
                        "asks": len(order_book.ask_entries()),
                        "has_snapshot": order_book.snapshot_message_count > 0,
                        "last_diff_uid": order_book._last_diff_uid,
                        "snapshot_uid": order_book._snapshot_uid,
                    }
                )

            result["order_book_status"][trading_pair] = ob_status

        self.logger().info(f"Order book status: {result}")
        return result

    async def _update_time_synchronizer(self, time_provider: Optional[Callable] = None):
        """
        Override base method to prevent time synchronization attempts
        as this connector does not use REST APIs requiring it currently.
        """
        self.logger().debug("Skipping time synchronization for QTX market data connector.")
        pass
