"""
QTX Perpetual Connector: Dynamic Runtime Inheritance Architecture

Combines QTX's market data feed with any supported exchange's trading API.
Uses runtime class generation to inherit from user-specified parent exchange
while overriding specific methods for QTX integration.
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
    Factory class for dynamic exchange inheritance.

    __new__ returns a runtime-generated class that inherits from the specified
    parent exchange, enabling QTX market data with any exchange's trading logic.
    """

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
        """Creates dynamic connector with specified exchange backend"""
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

        # Runtime class generation: Inherit from selected exchange + QTX overrides
        class QtxDynamicConnector(base_exchange_class):
            """
            Hybrid connector: Parent exchange API + QTX market data + Smart features.

            Architecture: Inherits everything from parent exchange except:
            - Market data (overridden for QTX UDP feed)
            - Order placement (overridden for market order conversion + SHM)
            - Position detection (overridden for intelligent action detection)
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
                """Returns UDP manager instance, creating if needed"""
                if self._udp_manager is None:
                    raise RuntimeError("UDP manager not initialized. Call _init_udp_manager() first.")
                return self._udp_manager

            async def _init_udp_manager(self):
                """Initialize and configure the UDP manager asynchronously"""
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

                    # Get singleton instance and configure it
                    self._udp_manager = await QtxPerpetualUDPManager.get_instance()
                    self._udp_manager.configure(
                        host=self._qtx_perpetual_host, port=self._qtx_perpetual_port, exchange_name=exchange_name_on_qtx
                    )

            @property
            def shm_manager(self) -> Optional[QtxPerpetualSharedMemoryManager]:
                """Returns SHM manager instance, creating if needed"""
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
                    self._shm_manager = QtxPerpetualSharedMemoryManager(
                        api_key=self._exchange_api_key,
                        api_secret=self._exchange_api_secret,
                        shm_name=self._qtx_shared_memory_name,
                        exchange_name_on_qtx=exchange_name_on_qtx,
                    )
                    self.logger().debug(
                        f"Shared memory manager initialized with segment '{self._qtx_shared_memory_name}' "
                        f"for exchange '{exchange_name_on_qtx}'"
                    )
                return self._shm_manager

            def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
                """Creates order book data source with QTX market data"""
                # Create the parent exchange's order book data source
                parent_data_source = super()._create_order_book_data_source()
                # Initialize UDP manager for QTX market data
                self._setup_qtx_market_data(parent_data_source)

                async def qtx_listen_for_subscriptions():
                    """Use QTX UDP for market data subscriptions"""
                    await self._init_udp_manager()
                    await self._setup_qtx_udp_subscriptions()

                async def qtx_listen_for_order_book_diffs(ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
                    """Processes order book diff messages directly from UDP queues"""
                    await self._consume_diff_messages(output)

                async def qtx_listen_for_order_book_snapshots(
                    ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue
                ):
                    """Handles order book snapshots using build_orderbook_snapshot"""
                    await self._handle_orderbook_snapshots(output)

                async def qtx_listen_for_trades(ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
                    """Processes trade messages directly from UDP queues"""
                    await self._consume_trade_messages(output)

                async def qtx_order_book_snapshot(trading_pair: str) -> OrderBookMessage:
                    """Builds and returns order book snapshot for trading pair"""
                    return await self._build_qtx_orderbook_snapshot(trading_pair)

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
                Intelligent order placement with market order conversion and position detection.

                Features:
                1. Auto-converts MARKET orders to aggressive LIMIT orders (±10% pricing)
                2. Auto-detects position actions when not explicitly provided
                3. Routes through QTX shared memory for high-performance execution
                """
                if self.shm_manager is not None:
                    # Use QTX shared memory for order placement
                    try:
                        # Convert to exchange format for placing order
                        # First check if symbols mapping is ready
                        if not super().trading_pair_symbol_map_ready():
                            self.logger().warning(
                                f"Symbol mapping not ready yet. Will retry order placement for {trading_pair}"
                            )
                            raise Exception("Trading pair symbol map not ready")

                        # Use the parent exchange's method to convert from Hummingbot to exchange format
                        exchange_pair = await super().exchange_symbol_associated_to_pair(trading_pair)
                        self.logger().debug(
                            f"Order placement: Converting {trading_pair} to exchange format: {exchange_pair}"
                        )

                        # Smart position action detection for ONE-WAY mode
                        # If position_action is not explicitly provided, determine it intelligently
                        final_position_action = self._determine_smart_position_action(
                            trading_pair, trade_type, position_action
                        )

                        self.logger().debug(
                            f"Position action: {position_action.name} → {final_position_action.name} "
                            f"(auto-detected for ONE-WAY mode)"
                        )

                        # Handle market order pricing - QTX doesn't support market orders
                        final_price = price
                        final_order_type = order_type

                        if order_type == OrderType.MARKET:
                            # Convert MARKET order to aggressive LIMIT order
                            try:
                                # Get current market price from order book
                                current_price = await self._get_current_market_price(trading_pair, trade_type)
                                if current_price is None:
                                    raise Exception(f"Unable to get current market price for {trading_pair}")

                                # Calculate aggressive price (±3% from current market price)
                                price_adjustment = Decimal("0.03")  # 3%
                                if trade_type == TradeType.BUY:
                                    # BUY: Use higher price to ensure execution at best ask
                                    aggressive_price = current_price * (Decimal("1") + price_adjustment)
                                else:
                                    # SELL: Use lower price to ensure execution at best bid
                                    aggressive_price = current_price * (Decimal("1") - price_adjustment)

                                # CRITICAL: Quantize price to match exchange precision rules
                                final_price = self.quantize_order_price(trading_pair, aggressive_price)

                                # Convert to GTC LIMIT order
                                final_order_type = OrderType.LIMIT

                                self.logger().info(
                                    f"Converting MARKET {trade_type.name} order to LIMIT: "
                                    f"current_price={current_price}, aggressive_price={aggressive_price} → "
                                    f"quantized_price={final_price} (±{price_adjustment*100}%) for {trading_pair}"
                                )

                            except Exception as e:
                                self.logger().error(f"Error calculating market order price for {trading_pair}: {e}")
                                raise Exception(f"Failed to convert MARKET order to LIMIT: {e}")

                        # Place the order using shared memory - SHM manager handles parameter translation
                        success, result = await self.shm_manager.place_order(
                            client_order_id=order_id,
                            symbol=exchange_pair,
                            trade_type=trade_type,
                            position_action=final_position_action,  # Use smart-detected position action
                            order_type=final_order_type,
                            price=final_price,
                            size=amount,
                            price_match=0,  # NONE for now
                        )

                        if not success:
                            error_msg = result.get("error", "Unknown error placing order via shared memory")
                            self.logger().error(f"Failed to place order via QTX shared memory: {error_msg}")
                            raise Exception(error_msg)

                        # Get processed results from SHM manager
                        exchange_order_id = result.get("exchange_order_id", order_id)
                        transaction_time = result.get("transaction_time", self.current_timestamp)

                        self.logger().debug(
                            f"Order placed via shared memory - Client ID: {order_id}, Exchange ID: {exchange_order_id}"
                        )

                        return exchange_order_id, transaction_time
                    except Exception as e:
                        self.logger().error(f"Error placing order via QTX shared memory: {e}", exc_info=True)
                        raise
                else:
                    raise Exception("QTX shared memory manager is not initialized. Order placement failed.")

            async def _get_current_market_price(self, trading_pair: str, trade_type: TradeType) -> Optional[Decimal]:
                """
                Get current market price from order book for market order conversion.

                :param trading_pair: The trading pair in Hummingbot format
                :param trade_type: BUY or SELL to determine bid vs ask price
                :return: Current market price or None if not available
                """
                try:
                    # Use the parent exchange's get_price method
                    # For BUY orders, we want the ask price (is_buy=True)
                    # For SELL orders, we want the bid price (is_buy=False)
                    is_buy = trade_type == TradeType.BUY
                    current_price = self.get_price(trading_pair, is_buy)

                    if current_price is None or current_price.is_nan():
                        self.logger().warning(f"Unable to get current market price for {trading_pair}")
                        return None

                    self.logger().debug(
                        f"Retrieved current market price for {trading_pair}: {current_price} "
                        f"({'ask' if is_buy else 'bid'} price for {trade_type.name} order)"
                    )
                    return current_price

                except Exception as e:
                    self.logger().error(f"Error getting current market price for {trading_pair}: {e}")
                    return None

            def _determine_smart_position_action(
                self, trading_pair: str, trade_type: TradeType, provided_action: PositionAction
            ) -> PositionAction:
                """
                Smart position action detection for user-friendly trading.

                Problem: Most users don't specify position_action, but we still need to
                determine the intent for proper order tracking and reporting.

                Solution: Inspect current positions and auto-detect user intent:
                - Explicit action provided → Respect user intent
                - Opposing direction to existing position → Auto-detect CLOSE
                - Same direction or no position → Auto-detect OPEN

                Note: In ONE-WAY mode, position_side is always BOTH (0), but the
                position_action still helps with order tracking and user feedback.
                """
                # If user explicitly provided a non-NIL action, respect their intent
                if provided_action != PositionAction.NIL:
                    self.logger().debug(
                        f"Using user-provided position action: {provided_action.name} for {trade_type.name}"
                    )
                    return provided_action

                try:
                    # Get current positions for this trading pair
                    current_positions = self.account_positions
                    position_key = trading_pair  # Position key might vary by exchange

                    self.logger().debug(
                        f"Checking current positions for {trading_pair}: {len(current_positions)} total positions"
                    )

                    # Look for existing position for this trading pair
                    existing_position = None
                    for key, position in current_positions.items():
                        if position.trading_pair == trading_pair:
                            existing_position = position
                            self.logger().debug(
                                f"Found existing position: {position.trading_pair}, "
                                f"amount={position.amount}, side={position.position_side.name if hasattr(position, 'position_side') else 'unknown'}"
                            )
                            break

                    if existing_position is None or existing_position.amount == 0:
                        # No existing position - use OPEN for any new order
                        self.logger().debug(
                            f"No existing position found for {trading_pair} → Using OPEN for {trade_type.name}"
                        )
                        return PositionAction.OPEN

                    # Existing position logic for ONE-WAY mode
                    if existing_position.amount > 0:
                        # Current LONG position
                        if trade_type == TradeType.SELL:
                            # SELL order with LONG position → CLOSE the long
                            self.logger().info(
                                f"Auto-detected: LONG position exists, SELL order → Converting to CLOSE "
                                f"(closing {existing_position.amount} LONG position)"
                            )
                            return PositionAction.CLOSE
                        else:
                            # BUY order with LONG position → Add to position (OPEN)
                            self.logger().debug(f"LONG position exists, BUY order → Using OPEN (add to position)")
                            return PositionAction.OPEN

                    elif existing_position.amount < 0:
                        # Current SHORT position
                        if trade_type == TradeType.BUY:
                            # BUY order with SHORT position → CLOSE the short
                            self.logger().info(
                                f"Auto-detected: SHORT position exists, BUY order → Converting to CLOSE "
                                f"(closing {abs(existing_position.amount)} SHORT position)"
                            )
                            return PositionAction.CLOSE
                        else:
                            # SELL order with SHORT position → Add to position (OPEN)
                            self.logger().debug(f"SHORT position exists, SELL order → Using OPEN (add to position)")
                            return PositionAction.OPEN

                    # Fallback: default to OPEN
                    self.logger().debug(
                        f"Fallback: Using OPEN for {trade_type.name} (position amount: {existing_position.amount})"
                    )
                    return PositionAction.OPEN

                except Exception as e:
                    self.logger().warning(f"Error determining smart position action for {trading_pair}: {e}")
                    self.logger().debug(f"Fallback: Using OPEN for {trade_type.name} due to error")
                    return PositionAction.OPEN

            async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
                """Cancels order using QTX shared memory"""
                if self.shm_manager is not None:
                    # Use QTX shared memory for order cancellation
                    try:
                        # Use the parent exchange's method to convert from Hummingbot to exchange format
                        exchange_pair = await super().exchange_symbol_associated_to_pair(tracked_order.trading_pair)
                        self.logger().debug(
                            f"Order cancellation: Converting {tracked_order.trading_pair} to exchange format: {exchange_pair}"
                        )
                        success, response_data = await self.shm_manager.cancel_order(
                            client_order_id=order_id,
                            symbol=exchange_pair,
                        )
                        if success:
                            self.logger().debug(f"Order cancelled successfully via QTX shared memory: {order_id}")
                            return True
                        else:
                            error_msg = response_data.get("error", "Unknown error")
                            if self.shm_manager._is_order_not_found_error(error_msg):
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
                """Sets up QTX market data integration with direct queue access"""
                # Store the parent data source
                self._parent_data_source = parent_data_source
                # Store direct queue references from UDP manager
                self._udp_queues = {}  # trading_pair -> {"diff": Queue, "trade": Queue}
                # Keep track of UDP subscriptions
                self._udp_subscriptions = set()

            async def _setup_qtx_udp_subscriptions(self):
                """Sets up UDP subscriptions with direct queue access"""
                # Start UDP listener
                await self.udp_manager.start()
                
                # Get exchange name from EXCHANGE_CONNECTOR_CLASSES
                exchange_info = EXCHANGE_CONNECTOR_CLASSES.get(self._exchange_backend.lower(), {})
                exchange_name_on_qtx = exchange_info.get("exchange_name_on_qtx")
                if exchange_name_on_qtx is None:
                    self.logger().error(
                        f"CRITICAL ERROR: No exchange_name_on_qtx configured for {self._exchange_backend}! "
                        f"Please add an 'exchange_name_on_qtx' entry in EXCHANGE_CONNECTOR_CLASSES for this exchange."
                    )
                    raise ValueError(f"Missing exchange_name_on_qtx configuration for {self._exchange_backend}")
                
                # Subscribe to trading pairs and get direct access to queues
                for trading_pair in self.trading_pairs:
                    try:
                        # Track subscriptions
                        qtx_symbol = trading_pair_utils.convert_to_qtx_trading_pair(trading_pair, exchange_name_on_qtx)
                        self._udp_subscriptions.add(qtx_symbol)
                        
                        # Subscribe and get queues directly (no snapshot queue)
                        queues = await self.udp_manager.subscribe_and_get_queues(trading_pair)
                        self._udp_queues[trading_pair] = queues
                        
                        self.logger().info(
                            f"Subscribed to QTX UDP market data for {qtx_symbol} (mapped to {trading_pair}) "
                            f"with direct queue access"
                        )
                    except Exception as e:
                        self.logger().error(f"Error subscribing to QTX UDP for {trading_pair}: {e}")

            async def _consume_diff_messages(self, output: asyncio.Queue):
                """Consume diff messages directly from UDP queues"""
                tasks = []
                
                for trading_pair in self.trading_pairs:
                    if trading_pair in self._udp_queues:
                        diff_queue = self._udp_queues[trading_pair]["diff"]
                        task = asyncio.create_task(
                            self._consume_messages_from_queue(
                                trading_pair, diff_queue, output, "diff"
                            )
                        )
                        tasks.append(task)
                
                # Wait for all consumption tasks
                try:
                    await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    for task in tasks:
                        task.cancel()
                    raise

            async def _consume_trade_messages(self, output: asyncio.Queue):
                """Consume trade messages directly from UDP queues"""
                tasks = []
                
                for trading_pair in self.trading_pairs:
                    if trading_pair in self._udp_queues:
                        trade_queue = self._udp_queues[trading_pair]["trade"]
                        task = asyncio.create_task(
                            self._consume_messages_from_queue(
                                trading_pair, trade_queue, output, "trade"
                            )
                        )
                        tasks.append(task)
                
                # Wait for all consumption tasks
                try:
                    await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    for task in tasks:
                        task.cancel()
                    raise

            async def _consume_messages_from_queue(
                self, 
                trading_pair: str,
                source_queue: asyncio.Queue, 
                output_queue: asyncio.Queue,
                message_type: str
            ):
                """Consume messages from UDP queue and forward to output queue"""
                messages_consumed = 0
                
                while True:
                    try:
                        # Get message from queue (blocks until available - event driven!)
                        message = await source_queue.get()
                        
                        # Forward all messages as-is (no special first message handling)
                        # Initial snapshots are handled by _build_qtx_orderbook_snapshot()
                        await output_queue.put(message)
                        
                        messages_consumed += 1
                        # Log progress occasionally
                        if messages_consumed % 10000 == 0:
                            self.logger().debug(
                                f"Consumed {messages_consumed} {message_type} messages for {trading_pair}"
                            )
                            
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.logger().error(f"Error consuming {message_type} messages for {trading_pair}: {e}")
                        await asyncio.sleep(1.0)

            async def _handle_orderbook_snapshots(self, output: asyncio.Queue):
                """
                Handle orderbook snapshots - No-op for QTX.
                
                QTX doesn't send snapshot messages. Initial snapshots are created only
                when explicitly requested via build_orderbook_snapshot(). Since QTX
                sends full orderbook state with each depth update, no periodic snapshots
                are needed.
                
                This method sleeps indefinitely to keep the async task alive.
                """
                try:
                    while True:
                        await asyncio.sleep(3600)  # Sleep for 1 hour
                except asyncio.CancelledError:
                    raise

            async def _build_qtx_orderbook_snapshot(self, trading_pair: str) -> OrderBookMessage:
                """
                Build order book snapshot when explicitly requested by Hummingbot core.
                
                This is the ONLY way snapshots are created for QTX. Since QTX doesn't send
                native snapshots, we collect depth messages over a short period to build
                a complete orderbook state. The continuous diff stream handles all ongoing
                orderbook updates without any special first-message processing.
                """
                self.logger().info(f"Building order book snapshot for {trading_pair} via QTX UDP feed")
                try:
                    # Ensure UDP manager is initialized
                    if self._udp_manager is None:
                        await self._init_udp_manager()

                    # Ensure subscription exists
                    if trading_pair not in self._udp_queues:
                        # Subscribe if not already subscribed
                        queues = await self.udp_manager.subscribe_and_get_queues(trading_pair)
                        self._udp_queues[trading_pair] = queues

                    # Build snapshot from collected messages
                    snapshot_data = await self.udp_manager.build_orderbook_snapshot(trading_pair)
                    
                    # Log snapshot information
                    bids_count = len(snapshot_data.get("bids", []))
                    asks_count = len(snapshot_data.get("asks", []))
                    self.logger().info(
                        f"Built order book snapshot for {trading_pair}: {bids_count} bids, {asks_count} asks"
                    )
                    
                    # Create and return OrderBookMessage
                    return OrderBookMessage(
                        message_type=OrderBookMessageType.SNAPSHOT,
                        content=snapshot_data,
                        timestamp=snapshot_data["timestamp"]
                    )
                        
                except Exception as e:
                    self.logger().error(f"Error building order book snapshot for {trading_pair}: {e}", exc_info=True)
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
                """Initializes QTX UDP connection and subscriptions"""
                await super().start_network()
                # Initialize UDP manager connections
                if hasattr(self, "_order_book_tracker") and self._order_book_tracker is not None:
                    data_source = self._order_book_tracker.data_source
                    if hasattr(data_source, "listen_for_subscriptions"):
                        # This will trigger the QTX subscription setup
                        await data_source.listen_for_subscriptions()

            async def stop_network(self):
                """Stops UDP and shared memory connections"""
                # First handle QTX-specific cleanup
                # Unsubscribe from all trading pairs first
                try:
                    if self._udp_manager is not None and self._udp_manager.is_connected:
                        # Clear queue references
                        self._udp_queues.clear()
                        
                        # Now unsubscribe
                        trading_pairs = list(self._udp_manager.subscribed_pairs)
                        if trading_pairs:
                            self.logger().info(f"Unsubscribing from {len(trading_pairs)} trading pairs")
                            try:
                                await self._udp_manager.unsubscribe_from_trading_pairs(trading_pairs)
                            except Exception as e:
                                self.logger().error(f"Error unsubscribing from trading pairs: {e}", exc_info=True)
                except Exception as e:
                    self.logger().error(f"Error during unsubscription process: {e}", exc_info=True)

                # Stop UDP manager if it exists
                if self._udp_manager is not None:
                    try:
                        # Stop the UDP manager - this will also close the socket
                        self.logger().debug("Stopping UDP listener")
                        await self._udp_manager.stop()
                        self.logger().debug("UDP manager stopped successfully")
                    except Exception as e:
                        self.logger().error(f"Error stopping UDP manager: {e}", exc_info=True)
                    finally:
                        self._udp_manager = None

                # Clean up shared memory manager
                if self.shm_manager is not None:
                    try:
                        # Use disconnect() method, not cleanup()
                        await self.shm_manager.disconnect()
                        self.logger().debug("Shared memory manager disconnected successfully")
                    except Exception as e:
                        self.logger().error(f"Error disconnecting shared memory manager: {e}", exc_info=True)
                    finally:
                        self._shm_manager = None

                # Finally stop parent network components after QTX resources are cleaned up
                try:
                    await super().stop_network()
                except Exception as e:
                    self.logger().error(f"Error in parent stop_network: {e}", exc_info=True)

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