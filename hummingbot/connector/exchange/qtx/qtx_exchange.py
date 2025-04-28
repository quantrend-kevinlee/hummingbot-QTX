#!/usr/bin/env python

import asyncio
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
import time

from bidict import bidict

from hummingbot.core.data_type.cancellation_result import CancellationResult
from hummingbot.core.utils.async_utils import safe_gather, safe_ensure_future

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.qtx import qtx_constants as CONSTANTS, qtx_utils
from hummingbot.connector.exchange.qtx.qtx_api_order_book_data_source import QTXAPIOrderBookDataSource
from hummingbot.connector.exchange.qtx.qtx_user_stream_data_source import QTXAPIUserStreamDataSource
from hummingbot.connector.exchange.qtx.qtx_auth import QtxAuth
from hummingbot.core.data_type.in_flight_order import OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TradeFeeBase
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter


class QtxExchange(ExchangePyBase):
    """
    QtxExchange connects to the QTX UDP market data source.
    This implementation is for market data display only and does not support trading.
    """

    def __init__(
        self,
        client_config_map: "ClientConfigAdapter",
        qtx_host: str = CONSTANTS.DEFAULT_UDP_HOST,
        qtx_port: int = CONSTANTS.DEFAULT_UDP_PORT,
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        qtx_api_key: str = "",
        qtx_secret_key: str = "",
        **kwargs,  # Accept arbitrary keyword args to prevent TypeError
    ):
        self._qtx_host = qtx_host
        self._qtx_port = qtx_port
        self.api_key = qtx_api_key
        self.secret_key = qtx_secret_key
        
        # Initialize trading pairs from configuration
        self._trading_pairs = trading_pairs if trading_pairs else []
        
        # For mock data, always ensure we have at least one trading pair
        if not self._trading_pairs:
            self._trading_pairs = ["BTC-USDT"]
            self.logger().info(f"No trading pairs configured, using default pair: {self._trading_pairs}")
            
        self._trading_required = trading_required
        self._web_assistants_factory = None
        self._api_factory = None
        # Internal state for mock orders and trades
        self._mock_orders: Dict[str, Dict[str, Any]] = {}
        self._mock_trades: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        super().__init__(client_config_map)

    async def _make_trading_pairs_request(self) -> Optional[Dict[str, Any]]:
        """
        Override the base class method to prevent REST API call for trading pairs.
        Returns None as the actual mapping is handled by
        _initialize_trading_pair_symbols_from_exchange_info based on configured pairs.
        """
        self.logger().info("Skipping REST call for trading pairs, using configured pairs.")
        return None

    @property
    def name(self) -> str:
        return "qtx"

    @property
    def trading_pairs(self):
        if not hasattr(self, "_trading_pairs") or self._trading_pairs is None:
             self._trading_pairs = []
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
        return QtxAuth(
            api_key=self.api_key,
            secret_key=self.secret_key,
            time_provider=self._time_synchronizer
        )

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
        when running a trading strategy with this mock/market-data connector.
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

    async def execute_buy(self, *args, **kwargs):
        raise NotImplementedError("Trading is not supported in this market data only connector")

    async def execute_sell(self, *args, **kwargs):
        raise NotImplementedError("Trading is not supported in this market data only connector")

    async def cancel_all(self, timeout_seconds: float) -> List[CancellationResult]:
        """
        Cancels all active mock orders.
        Returns a list of CancellationResult objects indicating success/failure per order.
        Note: timeout_seconds is currently ignored in this mock implementation.
        """
        return []

    def buy(self, *args, **kwargs):
        raise NotImplementedError("Trading is not supported in this market data only connector")

    def sell(self, *args, **kwargs):
        raise NotImplementedError("Trading is not supported in this market data only connector")

    def cancel(self, *args, **kwargs):
        raise NotImplementedError("Trading is not supported in this market data only connector")

    async def _update_balances(self):
        """
        Mock implementation to update balances.
        Since this is a market data only connector, we can use static values.
        """
        # Provide some basic mock balances for all trading pairs
        self._account_available_balances.clear()
        self._account_balances.clear()

        # For all configured pairs, create mock balances
        for trading_pair in self.trading_pairs:
            base, quote = trading_pair.split("-")
            self._account_balances[base] = Decimal("10.0")  # Mock base asset balance
            self._account_balances[quote] = Decimal("10000.0")  # Mock quote asset balance
            self._account_available_balances[base] = Decimal("10.0")  # Mock available base
            self._account_available_balances[quote] = Decimal("10000.0")  # Mock available quote

        self.logger().debug("Updated mock balances.")

    async def _update_trading_rules(self):
        """
        Fetch and update trading rules for all trading pairs.
        """
        self._trading_rules = {
            trading_rule.trading_pair: trading_rule
            for trading_rule in await self.get_trading_rules()
        }

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        """
        Initialize trading pair symbols directly from the configured list.
        The exchange_info parameter is ignored since we're not querying an actual exchange.
        """
        self._trading_pair_symbol_map = {}
        self._symbol_trading_pair_map = {}
        
        for trading_pair in self._trading_pairs:
            # Convert to exchange symbol format (lowercase, no separator)
            exchange_symbol = qtx_utils.format_trading_pair(trading_pair)
            self._trading_pair_symbol_map[trading_pair] = exchange_symbol
            self._symbol_trading_pair_map[exchange_symbol] = trading_pair
            
        self.logger().info(f"Initialized {len(self._trading_pair_symbol_map)} trading pairs for {self.name}.")

    async def _status_polling_loop_fetch_updates(self):
        """
        Mock implementation for fetching status updates periodically.
        This will update orders, balances, and positions as needed.
        """
        self.logger().debug("[MOCK] Fetching status updates")
        
        # Update the balances first
        await self._update_balances()
        
        # Update open orders
        await self._update_order_status()
        
        # Update lost orders if any
        await self._update_lost_orders_status()
        
        # Process any mock trades that might have been simulated
        # In a real connector, this would come from the exchange API

    async def check_network(self) -> NetworkStatus:
        """
        Checks if the exchange is online and operational.
        For the fake data source, we assume it's always available.
        When UDP is re-enabled, this might check socket status or ping.
        """
        return NetworkStatus.CONNECTED

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
         from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
         from hummingbot.core.web_assistant.connections.data_types import RESTMethod
         from hummingbot.core.api_throttler.async_throttler import AsyncThrottler

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

    # MOCK TRADING METHODS (non-functional for market data connector)
    
    async def _place_order(self, order_id: str, trading_pair: str, amount: Decimal, trade_type: TradeType, order_type: OrderType, price: Decimal, **kwargs) -> Tuple[str, float]:
        # Create a mock/dummy exchange order ID and return it
        dummy_exchange_order_id = f"qtx_mock_{int(time.time() * 1e6)}_{order_id[-4:]}"
        timestamp = time.time()
        return dummy_exchange_order_id, timestamp

    async def _place_cancel(self, order_id: str, tracked_order: 'InFlightOrder') -> bool:
        # Always return success for mock cancellation
        return True

    async def _request_order_status(self, tracked_order: 'InFlightOrder') -> OrderUpdate:
        # Return a mock pending order status
        return OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=tracked_order.exchange_order_id or "mock_id",
            trading_pair=tracked_order.trading_pair,
            update_timestamp=time.time(),
            new_state=OrderState.PENDING_CREATE,
        )

    async def _all_trade_updates_for_order(self, order: 'InFlightOrder') -> List[TradeUpdate]:
        # Return empty list as no actual trades happen
        return []

    async def _update_trading_fees(self):
        """
        Mock implementation for updating trading fees.
        For QTX mock connector, we'll use static zero fees.
        """
        self.logger().debug("[MOCK] Updating trading fees with zero values")
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
        Mock implementation of the user stream listener.
        This method should be empty as we're using simulated data, not real user stream events.
        """
        while True:
            try:
                await asyncio.sleep(0.5)  # Just sleep to keep the task alive
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in user stream listener: {e}", exc_info=True)
                await asyncio.sleep(5.0)

    async def _update_order_status(self):
        """
        Mock implementation to update the status of tracked orders.
        """
        # For market data connector, this can be a no-op
        pass

    async def _update_lost_orders_status(self):
        """
        Mock implementation to update the status of "lost" orders.
        """
        # For market data connector, this can be a no-op
        pass
    
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
        
        # Additional checks
        if not self.order_book_tracker.ready:
            self.logger().warning("Order book tracker is not ready. This might cause the 'Market connectors are not ready' error.")

        # Update status with QTX-specific mock indicators without overriding order_books_initialized
        # which is handled by the parent class and is critical for connector readiness
        custom_status = {
            "trading_rule_initialized": True,  # Always true for QTX mock
            "user_stream_initialized": True,   # Always true for QTX mock
            "account_balance": True,           # Always true for QTX mock
            "symbols_mapping_initialized": True, # Always true for QTX internal mapping
        }

        # Merge dictionaries, ensuring we don't override the order_books_initialized value
        return {**status, **custom_status}
        
    async def check_order_book_status(self) -> Dict[str, Any]:
        """
        Diagnostic function to check the status of the order books for all trading pairs.
        """
        result = {
            "connector": self.name,
            "status": "operational" if self._order_book_initialized.is_set() else "initializing",
            "trading_pairs_configured": self._trading_pairs,
            "order_book_status": {}
        }
        
        # Check each trading pair's order book status
        for trading_pair in self._trading_pairs:
            ob_status = {
                "exists": trading_pair in self.order_book_tracker.order_books
            }
            
            if ob_status["exists"]:
                order_book = self.order_book_tracker.order_books[trading_pair]
                ob_status.update({
                    "bids": len(order_book.bid_entries()),
                    "asks": len(order_book.ask_entries()),
                    "has_snapshot": order_book.snapshot_message_count > 0,
                    "last_diff_uid": order_book._last_diff_uid,
                    "snapshot_uid": order_book._snapshot_uid,
                })
            
            result["order_book_status"][trading_pair] = ob_status
        
        self.logger().info(f"Order book status: {result}")
        return result

    async def _update_time_synchronizer(self, time_provider: Optional[callable] = None):
        """
        Override base method to prevent time synchronization attempts
        as this connector does not use REST APIs requiring it currently.
        """
        self.logger().debug("Skipping time synchronization for QTX market data connector.")
        pass
