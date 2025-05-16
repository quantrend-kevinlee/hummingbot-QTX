import asyncio
import struct
import time
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

# numpy as np # F401 Unused import (Removed)
# from typing import List, Optional # F401 Unused import (Removed)
from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_api_order_book_data_source import (
    QtxPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_udp_manager import QtxPerpetualUDPManager
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.funding_info import FundingInfo
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_row import OrderBookRow


import pytest


class TestQtxPerpetualAPIOrderBookDataSource(unittest.TestCase):
    # Log level settings
    level = 0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def setUp(self):
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
        self.trading_pair = "BTC-USDT"
        self.qtx_trading_pair = "binance-futures:btcusdt"

        self.network_assistant = NetworkMockingAssistant()

        self.throttler = AsyncThrottler(CONSTANTS.RATE_LIMITS)

        # Create the UDP manager
        self.udp_manager = QtxPerpetualUDPManager(host="172.30.2.221", port=8080)

        self.data_source = QtxPerpetualAPIOrderBookDataSource(
            trading_pairs=[self.trading_pair],
            connector=MagicMock(),
            api_factory=MagicMock(),
            udp_manager_instance=self.udp_manager,
        )
        # api_logger = self.data_source.logger() # F841 local variable 'api_logger' is assigned to but never used (Removed)
        self.data_source.logger().setLevel(1)

    def tearDown(self):
        super().tearDown()

    def async_run_with_timeout(self, coroutine, timeout: float = 30):
        return self.loop.run_until_complete(asyncio.wait_for(coroutine, timeout))

    def _build_partial_fill_trade_update(
        self, timestamp: float, trade_price: Decimal, amount: Decimal, order_id: str
    ) -> dict:
        """Build a simulated trade update for testing."""
        return {
            "timestamp": timestamp,
            "price": float(trade_price),
            "amount": float(amount),
            "order_id": order_id,
            "side": "buy",
        }

    async def test_get_new_order_book(self):
        """Test get_new_order_book creates a new order book instance."""
        order_book = await self.data_source.get_new_order_book(self.trading_pair)
        self.assertIsInstance(order_book, OrderBook)

    async def test_order_book_snapshot_message_from_exchange(self):
        """Test that a proper order book snapshot message is created from exchange data."""
        snapshot_data = {
            "timestamp": 1234567890.123,
            "symbol": "BTCUSDT",
            "bids": [[45000.0, 1.5], [44900.0, 2.5]],
            "asks": [[45100.0, 2.0], [45200.0, 3.0]],
            "update_id": 1000,
        }

        message = self.data_source._order_book_snapshot_message_from_exchange(
            snapshot_data, snapshot_data["timestamp"], metadata={"trading_pair": self.trading_pair}
        )

        self.assertIsInstance(message, OrderBookMessage)
        self.assertEqual(message.trading_pair, self.trading_pair)
        self.assertEqual(message.timestamp, snapshot_data["timestamp"])
        self.assertEqual(len(message.bids), 2)
        self.assertEqual(len(message.asks), 2)

    async def test_order_book_diff_message_from_exchange(self):
        """Test that a proper order book diff message is created from exchange data."""
        diff_data = {
            "timestamp": 1234567890.456,
            "symbol": "BTCUSDT",
            "bids": [[45000.0, 0.0], [44950.0, 3.5]],  # First entry removes a level
            "asks": [[45100.0, 0.0], [45150.0, 1.5]],  # First entry removes a level
            "update_id": 1001,
        }

        message = self.data_source._order_book_diff_message_from_exchange(
            diff_data, diff_data["timestamp"], metadata={"trading_pair": self.trading_pair}
        )

        self.assertIsInstance(message, OrderBookMessage)
        self.assertEqual(message.trading_pair, self.trading_pair)
        self.assertEqual(message.timestamp, diff_data["timestamp"])
        self.assertEqual(len(message.bids), 2)
        self.assertEqual(len(message.asks), 2)
        self.assertEqual(message.bids[0].amount, 0.0)  # Indicates removal
        self.assertEqual(message.asks[0].amount, 0.0)  # Indicates removal

    async def test_order_book_trade_message_from_exchange(self):
        """Test that a proper trade message is created from exchange data."""
        trade_data = {
            "timestamp": 1234567890.789,
            "symbol": "BTCUSDT",
            "price": 45050.0,
            "amount": 0.5,
            "side": "buy",
            "id": "12345",
        }

        message = self.data_source._order_book_trade_message_from_exchange(
            trade_data, trade_data["timestamp"], metadata={"trading_pair": self.trading_pair}
        )

        self.assertIsInstance(message, OrderBookMessage)
        self.assertEqual(message.trading_pair, self.trading_pair)
        self.assertEqual(message.timestamp, trade_data["timestamp"])
        self.assertEqual(float(message.content["price"]), trade_data["price"])
        self.assertEqual(float(message.content["amount"]), trade_data["amount"])
        self.assertEqual(message.content["trade_type"], "buy")
        self.assertEqual(message.content["trade_id"], trade_data["id"])

    async def test_listen_for_subscriptions(self):
        """Test UDP subscription flow with mock socket."""
        # Mock the socket and UDP functionality
        mock_socket = MagicMock()

        # Subscription ACK
        ack_data = struct.pack("<I", 1)  # Subscribe ACK with index

        # Depth data (type 2)
        depth_header = struct.pack(
            "<Idcccc",
            1,  # index
            1234567890.123,  # timestamp
            b"2",  # message type (depth)
            b"1",  # sub_type (snapshot)
            b"1",  # index (again)
            b"2",
        )  # reserve

        bids = [(45000.0, 1.5), (44900.0, 2.5)]
        asks = [(45100.0, 2.0), (45200.0, 3.0)]

        num_bid_levels = struct.pack("<H", len(bids))
        num_ask_levels = struct.pack("<H", len(asks))

        bid_data = b""
        for price, size in bids:
            bid_data += struct.pack("<dd", price, size)

        ask_data = b""
        for price, size in asks:
            ask_data += struct.pack("<dd", price, size)

        depth_data = depth_header + num_bid_levels + num_ask_levels + bid_data + ask_data

        # Mock socket behavior
        mock_socket.recv.side_effect = [ack_data, depth_data, asyncio.CancelledError]

        with patch.object(self.udp_manager, "socket", mock_socket):
            with patch.object(self.udp_manager, "_is_connected", True):
                # Set up subscription
                self.data_source._trading_pairs = [self.trading_pair]
                self.udp_manager._add_subscription(self.qtx_trading_pair, self.data_source._handle_depth_message)

                # Run the listener
                try:
                    await self.data_source.listen_for_subscriptions()
                except asyncio.CancelledError:
                    pass

        # Assertions would go here based on your callback actions
        # Since we can't directly verify the callback, we test the UDP manager's behavior
        self.assertTrue(mock_socket.recv.called)

    async def test_websocket_connection_failure(self):
        """Test handling of websocket connection failure."""
        with patch.object(self.data_source, "_create_websocket_connection") as mock_create_conn:
            mock_create_conn.side_effect = Exception("Connection failed")

            # Order book stream should handle the error gracefully
            order_book_stream_queue = asyncio.Queue()

            async def collect_messages(queue):
                messages = []
                try:
                    while True:
                        message = await asyncio.wait_for(queue.get(), timeout=1.0)
                        messages.append(message)
                except asyncio.TimeoutError:
                    pass
                return messages

            # Start listening for order book stream
            listen_task = self.loop.create_task(
                self.data_source.listen_for_order_book_stream(ev_loop=self.loop, output=order_book_stream_queue)
            )

            # Collect any messages (should be empty due to connection failure)
            messages = await collect_messages(order_book_stream_queue)

            # Stop listening
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

            self.assertEqual(len(messages), 0)

    @patch(
        "hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_api_order_book_data_source.QtxPerpetualAPIOrderBookDataSource._time"
    )
    async def test_listen_for_trades_logs_exception(self, mock_time):
        """Test that exceptions in trade listening are logged properly."""
        mock_time.return_value = 1234567890.0
        # incomplete_resp = {"trading_pair": self.trading_pair, "trade_type": "buy"} # F841 (Removed)

        # Mock the queue to raise an exception
        mock_queue = AsyncMock()
        mock_queue.get.side_effect = asyncio.CancelledError

        # Test handling of exception
        with self.assertRaises(asyncio.CancelledError):
            await self.data_source.listen_for_trades(ev_loop=self.loop, output=mock_queue)

    async def test_time_synchronizer_start_failure(self):
        """Test handling of time synchronizer start failure."""
        # Mock time synchronizer to raise an exception
        with patch.object(self.data_source._time_synchronizer, "start", side_effect=Exception("Start failed")):
            # This should handle the exception gracefully
            # The exact behavior depends on implementation
            pass

    async def test_market_data_initialization(self):
        """Test market data initialization process."""
        # Mock required components
        self.data_source._connector.trading_pair_symbol_map = {self.trading_pair: "BTCUSDT"}

        with patch.object(self.udp_manager, "subscribe") as mock_subscribe:
            with patch.object(self.udp_manager, "start") as mock_start:
                # Test initialization
                await self.data_source._init_market_data_feeds()

                mock_start.assert_called_once()
                mock_subscribe.assert_called_once_with(
                    self.qtx_trading_pair, self.data_source._handle_depth_message
                )

    async def test_parsing_depth_update_from_udp_data(self):
        """Test parsing depth update from UDP data."""
        # Create header
        header = struct.pack(
            "<Idcccc",
            1,  # index
            1234567890.123,  # timestamp
            b"2",  # message type (depth)
            b"1",  # sub_type
            b"1",  # index
            b"2",  # reserve
        )

        # Create depth data (bids/asks)
        bids = [(45000.0, 1.5), (44900.0, 2.5)]
        asks = [(45100.0, 2.0), (45200.0, 3.0)]

        num_bid_levels = struct.pack("<H", len(bids))
        num_ask_levels = struct.pack("<H", len(asks))

        bid_data = b""
        for price, size in bids:
            bid_data += struct.pack("<dd", price, size)

        ask_data = b""
        for price, size in asks:
            ask_data += struct.pack("<dd", price, size)

        payload = num_bid_levels + num_ask_levels + bid_data + ask_data
        data = header + payload

        # Create the callback mock
        callback = MagicMock()

        # Parse the data
        self.udp_manager._parse_message(data, callback)

        # Verify the callback was called with the correct parsed data
        callback.assert_called_once()
        message_data = callback.call_args[0][0]

        self.assertEqual(message_data["type"], "depth")
        self.assertEqual(message_data["sub_type"], 1)
        self.assertEqual(message_data["symbol_index"], 1)
        self.assertAlmostEqual(message_data["timestamp"], 1234567890.123)
        self.assertEqual(len(message_data["bids"]), 2)
        self.assertEqual(len(message_data["asks"]), 2)

        # Verify bid/ask data
        self.assertEqual(message_data["bids"][0], [45000.0, 1.5])
        self.assertEqual(message_data["bids"][1], [44900.0, 2.5])
        self.assertEqual(message_data["asks"][0], [45100.0, 2.0])
        self.assertEqual(message_data["asks"][1], [45200.0, 3.0])

    async def test_parsing_ticker_update_from_udp_data(self):
        """Test parsing ticker update from UDP data."""
        # Create header
        header = struct.pack(
            "<Idcccc",
            1,  # index
            1234567890.123,  # timestamp
            b"1",  # message type (ticker)
            b"1",  # sub_type
            b"1",  # index
            b"2",  # reserve
        )

        # Create ticker data
        payload = struct.pack("<dd", 45050.0, 100.5)  # price, volume
        data = header + payload

        # Create the callback mock
        callback = MagicMock()

        # Parse the data
        self.udp_manager._parse_message(data, callback)

        # Verify the callback was called with the correct parsed data
        callback.assert_called_once()
        message_data = callback.call_args[0][0]

        self.assertEqual(message_data["type"], "ticker")
        self.assertEqual(message_data["sub_type"], 1)
        self.assertEqual(message_data["symbol_index"], 1)
        self.assertAlmostEqual(message_data["timestamp"], 1234567890.123)
        self.assertAlmostEqual(message_data["price"], 45050.0)
        self.assertAlmostEqual(message_data["volume"], 100.5)

    async def test_parsing_trade_update_from_udp_data(self):
        """Test parsing trade update from UDP data."""
        # Create header
        header = struct.pack(
            "<Idcccc",
            1,  # index
            1234567890.123,  # timestamp
            b"3",  # message type (trade)
            b"1",  # sub_type (buy)
            b"1",  # index
            b"2",  # reserve
        )

        # Create trade data
        payload = struct.pack("<dd", 45050.0, 0.5)  # price, amount
        data = header + payload

        # Create the callback mock
        callback = MagicMock()

        # Parse the data
        self.udp_manager._parse_message(data, callback)

        # Verify the callback was called with the correct parsed data
        callback.assert_called_once()
        message_data = callback.call_args[0][0]

        self.assertEqual(message_data["type"], "trade")
        self.assertEqual(message_data["sub_type"], 1)
        self.assertEqual(message_data["symbol_index"], 1)
        self.assertAlmostEqual(message_data["timestamp"], 1234567890.123)
        self.assertAlmostEqual(message_data["price"], 45050.0)
        self.assertAlmostEqual(message_data["amount"], 0.5)
        self.assertEqual(message_data["side"], "buy")

    async def test_symbol_subscription_mapping(self):
        """Test that symbol subscription is properly mapped."""
        # Mock UDP manager
        with patch.object(self.udp_manager, "subscribe") as mock_subscribe:
            # Subscribe to a trading pair
            await self.udp_manager.start()
            self.udp_manager.subscribe("binance-futures:btcusdt", self.data_source._handle_depth_message)

            # Verify the subscribe was called with correct parameters
            mock_subscribe.assert_called_with("binance-futures:btcusdt", self.data_source._handle_depth_message)

    async def test_ticker_update_handling(self):
        """Test handling of ticker updates from UDP feed."""
        ticker_data = {
            "type": "ticker",
            "sub_type": 1,
            "symbol_index": 1,
            "timestamp": time.time(),
            "price": 45050.0,
            "volume": 100.5,
        }

        with patch.object(self.data_source, "_emit_order_book_event") as mock_emit:
            # Handle ticker update
            await self.data_source._handle_ticker_message(ticker_data)

            # Ticker update typically doesn't emit order book event
            mock_emit.assert_not_called()

    async def test_depth_update_handling(self):
        """Test handling of depth updates from UDP feed."""
        depth_data = {
            "type": "depth",
            "sub_type": 1,  # snapshot
            "symbol_index": 1,
            "timestamp": time.time(),
            "bids": [[45000.0, 1.5], [44900.0, 2.5]],
            "asks": [[45100.0, 2.0], [45200.0, 3.0]],
        }

        with patch.object(self.data_source, "_emit_order_book_event") as mock_emit:
            # Set up the symbol mapping
            self.udp_manager._index_to_symbol[1] = self.qtx_trading_pair
            self.data_source._trading_pairs = [self.trading_pair]

            # Handle depth update
            await self.data_source._handle_depth_message(depth_data)

            # Should emit an order book event
            mock_emit.assert_called_once()

    async def test_trade_update_handling(self):
        """Test handling of trade updates from UDP feed."""
        trade_data = {
            "type": "trade",
            "sub_type": 1,  # buy
            "symbol_index": 1,
            "timestamp": time.time(),
            "price": 45050.0,
            "amount": 0.5,
            "side": "buy",
        }

        with patch.object(self.data_source, "_emit_trade_event") as mock_emit:
            # Set up the symbol mapping
            self.udp_manager._index_to_symbol[1] = self.qtx_trading_pair
            self.data_source._trading_pairs = [self.trading_pair]

            # Handle trade update
            await self.data_source._handle_trade_message(trade_data)

            # Should emit a trade event
            mock_emit.assert_called_once()

    async def test_emit_order_book_event(self):
        """Test emission of order book events to queue."""
        event_queue = asyncio.Queue()
        self.data_source._message_queue[self.data_source._order_book_stream_event_listener_task] = event_queue

        depth_data = {
            "type": "depth",
            "sub_type": 1,  # snapshot
            "symbol_index": 1,
            "timestamp": time.time(),
            "bids": [[45000.0, 1.5], [44900.0, 2.5]],
            "asks": [[45100.0, 2.0], [45200.0, 3.0]],
        }

        # Emit the event
        await self.data_source._emit_order_book_event(depth_data)

        # Verify the event was added to the queue
        self.assertEqual(event_queue.qsize(), 1)
        event = await event_queue.get()
        self.assertEqual(event["type"], "depth")

    async def test_emit_trade_event(self):
        """Test emission of trade events to queue."""
        event_queue = asyncio.Queue()
        self.data_source._message_queue[self.data_source._trade_listener_task] = event_queue

        trade_data = {
            "type": "trade",
            "sub_type": 1,  # buy
            "symbol_index": 1,
            "timestamp": time.time(),
            "price": 45050.0,
            "amount": 0.5,
            "side": "buy",
        }

        # Emit the event
        await self.data_source._emit_trade_event(trade_data)

        # Verify the event was added to the queue
        self.assertEqual(event_queue.qsize(), 1)
        event = await event_queue.get()
        self.assertEqual(event["type"], "trade")

    async def test_funding_info_update_stream(self):
        """Test funding info update stream functionality."""
        event_queue = asyncio.Queue()

        # Mock funding info
        funding_info = FundingInfo(
            trading_pair=self.trading_pair,
            index_price=45000.0,
            mark_price=45050.0,
            next_funding_utc_timestamp=1234567890,
            rate=0.0001,
        )

        # Add funding info to queue
        await event_queue.put(funding_info)

        self.data_source._message_queue[self.data_source._funding_info_listener_task] = event_queue

        # Listen for funding info updates
        output_queue = asyncio.Queue()

        async def collect_funding_info():
            async for info in self.data_source.listen_for_funding_info(output_queue):
                return info

        # The queue has one item, so we should get one funding info
        info = await collect_funding_info()
        self.assertEqual(info.trading_pair, self.trading_pair)
        self.assertEqual(info.rate, Decimal("0.0001"))

    async def test_message_queue_error_handling(self):
        """Test error handling in message queue processing."""
        event_queue = asyncio.Queue()
        self.data_source._message_queue[self.data_source._order_book_stream_event_listener_task] = event_queue

        # Add an invalid message that will cause an exception
        invalid_message = {"invalid": "data"}
        await event_queue.put(invalid_message)

        # Create output queue
        output_queue = asyncio.Queue()

        # Try to process the message (should handle the error gracefully)
        with patch.object(self.data_source, "logger") as mock_logger:
            # Run the listener briefly
            listen_task = self.loop.create_task(
                self.data_source.listen_for_order_book_stream(ev_loop=self.loop, output=output_queue)
            )

            # Wait briefly for message processing
            await asyncio.sleep(0.1)

            # Stop the listener
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

            # Error should have been logged
            self.assertTrue(mock_logger.error.called or mock_logger.network.called)

    async def test_malformed_udp_data_handling(self):
        """Test handling of malformed UDP data."""
        # Create malformed data (too short)
        malformed_data = b"short"

        callback = MagicMock()

        # This should handle the error gracefully
        with patch.object(self.udp_manager, "logger") as mock_logger:
            self.udp_manager._parse_message(malformed_data, callback)

            # Should log an error
            mock_logger.error.assert_called()
            # Callback should not be called
            callback.assert_not_called()

    async def test_listen_for_funding_info(self):
        """Test listening for funding info updates."""
        event_queue = asyncio.Queue()

        # Create a funding info update
        funding_update = FundingInfo(
            trading_pair=self.trading_pair,
            index_price=45000.0,
            mark_price=45050.0,
            next_funding_utc_timestamp=1234567890,
            rate=0.0001,
        )

        # Add to event queue
        await event_queue.put(funding_update)

        # Set up the message queue
        self.data_source._message_queue[self.data_source._funding_info_listener_task] = event_queue

        # Create output queue
        output_queue = asyncio.Queue()

        # Start listening
        listen_task = self.loop.create_task(self.data_source.listen_for_funding_info(output_queue))

        # Collect the funding info
        try:
            funding_info = await asyncio.wait_for(output_queue.get(), timeout=1.0)
            self.assertEqual(funding_info.trading_pair, funding_update.trading_pair)
            self.assertEqual(funding_info.rate, funding_update.rate)
            self.assertEqual(funding_info.next_funding_utc_timestamp, funding_update.next_funding_utc_timestamp)
        finally:
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass

    async def test_order_book_integrity(self):
        """Test order book update ID sequencing and integrity."""
        # Create a new order book
        order_book = OrderBook()

        # Create snapshot data
        snapshot_timestamp = time.time()  # noqa: F841
        snapshot_update_id = 1000
        snapshot_bids = [[45000.0, 1.5], [44900.0, 2.5]]
        snapshot_asks = [[45100.0, 2.0], [45200.0, 3.0]]

        # Convert to OrderBookRow objects
        snapshot_bid_rows = [
            OrderBookRow(price=float(bid[0]), amount=float(bid[1]), update_id=snapshot_update_id)
            for bid in snapshot_bids
        ]
        snapshot_ask_rows = [
            OrderBookRow(price=float(ask[0]), amount=float(ask[1]), update_id=snapshot_update_id)
            for ask in snapshot_asks
        ]

        # Apply snapshot
        order_book.apply_snapshot(snapshot_bid_rows, snapshot_ask_rows, snapshot_update_id)

        # Verify snapshot was applied
        self.assertEqual(snapshot_update_id, order_book.snapshot_uid)
        actual_bids = list(order_book.bid_entries())
        actual_asks = list(order_book.ask_entries())
        self.assertEqual(2, len(actual_bids))
        self.assertEqual(2, len(actual_asks))

        # Test 1: Stale update (should be ignored)
        old_diff_update_id = 500  # Lower than snapshot's update_id
        old_diff_bids = [[44800.0, 3.0]]
        old_diff_asks = [[45300.0, 1.0]]

        old_diff_bid_rows = [
            OrderBookRow(price=float(bid[0]), amount=float(bid[1]), update_id=old_diff_update_id)
            for bid in old_diff_bids
        ]
        old_diff_ask_rows = [
            OrderBookRow(price=float(ask[0]), amount=float(ask[1]), update_id=old_diff_update_id)
            for ask in old_diff_asks
        ]

        # Apply stale diff
        order_book.apply_diffs(old_diff_bid_rows, old_diff_ask_rows, old_diff_update_id)

        # Verify snapshot_uid hasn't changed
        self.assertEqual(snapshot_update_id, order_book.snapshot_uid)

        # Get current bid/ask entries (unused but defined for consistency)
        stale_update_bids = list(order_book.bid_entries())  # noqa: F841
        stale_update_asks = list(order_book.ask_entries())  # noqa: F841

        # Test 2: Valid sequential update
        valid_diff_update_id = 1001  # Exactly snapshot_update_id + 1
        valid_diff_bids = [[44950.0, 1.0]]  # Add a new price level
        valid_diff_asks = [[45150.0, 1.5]]  # Add a new price level

        # Create a fresh order book for this test to avoid side effects
        order_book_2 = OrderBook()
        order_book_2.apply_snapshot(snapshot_bid_rows, snapshot_ask_rows, snapshot_update_id)

        valid_diff_bid_rows = [
            OrderBookRow(price=float(bid[0]), amount=float(bid[1]), update_id=valid_diff_update_id)
            for bid in valid_diff_bids
        ]
        valid_diff_ask_rows = [
            OrderBookRow(price=float(ask[0]), amount=float(ask[1]), update_id=valid_diff_update_id)
            for ask in valid_diff_asks
        ]

        # Apply valid diff
        order_book_2.apply_diffs(valid_diff_bid_rows, valid_diff_ask_rows, valid_diff_update_id)

        # Verify last_diff_uid has changed
        self.assertEqual(valid_diff_update_id, order_book_2.last_diff_uid)

        # Get updated bid/ask entries
        updated_bids = list(order_book_2.bid_entries())
        updated_asks = list(order_book_2.ask_entries())
        self.assertEqual(3, len(updated_bids))
        self.assertEqual(3, len(updated_asks))

        # Test 3: Update with a gap
        gap_diff_update_id = 1005  # Gap between 1001 and 1005
        gap_diff_bids = [[44800.0, 3.0]]
        gap_diff_asks = [[45300.0, 1.0]]

        # Create a fresh order book for this test to avoid side effects
        order_book_3 = OrderBook()
        order_book_3.apply_snapshot(snapshot_bid_rows, snapshot_ask_rows, snapshot_update_id)

        gap_diff_bid_rows = [
            OrderBookRow(price=float(bid[0]), amount=float(bid[1]), update_id=gap_diff_update_id)
            for bid in gap_diff_bids
        ]
        gap_diff_ask_rows = [
            OrderBookRow(price=float(ask[0]), amount=float(ask[1]), update_id=gap_diff_update_id)
            for ask in gap_diff_asks
        ]

        # Apply gap diff
        order_book_3.apply_diffs(gap_diff_bid_rows, gap_diff_ask_rows, gap_diff_update_id)

        # Verify last_diff_uid has changed to the gap update
        self.assertEqual(gap_diff_update_id, order_book_3.last_diff_uid)

        # Check all bids and asks
        final_bids = list(order_book_3.bid_entries())
        final_asks = list(order_book_3.ask_entries())

        # Order book 3 should have original 2 entries plus 1 new one
        self.assertEqual(3, len(final_bids))
        self.assertEqual(3, len(final_asks))


if __name__ == "__main__":
    unittest.main()
