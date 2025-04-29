import asyncio
import socket
import struct
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from hummingbot.connector.exchange.qtx import qtx_constants as CONSTANTS, qtx_utils, qtx_web_utils as web_utils
from hummingbot.connector.exchange.qtx.qtx_order_book import QTXOrderBook
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.in_flight_order import OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.event.events import MarketEvent, OrderFilledEvent  # For triggering events
from hummingbot.core.utils.async_utils import safe_gather  # Add import for safe_gather
from hummingbot.core.web_assistant.ws_assistant import WSAssistant  # <<< Import Added
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.qtx.qtx_exchange import QtxExchange


class QTXAPIOrderBookDataSource(OrderBookTrackerDataSource):
    MESSAGE_TIMEOUT = 30.0
    SNAPSHOT_TIMEOUT = 10.0

    _logger: Optional[HummingbotLogger] = None

    def __init__(self, trading_pairs: List[str], connector: "QtxExchange"):
        super().__init__(trading_pairs)
        self._connector = connector
        self._trade_messages_queue_key = CONSTANTS.TRADE_EVENT_TYPE
        self._diff_messages_queue_key = CONSTANTS.DIFF_EVENT_TYPE
        self._snapshot_messages_queue_key = CONSTANTS.SNAPSHOT_EVENT_TYPE
        
        # Initialize message queue dictionary if not already created
        self._message_queue = {}
        self._message_queue[self._trade_messages_queue_key] = asyncio.Queue()
        self._message_queue[self._diff_messages_queue_key] = asyncio.Queue()
        self._message_queue[self._snapshot_messages_queue_key] = asyncio.Queue()
        
        # Set the order book create function to use QTXOrderBook
        self._order_book_create_function = lambda: QTXOrderBook()
        
        # UDP connection settings
        self._qtx_host = self._connector._qtx_host
        self._qtx_port = self._connector._qtx_port
        self._socket = None
        self._last_update_id = {}  # Track update_id by trading pair
        self._empty_orderbook = {}  # Track empty orderbook snapshots by trading pair
        
        # For trading pair mapping
        self._exchange_trading_pairs = {}  # Map from exchange format to hummingbot format
        self._subscription_indices = {}  # Map from subscription index to trading pair
        
        self.logger().info(f"QTX API Order Book Data Source initialized with UDP host: {self._qtx_host}, port: {self._qtx_port}")

    async def get_last_traded_prices(self, trading_pairs: List[str], domain: Optional[str] = None) -> Dict[str, float]:
        """
        Get last traded prices from the order book snapshots
        """
        result = {}
        for trading_pair in trading_pairs:
            # Try to get the last trade price from the order book
            order_book = self._connector.order_book_tracker.order_books.get(trading_pair)
            if order_book is not None and order_book.last_trade_price is not None:
                result[trading_pair] = float(order_book.last_trade_price)
            else:
                # Default price if no trades yet
                result[trading_pair] = 0.0
                
        return result

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        """
        Create an empty initial snapshot for a trading pair.
        Real data will be populated through UDP messages.
        """
        try:
            snapshot_timestamp = time.time()
            update_id = int(snapshot_timestamp * 1000)

            # Create an empty snapshot to be populated later
            self.logger().info(f"Creating empty initial snapshot for {trading_pair}")
            snapshot_data = {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "bids": [],  # Empty bids
                "asks": [],  # Empty asks
            }
            
            # Store the empty snapshot for this trading pair
            self._empty_orderbook[trading_pair] = True
            self._last_update_id[trading_pair] = update_id
            
            snapshot_msg = QTXOrderBook.snapshot_message_from_exchange(
                snapshot_data, snapshot_timestamp, metadata={"trading_pair": trading_pair}
            )
            return snapshot_msg

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().error(f"Error generating initial snapshot for {trading_pair}: {str(e)}", exc_info=True)
            return QTXOrderBook.snapshot_message_from_exchange(
                {"trading_pair": trading_pair, "update_id": int(time.time() * 1000), "bids": [], "asks": []},
                time.time(), metadata={"trading_pair": trading_pair}
            )

    # Required methods by base class
    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        Returns a dummy WSAssistant instance. Not used for UDP connection.
        """
        return WSAssistant(auth=None, api_factory=None)

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Not used in UDP implementation. Pass to satisfy interface.
        """
        pass
    
    def _convert_to_exchange_trading_pair(self, trading_pair: str) -> str:
        """
        Convert Hummingbot trading pair format to QTX format
        Format: "BTC-USDT" -> "binance:btcusdt"
        """
        # For simplicity, default to binance
        exchange = "binance"
        
        # Special cases for common pairs that might be futures
        if trading_pair.endswith("-PERP"):
            exchange = "binance-futures"
            # Remove the -PERP suffix for the pair part
            pair = trading_pair[:-5].replace("-", "").lower()
        else:
            # Regular spot pair
            pair = trading_pair.replace("-", "").lower()
        
        self.logger().info(f"Converting {trading_pair} to exchange format: {exchange}:{pair}")
        return f"{exchange}:{pair}"
    
    def _parse_binary_message(self, data: bytes) -> Optional[Dict[str, Any]]:
        """
        Parse binary message from UDP feed based on the format specified in the implementation guide.
        
        Binary format includes header with type, index, timestamps, and sequence number.
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
            if abs(msg_type) == 1:  # Ticker (Bid = 1, Ask = -1)
                return self._parse_ticker_binary(data, trading_pair, msg_type > 0)
            elif msg_type == 2:  # Depth (Order Book)
                return self._parse_depth_binary(data, trading_pair)
            elif abs(msg_type) == 3:  # Trade (Buy = 3, Sell = -3)
                return self._parse_trade_binary(data, trading_pair, msg_type > 0)
            else:
                self.logger().warning(f"Unknown message type: {msg_type}")
                return None
        except Exception as e:
            self.logger().error(f"Error parsing binary message: {e}", exc_info=True)
            return None
    
    def _parse_ticker_binary(self, data: bytes, trading_pair: str, is_bid: bool) -> Optional[Dict[str, Any]]:
        """
        Parse ticker data from binary format
        Used to update best bid/ask but not for order book messages
        """
        try:
            # Extract price and size (offset 40 for start of data after header)
            price, size = struct.unpack("<dd", data[40:56])
            
            # Create ticker message (used to update best bid/ask in memory)
            timestamp = time.time()
            update_id = int(timestamp * 1000)
            
            # Ensure update_id is always increasing for this trading pair
            if trading_pair in self._last_update_id:
                update_id = max(update_id, self._last_update_id[trading_pair] + 1)
            self._last_update_id[trading_pair] = update_id
            
            # For ticker data, create a diff message with single level
            side = "bid" if is_bid else "ask"
            msg = {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "timestamp": timestamp,
                "bids": [[price, size]] if is_bid else [],
                "asks": [] if is_bid else [[price, size]]
            }
            
            # Log ticker for debugging
            self.logger().debug(f"Ticker: {side}, {trading_pair}, price: {price}, size: {size}")
            return msg
            
        except Exception as e:
            self.logger().error(f"Error parsing ticker: {e}", exc_info=True)
            return None
    
    def _parse_depth_binary(self, data: bytes, trading_pair: str) -> Optional[Dict[str, Any]]:
        """
        Parse depth (order book) data from binary format
        This contains full order book information for a symbol
        """
        try:
            # Get ask and bid counts (header is 40 bytes, followed by counts)
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
                    price, size = struct.unpack("<dd", data[offset:offset+16])
                    asks.append([price, size])
                    offset += 16  # Each price/size pair is 16 bytes
            
            # Parse bids
            for i in range(bids_len):
                if offset + 16 <= len(data):
                    price, size = struct.unpack("<dd", data[offset:offset+16])
                    bids.append([price, size])
                    offset += 16  # Each price/size pair is 16 bytes
            
            # Create message with order book data
            timestamp = time.time()
            update_id = int(timestamp * 1000)
            
            # Ensure update_id is always increasing for this trading pair
            if trading_pair in self._last_update_id:
                update_id = max(update_id, self._last_update_id[trading_pair] + 1)
            self._last_update_id[trading_pair] = update_id
            
            # Sort bids (desc) and asks (asc)
            bids.sort(key=lambda x: float(x[0]), reverse=True)
            asks.sort(key=lambda x: float(x[0]))
            
            # Create snapshot or diff message based on whether we have an empty order book
            message_type = "snapshot" if trading_pair in self._empty_orderbook and self._empty_orderbook[trading_pair] else "diff"
            
            # If this is the first real data, mark the order book as no longer empty
            if trading_pair in self._empty_orderbook and self._empty_orderbook[trading_pair]:
                self._empty_orderbook[trading_pair] = False
                self.logger().info(f"Received first depth data for {trading_pair}: asks: {len(asks)}, bids: {len(bids)}")
            
            msg = {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "timestamp": timestamp,
                "bids": bids,
                "asks": asks,
                "message_type": message_type
            }
            
            self.logger().debug(f"Depth: {trading_pair}, asks: {len(asks)}, bids: {len(bids)}")
            return msg
            
        except Exception as e:
            self.logger().error(f"Error parsing depth: {e}", exc_info=True)
            return None
    
    def _parse_trade_binary(self, data: bytes, trading_pair: str, is_buy: bool) -> Optional[Dict[str, Any]]:
        """
        Parse trade data from binary format
        """
        try:
            # Extract price and size (offset 40 for start of data after header)
            price, size = struct.unpack("<dd", data[40:56])
            
            # Create trade message
            timestamp = time.time()
            trade_id = str(int(timestamp * 1000000))  # Use timestamp as trade ID
            side = "BUY" if is_buy else "SELL"
            
            msg = {
                "trading_pair": trading_pair,
                "trade_id": trade_id,
                "price": price,
                "amount": size,
                "side": side,
                "timestamp": timestamp
            }
            
            self.logger().debug(f"Trade: {side}, {trading_pair}, price: {price}, size: {size}")
            return msg
            
        except Exception as e:
            self.logger().error(f"Error parsing trade: {e}", exc_info=True)
            return None
    
    def _convert_from_exchange_trading_pair(self, exchange_trading_pair: str) -> Optional[str]:
        """
        Convert exchange trading pair format to Hummingbot format
        Format is typically: "exchange:symbol" (e.g., "binance:btcusdt")
        """
        try:
            # Check if we've already converted this pair
            if exchange_trading_pair in self._exchange_trading_pairs:
                return self._exchange_trading_pairs[exchange_trading_pair]
                
            # Split by colon to get exchange and pair
            if ":" not in exchange_trading_pair:
                return None
                
            exchange, pair = exchange_trading_pair.split(":")
            result = None
            
            # Special case for futures
            if exchange == "binance-futures":
                # Add -PERP suffix for perpetual futures
                # Try to intelligently parse the trading pair
                # Look for common base assets
                common_base_assets = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOT", "ADA"]
                for base in common_base_assets:
                    if base.lower() in pair.lower():
                        quote = pair.lower().replace(base.lower(), "").upper()
                        if quote:
                            result = f"{base}-{quote}-PERP"
                            break
                
                if result is None:
                    self.logger().warning(f"Could not convert futures pair: {exchange_trading_pair}")
                    # Check if we can find any common quote assets
                    common_quote_assets = ["USDT", "USD", "BUSD", "USDC"]
                    for quote in common_quote_assets:
                        if quote.lower() in pair.lower():
                            base = pair.lower().replace(quote.lower(), "").upper()
                            if base:
                                result = f"{base}-{quote}-PERP"
                                break
            else:
                # Regular spot pairs
                # First, check for configured trading pairs from Hummingbot
                for trading_pair in self._trading_pairs:
                    formatted_pair = trading_pair.replace("-", "").lower()
                    if formatted_pair == pair.lower():
                        result = trading_pair
                        break
                
                # If not found in configured pairs, try intelligent parsing
                if result is None:
                    # Try to intelligently parse the trading pair
                    common_base_assets = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOT", "ADA"]
                    for base in common_base_assets:
                        if base.lower() in pair.lower():
                            quote = pair.lower().replace(base.lower(), "").upper()
                            if quote:
                                result = f"{base}-{quote}"
                                break
                
                # If still not found, try looking for common quote assets
                if result is None:
                    common_quote_assets = ["USDT", "USD", "BUSD", "USDC", "BTC", "ETH"]
                    for quote in common_quote_assets:
                        if quote.lower() in pair.lower():
                            base = pair.lower().replace(quote.lower(), "").upper()
                            if base:
                                result = f"{base}-{quote}"
                                break
            
            # If we still couldn't parse it, log and return None
            if result is None:
                self.logger().warning(f"Could not convert exchange pair: {exchange_trading_pair}")
                return None
                
            # Cache the conversion
            self._exchange_trading_pairs[exchange_trading_pair] = result
            self.logger().info(f"Mapped {exchange_trading_pair} to {result}")
            
            return result
            
        except Exception as e:
            self.logger().error(f"Error converting exchange trading pair: {e}", exc_info=True)
            return None

    async def listen_for_subscriptions(self):
        """
        Connects to the QTX UDP feed, subscribes to trading pairs, and processes incoming market data.
        """
        self.logger().info(f"Starting UDP connection to {self._qtx_host}:{self._qtx_port}")
        
        # Initialize message queues if they don't exist
        if not hasattr(self, "_message_queue"):
            self._message_queue = {}
        
        if self._trade_messages_queue_key not in self._message_queue:
            self.logger().info(f"Initializing {self._trade_messages_queue_key} message queue")
            self._message_queue[self._trade_messages_queue_key] = asyncio.Queue()
        
        if self._diff_messages_queue_key not in self._message_queue:
            self.logger().info(f"Initializing {self._diff_messages_queue_key} message queue")
            self._message_queue[self._diff_messages_queue_key] = asyncio.Queue()
            
        if self._snapshot_messages_queue_key not in self._message_queue:
            self.logger().info(f"Initializing {self._snapshot_messages_queue_key} message queue")
            self._message_queue[self._snapshot_messages_queue_key] = asyncio.Queue()
        
        # Generate initial snapshots (empty) for order books
        try:
            self.logger().info("Generating initial empty order book snapshots...")
            for trading_pair in self._trading_pairs:
                self._empty_orderbook[trading_pair] = True
                snapshot_message = await self._order_book_snapshot(trading_pair)
                self.logger().info(f"Created initial empty snapshot for {trading_pair}")
                self._message_queue[self._snapshot_messages_queue_key].put_nowait(snapshot_message)
            self.logger().info("Initial empty snapshots generated successfully")
        except Exception as e:
            self.logger().error(f"Error generating initial snapshots: {e}", exc_info=True)
        
        # Create UDP socket
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        try:
            # Bind to any address on an automatic port for receiving data
            self._socket.bind(('0.0.0.0', 0))
            self.logger().info(f"UDP socket created and bound to local port")
            
            # Subscribe to symbols
            self._subscription_indices = {}
            subscription_success = False

            for trading_pair in self._trading_pairs:
                qtx_symbol = self._convert_to_exchange_trading_pair(trading_pair)
                self.logger().info(f"Subscribing to symbol: {qtx_symbol} for {trading_pair}")
                try:
                    # Send subscription request
                    self._socket.sendto(qtx_symbol.encode(), (self._qtx_host, self._qtx_port))
                    # Temporarily set socket to blocking for ACK
                    self._socket.setblocking(True)
                    self._socket.settimeout(5.0)
                    deadline = time.time() + 5.0
                    index = None
                    while time.time() < deadline:
                        try:
                            response, addr = self._socket.recvfrom(CONSTANTS.DEFAULT_UDP_BUFFER_SIZE)
                        except socket.timeout:
                            self.logger().error(f"Timeout waiting for ACK for {qtx_symbol}")
                            break
                        # ignore any packet not from the server
                        if addr[0] != self._qtx_host:
                            continue
                        try:
                            idx = int(response.decode().strip())
                            index = idx
                            self.logger().info(f"Got ACK {idx} for {qtx_symbol}")
                            break
                        except (UnicodeDecodeError, ValueError):
                            continue
                    if index is None:
                        self.logger().error(f"No valid ACK for {qtx_symbol}")
                        continue
                    self._subscription_indices[index] = trading_pair
                    subscription_success = True
                    self.logger().info(f"Successfully subscribed to {qtx_symbol} with index {index}")
                except Exception as e:
                    self.logger().error(f"Error subscribing to {qtx_symbol}: {e}")
                finally:
                    # Restore non-blocking mode
                    self._socket.setblocking(False)
                # Small delay before next subscription
                await asyncio.sleep(0.1)

            # Check subscription outcome
            if not subscription_success:
                self.logger().error("No successful subscriptions, UDP feed may not work properly")
            else:
                self.logger().info(f"Subscribed to {len(self._subscription_indices)} symbols: {self._subscription_indices}")
            
            # Set socket back to non-blocking for message handling
            self._socket.setblocking(False)
            
            # Start receiving UDP data
            try:
                while True:
                    try:
                        # Use asyncio to make UDP reading non-blocking
                        data = await asyncio.get_event_loop().sock_recv(self._socket, CONSTANTS.DEFAULT_UDP_BUFFER_SIZE)
                        
                        # Parse binary message
                        message = self._parse_binary_message(data)
                        
                        if message is not None:
                            # Route message to appropriate queue based on type
                            try:
                                if "trade_id" in message:
                                    # Process trade message
                                    trade_msg = QTXOrderBook.trade_message_from_exchange(
                                        message,
                                        {"trading_pair": message.get("trading_pair")}
                                    )
                                    self._message_queue[self._trade_messages_queue_key].put_nowait(trade_msg)
                                elif "message_type" in message and message["message_type"] == "snapshot":
                                    # Process snapshot message
                                    snapshot_msg = QTXOrderBook.snapshot_message_from_exchange(
                                        message,
                                        message.get("timestamp", time.time()),
                                        {"trading_pair": message.get("trading_pair")}
                                    )
                                    self._message_queue[self._snapshot_messages_queue_key].put_nowait(snapshot_msg)
                                elif "bids" in message or "asks" in message:
                                    # Process diff message
                                    diff_msg = QTXOrderBook.diff_message_from_exchange(
                                        message,
                                        message.get("timestamp", time.time()),
                                        {"trading_pair": message.get("trading_pair")}
                                    )
                                    self._message_queue[self._diff_messages_queue_key].put_nowait(diff_msg)
                                else:
                                    self.logger().debug(f"Received message with no recognized format: {message}")
                            except Exception as e:
                                self.logger().error(f"Error routing message: {e}, message: {message}")
                    
                    except asyncio.CancelledError:
                        raise
                    except BlockingIOError:
                        # No data available, just wait a bit
                        await asyncio.sleep(0.01)
                    except Exception as e:
                        self.logger().error(f"Error in UDP message processing: {e}", exc_info=True)
                        await asyncio.sleep(0.1)
                        
            except asyncio.CancelledError:
                # Unsubscribe from symbols on cancellation
                for trading_pair in self._trading_pairs:
                    qtx_symbol = self._convert_to_exchange_trading_pair(trading_pair)
                    unsubscribe_msg = f"-{qtx_symbol}"
                    self.logger().info(f"Unsubscribing from symbol: {qtx_symbol}")
                    self._socket.sendto(unsubscribe_msg.encode(), (self._qtx_host, self._qtx_port))
                raise
                
        except asyncio.CancelledError:
            self.logger().info("UDP listener cancelled")
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            raise
        except Exception as e:
            self.logger().error(f"Unexpected error in UDP listener: {e}", exc_info=True)
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            raise

    async def _parse_order_book_snapshot_message(self, raw_message, message_queue: asyncio.Queue):
        """
        Parse a raw order book snapshot message and put it into the message queue.
        """
        # If the message is already an OrderBookMessage, just put it in the queue
        if isinstance(raw_message, OrderBookMessage):
            await message_queue.put(raw_message)
            return
        
        # Add validation to ensure raw_message is a dictionary
        if not isinstance(raw_message, dict):
            self.logger().error(f"Invalid order book snapshot message format: {type(raw_message)}")
            return
            
        trading_pair = raw_message.get("trading_pair")
        if trading_pair is None:
            self.logger().error("Order book snapshot message does not contain a trading pair")
            return

        timestamp = raw_message.get("timestamp", time.time())
        update_id = raw_message.get("update_id", int(timestamp * 1000))

        # Create a snapshot message
        snapshot_msg = QTXOrderBook.snapshot_message_from_exchange(
            raw_message,
            timestamp,
            {"trading_pair": trading_pair}
        )

        # Put the message in the queue
        await message_queue.put(snapshot_msg)

    async def _parse_trade_message(self, raw_message, message_queue: asyncio.Queue):
        """
        Parse a raw trade message and put it into the message queue.
        """
        # If the message is already an OrderBookMessage, just put it in the queue
        if isinstance(raw_message, OrderBookMessage):
            await message_queue.put(raw_message)
            return
        
        # Add validation to ensure raw_message is a dictionary
        if not isinstance(raw_message, dict):
            self.logger().error(f"Invalid trade message format: {type(raw_message)}")
            return
            
        trading_pair = raw_message.get("trading_pair")
        if trading_pair is None:
            self.logger().error("Trade message does not contain a trading pair")
            return

        timestamp = raw_message.get("timestamp", time.time())
        
        # Create a trade message
        trade_msg = QTXOrderBook.trade_message_from_exchange(
            raw_message,
            timestamp,
            {"trading_pair": trading_pair}
        )

        # Put the message in the queue
        await message_queue.put(trade_msg)

    async def _parse_order_book_diff_message(self, raw_message, message_queue: asyncio.Queue):
        """
        Parse a raw order book diff message and put it into the message queue.
        """
        # If the message is already an OrderBookMessage, just put it in the queue
        if isinstance(raw_message, OrderBookMessage):
            await message_queue.put(raw_message)
            return
        
        # Add validation to ensure raw_message is a dictionary
        if not isinstance(raw_message, dict):
            self.logger().error(f"Invalid order book diff message format: {type(raw_message)}")
            return
        
        trading_pair = raw_message.get("trading_pair")
        if trading_pair is None:
            self.logger().error("Order book diff message does not contain a trading pair")
            return

        timestamp = raw_message.get("timestamp", time.time())
        update_id = raw_message.get("update_id", int(timestamp * 1000))

        # Create a diff message
        diff_msg = QTXOrderBook.diff_message_from_exchange(
            raw_message,
            timestamp,
            {"trading_pair": trading_pair}
        )

        # Put the message in the queue
        await message_queue.put(diff_msg)

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        """
        Determine which channel a message belongs to based on its content.
        """
        if "trade_id" in event_message:
            return self._trade_messages_queue_key
        elif event_message.get("message_type") == "snapshot":
            return self._snapshot_messages_queue_key
        else:
            return self._diff_messages_queue_key

    async def _on_order_stream_interruption(self, websocket_assistant: Optional[WSAssistant] = None):
        """
        Not used for UDP connection, but required by the base class.
        """
        self.logger().warning("Order stream interruption handler called, not applicable to UDP")
        pass

    async def _check_and_simulate_fills(self,
                                        trading_pair: str,
                                        trade_price: Decimal,
                                        trade_size: Decimal,
                                        trade_type: TradeType,
                                        trade_id: str,
                                        timestamp: float):
        """
        Not used for real exchange data.
        """
        pass

    async def listen_for_trades(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        Listens to the trade messages in the trade queue.
        """
        message_queue = self._message_queue[self._trade_messages_queue_key]
        while True:
            try:
                trade_message = await message_queue.get()
                
                # trade_message should already be an OrderBookMessage type from our UDP processing loop
                if not isinstance(trade_message, OrderBookMessage):
                    self.logger().error(f"Invalid trade message in queue: {trade_message}")
                    continue
                
                # Forward the message to the output queue
                output.put_nowait(trade_message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in trade message processing: {e}", exc_info=True)
                await asyncio.sleep(0.5)

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        Listens to the snapshot messages in the snapshot queue.
        """
        message_queue = self._message_queue[self._snapshot_messages_queue_key]
        while True:
            try:
                snapshot_message = await message_queue.get()
                
                # snapshot_message should already be an OrderBookMessage type from our UDP processing loop
                if not isinstance(snapshot_message, OrderBookMessage):
                    self.logger().error(f"Invalid snapshot message in queue: {snapshot_message}")
                    continue
                
                # Forward the message to the output queue
                output.put_nowait(snapshot_message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in snapshot message processing: {e}", exc_info=True)
                await asyncio.sleep(0.5)
                
    async def listen_for_order_book_diffs(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        Listens to the order book diff messages in the diff queue.
        """
        message_queue = self._message_queue[self._diff_messages_queue_key]
        while True:
            try:
                diff_message = await message_queue.get()
                
                # diff_message should already be an OrderBookMessage type from our UDP processing loop
                if not isinstance(diff_message, OrderBookMessage):
                    self.logger().error(f"Invalid diff message in queue: {diff_message}")
                    continue
                
                # Forward the message to the output queue
                output.put_nowait(diff_message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in diff message processing: {e}", exc_info=True)
                await asyncio.sleep(0.5)