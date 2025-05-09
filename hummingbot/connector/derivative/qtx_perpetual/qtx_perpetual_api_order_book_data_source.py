#!/usr/bin/env python

import asyncio
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_constants as BINANCE_CONSTANTS
from hummingbot.connector.derivative.qtx_perpetual import (
    qtx_perpetual_constants as CONSTANTS,
    qtx_perpetual_udp_manager as udp_manager,
)
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.funding_info import FundingInfo, FundingInfoUpdate
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.perpetual_api_order_book_data_source import PerpetualAPIOrderBookDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

# ---------------------------------------- Order Book Data Source Class ----------------------------------------


class QtxPerpetualAPIOrderBookDataSource(PerpetualAPIOrderBookDataSource):
    """
    QTX Perpetual API order book data source that connects to QTX's UDP market data feed.

    This class is responsible for:
    1. Setting up and managing the UDP connection via the centralized UDP manager
    2. Converting market data into Hummingbot's standard OrderBookMessage format
    3. Providing funding info (delegated to Binance) for consistent futures trading

    This implementation removes direct UDP socket handling, delegating to QtxPerpetualUDPManager.
    """

    MESSAGE_TIMEOUT = 30.0
    SNAPSHOT_TIMEOUT = 10.0

    _qtxpos_api_source_logger: Optional[HummingbotLogger] = None

    def __init__(
        self,
        trading_pairs: List[str],
        connector,
        api_factory: Optional[WebAssistantsFactory] = None,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,  # Accept domain for compatibility but don't use it
        host: str = None,  # Accept host for compatibility
        port: int = None,  # Accept port for compatibility
        udp_manager_instance: Optional[udp_manager.QtxPerpetualUDPManager] = None,  # Accept existing UDP manager
    ):
        super().__init__(trading_pairs)
        self._connector = connector
        # Store api_factory for Binance funding info calls only
        self._web_assistants_factory = api_factory

        # Initialize message queues for the different types of messages
        self._message_queue = {
            self._snapshot_messages_queue_key: asyncio.Queue(),
            self._diff_messages_queue_key: asyncio.Queue(),
            self._trade_messages_queue_key: asyncio.Queue(),
            self._funding_info_messages_queue_key: asyncio.Queue(),
        }

        # UDP connection settings - get from connector or use provided values
        self._host = host if host is not None else connector._qtx_perpetual_host
        self._port = port if port is not None else connector._qtx_perpetual_port

        # Use provided UDP manager or create a new one if not provided
        if udp_manager_instance is not None:
            self.logger().info("Using provided UDP manager instance")
            self._udp_manager = udp_manager_instance
        elif hasattr(connector, "udp_manager") and connector.udp_manager is not None:
            self.logger().info("Using connector's UDP manager instance via property getter")
            self._udp_manager = connector.udp_manager
        else:
            # Create a new UDP manager instance
            self.logger().info("Creating new UDP manager instance (fallback)")
            self._udp_manager = udp_manager.QtxPerpetualUDPManager(host=self._host, port=self._port)

        # Register callbacks for different message types
        self._register_callbacks()

        # Data tracking
        self._empty_orderbook = {}
        self._funding_info_listener_task = None

        # Cache for exchange symbols to trading pairs mapping
        self._exchange_trading_pairs = {}

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._qtxpos_api_source_logger is None:
            cls._qtxpos_api_source_logger = HummingbotLogger(__name__)
        return cls._qtxpos_api_source_logger

    def _register_callbacks(self):
        """Register callback functions for UDP manager message types"""
        # Ticker callbacks (bid = 1, ask = -1)
        self._udp_manager.register_message_callback(1, self._handle_ticker_message)
        self._udp_manager.register_message_callback(-1, self._handle_ticker_message)

        # Depth (orderbook) callback
        self._udp_manager.register_message_callback(2, self._handle_depth_message)

        # Trade callbacks (buy = 3, sell = -3)
        self._udp_manager.register_message_callback(3, self._handle_trade_message)
        self._udp_manager.register_message_callback(-3, self._handle_trade_message)

    # ---------------------------------------- Lifecycle Management ----------------------------------------

    async def start(self):
        """
        Starts the data source by:
        1. Setting up the UDP socket for market data
        2. Starting the funding info listener for Binance data
        """
        # Log start()
        self.logger().info("Calling start() in the api order book data source")

        # Start UDP socket listener - this sets up UDP connection and subscribes to trading pairs
        await self.listen_for_subscriptions()

        # Mark all orderbooks as empty initially
        # They will be populated by real market data from the UDP feed
        for trading_pair in self._trading_pairs:
            self._empty_orderbook[trading_pair] = True

        # Register callbacks for message handling
        self._register_callbacks()

        # Start funding info listener
        if self._funding_info_listener_task is None:
            self._funding_info_listener_task = asyncio.create_task(self._listen_for_funding_info())
            self.logger().info("Funding info listener started")

    async def stop(self):
        """
        Stops the data source and unsubscribes from all symbols
        """
        # Stop the UDP manager if we created it ourselves
        if self._udp_manager is not None:
            if not hasattr(self._connector, "_udp_manager") or self._connector._udp_manager is not self._udp_manager:
                self.logger().info("Stopping our own UDP manager instance")
                await self._udp_manager.stop_listening()
            else:
                self.logger().info("Skipping stop of connector's UDP manager instance")

        # Stop funding info listener
        if hasattr(self, "_funding_info_listener_task") and self._funding_info_listener_task is not None:
            self._funding_info_listener_task.cancel()
            try:
                await self._funding_info_listener_task
            except asyncio.CancelledError:
                self.logger().info("Funding info listener task cancelled during stop")
            self._funding_info_listener_task = None

        self.logger().info("QtxPerpetualAPIOrderBookDataSource stopped")

    # ---------------------------------------- End of Lifecycle Management ----------------------------------------

    # ---------------------------------------- Subscription Management ----------------------------------------

    async def listen_for_subscriptions(self):
        """
        Custom implementation using UDP for order book data instead of WebSockets.
        Uses the UDP manager to connect to QTX UDP feed, subscribe to trading pairs,
        and start the message listening process.
        """
        # UDP connection is centralized in the UDP manager
        self.logger().info(f"Setting up UDP connection to {self._host}:{self._port}")

        # First, determine if we need to stop an existing connection
        if hasattr(self, "_udp_manager") and self._udp_manager is not None:
            if self._udp_manager._is_connected:
                # We already have an active connection - check if we need to unsubscribe
                if self._udp_manager._subscribed_pairs:
                    self.logger().info("Connection already exists - unsubscribing from existing pairs")
                    await self._udp_manager.unsubscribe_from_all()

                # Always ensure the listening task is stopped before reconnecting
                if self._udp_manager._listening_task is not None and not self._udp_manager._listening_task.done():
                    self.logger().info("Stopping existing listener task")
                    await self._udp_manager.stop_listening()

        # Connect to UDP server - UDP manager handles retries internally
        try:
            success = await self._udp_manager.connect()
            if not success:
                self.logger().error("Failed to connect to UDP server")
                return

            self.logger().info("Successfully connected to UDP server")
        except Exception as e:
            self.logger().error(f"Error connecting to UDP server: {e}", exc_info=True)
            return

        # Mark all orderbooks as empty - will be populated by market data
        for trading_pair in self._trading_pairs:
            self._empty_orderbook[trading_pair] = True

        # Subscribe to trading pairs - UDP manager handles the conversion to exchange format
        success, subscribed_pairs = await self._udp_manager.subscribe_to_trading_pairs(self._trading_pairs)
        if not success:
            self.logger().error("Failed to subscribe to trading pairs")
            return

        self.logger().info(f"Successfully subscribed to {len(subscribed_pairs)} trading pairs")

        # Start listening for messages
        await self._udp_manager.start_listening()

        # Log confirmation of setup
        self.logger().info("UDP feed connection and subscriptions successfully established")

    # ---------------------------------------- End of Subscription Management ----------------------------------------

    # ---------------------------------------- Market Data Access ----------------------------------------

    async def get_last_traded_prices(self, trading_pairs: List[str]) -> Dict[str, float]:
        """
        Gets the last traded price for each trading pair from QTX data
        :param trading_pairs: List of trading pairs
        :return: Dictionary mapping each trading pair to its last traded price
        """
        result = {}
        for trading_pair in trading_pairs:
            # Use the connector's get_last_traded_price method which checks orderbook
            result[trading_pair] = await self._connector.get_last_traded_price(trading_pair)
        return result

    # ---------------------------------------- End of Market Data Access ----------------------------------------

    # ---------------------------------------- Message Handling ----------------------------------------

    async def _handle_ticker_message(self, message: Dict[str, Any]):
        """
        Handle ticker messages from UDP manager and convert to order book diff message

        :param message: Parsed ticker message
        """
        if not message:
            return

        trading_pair = message.get("trading_pair")
        if not trading_pair:
            return

        # Convert to diff message and queue
        diff_msg = OrderBookMessage(
            message_type=OrderBookMessageType.DIFF,
            content={
                "trading_pair": trading_pair,
                "update_id": message["update_id"],
                "bids": message["bids"],
                "asks": message["asks"],
            },
            timestamp=message["timestamp"],
        )
        self._message_queue[self._diff_messages_queue_key].put_nowait(diff_msg)

    async def _handle_depth_message(self, message: Dict[str, Any]):
        """
        Handle depth messages from UDP manager and convert to order book snapshot or diff message

        :param message: Parsed depth message
        """
        # Generate a unique ID for this message instance for tracking
        msg_id = f"depth_{int(time.time() * 1000)}"

        if not message:
            self.logger().warning(f"[{msg_id}] Received empty depth message")
            return

        trading_pair = message.get("trading_pair")
        if not trading_pair:
            self.logger().warning(
                f"[{msg_id}] Depth message missing trading_pair field. Message keys: {list(message.keys())}"
            )
            return

        # Get message details
        bids_count = len(message.get("bids", []))
        asks_count = len(message.get("asks", []))
        update_id = message.get("update_id", "unknown")
        timestamp = message.get("timestamp", time.time())
        message_type = message.get("message_type", "unknown")
        is_empty = trading_pair in self._empty_orderbook and self._empty_orderbook[trading_pair]

        # More detailed logging for all messages during debugging
        self.logger().info(
            f"[{msg_id}] Processing depth message for {trading_pair}: "
            f"type={message_type}, update_id={update_id}, bids={bids_count}, asks={asks_count}, "
            f"is_empty_orderbook={is_empty}, timestamp={timestamp}"
        )

        # Log sample of bid/ask data to debug data quality
        if bids_count > 0 or asks_count > 0:
            # Sample of up to 3 bids/asks for logging
            sample_bids = message.get("bids", [])[: min(3, bids_count)]
            sample_asks = message.get("asks", [])[: min(3, asks_count)]

            self.logger().debug(
                f"[{msg_id}] Sample data for {trading_pair}:\n"
                f"  Top bids (price, size): {sample_bids}\n"
                f"  Top asks (price, size): {sample_asks}"
            )
        else:
            self.logger().warning(f"[{msg_id}] No bids or asks in depth message for {trading_pair}")

        # Determine if this should be a snapshot or diff
        # Enhanced logic to ensure we create proper snapshots
        should_create_snapshot = False
        snapshot_reason = "none"

        # Case 1: First message with data for an empty orderbook
        if is_empty and (bids_count > 0 or asks_count > 0):
            # The very first message with any data should be treated as a snapshot
            should_create_snapshot = True
            message["message_type"] = "snapshot"
            snapshot_reason = "first_data_for_empty_book"
            self.logger().info(f"[{msg_id}] Converting first non-empty depth message to snapshot for {trading_pair}")

        # Case 2: Message explicitly marked as snapshot
        elif message_type == "snapshot":
            should_create_snapshot = True
            snapshot_reason = "explicit_snapshot_flag"
            self.logger().info(f"[{msg_id}] Processing explicit snapshot message for {trading_pair}")

        # Case 3: Substantial data and book still marked as empty
        elif bids_count >= 5 and asks_count >= 5 and is_empty:
            # If we have substantial data and empty book, use as snapshot
            # Reduced threshold to 5 from 10 to be more aggressive in creating snapshots
            should_create_snapshot = True
            message["message_type"] = "snapshot"
            snapshot_reason = "substantial_data_empty_book"
            self.logger().info(
                f"[{msg_id}] Using depth message with {bids_count} bids, {asks_count} asks as snapshot for {trading_pair}"
            )

        # Case 4: Book has no asks or bids, but we have good data - create a snapshot
        elif (bids_count >= 10 and asks_count >= 10) and hasattr(self._connector, "_order_book_tracker"):
            # Check if current order book is empty or nearly empty
            tracker = self._connector._order_book_tracker
            if trading_pair in tracker.order_books and (
                len(tracker.order_books[trading_pair].snapshot_bid_entries()) < 5
                or len(tracker.order_books[trading_pair].snapshot_ask_entries()) < 5
            ):

                # Create a snapshot if current order book is sparse
                should_create_snapshot = True
                message["message_type"] = "snapshot"
                snapshot_reason = "substantial_data_sparse_book"
                self.logger().info(
                    f"[{msg_id}] Creating snapshot from substantial depth message ({bids_count} bids, {asks_count} asks) "
                    f"for {trading_pair} with sparse order book"
                )

        # Track current order book tracker state to diagnose issues
        ob_tracker_state = "none"
        if hasattr(self._connector, "_order_book_tracker") and self._connector._order_book_tracker is not None:
            tracker = self._connector._order_book_tracker
            if trading_pair in tracker.order_books:
                bid_count = len(tracker.order_books[trading_pair].snapshot_bid_entries())
                ask_count = len(tracker.order_books[trading_pair].snapshot_ask_entries())
                ob_tracker_state = f"active_with_{bid_count}_bids_{ask_count}_asks"
            else:
                ob_tracker_state = "no_book_for_pair"
        else:
            ob_tracker_state = "no_tracker"

        self.logger().debug(f"[{msg_id}] OB tracker state: {ob_tracker_state}")

        try:
            if should_create_snapshot:
                # Process as a SNAPSHOT
                snapshot_msg = OrderBookMessage(
                    message_type=OrderBookMessageType.SNAPSHOT,
                    content={
                        "trading_pair": trading_pair,
                        "update_id": message["update_id"],
                        "bids": message["bids"],
                        "asks": message["asks"],
                    },
                    timestamp=message["timestamp"],
                )

                self.logger().info(
                    f"[{msg_id}] Created orderbook SNAPSHOT for {trading_pair}: "
                    f"bids={len(snapshot_msg.bids)}, asks={len(snapshot_msg.asks)}, "
                    f"update_id={snapshot_msg.update_id}, reason={snapshot_reason}"
                )

                # Queue the snapshot and track queue size for diagnostics
                queue = self._message_queue[self._snapshot_messages_queue_key]
                queue.put_nowait(snapshot_msg)
                queue_size = queue.qsize()
                self.logger().debug(f"[{msg_id}] Snapshot queue size after adding: {queue_size}")

                # Mark orderbook as no longer empty
                self._empty_orderbook[trading_pair] = False
            else:
                # Process as a DIFF
                diff_msg = OrderBookMessage(
                    message_type=OrderBookMessageType.DIFF,
                    content={
                        "trading_pair": trading_pair,
                        "update_id": message["update_id"],
                        "bids": message["bids"],
                        "asks": message["asks"],
                    },
                    timestamp=message["timestamp"],
                )

                # Log at info level for significant diffs, debug for small ones
                log_method = self.logger().info if (bids_count >= 5 or asks_count >= 5) else self.logger().debug
                log_method(
                    f"[{msg_id}] Processing orderbook DIFF for {trading_pair}: "
                    f"bids={len(diff_msg.bids)}, asks={len(diff_msg.asks)}, "
                    f"update_id={diff_msg.update_id}, current_ob_state={ob_tracker_state}"
                )

                # Queue the diff and track queue size for diagnostics
                queue = self._message_queue[self._diff_messages_queue_key]
                queue.put_nowait(diff_msg)
                queue_size = queue.qsize()
                self.logger().debug(f"[{msg_id}] Diff queue size after adding: {queue_size}")
        except Exception as e:
            self.logger().error(f"[{msg_id}] Error processing depth message: {e}", exc_info=True)

    async def _handle_trade_message(self, message: Dict[str, Any]):
        """
        Handle trade messages from UDP manager and convert to order book trade message

        :param message: Parsed trade message
        """
        if not message:
            return

        trading_pair = message.get("trading_pair")
        if not trading_pair:
            return

        # Convert to trade message and queue
        trade_type = TradeType.BUY if message["side"] == "BUY" else TradeType.SELL
        trade_msg = OrderBookMessage(
            message_type=OrderBookMessageType.TRADE,
            content={
                "trading_pair": trading_pair,
                "trade_type": trade_type,
                "trade_id": message["trade_id"],
                "update_id": int(message["timestamp"] * 1000),
                "price": message["price"],
                "amount": message["amount"],
            },
            timestamp=message["timestamp"],
        )
        self._message_queue[self._trade_messages_queue_key].put_nowait(trade_msg)

    # ---------------------------------------- End of Message Handling ----------------------------------------

    # ---------------------------------------- Queue Processing ----------------------------------------

    async def listen_for_order_book_diffs(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue) -> None:
        """
        Listen for orderbook diffs (updates) using UDP
        """
        while True:
            try:
                message = await self._message_queue[self._diff_messages_queue_key].get()
                output.put_nowait(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error listening for orderbook diffs: {e}", exc_info=True)
                await asyncio.sleep(1.0)

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue) -> None:
        """
        Listen for orderbook snapshots using UDP
        """
        while True:
            try:
                message = await self._message_queue[self._snapshot_messages_queue_key].get()
                output.put_nowait(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error listening for orderbook snapshots: {e}", exc_info=True)
                await asyncio.sleep(1.0)

    async def listen_for_trades(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue) -> None:
        """
        Listen for trades using UDP
        """
        while True:
            try:
                message = await self._message_queue[self._trade_messages_queue_key].get()
                output.put_nowait(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error listening for trades: {e}", exc_info=True)
                await asyncio.sleep(1.0)

    # ---------------------------------------- End of Queue Processing ----------------------------------------

    # ---------------------------------------- Order Book Management ----------------------------------------

    async def get_new_order_book(self, trading_pair: str) -> OrderBook:
        """
        Create a new order book for a trading pair with an empty initial snapshot
        """
        snapshot = await self.get_snapshot(trading_pair)
        snapshot_timestamp = time.time()
        snapshot_msg = OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            {
                "trading_pair": trading_pair,
                "update_id": int(snapshot_timestamp * 1000),
                "bids": snapshot["bids"],
                "asks": snapshot["asks"],
            },
            snapshot_timestamp,
        )
        order_book = self.order_book_create_function()
        order_book.apply_snapshot(snapshot_msg.bids, snapshot_msg.asks, snapshot_msg.update_id)
        return order_book

    async def get_snapshot(self, trading_pair: str) -> Dict:
        """
        Get an order book snapshot for a trading pair by directly collecting fresh market data.

        This method collects fresh market data for every call and does not use caching.
        It directly uses the UDP manager's collect_market_data() method to get the most
        up-to-date order book snapshot for the specified trading pair.

        :param trading_pair: Trading pair to get snapshot for (e.g. BTC-USDT)
        :return: Dictionary with bids and asks lists
        """
        req_id = f"snapshot_{int(time.time() * 1000)}"

        self.logger().info(
            f"[{req_id}] Getting fresh market data snapshot for {trading_pair} " f"with a 5-second collection window"
        )

        try:
            # Collect fresh market data using the UDP manager (with 5-second duration)
            collected_data = await self._udp_manager.collect_market_data(trading_pair, duration=5.0)

            if collected_data.get("bids") or collected_data.get("asks"):
                bids_count = len(collected_data.get("bids", []))
                asks_count = len(collected_data.get("asks", []))

                self.logger().info(
                    f"[{req_id}] Successfully collected fresh market data for {trading_pair}: "
                    f"{bids_count} bids, {asks_count} asks"
                )
                return collected_data
            else:
                self.logger().warning(f"[{req_id}] Collected market data for {trading_pair} is empty")
        except Exception as e:
            self.logger().error(f"[{req_id}] Error collecting market data for {trading_pair}: {e}", exc_info=True)

        # If the collection fails, return empty snapshot
        self.logger().warning(
            f"[{req_id}] No market data available for {trading_pair} from any source. "
            f"Returning empty snapshot which may cause issues with trading."
        )
        return {"bids": [], "asks": []}

    # ---------------------------------------- End of Order Book Management ----------------------------------------

    # ---------------------------------------- Funding Info ----------------------------------------

    async def get_funding_info(self, trading_pair: str) -> FundingInfo:
        """
        Get funding information for a trading pair by delegating to Binance

        :param trading_pair: Trading pair to get funding info for
        :return: FundingInfo object or raises an exception if Binance data is unavailable
        """
        # Check if Binance connector is available
        if self._connector.binance_connector is None:
            raise ValueError("Cannot get funding info: Binance connector not initialized")

        # Keep track of the original trading pair for consistent logging
        # Convert directly to Binance format (remove the hyphen)
        exchange_symbol = trading_pair.replace("-", "")

        # Log the conversion for debugging
        self.logger().info(f"Converting {trading_pair} to Binance format: {exchange_symbol} for funding info")

        try:
            # Try using the connector's get_funding_info if available
            if hasattr(self._connector.binance_connector, "get_funding_info"):
                return await self._connector.binance_connector.get_funding_info(trading_pair)
        except KeyError as e:
            self.logger().warning(
                f"Funding info not found for {trading_pair} in Binance connector, falling back to API request: {e}"
            )

        try:
            # Direct API call with exchange symbol (must be in uppercase for Binance)
            data = await self._connector.binance_connector._api_request(
                method=RESTMethod.GET,
                path_url=BINANCE_CONSTANTS.MARK_PRICE_URL,
                params={"symbol": exchange_symbol.upper()},
                is_auth_required=False,
            )

            self.logger().info(f"Successfully retrieved funding info via API for {trading_pair}")
            return FundingInfo(
                trading_pair=trading_pair,  # Use original trading pair for consistency
                index_price=Decimal(str(data.get("indexPrice", "0"))),
                mark_price=Decimal(str(data.get("markPrice", "0"))),
                next_funding_utc_timestamp=int(float(data.get("nextFundingTime", 0)) * 1e-3),
                rate=Decimal(str(data.get("lastFundingRate", "0"))),
            )
        except Exception as e:
            # We don't provide default values anymore - if Binance can't provide funding info,
            # the connector should report this error appropriately
            self.logger().error(f"Error getting funding info from Binance for {trading_pair}: {e}", exc_info=True)
            raise ValueError(f"Cannot retrieve funding info for {trading_pair} from Binance: {e}")

    async def _listen_for_funding_info(self):
        """
        Periodically fetch funding info updates from Binance connector
        """
        while True:
            try:
                # Check for each trading pair
                for trading_pair in self._trading_pairs:
                    # Get funding info using get_funding_info method
                    funding_info = await self.get_funding_info(trading_pair)

                    # Create funding info update and add to queue
                    update = FundingInfoUpdate(
                        trading_pair=trading_pair,
                        index_price=funding_info.index_price,
                        mark_price=funding_info.mark_price,
                        rate=funding_info.rate,
                        next_funding_utc_timestamp=funding_info.next_funding_utc_timestamp,
                    )

                    self._message_queue[self._funding_info_messages_queue_key].put_nowait(update)

                # Check funding info every 60 seconds
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error listening for funding info: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _parse_funding_info_message(
        self, raw_message: Dict[str, Any], message_queue: Optional[asyncio.Queue] = None
    ) -> FundingInfoUpdate:
        """
        Simple wrapper to convert funding info message format to FundingInfoUpdate
        """
        trading_pair = raw_message.get("trading_pair")

        # If trading pair is missing but symbol is present, convert it
        if not trading_pair and "symbol" in raw_message:
            try:
                trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(raw_message["symbol"])
            except Exception:
                trading_pair = "UNKNOWN"  # Fallback

        return FundingInfoUpdate(
            trading_pair=trading_pair,
            index_price=Decimal(str(raw_message.get("indexPrice", "0"))),
            mark_price=Decimal(str(raw_message.get("markPrice", "0"))),
            rate=Decimal(str(raw_message.get("lastFundingRate", "0"))),
            next_funding_utc_timestamp=int(float(raw_message.get("nextFundingTime", 0)) * 1e-3),
        )

    async def listen_for_funding_info(self, output: asyncio.Queue):
        """
        Listen for funding info updates and forward to output queue
        """
        message_queue = self._message_queue[self._funding_info_messages_queue_key]
        while True:
            try:
                funding_info_update = await message_queue.get()
                output.put_nowait(funding_info_update)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error in funding info listener: {e}")
                await asyncio.sleep(5)

    # ---------------------------------------- End of Funding Info ----------------------------------------

    # ---------------------------------------- WebSocket Interface (Dummy Implementation) ----------------------------------------
    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        This is a dummy method as we don't use WebSockets for QTX market data.
        Used to satisfy the interface required by the base class.
        """
        # Since this method is being called by base class but we're not using it,
        # we need to raise an informative error instead of NotImplementedError
        self.logger().debug("WebSocket assistant requested but QTX uses UDP. This is likely called by the base class.")
        # Create dummy/mock WSAssistant
        ws_assistant = WSAssistant(self._connector.authenticator, api_factory=self._connector._web_assistants_factory)
        return ws_assistant

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        """
        Dummy method as we don't use WebSockets for QTX market data.
        """
        pass

    # ---------------------------------------- End of WebSocket Interface ----------------------------------------

# ---------------------------------------- End of QtxPerpetualAPIOrderBookDataSource ----------------------------------------
