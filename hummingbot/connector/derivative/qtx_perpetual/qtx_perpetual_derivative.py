#!/usr/bin/env python

import asyncio
import decimal
import time
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
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager import QtxPerpetualSharedMemoryManager
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
from hummingbot.core.data_type.trade_fee import TradeFeeBase, TokenAmount
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.event.event_forwarder import EventForwarder
from hummingbot.core.event.events import MarketEvent, OrderFilledEvent
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
        binance_api_key: str = None,  # Binance API key (also used for QTX shared memory)
        binance_api_secret: str = None,  # Binance API secret (also used for QTX shared memory)
        qtx_place_order_shared_memory_name: str = None,  # Shared memory segment name for order execution
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):
        # QTX connection settings for market data
        self._qtx_perpetual_host = qtx_perpetual_host
        self._qtx_perpetual_port = qtx_perpetual_port

        # QTX shared memory settings for order execution
        self._qtx_shared_memory_name = qtx_place_order_shared_memory_name

        # Hold off on creating the managers until they're actually needed
        self._udp_manager = None
        self._shm_manager = None

        # API credentials for trading (used by both Binance API and QTX shared memory)
        self._binance_api_key = binance_api_key
        self._binance_api_secret = binance_api_secret
        self._domain = domain

        # Trading flags
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs

        # Initialize state for position tracking and funding info
        self._positions_cache: Dict[str, Dict[PositionSide, Position]] = defaultdict(dict)
        self._funding_info = {}

        # For tracking shared memory orders
        self._client_order_id_to_exchange_id = {}

        # Initialize trading pair symbols map (bidict for bidirectional mapping)
        self._trading_pair_symbol_map = bidict()

        # Initialize binance connector first
        self._binance_connector_instance = None

        # Initialize the base class after binance connector initialization
        super().__init__(client_config_map)

        # Set up event forwarders to relay events from Binance to QTX
        self._event_forwarders: List[Tuple[MarketEvent, EventForwarder]] = []

        # Always initialize Binance connector for market data and other functions
        self._init_binance_connector()

        # Configure event forwarding after Binance connector is initialized
        self._configure_event_forwarding()

    def _configure_event_forwarding(self):
        """Configure event forwarding from Binance connector to QTX"""
        if self.binance_connector is None:
            self.logger().warning("Cannot configure event forwarding - Binance connector not available")
            return

        # Import event types we need to forward
        from hummingbot.core.event.events import (
            OrderFilledEvent,
            BuyOrderCompletedEvent,
            SellOrderCompletedEvent,
            OrderCancelledEvent,
            MarketOrderFailureEvent,
            BuyOrderCreatedEvent,
            SellOrderCreatedEvent,
            PositionUpdateEvent,
        )

        # Define event forwarder function
        def create_forwarder(market_event_type):
            def forward_event(event):
                # Change the event source from Binance to QTX
                if hasattr(event, "_event_source"):
                    event._event_source = self.name
                # Re-emit the event from this connector
                self.trigger_event(market_event_type, event)
                self.logger().debug(f"Forwarded {market_event_type} from Binance to QTX: {event}")

            return forward_event

        # Event types to forward
        event_mappings = [
            (MarketEvent.OrderFilled, OrderFilledEvent),
            (MarketEvent.BuyOrderCompleted, BuyOrderCompletedEvent),
            (MarketEvent.SellOrderCompleted, SellOrderCompletedEvent),
            (MarketEvent.OrderCancelled, OrderCancelledEvent),
            (MarketEvent.OrderFailure, MarketOrderFailureEvent),
            (MarketEvent.BuyOrderCreated, BuyOrderCreatedEvent),
            (MarketEvent.SellOrderCreated, SellOrderCreatedEvent),
            (MarketEvent.PositionUpdate, PositionUpdateEvent),
        ]

        # Create and register forwarders
        self._event_forwarders = []
        for event_type, event_class in event_mappings:
            forwarder = create_forwarder(event_type)
            self.binance_connector.add_listener(event_type, forwarder)
            self._event_forwarders.append((event_type, forwarder))

        self.logger().info(f"Configured event forwarding for {len(self._event_forwarders)} event types")

    def _stop_event_forwarding(self):
        """Stop event forwarding from Binance connector"""
        if self.binance_connector is None or not self._event_forwarders:
            return

        for event_type, forwarder in self._event_forwarders:
            try:
                self.binance_connector.remove_listener(event_type, forwarder)
            except Exception as e:
                self.logger().warning(f"Failed to remove event forwarder for {event_type}: {e}")

        self._event_forwarders.clear()
        self.logger().info("Stopped event forwarding from Binance connector")

        # Log a warning if trading is required but no shared memory name is provided
        if self._trading_required and not self._qtx_shared_memory_name:
            self.logger().error(
                "No shared memory name provided. QTX Perpetual order placement via shared memory will be unavailable."
            )

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

    # ---------------------------------------- Properties and Accessors ----------------------------------------
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
    def shm_manager(self):
        """
        Returns the shared memory manager instance.
        Instance is created on first access, but connection is handled
        automatically in the manager's methods.

        :return: The shared memory manager instance, or None if no shared memory name is provided
        """
        try:
            if self._shm_manager is None and self._qtx_shared_memory_name:
                # Log warning if API keys are not available
                if not self._binance_api_key or not self._binance_api_secret:
                    self.logger().warning(
                        "Creating shared memory manager without API credentials. "
                        "Order placement will not work until credentials are provided."
                    )

                # Create instance if it doesn't exist yet
                self._shm_manager = QtxPerpetualSharedMemoryManager.get_instance(
                    api_key=self._binance_api_key,
                    api_secret=self._binance_api_secret,
                    shm_name=self._qtx_shared_memory_name,
                )

                self.logger().info(f"Shared memory manager initialized with segment '{self._qtx_shared_memory_name}'")
            elif self._shm_manager is None and not self._qtx_shared_memory_name:
                # Log debug message to clarify why the manager is None
                self.logger().debug("Shared memory manager not created: No shared memory name provided")
        except Exception as e:
            self.logger().error(f"Error creating shared memory manager: {e}", exc_info=True)
            # Reset the manager to None so it can be retried later
            self._shm_manager = None

        return self._shm_manager

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
    def funding_fee_poll_interval(self) -> int:
        binance_instance = self.binance_connector
        if binance_instance is not None:
            return binance_instance.funding_fee_poll_interval
        return 600

    @property
    def account_positions(self) -> Dict[str, Position]:
        """
        Returns a dictionary of current active open positions.

        In standalone mode, this returns QTX's own position tracking.
        When running as part of a cross-exchange strategy with Binance,
        this ensures each connector has its own position state.
        """
        # We no longer delegate to Binance's position tracking
        # QTX maintains its own position state through the _user_stream_event_listener
        positions = self._perpetual_trading.account_positions
        self.logger().debug(f"QTX account_positions accessed - Count: {len(positions)}, Keys: {list(positions.keys())}")
        return positions

    # ---------------------------------------- Network and Lifecycle Management ----------------------------------------
    async def _make_network_check_request(self):
        pass

    async def stop_network(self):
        """
        Stop networking for UDP and shared memory connections
        """
        await super().stop_network()

        # Stop event forwarders
        self._stop_event_forwarding()

        # Stop UDP manager
        if self._udp_manager is not None:
            for trading_pair in self._trading_pairs:
                try:
                    qtx_symbol = trading_pair_utils.convert_to_qtx_trading_pair(trading_pair)
                    self.logger().info(f"QTX Perpetual stop_network() unsubscribing from {trading_pair} ({qtx_symbol})")
                    await self.udp_manager.unsubscribe_from_symbol(qtx_symbol)
                    self.logger().info(f"QTX Perpetual stop_network() successfully unsubscribed from {trading_pair}")
                except Exception as e:
                    self.logger().error(f"Error unsubscribing from {trading_pair}: {e}")

            await self.udp_manager.stop_listening()

        # Stop shared memory manager
        if self._shm_manager is not None:
            try:
                # Just call disconnect - the manager can handle its own state
                await self._shm_manager.disconnect()
                self.logger().info("QTX Perpetual shared memory manager disconnected")
            except Exception as e:
                self.logger().error(f"Error disconnecting shared memory manager: {e}")

        self.logger().info("QTX Perpetual connector stop_network() FULLY completed.")

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
        Cancel an order using QTX shared memory
        """
        try:
            # Get shared memory manager (connection happens automatically in its methods)
            shm_manager = self.shm_manager
            if shm_manager is None:
                error_msg = "Shared memory manager is not available"
                self.logger().error(f"Cannot cancel order {order_id}: {error_msg}")
                raise ValueError(error_msg)

            # Verify API credentials are available
            if not self._binance_api_key or not self._binance_api_secret:
                error_msg = "API credentials are required for order cancellation"
                self.logger().error(f"Cannot cancel order {order_id}: {error_msg}")
                raise ValueError(error_msg)

            # Get the trading pair symbol in exchange format
            try:
                symbol = await self.exchange_symbol_associated_to_pair(tracked_order.trading_pair)
            except Exception as e:
                error_msg = f"Invalid trading pair format: {tracked_order.trading_pair}"
                self.logger().error(f"Error converting trading pair for cancellation: {e}", exc_info=True)
                raise ValueError(error_msg)

            self.logger().info(f"Cancelling order via QTX shared memory: {order_id}")

            # Attempt to cancel via shared memory with explicit error handling
            try:
                success, result = await shm_manager.cancel_order(client_order_id=order_id, symbol=symbol)
            except Exception as e:
                error_msg = f"Shared memory order cancellation operation failed: {str(e)}"
                self.logger().error(f"Error during cancel_order operation: {e}", exc_info=True)
                raise IOError(error_msg)

            if success:
                # Extract order status and update the order tracker
                status = result.get("status", "CANCELED")
                transaction_time = result.get("transaction_time", self.current_timestamp)

                # Get exchange order ID from our mapping or from the response
                exchange_order_id = self._client_order_id_to_exchange_id.get(
                    order_id, result.get("exchange_order_id", tracked_order.exchange_order_id)
                )

                # Create and process the order update
                order_state = self._binance_order_status_to_oms_order_status(status)
                order_update = OrderUpdate(
                    client_order_id=order_id,
                    exchange_order_id=exchange_order_id,
                    trading_pair=tracked_order.trading_pair,
                    update_timestamp=transaction_time,
                    new_state=order_state,
                )

                self._order_tracker.process_order_update(order_update)

                # Sync cancellation state with Binance connector if available
                if self.binance_connector is not None:
                    try:
                        # Create an equivalent order update for Binance's order tracker
                        binance_order_update = OrderUpdate(
                            client_order_id=order_id,
                            exchange_order_id=exchange_order_id,
                            trading_pair=tracked_order.trading_pair,
                            update_timestamp=transaction_time,
                            new_state=order_state,
                        )

                        # Use Binance's order tracker to process the update
                        self.binance_connector._order_tracker.process_order_update(binance_order_update)

                        self.logger().debug(f"Synchronized cancel for order {order_id} with Binance connector")
                    except Exception as e:
                        self.logger().warning(
                            f"Failed to synchronize cancel for order {order_id} with Binance connector: {e}"
                        )

                self.logger().info(f"Order cancelled successfully via QTX shared memory: {order_id}")
                return True
            else:
                # Check if this is a "not found" error, which we treat as success
                error_msg = result.get("error", "")
                if "not exist" in error_msg.lower() or "not found" in error_msg.lower():
                    self.logger().debug(f"The order {order_id} does not exist on QTX. Treating as already cancelled.")
                    await self._order_tracker.process_order_not_found(order_id)

                    # Sync "not found" state with Binance connector if available
                    if self.binance_connector is not None:
                        try:
                            await self.binance_connector._order_tracker.process_order_not_found(order_id)
                            self.logger().debug(
                                f"Synchronized 'not found' status for order {order_id} with Binance connector"
                            )
                        except Exception as e:
                            self.logger().warning(
                                f"Failed to sync 'not found' status for order {order_id} with Binance connector: {e}"
                            )

                    return True

                # For other errors, raise an exception
                self.logger().error(f"Failed to cancel order {order_id} via QTX shared memory: {result}")
                raise IOError(f"QTX shared memory order cancellation failed: {error_msg}")

        except Exception as e:
            self.logger().error(f"Error cancelling order via QTX shared memory: {e}", exc_info=True)
            # Do not fall back, let the error propagate
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
        Place an order using QTX shared memory
        """
        try:
            # Get shared memory manager (connection happens automatically in its methods)
            shm_manager = self.shm_manager
            if shm_manager is None:
                error_msg = "Shared memory manager is not available"
                self.logger().error(f"Cannot place order {order_id}: {error_msg}")
                raise ValueError(error_msg)

            # Verify API credentials are available
            if not self._binance_api_key or not self._binance_api_secret:
                error_msg = "API credentials are required for order placement"
                self.logger().error(f"Cannot place order {order_id}: {error_msg}")
                raise ValueError(error_msg)

            # Convert parameters to QTX format
            try:
                qtx_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
            except Exception as e:
                error_msg = f"Invalid trading pair format: {trading_pair}"
                self.logger().error(f"Error converting trading pair: {e}", exc_info=True)
                raise ValueError(error_msg)

            # Import constants from the shm_manager module
            from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager import (
                QTX_ORDER_TYPE_GTC,
                QTX_ORDER_TYPE_IOC,
                QTX_ORDER_TYPE_POST_ONLY,
                QTX_POS_SIDE_BOTH,
                QTX_POS_SIDE_LONG,
                QTX_POS_SIDE_SHORT,
                QTX_PRICE_MATCH_NONE,
                QTX_SIDE_BUY,
                QTX_SIDE_SELL,
            )

            # Set side (1 for buy, -1 for sell)
            side = QTX_SIDE_BUY if trade_type is TradeType.BUY else QTX_SIDE_SELL

            # Set position side
            if position_action is PositionAction.OPEN:
                if trade_type is TradeType.BUY:
                    pos_side = QTX_POS_SIDE_LONG
                else:
                    pos_side = QTX_POS_SIDE_SHORT
            elif position_action is PositionAction.CLOSE:
                if trade_type is TradeType.BUY:
                    pos_side = QTX_POS_SIDE_SHORT  # Buy to close a short
                else:
                    pos_side = QTX_POS_SIDE_LONG  # Sell to close a long
            else:
                pos_side = QTX_POS_SIDE_BOTH

            # Map Hummingbot order type to QTX order type
            if order_type is OrderType.LIMIT:
                qtx_order_type = QTX_ORDER_TYPE_GTC
            elif order_type is OrderType.LIMIT_MAKER:
                qtx_order_type = QTX_ORDER_TYPE_POST_ONLY
            else:
                qtx_order_type = QTX_ORDER_TYPE_IOC

            self.logger().info(
                f"Placing order via QTX shared memory: {order_id}, {trading_pair}, {amount}, {side}, {pos_side}, {price}"
            )

            # Place the order via shared memory with additional error handling
            try:
                # The shared memory manager will handle NaN/None price validation
                success, order_result = await shm_manager.place_order(
                    client_order_id=order_id,
                    symbol=qtx_symbol,
                    side=side,
                    position_side=pos_side,
                    order_type=qtx_order_type,
                    price=price,
                    size=amount,
                    price_match=QTX_PRICE_MATCH_NONE,
                )
            except Exception as e:
                error_msg = f"Shared memory order placement operation failed: {str(e)}"
                self.logger().error(f"Error during place_order operation: {e}", exc_info=True)
                raise IOError(error_msg)

            if not success:
                error_msg = order_result.get("error", "Unknown error")
                self.logger().error(f"Error placing order via QTX shared memory: {error_msg}")
                raise IOError(f"QTX shared memory order placement failed: {error_msg}")

            # Extract the exchange order ID
            exchange_order_id = order_result.get("exchange_order_id", None)
            if not exchange_order_id:
                # Generate a temporary ID if not provided by the exchange
                exchange_order_id = f"qtx_{int(time.time() * 1000)}_{order_id}"

            # Store mapping for future reference
            self._client_order_id_to_exchange_id[order_id] = exchange_order_id

            # Get timestamp from response or use current time
            transaction_time = order_result.get("transaction_time", self.current_timestamp)

            # Get order status and create an order update to immediately reflect the state
            status = order_result.get("status", "NEW")

            # Process the order update in our tracker
            order_state = self._binance_order_status_to_oms_order_status(status)
            order_update = OrderUpdate(
                client_order_id=order_id,
                exchange_order_id=exchange_order_id,
                trading_pair=trading_pair,
                update_timestamp=transaction_time,
                new_state=order_state,
            )

            self._order_tracker.process_order_update(order_update)

            # Sync order state with Binance connector if available
            if self.binance_connector is not None:
                try:
                    # Create an equivalent order update for Binance's order tracker
                    binance_order_update = OrderUpdate(
                        client_order_id=order_id,
                        exchange_order_id=exchange_order_id,
                        trading_pair=trading_pair,
                        update_timestamp=transaction_time,
                        new_state=order_state,
                    )

                    # Use Binance's order tracker to process the update
                    self.binance_connector._order_tracker.process_order_update(binance_order_update)

                    # Store the mapping in Binance's order tracker
                    # This ensures Binance connector can find the order in future operations
                    # Create an InFlightOrder object to pass to start_tracking_order
                    from hummingbot.core.data_type.in_flight_order import InFlightOrder

                    binance_order = InFlightOrder(
                        client_order_id=order_id,
                        exchange_order_id=exchange_order_id,
                        trading_pair=trading_pair,
                        order_type=order_type,
                        trade_type=trade_type,
                        price=price,
                        amount=amount,
                        creation_timestamp=transaction_time,
                        initial_state=order_state,
                    )
                    self.binance_connector._order_tracker.start_tracking_order(binance_order)

                    self.logger().debug(f"Synchronized order {order_id} state with Binance connector")
                except Exception as e:
                    self.logger().warning(f"Failed to synchronize order {order_id} with Binance connector: {e}")

            self.logger().info(
                f"Order placed via QTX shared memory: {order_id} -> {exchange_order_id}, status: {status}"
            )

            # Check if the order was immediately filled and create a TradeUpdate if needed
            if status in ["FILLED", "PARTIALLY_FILLED"]:
                # Extract fill information from the response
                fill_price = order_result.get("price", price)
                if fill_price and isinstance(fill_price, str):
                    fill_price = Decimal(fill_price)

                # Use executed_qty if available, otherwise fall back to quantity or order amount
                fill_quantity = order_result.get("executed_qty", order_result.get("quantity", amount))
                if fill_quantity and isinstance(fill_quantity, str):
                    fill_quantity = Decimal(fill_quantity)

                # Create TradeUpdate for the fill
                if fill_price and fill_quantity:
                    trade_id = order_result.get("trade_id", exchange_order_id)
                    # Extract quote asset from trading pair for fee calculation
                    base_asset, quote_asset = trading_pair.split("-")
                    fee_asset = order_result.get("fee_asset", quote_asset)
                    fee_amount = order_result.get("fee_amount", Decimal("0"))
                    if fee_amount and isinstance(fee_amount, str):
                        fee_amount = Decimal(fee_amount)

                    # Create flat fees list
                    flat_fees = [] if fee_amount == Decimal("0") else [TokenAmount(amount=fee_amount, token=fee_asset)]

                    # Create TradeFeeBase object based on position action
                    fee = TradeFeeBase.new_perpetual_fee(
                        fee_schema=self.trade_fee_schema(),
                        position_action=position_action,
                        percent_token=fee_asset,
                        flat_fees=flat_fees,
                    )

                    # Create and process the trade update
                    trade_update = TradeUpdate(
                        client_order_id=order_id,
                        exchange_order_id=exchange_order_id,
                        trade_id=trade_id,
                        trading_pair=trading_pair,
                        fee=fee,
                        fill_price=fill_price,
                        fill_base_amount=fill_quantity,
                        fill_quote_amount=fill_price * fill_quantity,
                        fill_timestamp=transaction_time,
                    )
                    self._order_tracker.process_trade_update(trade_update)

                    self.logger().info(
                        f"Order {order_id} immediately filled at price {fill_price} with quantity {fill_quantity}"
                    )

            # Return exchange order ID and transaction time as required by the interface
            return exchange_order_id, transaction_time

        except Exception as e:
            self.logger().error(f"Error placing order via QTX shared memory: {e}", exc_info=True)
            # If shared memory order placement fails, propagate the error without fallback
            raise

    def _is_order_not_found_during_cancelation_error(self, exception) -> bool:
        """
        Determine if an error from cancel order request is due to order not found

        Handles both Binance API errors and QTX shared memory errors
        """
        # Check for QTX shared memory specific error messages
        if isinstance(exception, dict) and "error" in exception:
            error_msg = exception["error"].lower()
            return "not exist" in error_msg or "not found" in error_msg

        # Check for string-based error messages from QTX
        if isinstance(exception, str) and ("not exist" in exception.lower() or "not found" in exception.lower()):
            return True

        # Delegate to Binance for Binance-format errors
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

        Handles both Binance API errors and QTX shared memory errors
        """
        # Check for QTX shared memory specific error messages
        if isinstance(exception, dict) and "error" in exception:
            error_msg = exception["error"].lower()
            return "not exist" in error_msg or "not found" in error_msg

        # Check for string-based error messages from QTX
        if isinstance(exception, str) and ("not exist" in exception.lower() or "not found" in exception.lower()):
            return True

        # Delegate to Binance for Binance-format errors
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
        Update positions directly in QTX's connector.

        While user stream events should keep positions updated in real-time,
        this periodic update ensures positions are still synchronized in case
        some events are missed or during initialization.
        """
        binance_instance = self.binance_connector
        if binance_instance is None:
            self.logger().error("Binance connector not available: _update_positions failed")
            return

        try:
            # First, fetch position information from Binance's API
            positions = await binance_instance._api_get(
                path_url=BINANCE_CONSTANTS.POSITION_INFORMATION_URL, is_auth_required=True
            )

            # Clear existing positions to avoid stale data
            for trading_pair in self._trading_pairs:
                for position_side in [PositionSide.LONG, PositionSide.SHORT]:
                    pos_key = self._perpetual_trading.position_key(
                        trading_pair, position_side, self._perpetual_trading.position_mode
                    )
                    self._perpetual_trading.remove_position(pos_key)

            # Process and update positions in QTX's own position tracker
            for position in positions:
                trading_pair = position.get("symbol")
                try:
                    hb_trading_pair = await self.trading_pair_associated_to_exchange_symbol(trading_pair)
                except KeyError:
                    # Skip positions for trading pairs not tracked by the connector
                    continue

                position_side = PositionSide[position.get("positionSide")]
                if position_side is None:
                    continue

                try:
                    entry_price = Decimal(position.get("entryPrice"))
                    amount = Decimal(position.get("positionAmt"))
                    unrealized_pnl = Decimal(position.get("unRealizedProfit"))
                    leverage = Decimal(position.get("leverage"))

                    # Get position key
                    pos_key = self._perpetual_trading.position_key(
                        hb_trading_pair, position_side, self._perpetual_trading.position_mode
                    )

                    # Only add positions with non-zero amounts
                    if amount != 0:
                        _position = Position(
                            trading_pair=hb_trading_pair,
                            position_side=position_side,
                            unrealized_pnl=unrealized_pnl,
                            entry_price=entry_price,
                            amount=amount,
                            leverage=leverage,
                        )
                        self._perpetual_trading.set_position(pos_key, _position)
                        self.logger().info(
                            f"API Position Updated - Key: {pos_key}, "
                            f"Side: {position_side}, Amount: {amount}, Entry: {entry_price}"
                        )
                except (ValueError, TypeError, decimal.InvalidOperation) as e:
                    self.logger().error(f"Error parsing position data: {e}. Position data: {position}", exc_info=True)
                    continue

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
        Process user stream events received from the Binance connector.

        This handler processes the same events that were processed by Binance's connector,
        ensuring that QTX's own order tracker is properly updated. This is especially
        important for position tracking when orders are filled.
        """

        while True:
            try:
                # Get data from the user stream - handle data format from Binance
                # Since we're delegating to Binance's user stream, we need to adapt to its format
                data = await self._user_stream_tracker.user_stream.get()

                # Binance sends the event message directly without a channel component
                event_message = data

                # Process the same message types as Binance does
                event_type = event_message.get("e")

                # Process order updates
                if event_type == "ORDER_TRADE_UPDATE":
                    order_data = event_message.get("o", {})
                    client_order_id = order_data.get("c", "")
                    
                    # Log the raw order data for debugging
                    self.logger().debug(f"ORDER_TRADE_UPDATE received: {order_data}")

                    # Find the order in our order tracker
                    tracked_order = self._order_tracker.fetch_order(client_order_id=client_order_id)
                    if tracked_order is not None:
                        # Process the order update in QTX's order tracker
                        exchange_order_id = str(order_data.get("i", ""))
                        trade_id = str(order_data.get("t", ""))
                        trading_pair = await self.trading_pair_associated_to_exchange_symbol(order_data.get("s", ""))

                        # Process different order statuses
                        order_status = order_data.get("X", "")
                        fill_price = Decimal(order_data.get("L", "0"))
                        fill_amount = Decimal(order_data.get("l", "0"))
                        fee_asset = order_data.get("N", "")
                        fee_amount = Decimal(order_data.get("n", "0"))

                        # Process filled orders
                        if fill_amount > 0 and fill_price > 0:
                            # Create proper TradeFeeBase object like Binance does
                            position_side = order_data.get("ps", "BOTH")

                            # Determine position action based on trade type and position side
                            if position_side == "BOTH":
                                # If no specific position side, we can't determine position action
                                position_action = PositionAction.NIL
                            else:
                                position_action = (
                                    PositionAction.OPEN
                                    if (
                                        tracked_order.trade_type is TradeType.BUY
                                        and position_side == "LONG"
                                        or tracked_order.trade_type is TradeType.SELL
                                        and position_side == "SHORT"
                                    )
                                    else PositionAction.CLOSE
                                )

                            # Create flat fees list
                            flat_fees = (
                                [] if fee_amount == Decimal("0") else [TokenAmount(amount=fee_amount, token=fee_asset)]
                            )

                            # Create TradeFeeBase object
                            fee = TradeFeeBase.new_perpetual_fee(
                                fee_schema=self.trade_fee_schema(),
                                position_action=position_action,
                                percent_token=fee_asset,
                                flat_fees=flat_fees,
                            )

                            # Create proper TradeUpdate object
                            trade_update = TradeUpdate(
                                client_order_id=tracked_order.client_order_id,
                                exchange_order_id=exchange_order_id,
                                trade_id=trade_id,
                                trading_pair=trading_pair,
                                fee=fee,
                                fill_price=fill_price,
                                fill_base_amount=fill_amount,
                                fill_quote_amount=fill_price * fill_amount,
                                fill_timestamp=self.current_timestamp,
                            )
                            self._order_tracker.process_trade_update(trade_update)

                        # Process order status updates
                        if order_status in ["NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED"]:
                            # Create proper OrderUpdate object instead of passing individual parameters
                            order_update = OrderUpdate(
                                client_order_id=tracked_order.client_order_id,
                                exchange_order_id=exchange_order_id,
                                trading_pair=trading_pair,
                                update_timestamp=self.current_timestamp,
                                new_state=self._binance_order_status_to_oms_order_status(order_status),
                            )
                            self._order_tracker.process_order_update(order_update)

                # Process account position updates
                elif event_type == "ACCOUNT_UPDATE":
                    account_data = event_message.get("a", {})
                    positions_data = account_data.get("P", [])
                    
                    # Log the raw position data for debugging
                    self.logger().debug(f"ACCOUNT_UPDATE received with {len(positions_data)} positions")

                    # Process position updates
                    for position in positions_data:
                        symbol = position.get("s", "")
                        try:
                            trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol)
                        except KeyError:
                            # Skip positions for trading pairs not tracked by this connector
                            continue

                        position_side = (
                            PositionSide.LONG
                            if position.get("ps", "") == "LONG"
                            else PositionSide.SHORT if position.get("ps", "") == "SHORT" else None
                        )
                        if position_side is None:
                            continue

                        try:
                            entry_price = Decimal(position.get("ep", "0"))
                            amount = Decimal(position.get("pa", "0"))
                            unrealized_pnl = Decimal(position.get("up", "0"))
                            # Binance's ACCOUNT_UPDATE event doesn't include leverage directly
                            # We can infer it from the margin type or use a default
                            leverage = Decimal("1")  # Default leverage
                            self.logger().debug(f"Position data fields: {position.keys()}")
                        except (ValueError, TypeError, decimal.InvalidOperation) as e:
                            self.logger().error(
                                f"Error parsing position data: {e}. Position data: {position}", exc_info=True
                            )
                            continue

                        # Get position key
                        pos_key = self._perpetual_trading.position_key(
                            trading_pair, position_side, self._perpetual_trading.position_mode
                        )

                        # If the position amount is non-zero, update or add the position
                        if amount != 0:
                            _position = Position(
                                trading_pair=trading_pair,
                                position_side=position_side,
                                unrealized_pnl=unrealized_pnl,
                                entry_price=entry_price,
                                amount=amount,
                                leverage=leverage,
                            )
                            self._perpetual_trading.set_position(pos_key, _position)
                            self.logger().info(
                                f"QTX Position Added/Updated - Key: {pos_key}, "
                                f"Side: {position_side}, Amount: {amount}, Entry: {entry_price}"
                            )
                        else:
                            # If position amount is 0, remove the position
                            self._perpetual_trading.remove_position(pos_key)
                            self.logger().info(f"QTX Position Removed - Key: {pos_key}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in user stream listener loop: {e}", exc_info=True)

    def _binance_order_status_to_oms_order_status(self, status: str):
        """Convert Binance order status to our internal OrderState enum"""
        from hummingbot.core.data_type.in_flight_order import OrderState

        binance_to_oms_status = {
            "NEW": OrderState.OPEN,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELED,
            "PENDING_CANCEL": OrderState.PENDING_CANCEL,
            "REJECTED": OrderState.FAILED,
            "EXPIRED": OrderState.FAILED,
        }
        return binance_to_oms_status.get(status, OrderState.OPEN)

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

    # ---------------------------------------- Event Forwarding ----------------------------------------
    def _configure_event_forwarding(self):
        """
        Configure event forwarders to relay events from Binance to QTX.
        This is necessary so that order fills and trade events from Binance
        get properly recorded in the markets_recorder and history.
        """
        if self.binance_connector is None:
            return

        self.logger().info("Setting up event forwarding from Binance to QTX")

        # Forward key trading events from Binance to QTX
        event_types = [
            MarketEvent.OrderFilled,
            MarketEvent.BuyOrderCompleted,
            MarketEvent.SellOrderCompleted,
            MarketEvent.OrderCancelled,
            MarketEvent.OrderFailure,
            MarketEvent.BuyOrderCreated,
            MarketEvent.SellOrderCreated,
        ]

        for event_type in event_types:
            forwarder = EventForwarder(
                to_function=lambda event, evt_type=event_type: self.trigger_event(evt_type, event)
            )
            self.binance_connector.add_listener(event_type, forwarder)
            self._event_forwarders.append((event_type, forwarder))

    def _stop_event_forwarding(self):
        """
        Stop all event forwarders when the connector shuts down.
        """
        if self.binance_connector is None:
            return

        for event_type, forwarder in self._event_forwarders:
            self.binance_connector.remove_listener(event_type, forwarder)
        self._event_forwarders.clear()

        self.logger().info("Stopped event forwarding from Binance to QTX")
