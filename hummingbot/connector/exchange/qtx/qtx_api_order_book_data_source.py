import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

from hummingbot.connector.exchange.qtx import qtx_constants as CONSTANTS, qtx_utils, qtx_web_utils as web_utils
from hummingbot.connector.exchange.qtx.qtx_order_book import QTXOrderBook
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.in_flight_order import OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TradeFeeBase, TokenAmount
from hummingbot.core.event.events import MarketEvent, OrderFilledEvent # For triggering events
from hummingbot.core.web_assistant.ws_assistant import WSAssistant # <<< Import Added
from hummingbot.core.utils.async_utils import safe_gather  # Add import for safe_gather
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
        self.logger().info("QTX API Order Book Data Source initialized")

    async def get_last_traded_prices(self, trading_pairs: List[str], domain: Optional[str] = None) -> Dict[str, float]:
        self.logger().warning("Using static last traded prices for testing.")
        return {pair: 10000.0 for pair in trading_pairs}

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        try:
            snapshot_timestamp = time.time()
            update_id = int(snapshot_timestamp * 1000)

            # Create a more complete and realistic snapshot
            snapshot_data = {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "bids": [[Decimal("9980.0"), Decimal("3.0")], 
                         [Decimal("9990.0"), Decimal("2.0")], 
                         [Decimal("10000.0"), Decimal("1.0")]],  # Multiple bid levels
                "asks": [[Decimal("10010.0"), Decimal("1.0")], 
                         [Decimal("10020.0"), Decimal("2.0")], 
                         [Decimal("10030.0"), Decimal("3.0")]],  # Multiple ask levels
            }
            
            snapshot_msg = QTXOrderBook.snapshot_message_from_exchange(
                snapshot_data, snapshot_timestamp, metadata={"trading_pair": trading_pair}
            )
            self.logger().info(f"Generated initial snapshot for {trading_pair}")
            return snapshot_msg

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().error(f"Error generating static snapshot for {trading_pair}: {str(e)}", exc_info=True)
            return QTXOrderBook.snapshot_message_from_exchange(
                {"trading_pair": trading_pair, "update_id": int(time.time() * 1000), "bids": [], "asks": []},
                time.time(), metadata={"trading_pair": trading_pair}
            )

    # Required methods by base class
    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        Returns a dummy WSAssistant instance. Not used, but might be required by base tracker logic.
        """
        # <<< Return dummy instance instead of pass
        return WSAssistant(auth=None, api_factory=None)

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Not used in this implementation. Pass to satisfy interface.
        """
        pass

    async def listen_for_subscriptions(self):
        """
        Simulates WebSocket subscriptions for market data and generates order book updates.
        Generates diffs and trades. Initial snapshot is handled by _order_book_snapshot.
        """
        self.logger().info("Starting simulated market data generation loop...")
        
        # Ensure message queues exist
        if not hasattr(self, "_message_queue"):
            self._message_queue = {}
        
        # Initialize message queues if they don't exist
        if self._trade_messages_queue_key not in self._message_queue:
            self.logger().info(f"Initializing {self._trade_messages_queue_key} message queue")
            self._message_queue[self._trade_messages_queue_key] = asyncio.Queue()
        
        if self._diff_messages_queue_key not in self._message_queue:
            self.logger().info(f"Initializing {self._diff_messages_queue_key} message queue")
            self._message_queue[self._diff_messages_queue_key] = asyncio.Queue()
            
        if self._snapshot_messages_queue_key not in self._message_queue:
            self.logger().info(f"Initializing {self._snapshot_messages_queue_key} message queue")
            self._message_queue[self._snapshot_messages_queue_key] = asyncio.Queue()
        
        # Debug log for trading pairs
        self.logger().info(f"Generating market data for trading pairs: {self._trading_pairs}")
        
        # last_snapshot_timestamp is used for periodic refresh, not initial snapshot
        last_snapshot_timestamp = 0

        # Generate initial snapshots for order books
        try:
            self.logger().info("Generating initial order book snapshots...")
            for trading_pair in self._trading_pairs:
                snapshot_message = await self._order_book_snapshot(trading_pair)
                self.logger().info(f"Created initial snapshot for {trading_pair}")
                self._message_queue[self._snapshot_messages_queue_key].put_nowait(snapshot_message)
            self.logger().info("Initial snapshots generated successfully")
        except Exception as e:
            self.logger().error(f"Error generating initial snapshots: {e}", exc_info=True)

        while True:
            try:
                # Generate incremental updates (diffs) and trades
                for trading_pair in self._trading_pairs:
                    timestamp = time.time()
                    update_id = int(timestamp * 1000)

                    # Generate bid and ask updates, alternating to simulate real market activity
                    price_variation = (timestamp % 5)  # Create some price movement

                    # Bid updates
                    bid_price = 9995.0 + price_variation
                    bid_size = 1.0 + (timestamp % 1.0)

                    bid_message = {
                        "msg_type": CONSTANTS.MSG_TYPE_L1_BID,
                        "event_ms": int(timestamp * 1000),
                        "sn_id": update_id,
                        "price": bid_price,
                        "size": bid_size,
                        "trading_pair": trading_pair
                    }

                    # Ask updates
                    ask_price = 10005.0 + price_variation
                    ask_size = 1.0 + (timestamp % 0.8)

                    ask_message = {
                        "msg_type": CONSTANTS.MSG_TYPE_L1_ASK,
                        "event_ms": int(timestamp * 1000),
                        "sn_id": update_id + 1, # Ensure unique ID
                        "price": ask_price,
                        "size": ask_size,
                        "trading_pair": trading_pair
                    }

                    # Send updates to the diff queue
                    self._message_queue[self._diff_messages_queue_key].put_nowait(bid_message)
                    self._message_queue[self._diff_messages_queue_key].put_nowait(ask_message)

                    # Occasionally send trade updates
                    if int(timestamp) % 3 == 0: # Adjusted frequency slightly
                        is_buy = (int(timestamp) % 6 < 3)
                        trade_price = bid_price if is_buy else ask_price
                        trade_size = 0.1 + (timestamp % 0.2)
                        trade_sn_id = update_id + 2 # Ensure unique ID

                        trade_message = {
                            "msg_type": CONSTANTS.MSG_TYPE_TRADE_BUY if is_buy else CONSTANTS.MSG_TYPE_TRADE_SELL,
                            "event_ms": int(timestamp * 1000),
                            "sn_id": trade_sn_id,
                            "price": trade_price,
                            "size": trade_size,
                            "trading_pair": trading_pair
                        }

                        await self._parse_trade_message(
                            trade_message,
                            self._message_queue[self._trade_messages_queue_key]
                        )
                        # Simulate potential fills based on this trade
                        await self._check_and_simulate_fills(
                            trading_pair, Decimal(str(trade_price)), Decimal(str(trade_size)),
                            TradeType.BUY if is_buy else TradeType.SELL,
                            str(trade_sn_id), timestamp
                        )

                # Optional: Add a periodic refresh snapshot logic here if needed, maybe less frequently
                time_for_refresh = (time.time() - last_snapshot_timestamp) > 60 # e.g., every 60s
                if time_for_refresh:
                    self.logger().info("Generating periodic refresh snapshots...")
                    refresh_timestamp = time.time()
                    refresh_sn_id = int(refresh_timestamp * 1000)
                    refresh_tasks = []
                    for trading_pair in self._trading_pairs:
                        price_base = 10000.0 + ((refresh_timestamp % 20) - 10) # Wider price movement
                        refresh_snapshot_data = {
                            "msg_type": 2,
                            "trading_pair": trading_pair,
                            "event_ms": int(refresh_timestamp * 1000),
                            "sn_id": refresh_sn_id,
                            "bids": [[price_base - 10, 5.0], [price_base - 20, 4.0]],
                            "asks": [[price_base + 10, 1.0], [price_base + 20, 2.0]]
                        }
                        refresh_tasks.append(self._parse_order_book_snapshot_message(
                            refresh_snapshot_data,
                            self._message_queue[self._snapshot_messages_queue_key]
                        ))
                    
                    # Use safe_gather with properly imported function
                    if refresh_tasks:
                        await safe_gather(*refresh_tasks)
                    
                    last_snapshot_timestamp = refresh_timestamp
                    self.logger().info("Periodic refresh snapshots generated.")


                # Sleep to control overall loop frequency
                await asyncio.sleep(0.1) # Adjust sleep as needed for desired update rate

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in market data simulation: {e}", exc_info=True)
                # Reset last refresh timestamp on error
                last_snapshot_timestamp = 0
                await asyncio.sleep(5.0) # Wait before retrying after error

    async def _check_and_simulate_fills(self,
                                        trading_pair: str,
                                        trade_price: Decimal,
                                        trade_size: Decimal,
                                        trade_type: TradeType,
                                        trade_id: str,
                                        timestamp: float):
        # Check if connector has the necessary attributes (might not exist during early init)
        if not hasattr(self._connector, '_order_tracker') or not hasattr(self._connector, '_mock_orders') or not hasattr(self._connector, '_mock_trades'):
            self.logger().debug("_order_tracker, _mock_orders or _mock_trades not found on connector. Skipping fill simulation.")
            return

        # Use items() for safe iteration if dictionary might be modified
        mock_orders_items = list(self._connector._mock_orders.items())

        for order_id, mock_order in mock_orders_items:
            # Ensure order is in a fillable state and matches the trading pair
            if mock_order["state"] not in [OrderState.OPEN, OrderState.PARTIALLY_FILLED] \
               or mock_order["trading_pair"] != trading_pair:
                continue

            order_price = mock_order["price"]
            order_type = mock_order["order_type"]
            order_trade_type = mock_order["trade_type"]
            order_remaining_amount = mock_order["amount"] - mock_order["executed_base_amount"]

            # Determine if the trade price crosses the order price
            should_fill = False
            # If incoming trade is BUY, check if it crosses resting SELL orders
            if trade_type == TradeType.BUY and order_trade_type == TradeType.SELL and order_price <= trade_price:
                 should_fill = True
            # If incoming trade is SELL, check if it crosses resting BUY orders
            elif trade_type == TradeType.SELL and order_trade_type == TradeType.BUY and order_price >= trade_price:
                 should_fill = True

            if should_fill:
                 # Calculate fill amount: minimum of trade size and remaining order amount
                 fill_amount = min(trade_size, order_remaining_amount)
                 if fill_amount <= Decimal("0"): # Ensure fill amount is positive
                     continue

                 self.logger().info(
                     f"[MOCK FILL] Simulated {trade_type.name} trade ({trade_size} @ {trade_price}) potential fill against "
                     f"mock order {mock_order['client_order_id']} ({order_trade_type.name} {order_remaining_amount} @ {order_price}). "
                     f"Fill amount: {fill_amount}"
                 )

                 # Update mock order state
                 mock_order["executed_base_amount"] += fill_amount
                 mock_order["last_update_timestamp"] = timestamp
                 if mock_order["executed_base_amount"] >= mock_order["amount"]:
                     mock_order["state"] = OrderState.FILLED
                 else:
                     mock_order["state"] = OrderState.PARTIALLY_FILLED

                 # Create mock fee and trade record
                 mock_fee = DeductedFromReturnsTradeFee(percent=Decimal("0")) # Zero fees for mock
                 quote_amount = fill_amount * order_price # Use order price for fill value
                 mock_trade_record = {
                    "trade_id": trade_id,
                    "fee": mock_fee,
                    "fill_base_amount": fill_amount,
                    "fill_quote_amount": quote_amount,
                    "fill_price": order_price, # Fill at the resting order's price
                    "timestamp": timestamp,
                 }
                 self._connector._mock_trades[order_id].append(mock_trade_record) # Add to mock trades list

                 # Trigger Hummingbot OrderFilled event
                 self._connector.trigger_event(
                    MarketEvent.OrderFilled,
                    OrderFilledEvent(
                         timestamp=timestamp,
                         order_id=mock_order['client_order_id'], # Use client_order_id for the event
                         trading_pair=trading_pair,
                         trade_type=order_trade_type,
                         order_type=order_type,
                         price=order_price, # Price from the resting order
                         amount=fill_amount,
                         trade_fee=mock_fee,
                         exchange_trade_id=trade_id # Use the simulated trade ID
                     )
                 )

                 # Create and process TradeUpdate for the OrderTracker
                 trade_update = TradeUpdate(
                     trade_id=trade_id,
                     client_order_id=mock_order['client_order_id'],
                     exchange_order_id=order_id, # Use the internal mock order ID
                     trading_pair=trading_pair,
                     fee=mock_fee,
                     fill_base_amount=fill_amount,
                     fill_quote_amount=quote_amount,
                     fill_price=order_price,
                     fill_timestamp=timestamp,
                 )
                 # Use the connector's order tracker instance
                 self._connector.order_tracker.process_trade_update(trade_update)

                 # If the order is fully filled, process an OrderUpdate
                 if mock_order["state"] == OrderState.FILLED:
                     order_update = OrderUpdate(
                         trading_pair=trading_pair,
                         update_timestamp=timestamp,
                         new_state=OrderState.FILLED,
                         client_order_id=mock_order['client_order_id'],
                         exchange_order_id=order_id,
                     )
                     self._connector.order_tracker.process_order_update(order_update)

                 # Decrease the remaining size of the incoming trade
                 trade_size -= fill_amount
                 # If the incoming trade is fully consumed, stop checking against other orders
                 if trade_size <= Decimal("0"):
                     break # Exit the inner loop (checking against mock orders)

    async def _parse_order_book_snapshot_message(self, raw_message, message_queue: asyncio.Queue):
        """
        Parse order book snapshot message from either a dict or an OrderBookMessage
        """
        # If already an OrderBookMessage, just pass it through
        if isinstance(raw_message, OrderBookMessage):
            message_queue.put_nowait(raw_message)
            return
            
        # Otherwise, treat as a dict and parse normally
        if isinstance(raw_message, dict):
            if raw_message.get("msg_type") != 2:
                self.logger().warning(f"Received non-L2 message in snapshot parser: {raw_message.get('msg_type')}")
                return

            if "trading_pair" not in raw_message:
                self.logger().warning(f"Snapshot message missing 'trading_pair': {raw_message}")
                return

            trading_pair = raw_message["trading_pair"]
            snapshot_timestamp = raw_message.get("event_ms", time.time() * 1000) / 1000

            formatted_message = {
                "trading_pair": trading_pair,
                "update_id": raw_message.get("sn_id", int(snapshot_timestamp * 1000)),
                "bids": raw_message.get("bids", []),
                "asks": raw_message.get("asks", []),
            }

            order_book_message = QTXOrderBook.snapshot_message_from_exchange(
                formatted_message, snapshot_timestamp, {"trading_pair": trading_pair}
            )
            message_queue.put_nowait(order_book_message)
        else:
            self.logger().warning(f"Received invalid message type in snapshot parser: {type(raw_message)}")

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        if "trading_pair" not in raw_message:
            self.logger().warning(f"Trade message missing 'trading_pair': {raw_message}")
            return

        trading_pair = raw_message["trading_pair"]

        is_buy = raw_message.get("msg_type") == CONSTANTS.MSG_TYPE_TRADE_BUY

        formatted_message = {
            "trading_pair": trading_pair,
            "trade_id": str(raw_message.get("sn_id", int(time.time() * 1000))),
            "price": raw_message.get("price", 0),
            "amount": raw_message.get("size", 0),
            "side": "BUY" if is_buy else "SELL",
        }

        trade_message = QTXOrderBook.trade_message_from_exchange(formatted_message, {"trading_pair": trading_pair})
        message_queue.put_nowait(trade_message)

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        if "trading_pair" not in raw_message:
            self.logger().warning(f"Diff message missing 'trading_pair': {raw_message}")
            return

        trading_pair = raw_message["trading_pair"]
        timestamp = raw_message.get("event_ms", time.time() * 1000) / 1000

        price = raw_message.get("price", 0)
        size = raw_message.get("size", 0)
        msg_type = raw_message.get("msg_type")

        if abs(msg_type) != 1:
             self.logger().warning(f"Received non-L1 message in diff parser: {msg_type}")
             return

        is_bid = msg_type == CONSTANTS.MSG_TYPE_L1_BID

        bids = [[price, size]] if is_bid else []
        asks = [] if is_bid else [[price, size]]

        formatted_message = {
            "trading_pair": trading_pair,
            "update_id": raw_message.get("sn_id", int(timestamp * 1000)),
            "bids": bids,
            "asks": asks,
        }

        order_book_message = QTXOrderBook.diff_message_from_exchange(
            formatted_message, timestamp, {"trading_pair": trading_pair}
        )
        message_queue.put_nowait(order_book_message)

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        msg_type = event_message.get("msg_type", 0)

        if abs(msg_type) == 2:
            return self._snapshot_messages_queue_key
        elif abs(msg_type) == 3:
            return self._trade_messages_queue_key
        elif abs(msg_type) == 1:
            return self._diff_messages_queue_key
        else:
            self.logger().warning(f"Unknown message type in fake data: {msg_type}")
            return self._diff_messages_queue_key

    async def _on_order_stream_interruption(self, websocket_assistant: Optional[WSAssistant] = None):
        self.logger().warning("Order stream interrupted (using fake data).")
        await super()._on_order_stream_interruption(websocket_assistant)
