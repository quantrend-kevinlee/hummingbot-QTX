"""
QTX Perpetual UDP Manager - Centralized market data feed manager

IMPORTANT: QTX does NOT send snapshot messages. It only sends:
- Ticker messages (type 1/-1): Single best bid/ask updates
- Depth messages (type 2): Full order book updates (treated as snapshots initially, then diffs)
- Trade messages (type 3/-3): Individual trade executions

The first depth message for each trading pair is treated as a snapshot,
subsequent depth messages are treated as diffs.
"""

import asyncio
import logging
import random
import select
import socket
import struct
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from hummingbot.connector.derivative.qtx_perpetual import (
    qtx_perpetual_constants as CONSTANTS,
    qtx_perpetual_trading_pair_utils,
)
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.logger import HummingbotLogger


@dataclass
class SubscriptionState:
    """Tracks the state of a subscription for a trading pair"""
    trading_pair: str
    qtx_symbol: str
    index: int
    is_active: bool = True
    last_update_id: int = 0
    last_message_time: float = 0.0


@dataclass 
class ConnectionState:
    """Tracks the state of the UDP connection"""
    is_connected: bool = False
    connection_start_time: float = 0.0
    reconnect_attempts: int = 0
    last_reconnect_time: float = 0.0
    consecutive_failures: int = 0
    last_message_time: float = 0.0
    

