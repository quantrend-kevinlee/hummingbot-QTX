#!/usr/bin/env python

import asyncio
import socket
import struct
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional

from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_constants as BINANCE_CONSTANTS
from hummingbot.connector.derivative.qtx_perpetual import (
    qtx_perpetual_constants as CONSTANTS,
    qtx_perpetual_web_utils as web_utils,
)
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.funding_info import FundingInfo, FundingInfoUpdate
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.perpetual_api_order_book_data_source import PerpetualAPIOrderBookDataSource
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.logger import HummingbotLogger


class QtxPerpetualAPIOrderBookDataSource(PerpetualAPIOrderBookDataSource):
    """
    OrderBookTrackerDataSource implementation for QTX Perpetual using UDP for market data.
    This class connects to QTX's UDP feed for real-time order book data, while
    using Binance API for funding rate information.
    """
    _logger: Optional[HummingbotLogger] = None

    def __init__(
        self,
        trading_pairs: List[str],
        connector,
        api_factory: WebAssistantsFactory,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
        host: str = CONSTANTS.DEFAULT_UDP_HOST,
        port: int = CONSTANTS.DEFAULT_UDP_PORT,
    ):
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._host = host
        self._port = port
        self._throttler = AsyncThrottler(CONSTANTS.RATE_LIMITS)
        self._udp_socket = None
        self._subscribed_pairs = set()
        self._listening_task = None
        self._funding_info_listener_task = None
        self._funding_info: Dict[str, Dict] = {}
        self._funding_info_callbacks = []

        # Message queues for different types of data
        self._message_queue = defaultdict(asyncio.Queue)
        self._trade_messages_queue_key = "trade"
        self._diff_messages_queue_key = "diff"
        self._snapshot_messages_queue_key = "snapshot"
        self._funding_info_messages_queue_key = "funding_info"

        # Subscription index tracking for UDP feed
        self._subscription_indices = {}
        self._last_update_id = {}
        self._empty_orderbook = {}

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = HummingbotLogger(__name__)
        return cls._logger

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

    async def get_funding_info(self, trading_pair: str) -> FundingInfo:
        """
        Get funding information for a trading pair

        :param trading_pair: Trading pair to get funding info for
        :return: FundingInfo object
        """
        # Check if we have cached info that's still fresh
        if trading_pair in self._funding_info:
            cached_info = self._funding_info[trading_pair]
            # If info is less than 60 seconds old, use it
            if time.time() - cached_info["timestamp"] < 60:
                return FundingInfo(
                    trading_pair=trading_pair,
                    index_price=cached_info["indexPrice"],
                    mark_price=cached_info["markPrice"],
                    next_funding_utc_timestamp=int(float(cached_info["nextFundingTime"]) * 1e-3),
                    rate=cached_info["fundingRate"],
                )

        # If no cached info or data is stale, get from Binance
        if self._connector._trading_required and hasattr(self._connector, "_binance_connector"):
            try:
                # Get exchange symbol format
                exchange_symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair)

                # Request funding info from Binance
                data = await self._connector._binance_connector._api_request(
                    method="GET",
                    path_url=BINANCE_CONSTANTS.MARK_PRICE_URL,
                    params={"symbol": exchange_symbol},
                    is_auth_required=False
                )

                # Store the funding info in cache
                self._funding_info[trading_pair] = {
                    "timestamp": time.time(),
                    "indexPrice": Decimal(str(data.get("indexPrice", "0"))),
                    "markPrice": Decimal(str(data.get("markPrice", "0"))),
                    "nextFundingTime": int(data.get("nextFundingTime", 0)),
                    "fundingRate": Decimal(str(data.get("lastFundingRate", "0"))),
                }

                # Create funding info object
                funding_info = FundingInfo(
                    trading_pair=trading_pair,
                    index_price=Decimal(str(data.get("indexPrice", "0"))),
                    mark_price=Decimal(str(data.get("markPrice", "0"))),
                    next_funding_utc_timestamp=int(float(data.get("nextFundingTime", 0)) * 1e-3),
                    rate=Decimal(str(data.get("lastFundingRate", "0"))),
                )

                # Notify any registered callbacks
                for callback in self._funding_info_callbacks:
                    await callback(trading_pair, funding_info)

                return funding_info
            except Exception as e:
                self.logger().error(f"Error getting funding info from Binance: {e}", exc_info=True)

        # Default values if we couldn't get data
        return FundingInfo(
            trading_pair=trading_pair,
            index_price=Decimal("0"),
            mark_price=Decimal("0"),
            next_funding_utc_timestamp=0,
            rate=Decimal("0"),
        )

    async def register_funding_info_callback(self, callback):
        """
        Register a callback for funding info updates
        """
        self._funding_info_callbacks.append(callback)

    async def _subscribe_to_order_book_streams(self) -> None:
        """
        Subscribe to order book streams for all trading pairs using UDP
        """
        try:
            # Initialize UDP socket
            self._udp_socket = web_utils.get_udp_socket(self._host, self._port)

            # Set socket to blocking mode for subscription phase
            self._udp_socket.setblocking(True)
            self._udp_socket.settimeout(2.0)  # Timeout for subscription responses

            # Subscribe to each trading pair
            self._subscription_indices = {}
            subscription_success = False

            for trading_pair in self._trading_pairs:
                try:
                    # Convert trading pair to QTX format (e.g., BTC-USDT-PERP -> binance-futures:btcusdt)
                    exchange_symbol = self._get_exchange_symbol_from_trading_pair(trading_pair)
                    self.logger().info(f"Subscribing to QTX UDP feed: {exchange_symbol} for {trading_pair}")

                    # Send subscription request
                    self._udp_socket.sendto(exchange_symbol.encode(), (self._host, self._port))

                    # Wait for ACK (index number)
                    deadline = time.time() + 2.0
                    index = None

                    while time.time() < deadline:
                        try:
                            response, addr = self._udp_socket.recvfrom(CONSTANTS.DEFAULT_UDP_BUFFER_SIZE)
                        except socket.timeout:
                            self.logger().error(f"Timeout waiting for ACK for {exchange_symbol}")
                            break

                        # Ignore packets not from the server
                        if addr[0] != self._host:
                            continue

                        try:
                            # ACK should be an integer index
                            idx = int(response.decode().strip())
                            index = idx
                            self.logger().info(f"Got ACK {idx} for {exchange_symbol}")
                            break
                        except (UnicodeDecodeError, ValueError):
                            # Not an index, likely market data - ignore
                            continue

                    if index is None:
                        self.logger().error(f"No valid ACK for {exchange_symbol}")
                        continue

                    # Store subscription index
                    self._subscription_indices[index] = trading_pair
                    self._subscribed_pairs.add(trading_pair)
                    subscription_success = True

                    # Initialize empty state for this pair
                    self._empty_orderbook[trading_pair] = True
                    self._last_update_id[trading_pair] = int(time.time() * 1000)

                    self.logger().info(f"Successfully subscribed to {exchange_symbol} with index {index}")

                except Exception as e:
                    self.logger().error(f"Error subscribing to {trading_pair}: {e}", exc_info=True)

                # Small delay between subscriptions
                await asyncio.sleep(0.1)

            # Set socket back to non-blocking for receiving data
            self._udp_socket.setblocking(False)

            if not subscription_success:
                self.logger().error("No successful subscriptions to QTX UDP feed, market data may not be available")

            # Start listening task for incoming data
            if self._listening_task is None:
                self._listening_task = asyncio.create_task(self._listen_for_trades_and_orderbooks())

        except Exception as e:
            self.logger().error(f"Error subscribing to QTX UDP feed: {e}", exc_info=True)
            raise

    def _get_exchange_symbol_from_trading_pair(self, trading_pair: str) -> str:
        """
        Convert Hummingbot trading pair format to QTX format
        Format: "BTC-USD" -> "binance-futures:btcusdt"
        """
        # Check if this is a perpetual contract
        if (trading_pair.endswith("-PERP")):
            pair = trading_pair[:-5].replace("-", "").lower()
        else:
            pair = trading_pair.replace("-", "").lower()

        # Format the symbol
        return f"binance-futures:{pair}"

    def _parse_binary_message(self, data: bytes) -> Optional[Dict]:
        """
        Parse binary message from UDP feed based on the format in qtx_udp_logger.py
        UDP message format: <msg_type:int><index:int><tx_ms:long><event_ms:long><local_ns:long><sn_id:long>
        followed by message-specific data
        """
        if not data or len(data) < 40:  # Minimum header size
            return None

        try:
            # Parse the message header (40 bytes)
            msg_type, index, tx_ms, event_ms, local_ns, sn_id = struct.unpack("<iiqqqq", data[:40])

            # Get the trading pair from the subscription index
            trading_pair = self._subscription_indices.get(index)
            if not trading_pair:
                self.logger().debug(f"Received message for unknown subscription index: {index}")
                return None

            # Process based on message type
            timestamp = time.time()

            if abs(msg_type) == 1:  # Ticker (Bid = 1, Ask = -1)
                # Extract price and size
                if len(data) < 56:
                    return None

                price, size = struct.unpack("<dd", data[40:56])
                side = "bid" if msg_type > 0 else "ask"

                # Create ticker message
                update_id = int(timestamp * 1000)
                if trading_pair in self._last_update_id:
                    update_id = max(update_id, self._last_update_id[trading_pair] + 1)
                self._last_update_id[trading_pair] = update_id

                # Log for debugging
                self.logger().debug(f"Ticker: {side}, {trading_pair}, price: {price}, size: {size}")

                return {
                    "type": CONSTANTS.DIFF_MESSAGE_TYPE,  # Map ticker to diff for Hummingbot
                    "trading_pair": trading_pair,
                    "timestamp": timestamp,
                    "update_id": update_id,
                    "bids": [[price, size]] if side == "bid" else [],
                    "asks": [] if side == "bid" else [[price, size]]
                }

            elif msg_type == 2:  # Depth (Order Book)
                # Get ask and bid counts
                if len(data) < 56:  # 40 bytes header + 16 bytes counts
                    return None

                asks_len, bids_len = struct.unpack("<qq", data[40:56])

                # Extract asks and bids
                asks = []
                bids = []
                offset = 56  # Start after the header and counts

                # Parse asks
                for i in range(asks_len):
                    if offset + 16 <= len(data):
                        price, size = struct.unpack("<dd", data[offset:offset + 16])
                        asks.append([price, size])
                        offset += 16  # Each price/size pair is 16 bytes

                # Parse bids
                for i in range(bids_len):
                    if offset + 16 <= len(data):
                        price, size = struct.unpack("<dd", data[offset:offset + 16])
                        bids.append([price, size])
                        offset += 16  # Each price/size pair is 16 bytes

                # Sort bids (desc) and asks (asc)
                bids.sort(key=lambda x: float(x[0]), reverse=True)
                asks.sort(key=lambda x: float(x[0]))

                update_id = int(timestamp * 1000)
                if trading_pair in self._last_update_id:
                    update_id = max(update_id, self._last_update_id[trading_pair] + 1)
                self._last_update_id[trading_pair] = update_id

                # Log for debugging
                self.logger().debug(f"Depth: {trading_pair}, asks: {len(asks)}, bids: {len(bids)}")

                # Mark that we've received data for this trading pair
                if trading_pair in self._empty_orderbook:
                    self._empty_orderbook[trading_pair] = False

                return {
                    "type": CONSTANTS.DIFF_MESSAGE_TYPE,  # Always treat as diff
                    "trading_pair": trading_pair,
                    "timestamp": timestamp,
                    "update_id": update_id,
                    "bids": bids,
                    "asks": asks
                }

            elif abs(msg_type) == 3:  # Trade (Buy = 3, Sell = -3)
                # Extract price and size
                if len(data) < 56:
                    return None

                price, size = struct.unpack("<dd", data[40:56])
                side = TradeType.BUY if msg_type > 0 else TradeType.SELL

                # Create trade ID
                trade_id = str(int(timestamp * 1000000))

                # Log for debugging
                self.logger().debug(f"Trade: {side.name}, {trading_pair}, price: {price}, size: {size}")

                return {
                    "type": CONSTANTS.TRADE_MESSAGE_TYPE,
                    "trading_pair": trading_pair,
                    "timestamp": timestamp,
                    "trade_id": trade_id,
                    "price": price,
                    "amount": size,
                    "trade_type": side
                }

            else:
                self.logger().warning(f"Unknown message type: {msg_type}")
                return None

        except Exception as e:
            self.logger().error(f"Error parsing binary message: {e}", exc_info=True)
            return None

    async def _process_binary_message(self, data: bytes, addr) -> None:
        """
        Process the binary message from UDP feed
        """
        if not data or len(data) < 40:  # Minimum header size
            return

        try:
            # Parse the binary message
            message = self._parse_binary_message(data)

            if message:
                message_type = message.get("type")
                trading_pair = message.get("trading_pair")

                if message_type == CONSTANTS.TRADE_MESSAGE_TYPE:
                    # Create trade message
                    trade_msg = OrderBookMessage(
                        message_type=OrderBookMessageType.TRADE,
                        content={
                            "trading_pair": trading_pair,
                            "trade_type": float(message["trade_type"].value),
                            "trade_id": message["trade_id"],
                            "update_id": message["trade_id"],
                            "price": Decimal(str(message["price"])),
                            "amount": Decimal(str(message["amount"])),
                        },
                        timestamp=message["timestamp"]
                    )
                    await self._message_queue[self._trade_messages_queue_key].put(trade_msg)

                elif message_type == CONSTANTS.DIFF_MESSAGE_TYPE:
                    # Create diff message
                    diff_msg = OrderBookMessage(
                        message_type=OrderBookMessageType.DIFF,
                        content={
                            "trading_pair": trading_pair,
                            "update_id": message["update_id"],
                            "bids": [[Decimal(str(price)), Decimal(str(amount))] for price, amount in message["bids"]],
                            "asks": [[Decimal(str(price)), Decimal(str(amount))] for price, amount in message["asks"]],
                        },
                        timestamp=message["timestamp"]
                    )
                    await self._message_queue[self._diff_messages_queue_key].put(diff_msg)

        except Exception as e:
            self.logger().error(f"Error processing binary message: {e}", exc_info=True)

    async def _listen_for_trades_and_orderbooks(self) -> None:
        """
        Listen for trades and orderbook updates from the UDP socket
        """
        buffer_size = CONSTANTS.DEFAULT_UDP_BUFFER_SIZE

        # Create a buffer for receiving data
        recv_buffer = bytearray(buffer_size)

        while True:
            try:
                if self._udp_socket is None:
                    await asyncio.sleep(1)
                    continue

                # Receive data from UDP socket (non-blocking way)
                loop = asyncio.get_event_loop()
                try:
                    bytes_read, addr = await loop.run_in_executor(
                        None,
                        lambda: self._udp_socket.recvfrom_into(recv_buffer)
                    )
                except BlockingIOError:
                    # No data available
                    await asyncio.sleep(0.001)
                    continue
                except ConnectionRefusedError:
                    self.logger().error("UDP connection refused. Attempting to reconnect...")
                    await asyncio.sleep(1)
                    self._udp_socket = web_utils.get_udp_socket(self._host, self._port)
                    continue

                if bytes_read == 0:
                    await asyncio.sleep(0.001)
                    continue

                # Parse binary message
                message = self._parse_binary_message(recv_buffer[:bytes_read])

                if message:
                    message_type = message.get("type")
                    trading_pair = message.get("trading_pair")

                    if message_type == CONSTANTS.TRADE_MESSAGE_TYPE:
                        # Create trade message
                        trade_msg = OrderBookMessage(
                            message_type=OrderBookMessageType.TRADE,
                            content={
                                "trading_pair": trading_pair,
                                "trade_type": float(message["trade_type"].value),
                                "trade_id": message["trade_id"],
                                "update_id": message["trade_id"],
                                "price": Decimal(str(message["price"])),
                                "amount": Decimal(str(message["amount"])),
                            },
                            timestamp=message["timestamp"]
                        )
                        await self._message_queue[self._trade_messages_queue_key].put(trade_msg)

                    elif message_type == CONSTANTS.DIFF_MESSAGE_TYPE:
                        # Create diff message
                        diff_msg = OrderBookMessage(
                            message_type=OrderBookMessageType.DIFF,
                            content={
                                "trading_pair": trading_pair,
                                "update_id": message["update_id"],
                                "bids": [[Decimal(str(price)), Decimal(str(amount))] for price, amount in message["bids"]],
                                "asks": [[Decimal(str(price)), Decimal(str(amount))] for price, amount in message["asks"]],
                            },
                            timestamp=message["timestamp"]
                        )
                        await self._message_queue[self._diff_messages_queue_key].put(diff_msg)

                # Prevent CPU hogging
                await asyncio.sleep(0.00001)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error listening for market data: {e}", exc_info=True)
                # Reconnect socket on failure
                try:
                    if self._udp_socket:
                        self._udp_socket.close()
                except Exception:
                    pass

                # Wait a moment before reconnecting
                await asyncio.sleep(1)

                # Attempt to reconnect
                try:
                    self._udp_socket = web_utils.get_udp_socket(self._host, self._port)
                except Exception as e:
                    self.logger().error(f"Error reconnecting to UDP socket: {e}")
                    await asyncio.sleep(5)  # Wait longer after a reconnection failure

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
            snapshot_timestamp)
        order_book = self.order_book_create_function()
        order_book.apply_snapshot(snapshot_msg.bids, snapshot_msg.asks, snapshot_msg.update_id)
        return order_book

    async def get_snapshot(self, trading_pair: str) -> Dict:
        """
        Give empty snapshot since QTX does not support order book snapshots,
        and snapshots could be built after receiving enough diffs.
        Indicating that we should build a warm-up phase once a strategy is running, and during warm-up,
        the connector should be marked as not ready in Hummingbot.
        """
        return {
            "bids": [],
            "asks": []
        }

    async def _listen_for_funding_info(self):
        """
        Periodically fetch funding info updates from Binance
        """
        while True:
            try:
                for trading_pair in self._trading_pairs:
                    await self.get_funding_info(trading_pair)

                # Check funding info every 60 seconds
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error listening for funding info: {e}", exc_info=True)
                await asyncio.sleep(5)

    def _parse_funding_info_message(self, message: Dict[str, Any], timestamp: Optional[float] = None) -> FundingInfoUpdate:
        """
        Parse funding info from Binance message format
        """
        if timestamp is None:
            timestamp = time.time()

        trading_pair = message.get("trading_pair")
        if not trading_pair:
            # Try to extract trading pair from symbol if available
            symbol = message.get("symbol")
            if symbol:
                # Convert from exchange symbol to trading pair
                trading_pair = self._connector.trading_pair_associated_to_exchange_symbol(symbol)

        # Parse funding rate
        funding_rate = Decimal(str(message.get("lastFundingRate", "0")))

        # Parse index price
        index_price = Decimal(str(message.get("indexPrice", "0")))

        # Parse mark price
        mark_price = Decimal(str(message.get("markPrice", "0")))

        # Parse next funding time
        next_funding_utc_timestamp = int(message.get("nextFundingTime", 0)) // 1000

        return FundingInfoUpdate(
            trading_pair=trading_pair,
            index_price=index_price,
            mark_price=mark_price,
            rate=funding_rate,
            next_funding_utc_timestamp=next_funding_utc_timestamp
        )

    async def start(self):
        """
        Starts the data source
        """
        # Start UDP socket listener
        await self._subscribe_to_order_book_streams()

        # Start funding info listener
        if hasattr(self, "_funding_info_listener_task") and self._funding_info_listener_task is not None:
            self._funding_info_listener_task.cancel()
        self._funding_info_listener_task = asyncio.create_task(self._listen_for_funding_info())

    async def stop(self):
        """
        Stops the data source
        """
        if self._listening_task is not None:
            self._listening_task.cancel()
            self._listening_task = None

        if hasattr(self, "_funding_info_listener_task") and self._funding_info_listener_task is not None:
            self._funding_info_listener_task.cancel()
            self._funding_info_listener_task = None

        if self._udp_socket is not None:
            # Unsubscribe from all symbols
            for trading_pair in self._subscribed_pairs:
                exchange_symbol = self._get_exchange_symbol_from_trading_pair(trading_pair)
                try:
                    # Set socket to blocking for unsubscribe
                    self._udp_socket.setblocking(True)
                    self._udp_socket.settimeout(1.0)

                    # Send unsubscribe request (prepend - to symbol)
                    unsubscribe_msg = f"-{exchange_symbol}"
                    self.logger().info(f"Unsubscribing from symbol: {exchange_symbol}")
                    self._udp_socket.sendto(unsubscribe_msg.encode(), (self._host, self._port))

                    # Wait for unsubscribe response
                    try:
                        response, _ = self._udp_socket.recvfrom(CONSTANTS.DEFAULT_UDP_BUFFER_SIZE)
                        try:
                            response_text = response.decode().strip()
                            self.logger().info(f"Unsubscribe response for {exchange_symbol}: {response_text}")
                        except UnicodeDecodeError:
                            self.logger().info(f"Received binary unsubscribe response for {exchange_symbol}")
                    except socket.timeout:
                        self.logger().warning(f"Timeout waiting for unsubscribe ACK for {exchange_symbol}")
                    except Exception as e:
                        self.logger().error(f"Error getting unsubscribe response for {exchange_symbol}: {e}")
                except Exception as e:
                    self.logger().error(f"Error unsubscribing from {exchange_symbol}: {e}")

            # Close the socket
            self._udp_socket.close()
            self._udp_socket = None

            # Clear subscription data
            self._subscription_indices = {}
            self._subscribed_pairs = set()

        self.logger().info("Funding info listener has been canceled")
