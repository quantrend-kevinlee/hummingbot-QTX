#!/usr/bin/env python
"""
QTX Perpetual Derivative Connector with dynamic exchange backend support.
This connector uses QTX's market data and shared memory order placement/cancellation combined with a selected exchange's trading
capabilities (Binance, OKX, Bybit, etc.) to provide a hybrid trading solution.
"""
import asyncio
import importlib
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional, Tuple

from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_trading_pair_utils as trading_pair_utils
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager import QtxPerpetualSharedMemoryManager
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_udp_manager import QtxPerpetualUDPManager
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter

# Exchange connector mapping
EXCHANGE_CONNECTOR_CLASSES = {
    "binance": {
        "module": "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_derivative",
        "class": "BinancePerpetualDerivative",
        "exchange_name_on_qtx": "binance-futures",
    },
    "okx": {
        "module": "hummingbot.connector.derivative.okx_perpetual.okx_perpetual_derivative",
        "class": "OkxPerpetualDerivative",
        "exchange_name_on_qtx": "okx-futures",
    },
    "bybit": {
        "module": "hummingbot.connector.derivative.bybit_perpetual.bybit_perpetual_derivative",
        "class": "BybitPerpetualDerivative",
        "exchange_name_on_qtx": "bybit-futures",
    },
}