class QtxPerpetualUDPManager:
    """
    Singleton manager for QTX UDP market data connections.
    
    Provides direct queue access API for simplified integration with QtxPerpetualDerivative.
    
    IMPORTANT: QTX does not send snapshot messages. The first depth message received
    for each trading pair should be treated as a snapshot to initialize the order book.
    """
    _logger: Optional[HummingbotLogger] = None
    _instance: Optional["QtxPerpetualUDPManager"] = None
    _instance_lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(QtxPerpetualUDPManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._host = CONSTANTS.DEFAULT_UDP_HOST
        self._port = CONSTANTS.DEFAULT_UDP_PORT
        self._buffer_size = 65536

        # Connection management
        self._udp_socket: Optional[socket.socket] = None
        self._connection_state = ConnectionState()
        
        # Subscription tracking
        self._subscriptions: Dict[str, SubscriptionState] = {}
        self._index_to_pair: Dict[int, str] = {}
        
        # Direct message queues - Only diff and trade (NO SNAPSHOT)
        self._message_queues: Dict[str, Dict[str, asyncio.Queue]] = {}
        # Structure: {trading_pair: {"diff": Queue, "trade": Queue}}
        
        # Track update_id for each trading pair
        self._last_update_id: Dict[str, int] = {}
        
        # Tasks
        self._listen_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        # Error recovery settings
        self._max_reconnect_attempts = 5
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        
        # Exchange name for QTX format conversion
        self._exchange_name: Optional[str] = None
        
        self._initialized = True

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    @classmethod
    async def get_instance(cls) -> "QtxPerpetualUDPManager":
        """Get or create the singleton instance of QtxPerpetualUDPManager"""
        if cls._instance is None:
            async with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def is_connected(self) -> bool:
        """Check if UDP connection is established"""
        return self._connection_state.is_connected

    @property
    def subscribed_pairs(self) -> Set[str]:
        """Returns set of subscribed trading pairs"""
        return {sub.trading_pair for sub in self._subscriptions.values() if sub.is_active}

    # ========== Direct Queue API ==========

    def get_message_queue(self, trading_pair: str, message_type: str) -> asyncio.Queue:
        """
        Get message queue for a trading pair and message type.
        Creates queues lazily if they don't exist.
        
        NOTE: Only "diff" and "trade" queues exist. QTX doesn't send snapshots.
        
        Args:
            trading_pair: The trading pair in Hummingbot format
            message_type: One of "diff" or "trade"
            
        Returns:
            The message queue for the specified trading pair and type
            
        Raises:
            ValueError: If invalid message_type is provided
        """
        if message_type not in ["diff", "trade"]:
            raise ValueError(f"Invalid message_type '{message_type}'. Only 'diff' and 'trade' are supported.")
            
        if trading_pair not in self._message_queues:
            self._message_queues[trading_pair] = {
                "diff": asyncio.Queue(maxsize=1000),
                "trade": asyncio.Queue(maxsize=1000),
            }
        return self._message_queues[trading_pair][message_type]
    
    async def subscribe_and_get_queues(self, trading_pair: str) -> Dict[str, asyncio.Queue]:
        """
        Subscribe to trading pair and return message queues.
        This combines subscription and queue access for cleaner API.
        
        NOTE: Only returns "diff" and "trade" queues since QTX doesn't send snapshots.
        
        Args:
            trading_pair: The trading pair to subscribe to
            
        Returns:
            Dictionary with "diff" and "trade" queues
            
        Raises:
            RuntimeError: If subscription fails
        """
        # Subscribe
        success = await self._subscribe_to_trading_pair(trading_pair)
        if not success:
            raise RuntimeError(f"Failed to subscribe to {trading_pair}")
        
        # Return queues (no snapshot queue)
        return {
            "diff": self.get_message_queue(trading_pair, "diff"),
            "trade": self.get_message_queue(trading_pair, "trade"),
        }

    # ========== Connection Management ==========

    async def connect(self) -> bool:
        """Establish UDP connection"""
        async with self._lock:
            return await self._connect_internal()
    
    async def _connect_internal(self) -> bool:
        """Internal connection method"""
        try:
            self._close_socket()
            
            self._connection_state.reconnect_attempts += 1
            self._connection_state.last_reconnect_time = time.time()
            
            # Create UDP socket
            self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp_socket.setblocking(False)
            
            # Test connection
            ping_successful = await self._test_connection()
            
            if ping_successful:
                self._connection_state.is_connected = True
                self._connection_state.connection_start_time = time.time()
                self._connection_state.consecutive_failures = 0
                
                self.logger().info("UDP connection established to %s:%d", self._host, self._port)
                
                # Start health check
                if self._health_check_task is None or self._health_check_task.done():
                    self._health_check_task = asyncio.create_task(self._connection_health_check())
                
                return True
            else:
                self._close_socket()
                self._connection_state.consecutive_failures += 1
                return False
                
        except Exception as e:
            self._connection_state.consecutive_failures += 1
            self.logger().error("Error establishing UDP connection: %s", e)
            self._close_socket()
            return False
    
    async def _test_connection(self) -> bool:
        """Test UDP connection with simplified ping logic"""
        try:
            # Send ping
            self._udp_socket.sendto(b"ping", (self._host, self._port))
            
            # Set timeout for response
            self._udp_socket.setblocking(True)
            self._udp_socket.settimeout(1.0)
            
            try:
                # Try to receive any response
                response, addr = self._udp_socket.recvfrom(self._buffer_size)
                return True
            except socket.timeout:
                # Some servers don't respond to pings, consider it successful
                return True
            finally:
                self._udp_socket.setblocking(False)
                
        except Exception as e:
            self.logger().error("Connection test failed: %s", e)
            return False

    def _close_socket(self):
        """Close the UDP socket safely"""
        if self._udp_socket is not None:
            try:
                self._udp_socket.close()
            except Exception:
                pass
            finally:
                self._udp_socket = None
                self._connection_state.is_connected = False

    # ========== Configuration ==========

    def configure(self, host: Optional[str] = None, port: Optional[int] = None, exchange_name: Optional[str] = None):
        """Configure the UDP manager with custom settings"""
        if self._connection_state.is_connected:
            self.logger().warning("Cannot configure while connected. Please disconnect first.")
            return
            
        if host is not None:
            self._host = host
        if port is not None:
            self._port = port
        if exchange_name is not None:
            self._exchange_name = exchange_name

    # ========== Subscription Management ==========

    async def subscribe_to_trading_pairs(self, trading_pairs: List[str]) -> Tuple[bool, List[str]]:
        """Subscribe to multiple trading pairs"""
        if not trading_pairs:
            return True, []
            
        successful_pairs = []
        
        for trading_pair in trading_pairs:
            try:
                if await self._subscribe_to_trading_pair(trading_pair):
                    successful_pairs.append(trading_pair)
            except Exception as e:
                self.logger().error(f"Error subscribing to {trading_pair}: {e}")
                
        return len(successful_pairs) == len(trading_pairs), successful_pairs

    async def _subscribe_to_trading_pair(self, trading_pair: str) -> bool:
        """Subscribe to a single trading pair"""
        try:
            # Check if already subscribed
            if trading_pair in self._subscriptions and self._subscriptions[trading_pair].is_active:
                return True
                
            # Convert to QTX format
            if self._exchange_name is None:
                self.logger().error(f"Exchange name not set - cannot convert {trading_pair} to QTX format")
                return False
                
            qtx_symbol = qtx_perpetual_trading_pair_utils.convert_to_qtx_trading_pair(trading_pair, self._exchange_name)
            
            # Subscribe via UDP
            return await self._subscribe_to_symbol(qtx_symbol, trading_pair)
            
        except Exception as e:
            self.logger().error(f"Failed to subscribe to {trading_pair}: {e}")
            return False

    async def _subscribe_to_symbol(self, qtx_symbol: str, trading_pair: str) -> bool:
        """Subscribe to a QTX symbol"""
        max_attempts = 3
        
        for attempt in range(1, max_attempts + 1):
            try:
                if not self._connection_state.is_connected or self._udp_socket is None:
                    if not await self.connect():
                        continue
                        
                # Send subscription request
                self._udp_socket.sendto(qtx_symbol.encode("utf-8"), (self._host, self._port))
                
                # Wait for response
                self._udp_socket.setblocking(True)
                self._udp_socket.settimeout(5.0)
                
                try:
                    response, addr = self._udp_socket.recvfrom(self._buffer_size)
                    
                    # Parse response
                    try:
                        response_str = response.decode("utf-8").strip()
                        # Parse new format: "index:symbol" (e.g., "9:binance-futures:ethusdt")
                        index_str, returned_symbol = response_str.split(":", 1)
                        index = int(index_str)
                        # Verify the returned symbol matches what we subscribed to
                        if returned_symbol != qtx_symbol:
                            self.logger().warning(f"Subscribed to {qtx_symbol} but received confirmation for {returned_symbol}")
                        
                        # Create or update subscription
                        if trading_pair in self._subscriptions:
                            subscription = self._subscriptions[trading_pair]
                            subscription.index = index
                            subscription.is_active = True
                        else:
                            subscription = SubscriptionState(
                                trading_pair=trading_pair,
                                qtx_symbol=qtx_symbol,
                                index=index
                            )
                            self._subscriptions[trading_pair] = subscription
                            
                        self._index_to_pair[index] = trading_pair
                        
                        # Initialize update_id tracking
                        self._last_update_id[trading_pair] = int(time.time() * 1000)
                        
                        self.logger().info(f"Subscribed to {qtx_symbol} with index {index}")
                        return True
                        
                    except (ValueError, UnicodeDecodeError) as e:
                        self.logger().error(f"Invalid subscription response for {qtx_symbol}: {e}")
                        continue
                        
                except socket.timeout:
                    self.logger().warning(f"Subscription timeout for {qtx_symbol} (attempt {attempt}/{max_attempts})")
                    continue
                    
                finally:
                    self._udp_socket.setblocking(False)
                    
            except Exception as e:
                self.logger().error(f"Subscription error for {qtx_symbol} (attempt {attempt}/{max_attempts}): {e}")
                
        return False

    async def unsubscribe_from_trading_pairs(self, trading_pairs: List[str]) -> bool:
        """Unsubscribe from trading pairs"""
        if not trading_pairs:
            return True
            
        results = []
        for trading_pair in trading_pairs:
            if trading_pair not in self._subscriptions:
                results.append(True)
                continue
                
            subscription = self._subscriptions[trading_pair]
            results.append(await self._unsubscribe_from_symbol(subscription.qtx_symbol))
                
        return all(results)

    async def _unsubscribe_from_symbol(self, qtx_symbol: str) -> bool:
        """Unsubscribe from a QTX symbol"""
        try:
            if not self._connection_state.is_connected or self._udp_socket is None:
                return False
                
            # Send unsubscribe request
            unsubscribe_msg = f"-{qtx_symbol}"
            self._udp_socket.sendto(unsubscribe_msg.encode("utf-8"), (self._host, self._port))
            
            # Clean up tracking
            pairs_to_remove = []
            for trading_pair, subscription in self._subscriptions.items():
                if subscription.qtx_symbol == qtx_symbol:
                    subscription.is_active = False
                    pairs_to_remove.append(trading_pair)
                    
                    if subscription.index in self._index_to_pair:
                        del self._index_to_pair[subscription.index]
                        
            for trading_pair in pairs_to_remove:
                del self._subscriptions[trading_pair]
                # Clear message queues
                if trading_pair in self._message_queues:
                    del self._message_queues[trading_pair]
                # Clear tracking
                self._last_update_id.pop(trading_pair, None)
                
            self.logger().info(f"Unsubscribed from {qtx_symbol}")
            return True
            
        except Exception as e:
            self.logger().error(f"Failed to unsubscribe from {qtx_symbol}: {e}")
            return False

    # ========== Message Processing ==========

    async def _listen_for_messages(self):
        """Main listening loop"""
        self.logger().info("Starting UDP message listener")
        recv_buffer = bytearray(self._buffer_size)
        
        loop = asyncio.get_event_loop()
        consecutive_errors = 0
        max_consecutive_errors = 10
        
        try:
            while True:
                try:
                    # Check connection
                    if self._udp_socket is None:
                        await asyncio.sleep(self._get_reconnect_delay())
                        if not await self.connect():
                            consecutive_errors += 1
                            if consecutive_errors >= max_consecutive_errors:
                                break
                            continue
                        consecutive_errors = 0
                    
                    # Check for data
                    ready_to_read, _, _ = select.select([self._udp_socket], [], [], 0)
                    
                    if ready_to_read:
                        try:
                            # Read data
                            self._udp_socket.setblocking(False)
                            bytes_read, addr = await loop.sock_recvfrom_into(self._udp_socket, recv_buffer)
                            
                            if bytes_read > 0:
                                # Process message
                                message_data = recv_buffer[:bytes_read]
                                await self._process_message(message_data)
                                
                                # Update connection health
                                self._connection_state.last_message_time = time.time()
                                consecutive_errors = 0
                                
                        except (BlockingIOError, socket.error) as e:
                            if e.errno not in (socket.errno.EAGAIN, socket.errno.EWOULDBLOCK):
                                raise
                        except Exception as e:
                            consecutive_errors += 1
                            self.logger().error(f"Message processing error: {e}")
                    else:
                        await asyncio.sleep(0)
                        
                except (ConnectionError, OSError) as e:
                    if self._udp_socket is None:
                        break
                    self.logger().error(f"Connection lost: {e}")
                    self._close_socket()
                    await asyncio.sleep(self._get_reconnect_delay())
                    await self.connect()
                    
                except asyncio.CancelledError:
                    raise
                    
                except Exception as e:
                    consecutive_errors += 1
                    self.logger().error(f"Unexpected error in message listener: {e}")
                    
                    if consecutive_errors >= max_consecutive_errors:
                        self.logger().error("Too many consecutive errors, stopping listener")
                        break
                        
                    await asyncio.sleep(1)
                    
        except asyncio.CancelledError:
            pass
        finally:
            self.logger().info("UDP message listener stopped")

    async def _process_message(self, data: bytes):
        """Process a received UDP message and route to appropriate queue"""
        try:
            # Check minimum message size
            if len(data) < 40:
                return
                
            # Parse header
            msg_type, index, update_id, timestamp_ns, recv_time_ns, send_time_ns = struct.unpack("<iiqqqq", data[:40])
            
            # Convert timestamp
            timestamp = timestamp_ns / 1e9
            
            # Look up trading pair
            trading_pair = self._index_to_pair.get(index)
            if not trading_pair:
                return
                
            # Update subscription state
            if trading_pair in self._subscriptions:
                subscription = self._subscriptions[trading_pair]
                subscription.last_message_time = time.time()
                
                # Ensure update_id is always increasing
                if trading_pair in self._last_update_id:
                    update_id = max(update_id, self._last_update_id[trading_pair] + 1)
                self._last_update_id[trading_pair] = update_id
                subscription.last_update_id = update_id
                
            # Process by message type and send directly to queue
            if abs(msg_type) == 1:  # Ticker
                message = await self._create_ticker_message(data, trading_pair, update_id, timestamp)
                if message:
                    await self._send_to_queue(trading_pair, "diff", message)
            elif msg_type == 2:  # Depth
                message = await self._create_depth_message(data, trading_pair, update_id, timestamp)
                if message:
                    # All depth messages go to diff queue
                    await self._send_to_queue(trading_pair, "diff", message)
            elif abs(msg_type) == 3:  # Trade
                message = await self._create_trade_message(data, trading_pair, update_id, timestamp)
                if message:
                    await self._send_to_queue(trading_pair, "trade", message)
                
        except struct.error as e:
            self.logger().error(f"Message unpacking error: {e}")
        except Exception as e:
            self.logger().error(f"Message processing error: {e}")

    async def _create_ticker_message(self, data: bytes, trading_pair: str, update_id: int, timestamp: float) -> Optional[OrderBookMessage]:
        """Process ticker message"""
        try:
            if len(data) < 56:
                return None
                
            # Parse ticker data
            msg_type = struct.unpack("<i", data[:4])[0]
            price, size = struct.unpack("<dd", data[40:56])
            
            # Determine if bid or ask
            is_bid = msg_type > 0
            
            # Create order book message
            content = {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "bids": [[price, size]] if is_bid and size > 0 else [],
                "asks": [[price, size]] if not is_bid and size > 0 else [],
            }
            
            return OrderBookMessage(
                message_type=OrderBookMessageType.DIFF,
                content=content,
                timestamp=timestamp
            )
            
        except Exception as e:
            self.logger().error(f"Ticker processing error for {trading_pair}: {e}")
            return None

    async def _create_depth_message(self, data: bytes, trading_pair: str, update_id: int, timestamp: float) -> Optional[OrderBookMessage]:
        """
        Process depth message. 
        
        IMPORTANT: All depth messages are created as DIFF type.
        The derivative is responsible for treating the first one as a snapshot.
        """
        try:
            if len(data) < 56:
                return None
                
            # Parse counts
            asks_len, bids_len = struct.unpack("<qq", data[40:56])
            
            # Validate message size
            entry_size = 16
            expected_size = 56 + (asks_len + bids_len) * entry_size
            if len(data) < expected_size:
                return None
                
            # Parse entries
            offset = 56
            asks = []
            bids = []
            
            # Parse asks
            for i in range(asks_len):
                price, size = struct.unpack("<dd", data[offset:offset + entry_size])
                if size > 0:
                    asks.append([price, size])
                offset += entry_size
                
            # Parse bids
            for i in range(bids_len):
                price, size = struct.unpack("<dd", data[offset:offset + entry_size])
                if size > 0:
                    bids.append([price, size])
                offset += entry_size
                
            # Create order book message - always DIFF type
            content = {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "bids": bids,
                "asks": asks,
            }
            
            return OrderBookMessage(
                message_type=OrderBookMessageType.DIFF,
                content=content,
                timestamp=timestamp
            )
            
        except Exception as e:
            self.logger().error(f"Depth processing error for {trading_pair}: {e}")
            return None

    async def _create_trade_message(self, data: bytes, trading_pair: str, update_id: int, timestamp: float) -> Optional[OrderBookMessage]:
        """Process trade message"""
        try:
            if len(data) < 56:
                return None
                
            # Parse trade data
            msg_type = struct.unpack("<i", data[:4])[0]
            price, size = struct.unpack("<dd", data[40:56])
            
            # Determine if buy or sell
            is_buy = msg_type > 0
            
            # Create trade message
            content = {
                "trading_pair": trading_pair,
                "trade_type": float(TradeType.BUY.value if is_buy else TradeType.SELL.value),
                "trade_id": update_id,
                "update_id": update_id,
                "price": str(price),
                "amount": str(size),
                "trade_timestamp": timestamp,
            }
            
            return OrderBookMessage(
                message_type=OrderBookMessageType.TRADE,
                content=content,
                timestamp=timestamp
            )
            
        except Exception as e:
            self.logger().error(f"Trade processing error for {trading_pair}: {e}")
            return None

    async def _send_to_queue(self, trading_pair: str, message_type: str, message: OrderBookMessage):
        """Send message to appropriate queue"""
        try:
            queue = self.get_message_queue(trading_pair, message_type)
            # Non-blocking put with error handling
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Drop oldest message and retry
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except asyncio.QueueEmpty:
                    pass
        except Exception as e:
            self.logger().error(f"Queue send error for {trading_pair}: {e}")

    # ========== Health Monitoring ==========

    async def _connection_health_check(self):
        """Monitor connection health"""
        try:
            while self._connection_state.is_connected:
                await asyncio.sleep(30)  # Check every 30 seconds
                
                # Check if we've received messages recently
                time_since_last_message = time.time() - self._connection_state.last_message_time
                
                if time_since_last_message > 60 and self._subscriptions:
                    self.logger().warning(f"No messages received for {int(time_since_last_message)}s")
                    
                    # Test connection
                    if not await self._test_connection():
                        self.logger().error("Connection health check failed, reconnecting...")
                        await self.connect()
                        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger().error(f"Health check error: {e}")

    def _get_reconnect_delay(self) -> float:
        """Calculate reconnect delay with exponential backoff"""
        delay = min(
            self._reconnect_delay * (2 ** self._connection_state.consecutive_failures),
            self._max_reconnect_delay
        )
        # Add jitter
        jitter = random.uniform(0, delay * 0.1)
        return delay + jitter

    # ========== Public Interface ==========

    async def start(self):
        """Start the UDP manager"""
        if not self._connection_state.is_connected:
            if not await self.connect():
                raise RuntimeError("Failed to establish UDP connection")
                
        if self._listen_task is None or self._listen_task.done():
            self._listen_task = asyncio.create_task(self._listen_for_messages())
            self.logger().info("UDP message listener started")

    async def stop(self):
        """Stop the UDP manager"""
        self.logger().info("Stopping QTX UDP manager")
        
        async with self._lock:
            # Cancel tasks
            for task in [self._listen_task, self._health_check_task]:
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            
            # Unsubscribe from all symbols
            if self._subscriptions:
                active_symbols = {sub.qtx_symbol for sub in self._subscriptions.values() if sub.is_active}
                for qtx_symbol in active_symbols:
                    try:
                        await self._unsubscribe_from_symbol(qtx_symbol)
                    except Exception as e:
                        self.logger().error(f"Unsubscribe error for {qtx_symbol}: {e}")
            
            # Close socket and clear data
            self._close_socket()
            self._subscriptions.clear()
            self._index_to_pair.clear()
            self._message_queues.clear()
            self._last_update_id.clear()
            
            self.logger().info("QTX UDP manager stopped")

    async def build_orderbook_snapshot(self, trading_pair: str, duration: float = 5.0) -> Dict[str, Any]:
        """
        Builds an orderbook snapshot by collecting depth messages over a specified duration.
        
        This method is essential because QTX doesn't send snapshot messages.
        It collects depth updates over time to build a complete order book state.
        
        :param trading_pair: Trading pair in Hummingbot format (e.g. BTC-USDT)
        :param duration: Duration in seconds to collect data for (defaults to 5 seconds)
        :return: Dictionary with collected market data: bids, asks, update_id, timestamp
        """
        # Check if we need to subscribe to this trading pair
        if trading_pair not in self._subscriptions or not self._subscriptions[trading_pair].is_active:
            success = await self._subscribe_to_trading_pair(trading_pair)
            if not success:
                self.logger().error(f"Failed to subscribe to {trading_pair}")
                return {
                    "trading_pair": trading_pair,
                    "bids": [],
                    "asks": [],
                    "update_id": int(time.time() * 1000),
                    "timestamp": time.time(),
                }
        
        # Initialize collection variables
        bids_dict: Dict[Decimal, Decimal] = {}  # price -> size
        asks_dict: Dict[Decimal, Decimal] = {}  # price -> size
        collection_start = time.time()
        message_count = 0
        latest_update_id = self._last_update_id.get(trading_pair, int(collection_start * 1000))
        
        # Get the diff queue for this trading pair
        diff_queue = self.get_message_queue(trading_pair, "diff")
        
        # Collect messages for the specified duration
        end_time = time.time() + duration
        collected_messages = []
        
        while time.time() < end_time:
            try:
                # Try to get a message with a short timeout
                remaining_time = end_time - time.time()
                if remaining_time <= 0:
                    break
                    
                timeout = min(0.1, remaining_time)
                message = await asyncio.wait_for(diff_queue.get(), timeout=timeout)
                
                # Store the message to put back later
                collected_messages.append(message)
                
                # Process the message to update our order book state
                if message.type == OrderBookMessageType.DIFF:
                    content = message.content
                    message_count += 1
                    
                    # Update bids
                    for price, size in content.get("bids", []):
                        price_dec = Decimal(str(price))
                        size_dec = Decimal(str(size))
                        if size_dec > 0:
                            bids_dict[price_dec] = size_dec
                        else:
                            bids_dict.pop(price_dec, None)
                    
                    # Update asks
                    for price, size in content.get("asks", []):
                        price_dec = Decimal(str(price))
                        size_dec = Decimal(str(size))
                        if size_dec > 0:
                            asks_dict[price_dec] = size_dec
                        else:
                            asks_dict.pop(price_dec, None)
                    
                    # Update latest update_id
                    latest_update_id = max(latest_update_id, content.get("update_id", 0))
                    
            except asyncio.TimeoutError:
                # No message available, continue
                continue
            except Exception as e:
                self.logger().error(f"Error collecting message: {e}")
                continue
        
        # Put collected messages back to the queue
        for message in collected_messages:
            try:
                diff_queue.put_nowait(message)
            except:
                pass
                
        # Convert dictionaries to sorted lists
        bids = [[float(price), float(size)] for price, size in bids_dict.items()]
        bids.sort(key=lambda x: x[0], reverse=True)
        
        asks = [[float(price), float(size)] for price, size in asks_dict.items()]
        asks.sort(key=lambda x: x[0])
        
        return {
            "trading_pair": trading_pair,
            "bids": bids,
            "asks": asks,
            "update_id": latest_update_id,
            "timestamp": time.time(),
        }