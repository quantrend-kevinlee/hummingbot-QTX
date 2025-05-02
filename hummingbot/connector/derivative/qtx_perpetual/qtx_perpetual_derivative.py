#!/usr/bin/env python

import asyncio
import socket
import time
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, AsyncIterable, Dict, List, Optional, Tuple

from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.derivative.qtx_perpetual import (
    qtx_perpetual_constants as CONSTANTS,
    qtx_perpetual_utils,
    qtx_perpetual_web_utils as web_utils,
)
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_api_order_book_data_source import (
    QtxPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_auth import QtxPerpetualAuth
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_user_stream_data_source import (
    QtxPerpetualUserStreamDataSource,
)
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter


class QtxPerpetualDerivative(PerpetualDerivativePyBase):
    """
    QtxPerpetualDerivative connects to the QTX UDP market data source for real-time market data.
    Trading functionality is currently simulated until QTX provides order execution endpoints.
    When QTX adds trading API endpoints, this connector can be updated to use them without changing the interface.
    """

    web_utils = web_utils
    SHORT_POLL_INTERVAL = 5.0
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    LONG_POLL_INTERVAL = 120.0

    def __init__(
        self,
        client_config_map: "ClientConfigAdapter",
        qtx_perpetual_api_key: str = None,
        qtx_perpetual_api_secret: str = None,
        qtx_perpetual_host: str = CONSTANTS.DEFAULT_UDP_HOST,
        qtx_perpetual_port: int = CONSTANTS.DEFAULT_UDP_PORT,
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        domain: str = CONSTANTS.DOMAIN,
    ):
        self.qtx_perpetual_api_key = qtx_perpetual_api_key
        self.qtx_perpetual_secret_key = qtx_perpetual_api_secret
        self._qtx_perpetual_host = qtx_perpetual_host
        self._qtx_perpetual_port = qtx_perpetual_port
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._domain = domain
        self._position_mode = PositionMode.ONEWAY  # Default position mode
        self._leverage = defaultdict(lambda: Decimal("1"))  # Default leverage is 1x
        self._funding_payment_span = CONSTANTS.FUNDING_SETTLEMENT_DURATION
        self._last_trade_history_timestamp = None

        # Internal state for orders and trades (simulated until API endpoints are available)
        self._orders_cache: Dict[str, Dict[str, Any]] = {}
        self._trades_cache: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._positions_cache: Dict[str, Dict[PositionSide, Position]] = defaultdict(dict)
        self._next_trade_id = 0  # Initialize trade ID counter

        # Initialize funding info
        self._funding_info = {}
        self._funding_info_stream = None

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
        return CONSTANTS.EXCHANGE_NAME

    @property
    def authenticator(self) -> QtxPerpetualAuth:
        return QtxPerpetualAuth(self.qtx_perpetual_api_key, self.qtx_perpetual_secret_key, self._time_synchronizer)

    @property
    def rate_limits_rules(self) -> List[RateLimit]:
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def client_order_id_max_length(self) -> int:
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self) -> str:
        return CONSTANTS.BROKER_ID

    @property
    def trading_rules_request_path(self) -> str:
        return CONSTANTS.EXCHANGE_INFO_PATH_URL

    @property
    def trading_pairs_request_path(self) -> str:
        return CONSTANTS.EXCHANGE_INFO_PATH_URL

    @property
    def check_network_request_path(self) -> str:
        return CONSTANTS.PING_PATH_URL
        
    async def check_network(self) -> NetworkStatus:
        """
        Checks UDP connectivity to the QTX feed.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            sock.sendto(b"", (self._qtx_perpetual_host, self._qtx_perpetual_port))
            sock.close()
            return NetworkStatus.CONNECTED
        except Exception as e:
            self.logger().warning(f"UDP network check failed: {e}")
            return NetworkStatus.NOT_CONNECTED

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    @property
    def funding_fee_poll_interval(self) -> int:
        return 600  # 10 minutes
        
    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        """
        Retrieves all trade updates for a specific order from the trades cache
        Returns a list of TradeUpdate objects
        """
        client_order_id = order.client_order_id
        trades = []
        
        # Get trade updates from the internal cache
        for trade in self._trades_cache.get(client_order_id, []):
            trades.append(
                TradeUpdate(
                    trade_id=trade["id"],
                    client_order_id=client_order_id,
                    exchange_order_id=trade["order_id"],
                    trading_pair=order.trading_pair,
                    fee=TradeFeeBase.new_spot_fee(
                        fee_schema=self.trade_fee_schema(),
                        trade_type=order.trade_type,
                        percent=trade.get("fee_percent", Decimal("0")),
                        flat_fees=[TokenAmount(
                            trade.get("fee_asset", "USDT"),
                            Decimal(trade.get("fee_amount", "0"))
                        )]
                    ),
                    fill_base_amount=Decimal(trade["amount"]),
                    fill_quote_amount=Decimal(trade["amount"]) * Decimal(trade["price"]),
                    fill_price=Decimal(trade["price"]),
                    fill_timestamp=trade["timestamp"],
                )
            )
        
        return trades
    
    async def _fetch_last_fee_payment(self, trading_pair: str) -> Tuple[int, Decimal]:
        """
        Simulates fetching the last funding fee payment
        Returns the timestamp and amount of the last funding fee payment
        """
        last_timestamp = self._last_funding_fee_payment_ts.get(trading_pair, 0)
        return last_timestamp, Decimal("0")  # For simulation, return 0 funding fee
    
    def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> Dict[str, TradingRule]:
        """
        Creates trading rules from exchange info for all available trading pairs
        Returns a dictionary of trading pair to TradingRule
        """
        trading_rules = {}
        
        # Create simulated trading rules with standard values
        for trading_pair in self._trading_pairs or []:
            trading_rules[trading_pair] = TradingRule(
                trading_pair=trading_pair,
                min_order_size=Decimal("0.001"),
                min_price_increment=Decimal("0.01"),
                min_base_amount_increment=Decimal("0.001"),
                min_notional_size=Decimal("10"),
                max_order_size=Decimal("1000"),
            )
        
        return trading_rules
    
    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]) -> None:
        """
        Initialize trading pair symbols directly from the configured list.
        The exchange_info parameter is ignored since we're not querying an actual exchange.
        """
        self._trading_pair_symbol_map = {}
        self._symbol_trading_pair_map = {}
        
        # Use the trading_pairs property to ensure we have valid pairs
        pairs_to_map = self._trading_pairs or []
        
        for trading_pair in pairs_to_map:
            # Convert to exchange symbol format (e.g., 'binance-futures:btcusdt')
            exchange_symbol = qtx_perpetual_utils.convert_to_exchange_trading_pair(trading_pair)
            self._trading_pair_symbol_map[trading_pair] = exchange_symbol
            self._symbol_trading_pair_map[exchange_symbol] = trading_pair
            
        # Initialize simulated trading rules
        self._trading_rules.clear()
        self._trading_rules = self._format_trading_rules({})
    
    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
        """
        Simulates order cancellation by updating the order status in the internal cache
        Returns whether the cancellation was successful
        """
        if order_id not in self._orders_cache:
            return False
        
        # Update order status to CANCELED
        self._orders_cache[order_id]["status"] = "CANCELED"
        return True
    
    async def _place_order(
        self,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        trade_type: TradeType,
        order_type: OrderType,
        price: Decimal,
        position_action: PositionAction = PositionAction.NIL,
        **kwargs,
    ) -> Tuple[str, float]:
        """
        Simulates order placement by creating an order in the internal cache
        Returns the exchange order ID and the timestamp of order creation
        """
        timestamp = self.current_timestamp
        exchange_order_id = f"s-{order_id}"  # prefix with 's-' to indicate simulated
        
        # Simulate order placement in internal cache
        self._orders_cache[order_id] = {
            "id": exchange_order_id,
            "client_order_id": order_id,
            "trading_pair": trading_pair,
            "amount": amount,
            "trade_type": trade_type,
            "order_type": order_type,
            "price": price,
            "position_action": position_action,
            "status": "NEW",
            "created_time": timestamp,
        }
        
        # Return simulated exchange order ID and timestamp
        return exchange_order_id, timestamp
    
    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        """
        Simulates order status request by retrieving the order from internal cache
        Returns an OrderUpdate object with the current status
        """
        client_order_id = tracked_order.client_order_id
        
        if client_order_id not in self._orders_cache:
            return OrderUpdate(
                client_order_id=client_order_id,
                exchange_order_id=None,
                trading_pair=tracked_order.trading_pair,
                update_timestamp=self.current_timestamp,
                new_state=OrderState.FAILED,
            )
        
        cached_order = self._orders_cache[client_order_id]
        state = CONSTANTS.ORDER_STATE.get(cached_order["status"], OrderState.OPEN)
        
        return OrderUpdate(
            client_order_id=client_order_id,
            exchange_order_id=cached_order["id"],
            trading_pair=cached_order["trading_pair"],
            update_timestamp=self.current_timestamp,
            new_state=state,
        )
    
    async def _set_trading_pair_leverage(self, trading_pair: str, leverage: int) -> Tuple[bool, str]:
        """
        Simulates setting the leverage for a trading pair
        Returns whether the operation was successful and an error message if any
        """
        self._leverage[trading_pair] = Decimal(str(leverage))
        return True, ""
    
    async def _trading_pair_position_mode_set(self, mode: PositionMode, trading_pair: str) -> Tuple[bool, str]:
        """
        Simulates setting the position mode for a trading pair
        Returns whether the operation was successful and an error message if any
        """
        self._position_mode = mode
        return True, ""
    
    async def _update_trading_fees(self):
        """
        Updates the trading fees for all trading pairs
        This is a simulated implementation that sets standard fees
        """
        for trading_pair in self._trading_pairs or []:
            # Standard maker/taker fees
            self._trading_fees[trading_pair] = TradeFeeBase.new_spot_fee(
                fee_schema=self.trade_fee_schema(),
                trade_type=TradeType.BUY,
                percent=Decimal("0.0004"),  # 0.04% taker fee
            )
    
    async def _user_stream_event_listener(self):
        """
        Simulates the user stream event listener by processing market updates
        This is now a lightweight implementation that doesn't directly process fills
        """
        self.logger().info("Starting user stream event listener")
        while True:
            try:
                # Just simulate a lightweight user stream connection
                # This should not be doing heavy processing
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                self.logger().info("User stream event listener cancelled")
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in user stream listener: {e}", exc_info=True)
                await asyncio.sleep(5.0)
    
    def _should_fill_order(self, order: InFlightOrder) -> bool:
        """
        Helper method to determine if an order should be filled in simulation
        For now, randomly fill orders with a 20% chance each check
        
        :param order: The InFlightOrder to check
        :return: True if the order should be filled, False otherwise
        """
        import random
        return random.random() < 0.2  # 20% chance to fill an order
    
    async def _simulate_order_fills(self):
        """
        Simulates order fills based on current market data.
        This method is called periodically to check if any open orders should be filled.
        Similar to the spot connector's approach, this runs in a separate task to avoid blocking
        the user stream listener.
        """
        while True:
            try:
                if not self._in_flight_orders:
                    await asyncio.sleep(1)  # No open orders to process
                    continue
                    
                # Process each open order
                for order_id, order in list(self._in_flight_orders.items()):
                    # Skip orders that are not in OPEN state
                    if order.current_state != OrderState.OPEN:
                        continue
                    
                    # Check if the order should be filled
                    if self._should_fill_order(order):
                        # Schedule order fill with a small delay
                        self.schedule_async_call(
                            self._simulate_order_fill(order_id, order.amount, order.price), 
                            0.1  # Small delay to simulate network latency
                        )
                        
                # Sleep before next check
                await asyncio.sleep(2)  # Check every 2 seconds, adjust as needed
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error in order fill simulation: {e}", exc_info=True)
                await asyncio.sleep(5.0)  # Longer delay on error
    
    def _process_order_update(self, order_id: str, order_data: Dict[str, Any]) -> None:
        """
        Process order updates and notify the hummingbot client
        """
        # This would be implemented to update the order tracker and emit events
        # For now, just log the update
        self.logger().info(f"Order update: {order_id} - {order_data['status']}")

    def supported_order_types(self) -> List[OrderType]:
        """
        :return a list of OrderType supported by this connector
        """
        return [OrderType.LIMIT, OrderType.MARKET, OrderType.LIMIT_MAKER]

    def supported_position_modes(self):
        """
        This method needs to be overridden to provide the accurate information depending on the exchange.
        """
        return [PositionMode.ONEWAY, PositionMode.HEDGE]

    def get_buy_collateral_token(self, trading_pair: str) -> str:
        """
        Returns the token used as collateral for buying
        """
        trading_rule: TradingRule = self._trading_rules[trading_pair]
        return trading_rule.buy_order_collateral_token

    def get_sell_collateral_token(self, trading_pair: str) -> str:
        """
        Returns the token used as collateral for selling
        """
        trading_rule: TradingRule = self._trading_rules[trading_pair]
        return trading_rule.sell_order_collateral_token

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        """
        Checks if the request exception is related to time synchronization
        """
        error_description = str(request_exception)
        is_time_synchronizer_related = (
            "-1021" in error_description and "Timestamp for this request" in error_description
        )
        return is_time_synchronizer_related

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        """
        Checks if the exception is due to order not found during status update
        """
        return str(CONSTANTS.ORDER_NOT_EXIST_ERROR_CODE) in str(
            status_update_exception
        ) and CONSTANTS.ORDER_NOT_EXIST_MESSAGE in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        """
        Checks if the exception is due to order not found during cancelation
        """
        return str(CONSTANTS.UNKNOWN_ORDER_ERROR_CODE) in str(
            cancelation_exception
        ) and CONSTANTS.UNKNOWN_ORDER_MESSAGE in str(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        """
        Creates a minimal web assistants factory to satisfy status checks.
        This matches the approach used in the QTX spot connector.
        """
        # Create a throttler if it doesn't exist
        if self._throttler is None:
            self._throttler = AsyncThrottler(rate_limits=[])
            
        # Don't use the time synchronizer to avoid errors
        return WebAssistantsFactory(throttler=self._throttler, auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        """
        Creates the order book data source
        """
        return QtxPerpetualAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        """
        Creates the user stream data source
        """
        return QtxPerpetualUserStreamDataSource(
            auth=self._auth,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _get_fee(
        self,
        base_currency: str,
        quote_currency: str,
        order_type: OrderType,
        order_side: TradeType,
        position_action: PositionAction,
        amount: Decimal,
        price: Decimal = s_decimal_NaN,
        is_maker: Optional[bool] = None,
    ) -> TradeFeeBase:
        """
        Calculate the fee for an order
        """
        is_maker = order_type is OrderType.LIMIT_MAKER
        fee = build_trade_fee(
            self.name,
            is_maker,
            base_currency=base_currency,
            quote_currency=quote_currency,
            order_type=order_type,
            order_side=order_side,
            amount=amount,
            price=price,
        )
        return fee

    async def _update_trading_rules(self):
        """
        Update trading rules from the exchange
        """
        # Simulate trading rules for all trading pairs
        self._trading_rules.clear()

        for trading_pair in self._trading_pairs:
            # Extract base and quote from trading pair
            base, quote = trading_pair.split("-")

            # Create simulated trading rule
            self._trading_rules[trading_pair] = TradingRule(
                trading_pair=trading_pair,
                min_order_size=Decimal("0.001"),
                min_price_increment=Decimal("0.01"),
                min_base_amount_increment=Decimal("0.001"),
                min_quote_amount_increment=Decimal("0.01"),
                min_notional_size=Decimal("1"),
                max_order_size=Decimal("1000"),
                supports_market_orders=True,
                buy_order_collateral_token=quote,
                sell_order_collateral_token=quote,
            )

    async def _update_balances(self):
        """
        Update account balances (simulated)
        """
        # Simulate account balances for all trading pairs
        for trading_pair in self._trading_pairs:
            base, quote = trading_pair.split("-")

            # Initialize balances if not already present
            if base not in self._account_balances:
                self._account_balances[base] = Decimal("100")  # Simulated base balance
                self._account_available_balances[base] = Decimal("100")

            if quote not in self._account_balances:
                self._account_balances[quote] = Decimal("10000")  # Simulated quote balance
                self._account_available_balances[quote] = Decimal("10000")

    async def _update_positions(self):
        """
        Update positions (simulated)
        """
        # Simulate positions for all trading pairs
        for trading_pair in self._trading_pairs:
            if trading_pair not in self._positions_cache:
                # Initialize with empty positions
                if self._position_mode == PositionMode.ONEWAY:
                    # One-way mode has only BOTH position side
                    self._positions_cache[trading_pair][PositionSide.BOTH] = Position(
                        trading_pair=trading_pair,
                        position_side=PositionSide.BOTH,
                        unrealized_pnl=Decimal("0"),
                        entry_price=Decimal("0"),
                        amount=Decimal("0"),
                        leverage=self._leverage[trading_pair],
                    )
                else:
                    # Hedge mode has LONG and SHORT position sides
                    self._positions_cache[trading_pair][PositionSide.LONG] = Position(
                        trading_pair=trading_pair,
                        position_side=PositionSide.LONG,
                        unrealized_pnl=Decimal("0"),
                        entry_price=Decimal("0"),
                        amount=Decimal("0"),
                        leverage=self._leverage[trading_pair],
                    )
                    self._positions_cache[trading_pair][PositionSide.SHORT] = Position(
                        trading_pair=trading_pair,
                        position_side=PositionSide.SHORT,
                        unrealized_pnl=Decimal("0"),
                        entry_price=Decimal("0"),
                        amount=Decimal("0"),
                        leverage=self._leverage[trading_pair],
                    )

    async def _update_funding_info(self):
        """
        Update funding information (simulated)
        """
        # Simulate funding info for all trading pairs
        for trading_pair in self._trading_pairs:
            current_time = int(time.time() * 1000)
            next_funding_time = current_time + 8 * 3600 * 1000  # 8 hours from now

            self._funding_info[trading_pair] = {
                "indexPrice": Decimal("10000"),  # Simulated index price
                "markPrice": Decimal("10000"),  # Simulated mark price
                "nextFundingTime": next_funding_time,
                "fundingRate": Decimal("0.0001"),  # Simulated funding rate (0.01%)
            }

    async def _status_polling_loop(self):
        """
        Periodically update trading rules, balances, positions, and funding info
        """
        while True:
            try:
                await self._update_trading_rules()
                await self._update_balances()
                await self._update_positions()
                await self._update_funding_info()

                # Sleep before next update
                await asyncio.sleep(self.SHORT_POLL_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error in status polling loop: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _trading_rules_polling_loop(self):
        """
        Periodically update trading rules
        """
        while True:
            try:
                await self._update_trading_rules()
                await asyncio.sleep(60)  # Update every minute
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error updating trading rules: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _create_order(
        self,
        trade_type: TradeType,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType,
        price: Decimal,
        position_action: PositionAction = PositionAction.OPEN,
        **kwargs,
    ) -> Tuple[str, float]:
        """
        Create a simulated order
        """
        exchange_order_id = f"s-{order_id}"  # Simulated exchange order ID

        # Store order in cache
        self._orders_cache[order_id] = {
            "id": order_id,
            "exchange_order_id": exchange_order_id,
            "trading_pair": trading_pair,
            "type": order_type,
            "trade_type": trade_type,
            "price": price,
            "amount": amount,
            "executed_amount": Decimal("0"),
            "status": "NEW",
            "created_time": time.time(),
            "position_action": position_action,
        }

        # Simulate immediate fill for market orders
        if order_type is OrderType.MARKET:
            # Schedule order fill
            self.schedule_async_call(
                self._simulate_order_fill(order_id, amount, price), 0.1  # Small delay to simulate network latency
            )

        # Return simulated exchange order ID and timestamp
        return exchange_order_id, time.time()

    async def _simulate_order_fill(self, order_id: str, amount: Decimal, price: Decimal):
        """
        Simulate order fill
        """
        self.logger().debug(f"Starting to simulate fill for order {order_id}")
        if order_id not in self._orders_cache:
            self.logger().debug(f"Order {order_id} not found in orders cache, cannot fill")
            return

        order = self._orders_cache[order_id]
        trading_pair = order["trading_pair"]
        trade_type = order["trade_type"]
        position_action = order["position_action"]

        # Update order status
        self.logger().debug(f"Updating order {order_id} status to FILLED")
        order["status"] = "FILLED"
        order["executed_amount"] = amount

        # Create trade update
        self._next_trade_id += 1
        trade_id = str(self._next_trade_id)
        self.logger().debug(f"Created trade ID {trade_id} for order {order_id}")

        # Store trade in cache
        self.logger().debug(f"Creating trade record for order {order_id}")
        trade = {
            "id": trade_id,
            "order_id": order_id,
            "trading_pair": trading_pair,
            "trade_type": trade_type,
            "price": price,
            "amount": amount,
            "trade_fee": self._get_fee(
                base_currency=trading_pair.split("-")[0],
                quote_currency=trading_pair.split("-")[1],
                order_type=order["type"],
                order_side=trade_type,
                position_action=position_action,
                amount=amount,
                price=price,
            ),
            "exchange_trade_id": f"t-{trade_id}",
            "timestamp": time.time(),
        }

        self.logger().debug(f"Adding trade to cache for order {order_id}")
        self._trades_cache[order_id].append(trade)

        # Update positions
        self.logger().debug(f"Updating position for {trading_pair} from fill")
        try:
            await self._update_position_from_fill(
            trading_pair=trading_pair,
            position_side=self._get_position_side(trade_type, position_action),
            amount=amount,
            price=price,
            trade_type=trade_type,
            position_action=position_action,
            )
            self.logger().debug(f"Position update completed for {trading_pair}")
        except Exception as e:
            self.logger().error(f"Error updating position: {e}", exc_info=True)

        # Trigger order update
        self.logger().debug(f"Creating order update for {order_id}")
        order_update = OrderUpdate(
            client_order_id=order_id,
            exchange_order_id=order["exchange_order_id"],
            trading_pair=trading_pair,
            update_timestamp=time.time(),
            new_state=OrderState.FILLED,
        )

        # Trigger trade update
        self.logger().debug(f"Creating trade update for {order_id}")
        trade_update = TradeUpdate(
            trade_id=trade_id,
            client_order_id=order_id,
            exchange_order_id=order["exchange_order_id"],
            trading_pair=trading_pair,
            fee=trade["trade_fee"],
            fill_base_amount=amount,
            fill_quote_amount=amount * price,
            fill_price=price,
            fill_timestamp=time.time(),
        )

        # Emit events
        self.logger().debug(f"Processing order update for {order_id}")
        try:
            self._order_tracker.process_order_update(order_update)
            self.logger().debug(f"Order update processed for {order_id}")
        except Exception as e:
            self.logger().error(f"Error processing order update: {e}", exc_info=True)
            
        self.logger().debug(f"Processing trade update for {order_id}")
        try:
            self._order_tracker.process_trade_update(trade_update)
            self.logger().debug(f"Trade update processed for {order_id}")
        except Exception as e:
            self.logger().error(f"Error processing trade update: {e}", exc_info=True)
            
        self.logger().debug(f"Completed simulating fill for order {order_id}")

    def _get_position_side(self, trade_type: TradeType, position_action: PositionAction) -> PositionSide:
        """
        Determine position side based on trade type and position action
        """
        if self._position_mode == PositionMode.ONEWAY:
            return PositionSide.BOTH

        # Hedge mode
        if position_action == PositionAction.OPEN:
            if trade_type == TradeType.BUY:
                return PositionSide.LONG
            else:
                return PositionSide.SHORT
        else:  # CLOSE
            if trade_type == TradeType.BUY:
                return PositionSide.SHORT  # Buy to close short
            else:
                return PositionSide.LONG  # Sell to close long

    async def _update_position_from_fill(
        self,
        trading_pair: str,
        position_side: PositionSide,
        amount: Decimal,
        price: Decimal,
        trade_type: TradeType,
        position_action: PositionAction,
    ):
        """
        Update position based on order fill
        """
        if trading_pair not in self._positions_cache or position_side not in self._positions_cache[trading_pair]:
            # Initialize position if not exists
            self._positions_cache[trading_pair][position_side] = Position(
                trading_pair=trading_pair,
                position_side=position_side,
                unrealized_pnl=Decimal("0"),
                entry_price=price,
                amount=Decimal("0"),
                leverage=self._leverage[trading_pair],
            )

        position = self._positions_cache[trading_pair][position_side]

        # Calculate new position
        if position_action == PositionAction.OPEN:
            # Opening or adding to position
            if trade_type == TradeType.BUY:
                # Long position
                new_amount = position.amount + amount
                new_entry_price = (
                    ((position.entry_price * position.amount) + (price * amount)) / new_amount
                    if new_amount > 0
                    else price
                )

                position.amount = new_amount
                position.entry_price = new_entry_price
            else:
                # Short position
                new_amount = position.amount - amount  # Short is negative
                new_entry_price = (
                    ((position.entry_price * abs(position.amount)) + (price * amount)) / abs(new_amount)
                    if new_amount < 0
                    else price
                )

                position.amount = new_amount
                position.entry_price = new_entry_price
        else:
            # Closing or reducing position
            if trade_type == TradeType.BUY:
                # Closing short position
                position.amount += amount
            else:
                # Closing long position
                position.amount -= amount

            # If position is fully closed, reset entry price
            if position.amount == 0:
                position.entry_price = Decimal("0")

        # Update unrealized PnL
        mark_price = self._funding_info.get(trading_pair, {}).get("markPrice", price)
        if position.amount > 0:  # Long position
            position.unrealized_pnl = (mark_price - position.entry_price) * position.amount
        elif position.amount < 0:  # Short position
            position.unrealized_pnl = (position.entry_price - mark_price) * abs(position.amount)
        else:  # No position
            position.unrealized_pnl = Decimal("0")

    async def _execute_cancel(self, trading_pair: str, order_id: str) -> str:
        """
        Execute order cancellation (simulated)
        """
        if order_id not in self._orders_cache:
            raise ValueError(f"Order {order_id} not found in cache")

        order = self._orders_cache[order_id]

        # Only cancel if order is still active
        if order["status"] in ["NEW", "PARTIALLY_FILLED"]:
            order["status"] = "CANCELED"

            # Create order update
            order_update = OrderUpdate(
                client_order_id=order_id,
                exchange_order_id=order["exchange_order_id"],
                trading_pair=trading_pair,
                update_timestamp=time.time(),
                new_state=OrderState.CANCELED,
            )

            # Process order update
            self._order_tracker.process_order_update(order_update)

        return order["exchange_order_id"]

    async def _set_leverage(self, trading_pair: str, leverage: int) -> Tuple[bool, str]:
        """
        Set leverage for a trading pair (simulated)
        """
        self._leverage[trading_pair] = Decimal(str(leverage))

        # Update positions with new leverage
        if trading_pair in self._positions_cache:
            for position in self._positions_cache[trading_pair].values():
                position.leverage = Decimal(str(leverage))

        return True, f"Leverage for {trading_pair} set to {leverage}x"

    async def _set_position_mode(self, position_mode: PositionMode) -> Tuple[bool, str]:
        """
        Set position mode (simulated)
        """
        old_mode = self._position_mode
        self._position_mode = position_mode

        # If changing from one-way to hedge mode, convert positions
        if old_mode == PositionMode.ONEWAY and position_mode == PositionMode.HEDGE:
            for trading_pair, positions in self._positions_cache.items():
                if PositionSide.BOTH in positions:
                    both_position = positions[PositionSide.BOTH]

                    # Convert to long or short based on amount
                    if both_position.amount > 0:
                        # Convert to long
                        positions[PositionSide.LONG] = Position(
                            trading_pair=trading_pair,
                            position_side=PositionSide.LONG,
                            unrealized_pnl=both_position.unrealized_pnl,
                            entry_price=both_position.entry_price,
                            amount=both_position.amount,
                            leverage=both_position.leverage,
                        )
                        positions[PositionSide.SHORT] = Position(
                            trading_pair=trading_pair,
                            position_side=PositionSide.SHORT,
                            unrealized_pnl=Decimal("0"),
                            entry_price=Decimal("0"),
                            amount=Decimal("0"),
                            leverage=both_position.leverage,
                        )
                    elif both_position.amount < 0:
                        # Convert to short
                        positions[PositionSide.SHORT] = Position(
                            trading_pair=trading_pair,
                            position_side=PositionSide.SHORT,
                            unrealized_pnl=both_position.unrealized_pnl,
                            entry_price=both_position.entry_price,
                            amount=both_position.amount,
                            leverage=both_position.leverage,
                        )
                        positions[PositionSide.LONG] = Position(
                            trading_pair=trading_pair,
                            position_side=PositionSide.LONG,
                            unrealized_pnl=Decimal("0"),
                            entry_price=Decimal("0"),
                            amount=Decimal("0"),
                            leverage=both_position.leverage,
                        )

                    # Remove BOTH position
                    del positions[PositionSide.BOTH]

        # If changing from hedge to one-way mode, convert positions
        elif old_mode == PositionMode.HEDGE and position_mode == PositionMode.ONEWAY:
            for trading_pair, positions in self._positions_cache.items():
                long_amount = Decimal("0")
                short_amount = Decimal("0")
                long_entry_price = Decimal("0")
                short_entry_price = Decimal("0")
                leverage = self._leverage[trading_pair]

                if PositionSide.LONG in positions:
                    long_amount = positions[PositionSide.LONG].amount
                    long_entry_price = positions[PositionSide.LONG].entry_price
                    leverage = positions[PositionSide.LONG].leverage

                if PositionSide.SHORT in positions:
                    short_amount = positions[PositionSide.SHORT].amount
                    short_entry_price = positions[PositionSide.SHORT].entry_price
                    leverage = positions[PositionSide.SHORT].leverage

                # Net position
                net_amount = long_amount + short_amount

                # Calculate entry price for net position
                if net_amount > 0:
                    entry_price = long_entry_price
                elif net_amount < 0:
                    entry_price = short_entry_price
                else:
                    entry_price = Decimal("0")

                # Create BOTH position
                positions[PositionSide.BOTH] = Position(
                    trading_pair=trading_pair,
                    position_side=PositionSide.BOTH,
                    unrealized_pnl=Decimal("0"),
                    entry_price=entry_price,
                    amount=net_amount,
                    leverage=leverage,
                )

                # Remove LONG and SHORT positions
                if PositionSide.LONG in positions:
                    del positions[PositionSide.LONG]
                if PositionSide.SHORT in positions:
                    del positions[PositionSide.SHORT]

        return True, f"Position mode set to {position_mode.name}"

    async def _fetch_positions(self) -> List[Position]:
        """
        Fetch all positions (simulated)
        """
        positions = []

        for trading_pair, position_dict in self._positions_cache.items():
            for position in position_dict.values():
                # Only include positions with non-zero amount
                if position.amount != 0:
                    positions.append(position)

        return positions

    async def _fetch_funding_payment(self, trading_pair: str) -> List[Dict[str, Any]]:
        """
        Fetch funding payments (simulated)
        """
        current_time = int(time.time() * 1000)

        # Simulate a funding payment every 8 hours
        funding_payments = []

        # Get position for the trading pair
        positions = self._positions_cache.get(trading_pair, {})

        for position_side, position in positions.items():
            if position.amount == 0:
                continue

            # Simulate funding payment
            funding_rate = self._funding_info.get(trading_pair, {}).get("fundingRate", Decimal("0.0001"))
            payment_amount = position.amount * position.entry_price * funding_rate

            funding_payments.append(
                {
                    "symbol": qtx_perpetual_utils.convert_to_exchange_trading_pair(trading_pair),
                    "incomeType": "FUNDING_FEE",
                    "income": str(payment_amount),
                    "asset": trading_pair.split("-")[1],  # Quote currency
                    "time": current_time - 8 * 3600 * 1000,  # 8 hours ago
                    "info": "Simulated funding payment",
                    "tranId": f"f-{current_time}",
                    "tradeId": "",
                }
            )

        return funding_payments

    async def _create_order(
        self,
        trade_type: TradeType,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType,
        price: Decimal,
        position_action: PositionAction = PositionAction.OPEN,
        **kwargs,
    ) -> Tuple[str, float]:
        """
        Create a simulated order
        """
        exchange_order_id = f"s-{order_id}"  # Simulated exchange order ID

        # Store order in cache
        self._orders_cache[order_id] = {
            "id": order_id,
            "exchange_order_id": exchange_order_id,
            "trading_pair": trading_pair,
            "type": order_type,
            "trade_type": trade_type,
            "price": price,
            "amount": amount,
            "executed_amount": Decimal("0"),
            "status": "NEW",
            "created_time": time.time(),
            "position_action": position_action,
        }

        # Simulate immediate fill for market orders
        if order_type is OrderType.MARKET:
            # Schedule order fill
            self.schedule_async_call(
                self._simulate_order_fill(order_id, amount, price), 0.1  # Small delay to simulate network latency
            )

        # Return simulated exchange order ID and timestamp
        return exchange_order_id, time.time()

    
    def _get_position_side(self, trade_type: TradeType, position_action: PositionAction) -> PositionSide:
        """
        Determine position side based on trade type and position action
        """
        if self._position_mode == PositionMode.ONEWAY:
            return PositionSide.BOTH

        # Hedge mode
        if position_action == PositionAction.OPEN:
            if trade_type == TradeType.BUY:
                return PositionSide.LONG
            else:
                return PositionSide.SHORT
        else:  # CLOSE
            if trade_type == TradeType.BUY:
                return PositionSide.SHORT  # Buy to close short
            else:
                return PositionSide.LONG  # Sell to close long

    async def _update_position_from_fill(
        self,
        trading_pair: str,
        position_side: PositionSide,
        amount: Decimal,
        price: Decimal,
        trade_type: TradeType,
        position_action: PositionAction,
    ):
        """
        Update position based on order fill
        """
        if trading_pair not in self._positions_cache or position_side not in self._positions_cache[trading_pair]:
            # Initialize position if not exists
            self._positions_cache[trading_pair][position_side] = Position(
                trading_pair=trading_pair,
                position_side=position_side,
                unrealized_pnl=Decimal("0"),
                entry_price=price,
                amount=Decimal("0"),
                leverage=self._leverage[trading_pair],
            )

        position = self._positions_cache[trading_pair][position_side]

        # Calculate new position
        if position_action == PositionAction.OPEN:
            # Opening or adding to position
            if trade_type == TradeType.BUY:
                # Long position
                new_amount = position.amount + amount
                new_entry_price = (
                    ((position.entry_price * position.amount) + (price * amount)) / new_amount
                    if new_amount > 0
                    else price
                )

                position.amount = new_amount
                position.entry_price = new_entry_price
            else:
                # Short position
                new_amount = position.amount - amount  # Short is negative
                new_entry_price = (
                    ((position.entry_price * abs(position.amount)) + (price * amount)) / abs(new_amount)
                    if new_amount < 0
                    else price
                )

                position.amount = new_amount
                position.entry_price = new_entry_price
        else:
            # Closing or reducing position
            if trade_type == TradeType.BUY:
                # Closing short position
                position.amount += amount
            else:
                # Closing long position
                position.amount -= amount

            # If position is fully closed, reset entry price
            if position.amount == 0:
                position.entry_price = Decimal("0")

        # Update unrealized PnL
        mark_price = self._funding_info.get(trading_pair, {}).get("markPrice", price)
        if position.amount > 0:  # Long position
            position.unrealized_pnl = (mark_price - position.entry_price) * position.amount
        elif position.amount < 0:  # Short position
            position.unrealized_pnl = (position.entry_price - mark_price) * abs(position.amount)
        else:  # No position
            position.unrealized_pnl = Decimal("0")

    async def _execute_cancel(self, trading_pair: str, order_id: str) -> str:
        """
        Execute order cancellation (simulated)
        """
        if order_id not in self._orders_cache:
            raise ValueError(f"Order {order_id} not found in cache")

        order = self._orders_cache[order_id]

        # Only cancel if order is still active
        if order["status"] in ["NEW", "PARTIALLY_FILLED"]:
            order["status"] = "CANCELED"

            # Create order update
            order_update = OrderUpdate(
                client_order_id=order_id,
                exchange_order_id=order["exchange_order_id"],
                trading_pair=trading_pair,
                update_timestamp=time.time(),
                new_state=OrderState.CANCELED,
            )

            # Process order update
            self._order_tracker.process_order_update(order_update)

        return order["exchange_order_id"]

    async def _set_leverage(self, trading_pair: str, leverage: int) -> Tuple[bool, str]:
        """
        Set leverage for a trading pair (simulated)
        """
        self._leverage[trading_pair] = Decimal(str(leverage))

        # Update positions with new leverage
        if trading_pair in self._positions_cache:
            for position in self._positions_cache[trading_pair].values():
                position.leverage = Decimal(str(leverage))

        return True, f"Leverage for {trading_pair} set to {leverage}x"

    async def _set_position_mode(self, position_mode: PositionMode) -> Tuple[bool, str]:
        """
        Set position mode (simulated)
        """
        old_mode = self._position_mode
        self._position_mode = position_mode

        # If changing from one-way to hedge mode, convert positions
        if old_mode == PositionMode.ONEWAY and position_mode == PositionMode.HEDGE:
            for trading_pair, positions in self._positions_cache.items():
                if PositionSide.BOTH in positions:
                    both_position = positions[PositionSide.BOTH]

                    # Convert to long or short based on amount
                    if both_position.amount > 0:
                        # Convert to long
                        positions[PositionSide.LONG] = Position(
                            trading_pair=trading_pair,
                            position_side=PositionSide.LONG,
                            unrealized_pnl=both_position.unrealized_pnl,
                            entry_price=both_position.entry_price,
                            amount=both_position.amount,
                            leverage=both_position.leverage,
                        )
                        positions[PositionSide.SHORT] = Position(
                            trading_pair=trading_pair,
                            position_side=PositionSide.SHORT,
                            unrealized_pnl=Decimal("0"),
                            entry_price=Decimal("0"),
                            amount=Decimal("0"),
                            leverage=both_position.leverage,
                        )
                    elif both_position.amount < 0:
                        # Convert to short
                        positions[PositionSide.SHORT] = Position(
                            trading_pair=trading_pair,
                            position_side=PositionSide.SHORT,
                            unrealized_pnl=both_position.unrealized_pnl,
                            entry_price=both_position.entry_price,
                            amount=both_position.amount,
                            leverage=both_position.leverage,
                        )
                        positions[PositionSide.LONG] = Position(
                            trading_pair=trading_pair,
                            position_side=PositionSide.LONG,
                            unrealized_pnl=Decimal("0"),
                            entry_price=Decimal("0"),
                            amount=Decimal("0"),
                            leverage=both_position.leverage,
                        )

                    # Remove BOTH position
                    del positions[PositionSide.BOTH]

        # If changing from hedge to one-way mode, convert positions
        elif old_mode == PositionMode.HEDGE and position_mode == PositionMode.ONEWAY:
            for trading_pair, positions in self._positions_cache.items():
                long_amount = Decimal("0")
                short_amount = Decimal("0")
                long_entry_price = Decimal("0")
                short_entry_price = Decimal("0")
                leverage = self._leverage[trading_pair]

                if PositionSide.LONG in positions:
                    long_amount = positions[PositionSide.LONG].amount
                    long_entry_price = positions[PositionSide.LONG].entry_price
                    leverage = positions[PositionSide.LONG].leverage

                if PositionSide.SHORT in positions:
                    short_amount = positions[PositionSide.SHORT].amount
                    short_entry_price = positions[PositionSide.SHORT].entry_price
                    leverage = positions[PositionSide.SHORT].leverage

                # Net position
                net_amount = long_amount + short_amount

                # Calculate entry price for net position
                if net_amount > 0:
                    entry_price = long_entry_price
                elif net_amount < 0:
                    entry_price = short_entry_price
                else:
                    entry_price = Decimal("0")

                # Create BOTH position
                positions[PositionSide.BOTH] = Position(
                    trading_pair=trading_pair,
                    position_side=PositionSide.BOTH,
                    unrealized_pnl=Decimal("0"),
                    entry_price=entry_price,
                    amount=net_amount,
                    leverage=leverage,
                )

                # Remove LONG and SHORT positions
                if PositionSide.LONG in positions:
                    del positions[PositionSide.LONG]
                if PositionSide.SHORT in positions:
                    del positions[PositionSide.SHORT]

        return True, f"Position mode set to {position_mode.name}"

    async def _fetch_positions(self) -> List[Position]:
        """
        Fetch all positions (simulated)
        """
        positions = []

        for trading_pair, position_dict in self._positions_cache.items():
            for position in position_dict.values():
                # Only include positions with non-zero amount
                if position.amount != 0:
                    positions.append(position)

        return positions

    async def _fetch_funding_payment(self, trading_pair: str) -> List[Dict[str, Any]]:
        """
        Fetch funding payments (simulated)
        """
        current_time = int(time.time() * 1000)

        # Simulate a funding payment every 8 hours
        funding_payments = []

        # Get position for the trading pair
        positions = self._positions_cache.get(trading_pair, {})

        for position_side, position in positions.items():
            if position.amount == 0:
                continue

            # Simulate funding payment
            funding_rate = self._funding_info.get(trading_pair, {}).get("fundingRate", Decimal("0.0001"))
            payment_amount = position.amount * position.entry_price * funding_rate

            funding_payments.append(
                {
                    "symbol": qtx_perpetual_utils.convert_to_exchange_trading_pair(trading_pair),
                    "incomeType": "FUNDING_FEE",
                    "income": str(payment_amount),
                    "asset": trading_pair.split("-")[1],  # Quote currency
                    "time": current_time - 8 * 3600 * 1000,  # 8 hours ago
                    "info": "Simulated funding payment",
                    "tranId": f"f-{current_time}",
                    "tradeId": "",
                }
            )

        return funding_payments

    async def start_network(self):
        """
        Start the network and initialize data sources
        """
        await super().start_network()

        # Start the status polling loop
        self._status_polling_task = asyncio.create_task(self._status_polling_loop())
        
        # Start the order simulation loop as a separate task
        self._order_fill_task = asyncio.create_task(self._simulate_order_fills())

        # Initialize with simulated data
        await self._update_trading_rules()
        await self._update_balances()
        await self._update_positions()
        await self._update_funding_info()

    async def stop_network(self):
        """
        Stop the network and clean up resources
        """
        # Cancel status polling task
        if self._status_polling_task is not None:
            self._status_polling_task.cancel()
            self._status_polling_task = None
            
        # Cancel order fill simulation task
        if hasattr(self, "_order_fill_task") and self._order_fill_task is not None:
            self._order_fill_task.cancel()
            self._order_fill_task = None

        await super().stop_network()

    def schedule_async_call(self, coro, delay):
        """
        Schedule an async coroutine to be executed after a delay
        """

        async def _delayed_call():
            await asyncio.sleep(delay)
            await coro

        return asyncio.create_task(_delayed_call())

    async def get_last_traded_price(self, trading_pair: str) -> float:
        """
        Get the last traded price from the order book
        """
        order_book = self.get_order_book(trading_pair)
        if order_book is not None and order_book.last_trade_price is not None:
            return float(order_book.last_trade_price)
        return 0.0

    async def get_position_mode(self) -> PositionMode:
        """
        Get the current position mode
        """
        return self._position_mode
