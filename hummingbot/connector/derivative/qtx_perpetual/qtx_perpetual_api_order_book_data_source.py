#!/usr/bin/env python

import asyncio
import socket
import struct
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from hummingbot.connector.derivative.qtx_perpetual import (
    qtx_perpetual_constants as CONSTANTS,
    qtx_perpetual_utils,
    qtx_perpetual_web_utils as web_utils,
)
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future, safe_gather
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.logger import HummingbotLogger


class QtxPerpetualAPIOrderBookDataSource(OrderBookTrackerDataSource):
    """
    OrderBookTrackerDataSource implementation for QTX Perpetual.
    """
    _logger: Optional[HummingbotLogger] = None
    
    def __init__(
        self,
        trading_pairs: List[str],
        connector: Any,
        api_factory: Optional[WebAssistantsFactory] = None,
        domain: str = CONSTANTS.DOMAIN,
    ):
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._throttler = AsyncThrottler(CONSTANTS.RATE_LIMITS)
        self._funding_info: Dict[str, Dict[str, Any]] = {}
        self._funding_info_callbacks = []
        self._udp_socket = None
        self._message_queue = asyncio.Queue()
        self._snapshot_msg_queues = defaultdict(asyncio.Queue)
        self._diff_msg_queues = defaultdict(asyncio.Queue)
        self._trade_msg_queues = defaultdict(asyncio.Queue)
        self._funding_info_msg_queues = defaultdict(asyncio.Queue)

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = HummingbotLogger(__name__)
        return cls._logger

    @property
    def funding_info(self) -> Dict[str, Dict[str, Any]]:
        return self._funding_info.copy()

    async def get_last_traded_prices(self, trading_pairs: List[str]) -> Dict[str, float]:
        """
        Gets the last traded price for each trading pair
        :param trading_pairs: List of trading pairs
        :return: Dictionary mapping each trading pair to its last traded price
        """
        result = {}
        for trading_pair in trading_pairs:
            result[trading_pair] = await self._connector.get_last_traded_price(trading_pair)
        return result

    async def get_funding_info(self, trading_pair: str) -> Dict[str, Any]:
        """
        Gets the funding information for a trading pair
        :param trading_pair: The trading pair to get funding info for
        :return: Dictionary containing funding information
        """
        exchange_trading_pair = await self._convert_to_exchange_trading_pair(trading_pair)
        
        try:
            response = await web_utils.api_request(
                path=CONSTANTS.FUNDING_INFO_PATH_URL,
                api_factory=self._api_factory,
                params={"symbol": exchange_trading_pair},
            )
            
            # Process and store funding info
            funding_info = {
                "indexPrice": Decimal(str(response.get("indexPrice", "0"))),
                "markPrice": Decimal(str(response.get("markPrice", "0"))),
                "nextFundingTime": int(response.get("nextFundingTime", 0)),
                "fundingRate": Decimal(str(response.get("fundingRate", "0"))),
            }
            
            self._funding_info[trading_pair] = funding_info
            
            # Notify callbacks
            for callback in self._funding_info_callbacks:
                await callback(trading_pair, funding_info)
            
            return funding_info
        except Exception as e:
            self.logger().error(f"Error getting funding info for {trading_pair}: {e}", exc_info=True)
            return {
                "indexPrice": Decimal("0"),
                "markPrice": Decimal("0"),
                "nextFundingTime": 0,
                "fundingRate": Decimal("0"),
            }

    async def register_funding_info_callback(self, callback):
        """
        Register a callback for funding info updates
        """
        self._funding_info_callbacks.append(callback)

    async def listen_for_subscriptions(self):
        """
        Connects to the UDP socket and listens for market data
        """
        while True:
            try:
                if self._udp_socket is None:
                    self._udp_socket = web_utils.get_udp_socket(
                        host=self._connector._qtx_perpetual_host,
                        port=self._connector._qtx_perpetual_port
                    )
                    self.logger().info("Connected to QTX UDP market data feed")
                    
                    # Subscribe to trading pairs
                    for trading_pair in self._trading_pairs:
                        # Convert to exchange format (e.g., "BTC-USDT-PERP" -> "binance-futures:btcusdt")
                        exchange_pair = self._convert_to_exchange_trading_pair(trading_pair)
                        if exchange_pair:
                            try:
                                self._udp_socket.sendto(exchange_pair.encode(), 
                                                       (self._connector._qtx_perpetual_host, self._connector._qtx_perpetual_port))
                                self.logger().info(f"Subscribed to {exchange_pair} for {trading_pair}")
                            except Exception as e:
                                self.logger().error(f"Error subscribing to {exchange_pair}: {e}")
                
                # Process incoming messages
                await self._process_udp_messages()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error listening for UDP messages: {e}", exc_info=True)
                self._udp_socket = None
                await asyncio.sleep(CONSTANTS.RECONNECT_DELAY)

    async def _process_udp_messages(self):
        """
        Processes UDP messages from the socket
        """
        while True:
            try:
                data, _ = self._udp_socket.recvfrom(CONSTANTS.DEFAULT_UDP_BUFFER_SIZE)
                
                # Parse the message header based on the format in the logs
                # Format from UDP feed logs: binance-futures:btcusdt
                if len(data) >= 40:  # Minimum size for message header
                    try:
                        # Parse header using the format from qtx_udp_logger.py
                        msg_type, index, tx_ms, event_ms, local_ns, sn_id = struct.unpack("<iiqqqq", data[:40])
                        
                        # Find the corresponding subscription/symbol
                        # In a real implementation, we would maintain a mapping of indices to symbols
                        # For now, we'll use a simpler approach based on the trading pairs
                        
                        # Get the trading pair from our configured pairs
                        # This is a simplified approach - in production, you'd maintain a proper mapping
                        trading_pair = None
                        
                        # Process message based on message type
                        if msg_type in [CONSTANTS.MSG_TYPE_L1_BID, CONSTANTS.MSG_TYPE_L1_ASK]:
                            # L1 ticker data
                            if len(data) >= 56:  # Header + price/size
                                price, size = struct.unpack("<dd", data[40:56])
                                # Try to identify the trading pair from the price
                                trading_pair = self._identify_trading_pair_from_price(price)
                                if trading_pair:
                                    is_bid = msg_type > 0
                                    await self._process_ticker_message(trading_pair, price, size, is_bid)
                        
                        elif msg_type == 2:  # L2 depth data
                            if len(data) >= 56:  # Has additional header data
                                asks_len, bids_len = struct.unpack("<qq", data[40:56])
                                # Try to identify the trading pair
                                if asks_len > 0 or bids_len > 0:
                                    # Process depth data
                                    trading_pair = self._identify_trading_pair_from_depth(data[56:], asks_len, bids_len)
                                    if trading_pair:
                                        await self._process_depth_message(trading_pair, data[40:], asks_len, bids_len)
                        
                        elif abs(msg_type) == 3:  # Trade data
                            if len(data) >= 56:  # Header + price/size
                                price, size = struct.unpack("<dd", data[40:56])
                                # Try to identify the trading pair from the price
                                trading_pair = self._identify_trading_pair_from_price(price)
                                if trading_pair:
                                    is_buy = msg_type > 0
                                    await self._process_trade_message(trading_pair, price, size, is_buy)
                        
                        else:
                            # Unknown message type
                            self.logger().debug(f"Unknown message type: {msg_type}")
                    
                    except Exception as e:
                        self.logger().error(f"Error parsing message header: {e}", exc_info=True)
            
            except socket.timeout:
                # Socket timeout, continue
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error processing UDP message: {e}", exc_info=True)
                await asyncio.sleep(1)  # Short delay before retrying

    def _identify_trading_pair_from_price(self, price: float) -> Optional[str]:
        """
        Attempts to identify the trading pair based on the price level
        This is a heuristic approach and should be replaced with proper symbol mapping
        """
        # Simple price-based heuristics for common futures
        if price > 20000:  # Likely BTC
            for pair in self._trading_pairs:
                if "BTC" in pair:
                    return pair
        elif 1000 < price < 10000:  # Likely ETH
            for pair in self._trading_pairs:
                if "ETH" in pair:
                    return pair
        elif 10 < price < 1000:  # Could be SOL, etc.
            for pair in self._trading_pairs:
                if "SOL" in pair:
                    return pair
        
        # If we can't identify, return the first trading pair as fallback
        if self._trading_pairs:
            return self._trading_pairs[0]
        
        return None
    
    def _identify_trading_pair_from_depth(self, data: bytes, asks_len: int, bids_len: int) -> Optional[str]:
        """
        Attempts to identify the trading pair based on the depth data
        """
        if asks_len > 0 or bids_len > 0:
            # Try to get the first price level to identify the pair
            try:
                offset = 0
                if asks_len > 0:
                    price, _ = struct.unpack("<dd", data[offset:offset+16])
                    return self._identify_trading_pair_from_price(price)
                elif bids_len > 0:
                    # Skip asks section if it exists
                    offset = asks_len * 16
                    price, _ = struct.unpack("<dd", data[offset:offset+16])
                    return self._identify_trading_pair_from_price(price)
            except Exception:
                pass
        
        # Fallback to first trading pair
        if self._trading_pairs:
            return self._trading_pairs[0]
        
        return None
    
    async def _process_ticker_message(self, trading_pair: str, price: float, size: float, is_bid: bool):
        """
        Process a ticker message (L1 data)
        """
        timestamp = time.time()
        update_id = int(timestamp * 1000)
        
        content = {
            "trading_pair": trading_pair,
            "update_id": update_id,
            "bids": [[price, size]] if is_bid else [],
            "asks": [] if is_bid else [[price, size]],
        }
        
        message = OrderBookMessage(
            message_type=OrderBookMessageType.DIFF,
            content=content,
            timestamp=timestamp
        )
        
        await self._diff_msg_queues[trading_pair].put(message)
        self.logger().debug(f"Ticker: {trading_pair}, {'bid' if is_bid else 'ask'}, price: {price}, size: {size}")
    
    async def _process_depth_message(self, trading_pair: str, data: bytes, asks_len: int, bids_len: int):
        """
        Process a depth message (L2 data)
        """
        timestamp = time.time()
        update_id = int(timestamp * 1000)
        
        # Extract asks and bids
        asks = []
        bids = []
        offset = 16  # Start after the asks_len and bids_len
        
        # Parse asks
        for i in range(asks_len):
            if offset + 16 <= len(data):
                price, size = struct.unpack("<dd", data[offset:offset+16])
                asks.append([price, size])
                offset += 16  # Each price/size pair is 16 bytes
        
        # Parse bids
        for i in range(bids_len):
            if offset + 16 <= len(data):
                price, size = struct.unpack("<dd", data[offset:offset+16])
                bids.append([price, size])
                offset += 16  # Each price/size pair is 16 bytes
        
        # Sort bids (desc) and asks (asc)
        bids.sort(key=lambda x: float(x[0]), reverse=True)
        asks.sort(key=lambda x: float(x[0]))
        
        # Determine if this is a snapshot or diff
        is_snapshot = len(bids) > 5 or len(asks) > 5
        
        content = {
            "trading_pair": trading_pair,
            "update_id": update_id,
            "bids": bids,
            "asks": asks,
        }
        
        message_type = OrderBookMessageType.SNAPSHOT if is_snapshot else OrderBookMessageType.DIFF
        message = OrderBookMessage(
            message_type=message_type,
            content=content,
            timestamp=timestamp
        )
        
        if is_snapshot:
            await self._snapshot_msg_queues[trading_pair].put(message)
            self.logger().debug(f"Depth snapshot: {trading_pair}, asks: {len(asks)}, bids: {len(bids)}")
        else:
            await self._diff_msg_queues[trading_pair].put(message)
            self.logger().debug(f"Depth update: {trading_pair}, asks: {len(asks)}, bids: {len(bids)}")

    def _convert_to_exchange_trading_pair(self, trading_pair: str) -> str:
        """
        Formats a trading pair into the exchange symbol format for futures.
        Format: "BTC-USDT-PERP" -> "binance-futures:btcusdt"

        :param trading_pair: The trading pair in Hummingbot format
        :return: The trading pair in exchange format
        """
        # For perpetual futures, we need to handle the -PERP suffix
        if trading_pair.endswith("-PERP"):
            base_quote = trading_pair[:-5]  # Remove -PERP suffix
            formatted_pair = base_quote.replace("-", "").lower()
            return f"binance-futures:{formatted_pair}"
        else:
            # Regular format for non-PERP pairs
            formatted_pair = trading_pair.replace("-", "").lower()
            return f"binance-futures:{formatted_pair}"
    
    async def _process_trade_message(self, trading_pair: str, price: float, size: float, is_buy: bool):
        """
        Process a trade message
        """
        timestamp = time.time()
        trade_id = str(int(timestamp * 1000000))  # Use timestamp as trade ID
        
        content = {
            "trading_pair": trading_pair,
            "trade_type": "buy" if is_buy else "sell",
            "trade_id": trade_id,
            "price": price,
            "amount": size,
        }
        
        message = OrderBookMessage(
            message_type=OrderBookMessageType.TRADE,
            content=content,
            timestamp=timestamp
        )
        
        await self._trade_msg_queues[trading_pair].put(message)
        self.logger().debug(f"Trade: {trading_pair}, {'buy' if is_buy else 'sell'}, price: {price}, size: {size}")

    async def _process_funding_info_message(self, trading_pair: str, data: Dict[str, Any]):
        """
        Processes a funding info message
        """
        timestamp = int(time.time() * 1000)
        
        content = {
            "trading_pair": trading_pair,
            "timestamp": timestamp,
            "index_price": float(data.get("indexPrice", 0)),
            "mark_price": float(data.get("markPrice", 0)),
            "next_funding_time": int(data.get("nextFundingTime", 0)),
            "funding_rate": float(data.get("fundingRate", 0)),
        }
        
        message = OrderBookMessage(
            message_type=OrderBookMessageType.DIFF,  # Using DIFF type for funding info
            content=content,
            timestamp=timestamp / 1000
        )
        
        await self._funding_info_msg_queues[trading_pair].put(message)
        
        # Update funding info cache
        self._funding_info[trading_pair] = {
            "indexPrice": Decimal(str(data.get("indexPrice", "0"))),
            "markPrice": Decimal(str(data.get("markPrice", "0"))),
            "nextFundingTime": int(data.get("nextFundingTime", 0)),
            "fundingRate": Decimal(str(data.get("fundingRate", "0"))),
        }
        
        # Notify callbacks
        for callback in self._funding_info_callbacks:
            await callback(trading_pair, self._funding_info[trading_pair])

    async def get_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        """
        Gets the order book snapshot for a trading pair
        """
        exchange_trading_pair = qtx_perpetual_utils.convert_to_exchange_trading_pair(trading_pair)
        
        try:
            # Try to get snapshot from API
            response = await web_utils.api_request(
                path=CONSTANTS.SNAPSHOT_EVENT_TYPE,
                api_factory=self._api_factory,
                params={"symbol": exchange_trading_pair, "limit": 1000},
            )
            
            timestamp = int(time.time() * 1000)
            
            return {
                "trading_pair": trading_pair,
                "update_id": timestamp,
                "bids": [[float(bid[0]), float(bid[1])] for bid in response.get("bids", [])],
                "asks": [[float(ask[0]), float(ask[1])] for ask in response.get("asks", [])],
            }
        except Exception as e:
            self.logger().error(f"Error getting snapshot for {trading_pair}: {e}", exc_info=True)
            # Return empty snapshot
            return {
                "trading_pair": trading_pair,
                "update_id": int(time.time() * 1000),
                "bids": [],
                "asks": [],
            }

    async def get_new_order_book(self, trading_pair: str) -> OrderBook:
        """
        Creates a new order book for a trading pair
        """
        snapshot = await self.get_snapshot(trading_pair)
        snapshot_timestamp = time.time()
        snapshot_msg = OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            {
                "trading_pair": trading_pair,
                "update_id": snapshot["update_id"],
                "bids": snapshot["bids"],
                "asks": snapshot["asks"],
            },
            snapshot_timestamp
        )
        
        # Create order book
        order_book = self.order_book_create_function()
        order_book.apply_snapshot(snapshot_msg.bids, snapshot_msg.asks, snapshot_msg.update_id)
        return order_book

    async def _request_order_book_snapshots(self, trading_pair: str):
        """
        Periodically requests order book snapshots
        """
        try:
            snapshot = await self.get_snapshot(trading_pair)
            snapshot_timestamp = time.time()
            snapshot_msg = OrderBookMessage(
                OrderBookMessageType.SNAPSHOT,
                {
                    "trading_pair": trading_pair,
                    "update_id": snapshot["update_id"],
                    "bids": snapshot["bids"],
                    "asks": snapshot["asks"],
                },
                snapshot_timestamp
            )
            
            await self._snapshot_msg_queues[trading_pair].put(snapshot_msg)
            
            # Also request funding info
            await self.get_funding_info(trading_pair)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().error(f"Error requesting snapshot for {trading_pair}: {e}", exc_info=True)

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        Listens for order book snapshots
        """
        while True:
            try:
                for trading_pair in self._trading_pairs:
                    await self._request_order_book_snapshots(trading_pair)
                
                await asyncio.sleep(60)  # Request snapshots every minute
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error listening for order book snapshots: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def listen_for_order_book_diffs(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        Listens for order book diffs from the message queues
        """
        tasks = [
            self._listen_for_diffs_from_queue(trading_pair, output)
            for trading_pair in self._trading_pairs
        ]
        
        await safe_gather(*tasks)

    async def _listen_for_diffs_from_queue(self, trading_pair: str, output: asyncio.Queue):
        """
        Listens for order book diffs from a specific queue
        """
        msg_queue = self._diff_msg_queues[trading_pair]
        
        while True:
            try:
                message = await msg_queue.get()
                output.put_nowait(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error listening for diffs for {trading_pair}: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def listen_for_trades(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        Listens for trades from the message queues
        """
        tasks = [
            self._listen_for_trades_from_queue(trading_pair, output)
            for trading_pair in self._trading_pairs
        ]
        
        await safe_gather(*tasks)

    async def _listen_for_trades_from_queue(self, trading_pair: str, output: asyncio.Queue):
        """
        Listens for trades from a specific queue
        """
        msg_queue = self._trade_msg_queues[trading_pair]
        
        while True:
            try:
                message = await msg_queue.get()
                output.put_nowait(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error listening for trades for {trading_pair}: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def listen_for_funding_info(self):
        """
        Listens for funding info updates
        """
        while True:
            try:
                for trading_pair in self._trading_pairs:
                    await self.get_funding_info(trading_pair)
                
                # Check funding info every 30 seconds
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error listening for funding info: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def start(self):
        """
        Starts the data source
        """
        # Start UDP socket listener
        self._udp_socket_listener_task = safe_ensure_future(self.listen_for_subscriptions())
        
        # Start funding info listener
        self._funding_info_listener_task = safe_ensure_future(self.listen_for_funding_info())

    async def stop(self):
        """
        Stops the data source
        """
        if self._udp_socket_listener_task is not None:
            self._udp_socket_listener_task.cancel()
            self._udp_socket_listener_task = None
        
        if self._funding_info_listener_task is not None:
            self._funding_info_listener_task.cancel()
            self._funding_info_listener_task = None
        
        if self._udp_socket is not None:
            self._udp_socket.close()
            self._udp_socket = None