class QtxPerpetualDerivative(PerpetualDerivativePyBase):
    """
    QtxPerpetualDerivative that dynamically creates a connector based on the specified exchange backend.
    """

    SHORT_POLL_INTERVAL = 5.0
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    LONG_POLL_INTERVAL = 120.0

    def __new__(
        cls,
        client_config_map: "ClientConfigAdapter",
        qtx_perpetual_host: str = None,
        qtx_perpetual_port: int = None,
        exchange_backend: str = None,
        exchange_api_key: str = None,
        exchange_api_secret: str = None,
        qtx_place_order_shared_memory_name: str = None,
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
    ):
        """
        Create a new instance of a dynamic QTX connector with the specified exchange backend.
        This uses Python's __new__ to return a custom subclass instance.
        """
        # Validate required parameters
        if not exchange_backend:
            raise ValueError("exchange_backend is required")
        if not qtx_perpetual_host:
            raise ValueError("qtx_perpetual_host is required")
        if not qtx_perpetual_port:
            raise ValueError("qtx_perpetual_port is required")
        # API keys are only required when trading is required
        if trading_required:
            if not exchange_api_key:
                raise ValueError("exchange_api_key is required for trading")
            if not exchange_api_secret:
                raise ValueError("exchange_api_secret is required for trading")
        # Get the exchange connector class dynamically
        if exchange_backend.lower() not in EXCHANGE_CONNECTOR_CLASSES:
            raise ValueError(f"Unsupported exchange backend: {exchange_backend}")
        exchange_info = EXCHANGE_CONNECTOR_CLASSES[exchange_backend.lower()]
        module = importlib.import_module(exchange_info["module"])
        base_exchange_class = getattr(module, exchange_info["class"])

        # Create a dynamic class that inherits from the selected exchange
        class QtxDynamicConnector(base_exchange_class):
            """
            Dynamic connector that uses QTX market data with a selected exchange's trading capabilities.
            """

            def __init__(self, *args, **kwargs):
                # Extract QTX-specific parameters
                self._qtx_perpetual_host = kwargs.pop("qtx_perpetual_host", qtx_perpetual_host)
                self._qtx_perpetual_port = kwargs.pop("qtx_perpetual_port", qtx_perpetual_port)
                self._qtx_shared_memory_name = kwargs.pop(
                    "qtx_place_order_shared_memory_name", qtx_place_order_shared_memory_name
                )
                self._exchange_backend = kwargs.pop("exchange_backend", exchange_backend)
                # Managers (created lazily)
                self._udp_manager = None
                self._shm_manager = None
                # Store API keys for later use
                self._exchange_api_key = kwargs.pop("exchange_api_key", exchange_api_key)
                self._exchange_api_secret = kwargs.pop("exchange_api_secret", exchange_api_secret)
                # Call parent constructor with appropriate parameters
                super().__init__(*args, **kwargs)
                # Override the exchange name to be QTX
                self._name = "qtx_perpetual"
                self._exchange_name = "qtx_perpetual"

            @property
            def name(self) -> str:
                """Return the exchange name as QTX."""
                return "qtx_perpetual"

            @property
            def udp_manager(self) -> QtxPerpetualUDPManager:
                """Get the UDP manager instance, creating it if needed."""
                if self._udp_manager is None:
                    # Get exchange name from EXCHANGE_CONNECTOR_CLASSES
                    exchange_info = EXCHANGE_CONNECTOR_CLASSES.get(self._exchange_backend.lower(), {})
                    exchange_name_on_qtx = exchange_info.get("exchange_name_on_qtx")
                    if exchange_name_on_qtx is None:
                        self.logger().error(
                            f"CRITICAL ERROR: No exchange_name_on_qtx configured for {self._exchange_backend}! "
                            f"Please add an 'exchange_name_on_qtx' entry in EXCHANGE_CONNECTOR_CLASSES for this exchange."
                        )
                        raise ValueError(f"Missing exchange_name_on_qtx configuration for {self._exchange_backend}")
                    self._udp_manager = QtxPerpetualUDPManager(
                        host=self._qtx_perpetual_host,
                        port=self._qtx_perpetual_port,
                        exchange_name_on_qtx=exchange_name_on_qtx,
                    )
                return self._udp_manager

            @property
            def shm_manager(self) -> Optional[QtxPerpetualSharedMemoryManager]:
                """Get the shared memory manager instance, creating it if needed."""
                if self._shm_manager is None and self._qtx_shared_memory_name:
                    # Get exchange name from EXCHANGE_CONNECTOR_CLASSES
                    exchange_info = EXCHANGE_CONNECTOR_CLASSES.get(self._exchange_backend.lower(), {})
                    exchange_name_on_qtx = exchange_info.get("exchange_name_on_qtx")
                    if exchange_name_on_qtx is None:
                        self.logger().error(
                            f"CRITICAL ERROR: No exchange_name_on_qtx configured for {self._exchange_backend}! "
                            f"Please add an 'exchange_name_on_qtx' entry in EXCHANGE_CONNECTOR_CLASSES for this exchange."
                        )
                        raise ValueError(f"Missing exchange_name_on_qtx configuration for {self._exchange_backend}")
                    self._shm_manager = QtxPerpetualSharedMemoryManager.get_instance(
                        api_key=self._exchange_api_key,
                        api_secret=self._exchange_api_secret,
                        shm_name=self._qtx_shared_memory_name,
                        exchange_name_on_qtx=exchange_name_on_qtx,
                    )
                    self.logger().info(
                        f"Shared memory manager initialized with segment '{self._qtx_shared_memory_name}' "
                        f"for exchange '{exchange_name_on_qtx}'"
                    )
                return self._shm_manager

            def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
                """Create order book data source that uses parent exchange's implementation with QTX market data."""
                # Create the parent exchange's order book data source
                parent_data_source = super()._create_order_book_data_source()
                # Initialize UDP manager for QTX market data
                self._setup_qtx_market_data(parent_data_source)

                async def qtx_listen_for_subscriptions():
                    """Use QTX UDP for market data subscriptions"""
                    await self._setup_qtx_udp_subscriptions()

                async def qtx_listen_for_order_book_diffs(ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
                    """Listen for orderbook diffs from QTX UDP"""
                    await self._listen_for_qtx_order_book_diffs(output)

                async def qtx_listen_for_order_book_snapshots(
                    ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue
                ):
                    """Listen for orderbook snapshots from QTX UDP"""
                    await self._listen_for_qtx_order_book_snapshots(output)

                async def qtx_listen_for_trades(ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
                    """Listen for trades from QTX UDP"""
                    await self._listen_for_qtx_trades(output)

                async def qtx_order_book_snapshot(trading_pair: str) -> OrderBookMessage:
                    """Get order book snapshot for a trading pair using QTX UDP data"""
                    return await self._get_qtx_order_book_snapshot(trading_pair)

                # Replace only the market data methods
                parent_data_source.listen_for_subscriptions = qtx_listen_for_subscriptions
                parent_data_source.listen_for_order_book_diffs = qtx_listen_for_order_book_diffs
                parent_data_source.listen_for_order_book_snapshots = qtx_listen_for_order_book_snapshots
                parent_data_source.listen_for_trades = qtx_listen_for_trades
                parent_data_source._order_book_snapshot = qtx_order_book_snapshot
                return parent_data_source

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
                Place an order using QTX shared memory if available, otherwise delegate to parent exchange.
                """
                if self.shm_manager is not None:
                    # Use QTX shared memory for order placement
                    try:
                        # Track the order before placing
                        # We start tracking order with initial state OPEN to ensure it's properly tracked
                        # This helps with order status management when order is filled
                        from hummingbot.core.data_type.in_flight_order import OrderState

                        self.start_tracking_order(
                            order_id=order_id,
                            exchange_order_id=None,
                            trading_pair=trading_pair,
                            trade_type=trade_type,
                            price=price,
                            amount=amount,
                            order_type=order_type,
                            position_action=position_action,
                            initial_state=OrderState.OPEN,
                        )

                        # Convert to exchange format for placing order
                        # First check if symbols mapping is ready
                        if not super().trading_pair_symbol_map_ready():
                            self.logger().warning(
                                f"Symbol mapping not ready yet. Will retry order placement for {trading_pair}"
                            )
                            # Cancel the order and retry later
                            self.stop_tracking_order(order_id)
                            raise Exception("Trading pair symbol map not ready")
                        # Use the parent exchange's method to convert from Hummingbot to exchange format
                        exchange_pair = await super().exchange_symbol_associated_to_pair(trading_pair)
                        self.logger().info(
                            f"Order placement: Converting {trading_pair} to exchange format: {exchange_pair}"
                        )
                        # Convert parameters to expected types for shared memory
                        side_int = 1 if trade_type == TradeType.BUY else -1
                        # Determine position side based on position action and trade type
                        # For HEDGE mode: LONG positions use BUY orders, SHORT positions use SELL orders
                        if position_action == PositionAction.OPEN:
                            if trade_type == TradeType.BUY:
                                position_side_int = 1  # LONG
                            else:
                                position_side_int = 2  # SHORT
                        elif position_action == PositionAction.CLOSE:
                            # Closing positions - opposite side
                            if trade_type == TradeType.BUY:
                                position_side_int = 2  # Closing SHORT
                            else:
                                position_side_int = 1  # Closing LONG
                        else:
                            # When position_action is NIL, default to OPEN behavior in HEDGE mode
                            if trade_type == TradeType.BUY:
                                position_side_int = 1  # LONG
                            else:
                                position_side_int = 2  # SHORT
                        # Map order types to shared memory format
                        order_type_map = {
                            OrderType.LIMIT: 2,  # GTC
                            OrderType.LIMIT_MAKER: 1,  # POST_ONLY
                            OrderType.MARKET: 0,  # IOC
                        }
                        order_type_int = order_type_map.get(order_type, 2)  # Default to GTC

                        success, result = await self.shm_manager.place_order(
                            client_order_id=order_id,
                            symbol=exchange_pair,
                            side=side_int,
                            position_side=position_side_int,
                            order_type=order_type_int,
                            price=price,
                            size=amount,
                            price_match=0,  # NONE for now
                        )
                        if not success:
                            error_msg = result.get("error", "Unknown error placing order via shared memory")
                            self.logger().error(f"Failed to place order via QTX shared memory: {error_msg}")
                            raise Exception(error_msg)
                        exchange_order_id = result.get("exchange_order_id", order_id)
                        transaction_time = result.get("transaction_time", self.current_timestamp)
                        self.logger().info(
                            f"Order placed via shared memory - Client ID: {order_id}, Exchange ID: {exchange_order_id}"
                        )

                        # Fetch and update the tracked order
                        tracked_order = self._order_tracker.fetch_order(client_order_id=order_id)
                        if tracked_order:
                            tracked_order.exchange_order_id = exchange_order_id
                        else:
                            self.logger().warning(f"Tracked order {order_id} not found after placement")
                        return exchange_order_id, transaction_time
                    except Exception as e:
                        self.logger().error(f"Error placing order via QTX shared memory: {e}", exc_info=True)
                        self.stop_tracking_order(order_id)
                        raise
                else:
                    raise Exception("QTX shared memory manager is not initialized. Order placement failed.")

            async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
                """
                Cancel an order using QTX shared memory if available, otherwise delegate to parent exchange.
                """
                if self.shm_manager is not None:
                    # Use QTX shared memory for order cancellation
                    try:
                        # Use the parent exchange's method to convert from Hummingbot to exchange format
                        exchange_pair = await super().exchange_symbol_associated_to_pair(tracked_order.trading_pair)
                        self.logger().info(
                            f"Order cancellation: Converting {tracked_order.trading_pair} to exchange format: {exchange_pair}"
                        )
                        success, response_data = await self.shm_manager.cancel_order(
                            client_order_id=order_id,
                            symbol=exchange_pair,
                        )
                        if success:
                            self.logger().info(f"Order cancelled successfully via QTX shared memory: {order_id}")
                            return True
                        else:
                            error_msg = response_data.get("error", "Unknown error")
                            if "not exist" in error_msg.lower() or "not found" in error_msg.lower():
                                self.logger().debug(
                                    f"Order {order_id} does not exist on QTX. Treating as already cancelled."
                                )
                                await self._order_tracker.process_order_not_found(order_id)
                                return True
                            raise IOError(f"QTX shared memory order cancellation failed: {error_msg}")
                    except Exception as e:
                        self.logger().error(f"Error cancelling order via QTX shared memory: {e}", exc_info=True)
                        raise
                else:
                    raise Exception("QTX shared memory manager is not initialized. Order cancellation failed.")

            def _setup_qtx_market_data(self, parent_data_source):
                """Setup QTX market data integration with the parent data source"""
                # Initialize message queues
                self._message_queue = {
                    "snapshots": asyncio.Queue(),  # Keep for compatibility but QTX doesn't send snapshots
                    "diffs": asyncio.Queue(),
                    "trades": asyncio.Queue(),
                }
                # Store the parent data source
                self._parent_data_source = parent_data_source
                # Keep track of UDP subscriptions
                self._udp_subscriptions = set()
                # Keep track of empty orderbooks
                self._empty_orderbook = {}
                for trading_pair in self._trading_pairs:
                    self._empty_orderbook[trading_pair] = True

            async def _handle_ticker_message(self, message):
                """
                Process ticker messages (type 1/-1) from QTX UDP feed.
                :param message: The parsed ticker message from the UDP manager
                """
                # Extract the trading pair from the message
                trading_pair = self._get_trading_pair_from_message(message)
                if not trading_pair:
                    return
                # Determine if this is a bid (1) or ask (-1) ticker
                is_bid = False
                if isinstance(message, dict):
                    is_bid = message.get("is_bid", False)
                    if "message_type" in message and message["message_type"] == "ticker":
                        is_bid = message.get("is_bid", False)
                elif hasattr(message, "type"):
                    is_bid = message.type > 0
                # Extract price and size from the message
                price = 0
                size = 0
                if isinstance(message, dict):
                    # For dict-style messages
                    bids = message.get("bids", [])
                    asks = message.get("asks", [])
                    if is_bid and bids:
                        price, size = bids[0]
                    elif not is_bid and asks:
                        price, size = asks[0]
                else:
                    # For object-style messages
                    price = getattr(message, "price", 0)
                    size = getattr(message, "size", 0)
                # Get timestamp and create update_id
                timestamp = 0
                if isinstance(message, dict):
                    timestamp = message.get("timestamp", self.current_timestamp)
                else:
                    timestamp = getattr(message, "timestamp", self.current_timestamp)
                update_id = int(timestamp * 1000)
                # Create a synthetic order book diff message with a single level
                order_book_message = OrderBookMessage(
                    message_type=OrderBookMessageType.DIFF,
                    content={
                        "trading_pair": trading_pair,
                        "update_id": update_id,
                        "bids": [[price, size]] if is_bid else [],
                        "asks": [] if is_bid else [[price, size]],
                    },
                    timestamp=timestamp,
                )
                # Add message to the queue for processing
                self._message_queue["diffs"].put_nowait(order_book_message)

            async def _handle_depth_message(self, message):
                """
                Process depth messages (type 2) from QTX UDP feed.
                :param message: The parsed depth message from the UDP manager
                """
                # Extract the trading pair from the message
                trading_pair = self._get_trading_pair_from_message(message)
                if not trading_pair:
                    return
                # Extract bids and asks
                bids = []
                asks = []
                if isinstance(message, dict):
                    bids = message.get("bids", [])
                    asks = message.get("asks", [])
                else:
                    bids = getattr(message, "bids", [])
                    asks = getattr(message, "asks", [])
                # Get timestamp
                timestamp = 0
                if isinstance(message, dict):
                    timestamp = message.get("timestamp", self.current_timestamp)
                else:
                    timestamp = getattr(message, "timestamp", self.current_timestamp)
                update_id = int(timestamp * 1000)
                # Determine if this should be a snapshot (first message) or a diff
                # Use _empty_orderbook dict to track if we've received a snapshot for this pair yet
                is_empty = self._empty_orderbook.get(trading_pair, True)
                message_type = OrderBookMessageType.SNAPSHOT if is_empty else OrderBookMessageType.DIFF
                queue_name = "snapshots" if is_empty else "diffs"
                # Create order book message
                order_book_message = OrderBookMessage(
                    message_type=message_type,
                    content={
                        "trading_pair": trading_pair,
                        "update_id": update_id,
                        "bids": bids,
                        "asks": asks,
                    },
                    timestamp=timestamp,
                )
                # Add message to the appropriate queue for processing
                self._message_queue[queue_name].put_nowait(order_book_message)
                # If this was a snapshot, mark the orderbook as no longer empty
                if is_empty:
                    self._empty_orderbook[trading_pair] = False
                    self.logger().info(
                        f"Received first depth data for {trading_pair}: {len(bids)} bids, {len(asks)} asks"
                    )

            async def _handle_trade_message(self, message):
                """
                Process trade messages (type 3/-3) from QTX UDP feed.
                :param message: The parsed trade message from the UDP manager
                """
                # Extract the trading pair from the message
                trading_pair = self._get_trading_pair_from_message(message)
                if not trading_pair:
                    return
                # Determine if this is a buy (3) or sell (-3) trade
                is_buy = False
                if isinstance(message, dict):
                    if "message_type" in message and message["message_type"] == "trade":
                        is_buy = message.get("is_buy", False)
                    else:
                        is_buy = message.get("is_buy", False)
                elif hasattr(message, "type"):
                    is_buy = message.type > 0
                # Extract price and size from the message
                price = 0
                size = 0
                if isinstance(message, dict):
                    price = message.get("price", 0)
                    size = message.get("amount", 0)
                else:
                    price = getattr(message, "price", 0)
                    size = getattr(message, "size", 0)
                # Get timestamp and create trade_id
                timestamp = 0
                if isinstance(message, dict):
                    timestamp = message.get("timestamp", self.current_timestamp)
                else:
                    timestamp = getattr(message, "timestamp", self.current_timestamp)
                trade_id = f"{int(timestamp * 1000000)}"
                update_id = int(timestamp * 1000)
                # Create trade message
                trade_message = OrderBookMessage(
                    message_type=OrderBookMessageType.TRADE,
                    content={
                        "trading_pair": trading_pair,
                        "trade_type": TradeType.BUY if is_buy else TradeType.SELL,
                        "trade_id": trade_id,
                        "update_id": update_id,
                        "price": price,
                        "amount": size,
                    },
                    timestamp=timestamp,
                )
                # Add message to the queue for processing
                self._message_queue["trades"].put_nowait(trade_message)

            def _get_trading_pair_from_message(self, message) -> Optional[str]:
                """
                Extract the trading pair from a message, handling different message formats.
                :param message: The message from the UDP manager
                :return: Trading pair string or None if not found
                """
                # Get the latest subscription_indices from the UDP manager
                subscription_indices = self.udp_manager.subscription_indices
                # Extract index from the message
                index = None
                if isinstance(message, dict) and "index" in message:
                    index = message.get("index")
                elif hasattr(message, "index"):
                    index = message.index
                else:
                    # If no index, check if the message has a trading_pair directly
                    if isinstance(message, dict) and "trading_pair" in message:
                        return message.get("trading_pair")
                    elif hasattr(message, "trading_pair"):
                        return message.trading_pair
                    # Can't process without a way to find the trading pair
                    self.logger().warning(f"Message without valid index or trading_pair received: {message}")
                    return None
                # Look up the trading pair using the index
                trading_pair = subscription_indices.get(index)
                if not trading_pair:
                    # Log at debug level since this is a common scenario when unsubscribing
                    # (messages may still arrive for recently unsubscribed indices)
                    self.logger().debug(f"No trading pair found for index {index}")
                    return None
                return trading_pair

            async def _setup_qtx_udp_subscriptions(self):
                """Setup UDP subscriptions for QTX market data with specialized handlers"""
                # Note: We don't need to reinitialize trading_pairs - use the ones from the parent connector
                # Start UDP listener
                await self.udp_manager.start_listening()
                # Register specialized callbacks with the UDP manager for different message types
                self.udp_manager.register_message_callback(1, self._handle_ticker_message)  # Ticker bid
                self.udp_manager.register_message_callback(-1, self._handle_ticker_message)  # Ticker ask
                self.udp_manager.register_message_callback(2, self._handle_depth_message)  # Depth
                self.udp_manager.register_message_callback(3, self._handle_trade_message)  # Trade buy
                self.udp_manager.register_message_callback(-3, self._handle_trade_message)  # Trade sell
                # Initialize the subscription indices
                self._subscription_indices = {}
                # Get exchange name from EXCHANGE_CONNECTOR_CLASSES
                exchange_info = EXCHANGE_CONNECTOR_CLASSES.get(self._exchange_backend.lower(), {})
                exchange_name_on_qtx = exchange_info.get("exchange_name_on_qtx")
                if exchange_name_on_qtx is None:
                    self.logger().error(
                        f"CRITICAL ERROR: No exchange_name_on_qtx configured for {self._exchange_backend}! "
                        f"Please add an 'exchange_name_on_qtx' entry in EXCHANGE_CONNECTOR_CLASSES for this exchange."
                    )
                    raise ValueError(f"Missing exchange_name_on_qtx configuration for {self._exchange_backend}")
                # Convert trading pairs to QTX format and subscribe
                for trading_pair in self.trading_pairs:
                    try:
                        # Convert to QTX format with dynamic exchange name
                        qtx_symbol = trading_pair_utils.convert_to_qtx_trading_pair(trading_pair, exchange_name_on_qtx)
                        # Track subscriptions
                        self._udp_subscriptions.add(qtx_symbol)
                        # Subscribe through the trading_pairs method
                        success, subscribed_pairs = await self.udp_manager.subscribe_to_trading_pairs([trading_pair])
                        # After subscription, get updated subscription indices
                        self._subscription_indices = self.udp_manager.subscription_indices
                        self.logger().info(
                            f"Subscribed to QTX UDP market data for {qtx_symbol} (mapped to {trading_pair})"
                        )
                        # Debug log the subscription indices
                        self.logger().debug(f"Current subscription indices: {self._subscription_indices}")
                    except Exception as e:
                        self.logger().error(f"Error subscribing to QTX UDP for {trading_pair}: {e}")

            async def _listen_for_qtx_order_book_diffs(self, output: asyncio.Queue):
                """Listen for orderbook diffs from QTX UDP"""
                messages_processed = 0
                while True:
                    try:
                        message = await self._message_queue["diffs"].get()
                        messages_processed += 1
                        # Periodic logging to verify message flow
                        if messages_processed % 100 == 0:
                            self.logger().debug(
                                f"Processed {messages_processed} diff messages, "
                                f"update_id={message.update_id}, trading_pair={message.trading_pair}"
                            )
                        output.put_nowait(message)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.logger().error(f"Error listening for QTX orderbook diffs: {e}")
                        await asyncio.sleep(1.0)

            async def _listen_for_qtx_order_book_snapshots(self, output: asyncio.Queue):
                """Listen for orderbook snapshots from QTX UDP"""
                while True:
                    try:
                        message = await self._message_queue["snapshots"].get()
                        output.put_nowait(message)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.logger().error(f"Error listening for QTX orderbook snapshots: {e}")
                        await asyncio.sleep(1.0)

            async def _listen_for_qtx_trades(self, output: asyncio.Queue):
                """Listen for trades from QTX UDP"""
                while True:
                    try:
                        message = await self._message_queue["trades"].get()
                        output.put_nowait(message)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.logger().error(f"Error listening for QTX trades: {e}")
                        await asyncio.sleep(1.0)

            async def _get_qtx_order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
                """Get order book snapshot by collecting market data through UDP"""
                self.logger().info(f"Getting order book snapshot for {trading_pair} via QTX UDP feed")
                try:
                    # Use the UDP manager to collect fresh market data
                    market_data = await self.udp_manager.collect_market_data(trading_pair, duration=5.0)
                    timestamp = market_data.get("timestamp", self.current_timestamp)
                    update_id = market_data.get("update_id", int(timestamp * 1000))
                    snapshot_msg = OrderBookMessage(
                        message_type=OrderBookMessageType.SNAPSHOT,
                        content={
                            "trading_pair": trading_pair,
                            "update_id": update_id,
                            "bids": market_data.get("bids", []),
                            "asks": market_data.get("asks", []),
                        },
                        timestamp=timestamp,
                    )
                    # Log snapshot information
                    bids_count = len(market_data.get("bids", []))
                    asks_count = len(market_data.get("asks", []))
                    self.logger().info(
                        f"Created order book snapshot for {trading_pair}: {bids_count} bids, {asks_count} asks"
                    )
                    # Mark this order book as no longer empty
                    self._empty_orderbook[trading_pair] = False
                    return snapshot_msg
                except Exception as e:
                    self.logger().error(f"Error getting order book snapshot for {trading_pair}: {e}", exc_info=True)
                    # Return empty snapshot as fallback
                    return OrderBookMessage(
                        message_type=OrderBookMessageType.SNAPSHOT,
                        content={
                            "trading_pair": trading_pair,
                            "update_id": int(self.current_timestamp * 1000),
                            "bids": [],
                            "asks": [],
                        },
                        timestamp=self.current_timestamp,
                    )

            async def start_network(self):
                """Override to manage QTX-specific network operations."""
                await super().start_network()
                # Initialize UDP manager connections
                if hasattr(self, "_order_book_tracker") and self._order_book_tracker is not None:
                    data_source = self._order_book_tracker.data_source
                    if hasattr(data_source, "listen_for_subscriptions"):
                        # This will trigger the QTX subscription setup
                        await data_source.listen_for_subscriptions()

            async def stop_network(self):
                """Stop network connections and clean up resources."""
                # Stop parent network components first
                try:
                    await super().stop_network()
                except Exception as e:
                    self.logger().error(f"Error in parent stop_network: {e}", exc_info=True)
                # Stop UDP manager if it exists
                if self.udp_manager is not None:
                    try:
                        # Stop the listening task first
                        if (
                            hasattr(self.udp_manager, "_listening_task")
                            and self.udp_manager._listening_task is not None
                        ):
                            self.logger().info("Stopping UDP listener")
                            await self.udp_manager.stop_listening()
                        # No close() method - stop_listening() handles socket cleanup
                        self.logger().info("UDP manager stopped successfully")
                    except Exception as e:
                        self.logger().error(f"Error stopping UDP manager: {e}", exc_info=True)
                    finally:
                        self._udp_manager = None
                # Clean up shared memory manager
                if self.shm_manager is not None:
                    try:
                        # Use disconnect() method, not cleanup()
                        await self.shm_manager.disconnect()
                        self.logger().info("Shared memory manager disconnected successfully")
                    except Exception as e:
                        self.logger().error(f"Error disconnecting shared memory manager: {e}", exc_info=True)
                    finally:
                        self._shm_manager = None

        # Prepare initialization parameters for the base exchange class
        init_params = {
            "client_config_map": client_config_map,
            "trading_pairs": trading_pairs,
            "trading_required": trading_required,
            "qtx_perpetual_host": qtx_perpetual_host,
            "qtx_perpetual_port": qtx_perpetual_port,
            "qtx_place_order_shared_memory_name": qtx_place_order_shared_memory_name,
            "exchange_backend": exchange_backend,
            "exchange_api_key": exchange_api_key,
            "exchange_api_secret": exchange_api_secret,
        }
        # Add exchange-specific parameters
        if exchange_backend.lower() == "binance":
            # Only pass API keys if they are not None
            if exchange_api_key is not None:
                init_params["binance_perpetual_api_key"] = exchange_api_key
            if exchange_api_secret is not None:
                init_params["binance_perpetual_api_secret"] = exchange_api_secret
        # Add other exchanges here as needed
        # Create and return an instance of the dynamic class
        return QtxDynamicConnector(**init_params)
