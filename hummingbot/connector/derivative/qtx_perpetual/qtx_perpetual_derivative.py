#!/usr/bin/env python

from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_constants as BINANCE_CONSTANTS
from hummingbot.connector.derivative.binance_perpetual.binance_perpetual_derivative import BinancePerpetualDerivative
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.derivative.qtx_perpetual import (
    qtx_perpetual_constants as CONSTANTS,
    qtx_perpetual_trading_pair_utils as trading_pair_utils,
)
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_api_order_book_data_source import (
    QtxPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_auth import QtxPerpetualBinanceAuth
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_udp_manager import QtxPerpetualUDPManager
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_user_stream_data_source import (
    QtxPerpetualUserStreamDataSource,
)
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter


# ---------------------------------------- Class Definition and Initialization ----------------------------------------


class QtxPerpetualDerivative(PerpetualDerivativePyBase):
    """
    QtxPerpetualDerivative connects to the QTX UDP market data source for real-time market data.
    Trading functionality is delegated to Binance's API for order execution on the mainnet.
    This hybrid approach leverages QTX's market data with Binance's order execution capabilities.
    """

    SHORT_POLL_INTERVAL = 5.0
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    LONG_POLL_INTERVAL = 120.0

    def __init__(
        self,
        client_config_map: "ClientConfigAdapter",
        qtx_perpetual_host: str = CONSTANTS.DEFAULT_UDP_HOST,
        qtx_perpetual_port: int = CONSTANTS.DEFAULT_UDP_PORT,
        binance_api_key: str = None,  # Binance API key
        binance_api_secret: str = None,  # Binance API secret
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):
        # QTX connection settings for market data
        self._qtx_perpetual_host = qtx_perpetual_host
        self._qtx_perpetual_port = qtx_perpetual_port

        # Hold off on creating the UDP manager until it's actually needed
        # This will be created on-demand in the property getter
        self._udp_manager = None

        # Binance API credentials for trading
        self._binance_api_key = binance_api_key
        self._binance_api_secret = binance_api_secret
        self._domain = domain

        # Trading flags
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs

        # Initialize state for position tracking and funding info
        self._positions_cache: Dict[str, Dict[PositionSide, Position]] = defaultdict(dict)
        self._funding_info = {}

        # Initialize trading pair symbols map (bidict for bidirectional mapping)
        self._trading_pair_symbol_map = bidict()

        # Initialize binance connector first
        self._binance_connector_instance = None

        # Initialize the base class after binance connector initialization
        super().__init__(client_config_map)

        self._init_binance_connector()

    # ---------------------------------------- Binance Connector Initialization ----------------------------------------

    def _init_binance_connector(self):
        """
        Initialize the Binance connector for order execution delegation
        """
        try:
            # Initialize the Binance connector with the same parameters
            binance_instance = BinancePerpetualDerivative(
                client_config_map=self._client_config,
                binance_perpetual_api_key=self._binance_api_key,
                binance_perpetual_api_secret=self._binance_api_secret,
                trading_pairs=self._trading_pairs,
                trading_required=self._trading_required,
                domain=self._domain,
            )

            # Store the instance using the setter
            self._binance_connector_instance = binance_instance

            self.logger().info("_init_binance_connector() is called successfully")
        except Exception as e:
            self._binance_connector_instance = None
            self.logger().error(
                f"Failed to initialize Binance connector in _init_binance_connector(): {str(e)}", exc_info=True
            )
            raise

    # ---------------------------------------- Properties and Accessors ----------------------------------------ßß

    @property
    def binance_connector(self) -> BinancePerpetualDerivative:
        """
        Returns the Binance connector instance
        """
        return self._binance_connector_instance

    @binance_connector.setter
    def binance_connector(self, value):
        """
        Sets the Binance connector instance
        """
        self._binance_connector_instance = value

    @property
    def udp_manager(self):
        """
        Returns the UDP manager instance, creating it if it doesn't exist yet
        """
        if self._udp_manager is None:
            self._udp_manager = QtxPerpetualUDPManager(host=self._qtx_perpetual_host, port=self._qtx_perpetual_port)
        return self._udp_manager

    @property
    def name(self) -> str:
        return CONSTANTS.EXCHANGE_NAME

    @property
    def authenticator(self) -> QtxPerpetualBinanceAuth:
        return QtxPerpetualBinanceAuth(
            binance_api_key=self._binance_api_key,
            binance_api_secret=self._binance_api_secret,
            time_provider=self._time_synchronizer,
        )

    @property
    def web_utils(self):
        from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_web_utils

        return qtx_perpetual_web_utils

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
        return BINANCE_CONSTANTS.EXCHANGE_INFO_URL

    @property
    def trading_pairs_request_path(self) -> str:
        return BINANCE_CONSTANTS.EXCHANGE_INFO_URL

    @property
    def check_network_request_path(self) -> str:
        return BINANCE_CONSTANTS.PING_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        binance_instance = self.binance_connector
        if binance_instance is not None:
            self._trading_required = binance_instance.is_trading_required
        return self._trading_required

    @property
    def status_dict(self) -> Dict[str, bool]:
        status_dict = super().status_dict
        self.logger().info(f"QTX Perpetual status check: {status_dict}")
        return status_dict

    @property
    def funding_fee_poll_interval(self) -> int:
        binance_instance = self.binance_connector
        if binance_instance is not None:
            return binance_instance.funding_fee_poll_interval
        return 600

    # ---------------------------------------- Network and Lifecycle Management ----------------------------------------

    async def _make_network_check_request(self):
        pass

    async def start_network(self):
        """
        Start network components
        """
        await super().start_network()

        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: start_network() failed")
            return
        else:
            await binance_instance.start_network()
            self.logger().debug("Binance connector connector start_network() completed.")

        self.logger().info("QTX Perpetual connector start_network() completed.")

    async def stop_network(self):
        """
        Stop networking
        """
        await super().stop_network()

        if self._udp_manager is not None:
            for trading_pair in self._trading_pairs:
                try:
                    qtx_symbol = trading_pair_utils.convert_to_qtx_trading_pair(trading_pair)
                    await self.udp_manager.unsubscribe_from_symbol(qtx_symbol)
                except Exception as e:
                    self.logger().error(f"Error unsubscribing from {trading_pair}: {e}")

            await self.udp_manager.stop_listening()

        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: stop_network() failed")
            return
        else:
            await binance_instance.stop_network()
            self.logger().debug("Binance connector connector stop_network() completed.")

        self.logger().info("QTX Perpetual connector stop_network() completed.")

    # ---------------------------------------- Data Source Creation ----------------------------------------

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        """
        Create the order book tracker data source using QTX
        """
        return QtxPerpetualAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
            host=self._qtx_perpetual_host,
            port=self._qtx_perpetual_port,
            udp_manager_instance=self.udp_manager,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        """
        Create a minimal user stream data source that merely delegates events from Binance to QTX's event queue.
        """
        return QtxPerpetualUserStreamDataSource(qtx_connector=self)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        """
        Create a web assistants factory
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().info("Binance connector not available: _create_web_assistants_factory() failed")
            return WebAssistantsFactory(throttler=self._throttler, auth=self.authenticator)
        else:
            return binance_instance._create_web_assistants_factory()

    # ---------------------------------------- Trading Pair Management ----------------------------------------

    async def _initialize_trading_pair_symbol_map(self):
        """
        Initialize the trading pair symbol map by fetching data from the exchange
        and processing it using the _initialize_trading_pair_symbols_from_exchange_info method.
        """
        try:
            exchange_info = await self._make_trading_pairs_request()
            self._initialize_trading_pair_symbols_from_exchange_info(exchange_info=exchange_info)
            self.logger().debug("_initialize_trading_pair_symbol_map() is called successfully")
        except Exception as e:
            self.logger().error(f"Error initializing trading pair symbol map: {e}", exc_info=True)
            raise

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        """
        Initialize trading pair symbols from exchange info - directly delegates to Binance connector.
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error(
                "Binance connector not available: _initialize_trading_pair_symbols_from_exchange_info() failed"
            )
            return

        try:
            binance_instance._initialize_trading_pair_symbols_from_exchange_info(exchange_info)

            # Copy back the trading pair symbol map from Binance connector
            self.logger().info(
                f"Binance trading pair symbol map before copy: {binance_instance._trading_pair_symbol_map}"
            )
            self._set_trading_pair_symbol_map(binance_instance._trading_pair_symbol_map.copy())
            self.logger().info(f"QTX trading pair symbol map after copy: {self._trading_pair_symbol_map}")

            self.logger().debug("_initialize_trading_pair_symbols_from_exchange_info() is called successfully")
        except Exception as e:
            self.logger().error(
                f"Error during delegation in _initialize_trading_pair_symbols_from_exchange_info(): {e}", exc_info=True
            )

    async def exchange_symbol_associated_to_pair(self, trading_pair: str) -> str:
        """
        Used to translate a trading pair from Hummingbot format to Binance format
        """
        return trading_pair_utils.convert_to_binance_trading_pair(trading_pair)

    async def trading_pair_associated_to_exchange_symbol(self, symbol: str) -> str:
        """
        Used to translate a trading pair from Binance format to Hummingbot format
        """
        return trading_pair_utils.convert_from_binance_trading_pair(symbol, self._trading_pairs)

    async def all_trading_pairs(self) -> List[str]:
        """
        Get all trading pairs from Binance
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: all_trading_pairs failed")
            return []

        try:
            return await binance_instance.all_trading_pairs()
        except Exception as e:
            self.logger().error(f"Error in all_trading_pairs: {e}", exc_info=True)
            return []

    async def _make_trading_pairs_request(self) -> Any:
        """
        Delegates trading pairs request to Binance connector.
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _make_trading_pairs_request failed")
            return {}

        try:
            return await binance_instance._make_trading_pairs_request()
        except Exception as e:
            self.logger().error(f"Error in _make_trading_pairs_request: {e}", exc_info=True)
            return {}

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

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        """
        Format trading rules - delegate to Binance connector.
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _format_trading_rules failed")
            return []

        try:
            return await binance_instance._format_trading_rules(exchange_info_dict)
        except Exception as e:
            self.logger().error(f"Error in _format_trading_rules: {e}", exc_info=True)
            return []

    # ---------------------------------------- Order Placement and Management ----------------------------------------

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
        """
        Delegate order cancellation to Binance connector
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            raise ValueError("Binance connector not available: _place_cancel failed")

        try:
            return await binance_instance._place_cancel(order_id=order_id, tracked_order=tracked_order)
        except Exception as e:
            self.logger().error(f"Error in _place_cancel: {e}", exc_info=True)
            raise

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
        Delegate order placement to Binance connector
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            raise ValueError("Binance connector not available: _place_order failed")

        try:
            return await binance_instance._place_order(
                order_id=order_id,
                trading_pair=trading_pair,
                amount=amount,
                trade_type=trade_type,
                order_type=order_type,
                price=price,
                position_action=position_action,
                **kwargs,
            )
        except Exception as e:
            self.logger().error(f"Error in _place_order: {e}", exc_info=True)
            raise

    def _is_order_not_found_during_cancelation_error(self, exception) -> bool:
        """
        Determine if an error from cancel order request is due to order not found
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _is_order_not_found_during_cancelation_error failed")
            return "Order does not exist" in str(exception) or "Unknown order" in str(exception)

        try:
            return binance_instance._is_order_not_found_during_cancelation_error(exception)
        except Exception as e:
            self.logger().error(f"Error in _is_order_not_found_during_cancelation_error: {e}", exc_info=True)
            return "Order does not exist" in str(exception) or "Unknown order" in str(exception)

    def _is_order_not_found_during_status_update_error(self, exception) -> bool:
        """
        Determine if an error from order status request is due to order not found
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error(
                "Binance connector not available: _is_order_not_found_during_status_update_error failed"
            )
            return "Order does not exist" in str(exception) or "Unknown order" in str(exception)

        try:
            return binance_instance._is_order_not_found_during_status_update_error(exception)
        except Exception as e:
            self.logger().error(f"Error in _is_order_not_found_during_status_update_error: {e}", exc_info=True)
            return "Order does not exist" in str(exception) or "Unknown order" in str(exception)

    def _is_request_exception_related_to_time_synchronizer(self, request_exception) -> bool:
        """
        Determine if a request exception is related to timestamp synchronization
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error(
                "Binance connector not available: _is_request_exception_related_to_time_synchronizer failed"
            )
            return "Timestamp for this request" in str(request_exception) and "reconfigure it" in str(request_exception)

        try:
            return binance_instance._is_request_exception_related_to_time_synchronizer(request_exception)
        except Exception as e:
            self.logger().error(f"Error in _is_request_exception_related_to_time_synchronizer: {e}", exc_info=True)
            return "Timestamp for this request" in str(request_exception) and "reconfigure it" in str(request_exception)

    # ---------------------------------------- Status Polling and Updates ----------------------------------------

    async def _status_polling_loop_fetch_updates(self):
        """
        Fetches updates for orders, trades, balances and positions
        """
        from hummingbot.core.utils.async_utils import safe_gather

        await safe_gather(
            self._update_order_fills_from_trades(),
            self._update_order_status(),
            self._update_balances(),
            self._update_positions(),
        )

    async def _update_order_fills_from_trades(self):
        """
        Get order trade updates from the exchange and update order status
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _update_order_fills_from_trades failed")
            return

        try:
            await binance_instance._update_order_fills_from_trades()
        except Exception as e:
            self.logger().error(f"Error in _update_order_fills_from_trades: {e}", exc_info=True)

    async def _update_order_status(self):
        """
        Get order status updates from the exchange
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _update_order_status failed")
            return

        try:
            await binance_instance._update_order_status()
        except Exception as e:
            self.logger().error(f"Error in _update_order_status: {e}", exc_info=True)

    async def _update_balances(self):
        """
        Update balances - directly delegate to Binance connector
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _update_balances failed")
            return

        try:
            await binance_instance._update_balances()
            self._account_available_balances = binance_instance._account_available_balances.copy()
            self._account_balances = binance_instance._account_balances.copy()
        except Exception as e:
            self.logger().error(f"Error in _update_balances: {e}", exc_info=True)

    async def _update_positions(self):
        """
        Update positions - delegate to Binance directly
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _update_positions failed")
            return

        try:
            await binance_instance._update_positions()
            self._positions_cache = {}

            for trading_pair in binance_instance._perpetual_trading._trading_pairs:
                for position_side in [PositionSide.LONG, PositionSide.SHORT]:
                    position = binance_instance._perpetual_trading.get_position(trading_pair, position_side)
                    if position is not None:
                        if trading_pair not in self._positions_cache:
                            self._positions_cache[trading_pair] = {}
                        self._positions_cache[trading_pair][position_side] = position
        except Exception as e:
            self.logger().error(f"Error in _update_positions: {e}", exc_info=True)

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        """
        Delegate order status request to Binance connector
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _request_order_status failed")
            return OrderUpdate(
                client_order_id=tracked_order.client_order_id,
                exchange_order_id=tracked_order.exchange_order_id,
                trading_pair=tracked_order.trading_pair,
                update_timestamp=self.current_timestamp,
                new_state=tracked_order.current_state,
            )

        try:
            return await binance_instance._request_order_status(tracked_order)
        except Exception as e:
            self.logger().error(f"Error in _request_order_status: {e}", exc_info=True)
            return OrderUpdate(
                client_order_id=tracked_order.client_order_id,
                exchange_order_id=tracked_order.exchange_order_id,
                trading_pair=tracked_order.trading_pair,
                update_timestamp=self.current_timestamp,
                new_state=tracked_order.current_state,
            )

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        """
        Delegate trade updates request to Binance connector
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _all_trade_updates_for_order failed")
            return []

        try:
            return await binance_instance._all_trade_updates_for_order(order)
        except Exception as e:
            self.logger().error(f"Error in _all_trade_updates_for_order: {e}", exc_info=True)
            return []

    # ---------------------------------------- User Stream Management ----------------------------------------

    async def _user_stream_event_listener(self):
        """
        No-op because we delegate all user stream events to the Binance sub-connector.
        """
        pass

    def _is_user_stream_initialized(self) -> bool:
        """
        QTX delegates all user-stream to binance sub-connector. If binance says it's initialized, we do too.
        """
        if not self.is_trading_required:
            return True

        if self._binance_connector_instance is not None:
            return self._binance_connector_instance._is_user_stream_initialized()
        else:
            self.logger().error("Binance connector not available: _is_user_stream_initialized failed")

        return False

    # ---------------------------------------- Trading Configuration ----------------------------------------

    def supported_order_types(self) -> List[OrderType]:
        """
        Returns order types supported by the connector
        """
        binance_instance = self.binance_connector
        if binance_instance is not None:
            return binance_instance.supported_order_types()
        return [OrderType.LIMIT, OrderType.MARKET, OrderType.LIMIT_MAKER]

    def supported_position_modes(self):
        """
        Returns supported position modes
        """
        binance_instance = self.binance_connector
        if binance_instance is not None:
            return binance_instance.supported_position_modes()
        return [PositionMode.ONEWAY, PositionMode.HEDGE]

    async def _set_trading_pair_leverage(self, trading_pair: str, leverage: int) -> Tuple[bool, str]:
        """
        Delegate leverage setting to Binance connector
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _set_trading_pair_leverage failed")
            return False, "Binance connector not available"

        try:
            return await binance_instance._set_trading_pair_leverage(trading_pair, leverage)
        except Exception as e:
            self.logger().error(f"Error in _set_trading_pair_leverage: {e}", exc_info=True)
            return False, f"Binance operation failed: {str(e)}"

    async def _trading_pair_position_mode_set(self, mode: PositionMode, trading_pair: str) -> Tuple[bool, str]:
        """
        Delegate position mode setting to Binance connector
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _trading_pair_position_mode_set failed")
            return False, "Binance connector not available"

        try:
            return await binance_instance._trading_pair_position_mode_set(mode, trading_pair)
        except Exception as e:
            self.logger().error(f"Error in _trading_pair_position_mode_set: {e}", exc_info=True)
            return False, f"Binance operation failed: {str(e)}"

    # ---------------------------------------- Fees and Collateral ----------------------------------------

    async def _fetch_last_fee_payment(self, trading_pair: str) -> Tuple[int, Decimal, Decimal]:
        """
        Delegate last fee payment fetch to Binance connector
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _fetch_last_fee_payment failed")
            return 0, Decimal("0"), Decimal("0")

        try:
            return await binance_instance._fetch_last_fee_payment(trading_pair)
        except Exception as e:
            self.logger().error(f"Error in _fetch_last_fee_payment: {e}", exc_info=True)
            return 0, Decimal("0"), Decimal("0")

    def get_buy_collateral_token(self, trading_pair: str) -> str:
        """
        Retrieve the collateral token for buying
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            return ""

        try:
            return binance_instance.get_buy_collateral_token(trading_pair)
        except Exception as e:
            self.logger().error(f"Error in get_buy_collateral_token: {e}", exc_info=True)
            return ""

    def get_sell_collateral_token(self, trading_pair: str) -> str:
        """
        Retrieve the collateral token for selling
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: get_sell_collateral_token failed")
            return ""

        try:
            return binance_instance.get_sell_collateral_token(trading_pair)
        except Exception as e:
            self.logger().error(f"Error in get_sell_collateral_token: {e}", exc_info=True)
            return ""

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
        Calculate fee for orders
        """
        binance_instance = self.binance_connector
        if binance_instance is not None:
            try:
                return binance_instance._get_fee(
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    order_type=order_type,
                    order_side=order_side,
                    position_action=position_action,
                    amount=amount,
                    price=price,
                    is_maker=is_maker,
                )
            except Exception as e:
                self.logger().error(f"Error in _get_fee: {e}", exc_info=True)

        is_maker = is_maker or (order_type is OrderType.LIMIT_MAKER)
        return build_trade_fee(
            self.name,
            is_maker,
            base_currency=base_currency,
            quote_currency=quote_currency,
            order_type=order_type,
            order_side=order_side,
            amount=amount,
            price=price,
        )

    # ---------------------------------------- Market Data Methods ----------------------------------------

    async def get_last_traded_price(self, trading_pair: str) -> float:
        """
        Get the last traded price for a trading pair from QTX market data
        """
        try:
            orderbook = self.get_order_book(trading_pair)
            if orderbook and orderbook.last_trade_price:
                return float(orderbook.last_trade_price)
            if orderbook:
                return (orderbook.get_price(True) + orderbook.get_price(False)) / 2
            raise ValueError(f"Order book not available for {trading_pair}")
        except Exception as e:
            self.logger().error(f"Error getting last traded price from QTX order book: {e}", exc_info=True)
            raise

    # ---------------------------------------- Dummy for satisfying the base class ----------------------------------------

    async def _update_trading_fees(self):
        """
        Update fees information from the exchange
        """
        pass

    # ---------------------------------------- Clock Management ----------------------------------------

    def start(self, clock: Clock, timestamp: float):
        """
        Relay start command to the Binance connector
        """
        # This is called by Hummingbot automatically
        super().start(clock, timestamp)

        # Relay the timer calls to the Binance connector
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: start(clock, timestamp) failed")
            return

        try:
            # Start the binance connector's own NetworkIterator logic
            binance_instance.start(clock, timestamp)
            self.logger().debug("Binance connector start(clock, timestamp) is called successfully")
        except Exception as e:
            self.logger().error(f"Error during delegation in start(clock, timestamp): {e}", exc_info=True)

    def stop(self, clock: Clock):
        """
        Relay stop command to the Binance connector
        """
        super().stop(clock)

        # Relay the timer calls to the Binance connector
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: stop(clock) failed")
            return

        try:
            binance_instance.stop(clock)
            self.logger().debug("Binance connector stop(clock) is called successfully")
        except Exception as e:
            self.logger().error(f"Error during delegation in stop(clock): {e}", exc_info=True)

    def tick(self, timestamp: float):
        """
        Relay tick command to the Binance connector
        """
        super().tick(timestamp)

        # Relay the timer calls to the Binance connector
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: tick(timestamp) failed")
            return

        try:
            # Also relay the clock tick to Binance
            binance_instance.tick(timestamp)
        except Exception as e:
            self.logger().error(f"Error during delegation in tick(timestamp): {e}", exc_info=True)
