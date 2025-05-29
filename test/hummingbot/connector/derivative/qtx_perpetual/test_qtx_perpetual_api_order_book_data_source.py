#!/usr/bin/env python
"""
Integration test for QTX perpetual's order book data source functionality.

This tests the integration between QtxPerpetualUDPManager and Hummingbot's core
order book tracking system through the dynamically overridden data source methods.
"""
import asyncio
import importlib
import struct
import time
from decimal import Decimal
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase

from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_derivative import (
    EXCHANGE_CONNECTOR_CLASSES,
    QtxPerpetualDerivative
)
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_udp_manager import QtxPerpetualUDPManager
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker import OrderBookTracker
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource


# Test configuration - centralized for easy modification
TEST_UDP_HOST = CONSTANTS.DEFAULT_UDP_HOST
TEST_UDP_PORT = CONSTANTS.DEFAULT_UDP_PORT


class MockUDPSocket:
    """Mock UDP socket for testing"""
    def __init__(self):
        self.sent_data = []
        self.receive_buffer = []
        self.closed = False
        self._auto_respond = True
        
    def sendto(self, data: bytes, address):
        self.sent_data.append((data, address))
        
        # Auto-respond to certain messages
        if self._auto_respond:
            decoded = data.decode('utf-8')
            if decoded == "ping":
                # Respond with pong
                self.receive_buffer.append((b"pong", address))
            elif decoded.startswith(("binance-futures:", "okx-futures:", "bitget-futures:", 
                                     "bybit-futures:", "kucoin-futures:", "gate-io-futures:")):
                # Simulate subscription ACK with index
                index = len([d for d in self.sent_data if any(prefix in d[0] for prefix in 
                            [b"binance-futures:", b"okx-futures:", b"bitget-futures:", 
                             b"bybit-futures:", b"kucoin-futures:", b"gate-io-futures:"])])
                response = struct.pack("<I", index)  # Pack index as uint32
                self.receive_buffer.append((response, address))
        
    def recvfrom(self, buffer_size):
        if self.receive_buffer:
            return self.receive_buffer.pop(0)
        raise BlockingIOError()
        
    def setblocking(self, blocking: bool):
        pass
        
    def settimeout(self, timeout: float):
        pass
        
    def close(self):
        self.closed = True


class TestQtxPerpetualOrderBookDataSourceIntegration(IsolatedAsyncioWrapperTestCase):
    """Integration tests for QTX perpetual order book data source functionality"""
    
    async def _reset_udp_manager(self):
        """Reset the UDP manager singleton for clean tests"""
        manager = await QtxPerpetualUDPManager.get_instance()
        if manager._connection_state.is_connected:
            await manager.stop()
        
        manager._subscriptions.clear()
        manager._index_to_pair.clear()
        manager._message_queues.clear()
        manager._last_update_id.clear()
    
    def setUp(self):
        super().setUp()
        self.trading_pair = "BTC-USDT"
        self.exchange_backend = "binance"
        
        # Reset UDP Manager
        self.run_async_with_timeout(self._reset_udp_manager())
        
        # Mock client config
        self.client_config_map = MagicMock()
        self.client_config_map.derivative_qtx_perpetual_exchange_backend = self.exchange_backend
        # Fix throttler initialization by adding rate_limits_share_pct
        self.client_config_map.rate_limits_share_pct = Decimal("100")
        
        # Create mock socket
        self.mock_socket = MockUDPSocket()
        
        # Patch socket creation to use our mock
        self.socket_patcher = patch('socket.socket', return_value=self.mock_socket)
        self.socket_patcher.start()
        
        # Create the connector
        self.connector = QtxPerpetualDerivative(
            client_config_map=self.client_config_map,
            qtx_perpetual_host=TEST_UDP_HOST,
            qtx_perpetual_port=TEST_UDP_PORT,
            exchange_backend=self.exchange_backend,
            trading_pairs=[self.trading_pair],
            trading_required=False  # Disable trading to avoid API key requirements
        )
        
    def tearDown(self):
        # Stop socket patcher
        if hasattr(self, 'socket_patcher'):
            self.socket_patcher.stop()
        
        # Clean up any remaining tasks in the local event loop
        for task in asyncio.all_tasks(self.local_event_loop):
            if not task.done():
                task.cancel()
        super().tearDown()
        
    def test_dynamic_inheritance_structure(self):
        """Test that the connector properly inherits from parent exchange and has required methods"""
        # Verify inheritance
        exchange_info = EXCHANGE_CONNECTOR_CLASSES[self.exchange_backend.lower()]
        module = importlib.import_module(exchange_info["module"])
        parent_class = getattr(module, exchange_info["class"])
        
        self.assertIsInstance(self.connector, parent_class)
        self.assertEqual(self.connector.name, "qtx_perpetual")
        
        # Verify QTX-specific methods exist
        self.assertTrue(hasattr(self.connector, "_setup_qtx_market_data"))
        self.assertTrue(hasattr(self.connector, "_setup_qtx_udp_subscriptions"))
        self.assertTrue(hasattr(self.connector, "_consume_diff_messages"))
        self.assertTrue(hasattr(self.connector, "_consume_trade_messages"))
        self.assertTrue(hasattr(self.connector, "_handle_orderbook_snapshots"))
        self.assertTrue(hasattr(self.connector, "_build_qtx_orderbook_snapshot"))
        
    def test_order_book_tracker_initialization(self):
        """Test that order book tracker is properly initialized with QTX data source"""
        # Access order book tracker
        tracker = self.connector.order_book_tracker
        self.assertIsNotNone(tracker)
        self.assertIsInstance(tracker, OrderBookTracker)
        
        # Verify data source is initialized
        data_source = tracker.data_source
        self.assertIsNotNone(data_source)
        self.assertIsInstance(data_source, OrderBookTrackerDataSource)
        
        # Verify trading pairs
        self.assertEqual(tracker._trading_pairs, [self.trading_pair])
        
    async def test_udp_subscription_flow(self):
        """Test the complete UDP subscription flow"""
        with patch('socket.socket') as mock_socket_class:
            mock_socket_class.return_value = self.mock_socket
            
            # Prepare subscription response
            exchange_info = EXCHANGE_CONNECTOR_CLASSES[self.exchange_backend.lower()]
            qtx_symbol = f"{exchange_info['exchange_name_on_qtx']}:{self.trading_pair.lower().replace('-', '')}"
            self.mock_socket.receive_buffer.append((f"1:{qtx_symbol}".encode(), (TEST_UDP_HOST, TEST_UDP_PORT)))
            
            # Initialize UDP manager
            await self.connector._init_udp_manager()
            udp_manager = self.connector.udp_manager
            
            # Start UDP connection
            await udp_manager.start()
            
            # Subscribe to trading pairs
            await self.connector._setup_qtx_udp_subscriptions()
            
            # Verify subscription was sent
            self.assertEqual(len(self.mock_socket.sent_data), 2)  # ping + subscription
            sent_symbol = self.mock_socket.sent_data[1][0].decode()
            self.assertEqual(sent_symbol, qtx_symbol)
            
            # Verify queues were created
            self.assertIn(self.trading_pair, self.connector._udp_queues)
            queues = self.connector._udp_queues[self.trading_pair]
            self.assertIn("diff", queues)
            self.assertIn("trade", queues)
            
    async def test_order_book_diff_message_flow(self):
        """Test that order book diff messages flow from UDP to order book"""
        with patch('socket.socket') as mock_socket_class:
            mock_socket_class.return_value = self.mock_socket
            
            # Setup subscription
            exchange_info = EXCHANGE_CONNECTOR_CLASSES[self.exchange_backend.lower()]
            qtx_symbol = f"{exchange_info['exchange_name_on_qtx']}:{self.trading_pair.lower().replace('-', '')}"
            self.mock_socket.receive_buffer.append((f"1:{qtx_symbol}".encode(), (TEST_UDP_HOST, TEST_UDP_PORT)))
            
            # Initialize and subscribe
            await self.connector._init_udp_manager()
            await self.connector.udp_manager.start()
            await self.connector._setup_qtx_udp_subscriptions()
            
            # Create a depth message
            timestamp_ns = int(time.time() * 1e9)
            update_id = 12345
            header = struct.pack("<iiqqqq", 2, 1, update_id, timestamp_ns, 0, 0)  # type=2 (depth), index=1
            asks_bids_count = struct.pack("<qq", 2, 2)  # 2 asks, 2 bids
            asks = struct.pack("<dd", 45100.0, 1.5) + struct.pack("<dd", 45200.0, 2.0)
            bids = struct.pack("<dd", 45000.0, 1.0) + struct.pack("<dd", 44900.0, 2.5)
            depth_message = header + asks_bids_count + asks + bids
            
            # Process the message
            await self.connector.udp_manager._process_message(depth_message)
            
            # Verify message was added to diff queue
            diff_queue = self.connector._udp_queues[self.trading_pair]["diff"]
            self.assertEqual(diff_queue.qsize(), 1)
            
            # Get the message
            message = await diff_queue.get()
            self.assertIsInstance(message, OrderBookMessage)
            self.assertEqual(message.type, OrderBookMessageType.DIFF)
            self.assertEqual(message.content["trading_pair"], self.trading_pair)
            self.assertEqual(message.content["update_id"], update_id)
            self.assertEqual(len(message.content["bids"]), 2)
            self.assertEqual(len(message.content["asks"]), 2)
            
    async def test_trade_message_flow(self):
        """Test that trade messages flow from UDP to trade queue"""
        with patch('socket.socket') as mock_socket_class:
            mock_socket_class.return_value = self.mock_socket
            
            # Setup subscription
            exchange_info = EXCHANGE_CONNECTOR_CLASSES[self.exchange_backend.lower()]
            qtx_symbol = f"{exchange_info['exchange_name_on_qtx']}:{self.trading_pair.lower().replace('-', '')}"
            self.mock_socket.receive_buffer.append((f"1:{qtx_symbol}".encode(), (TEST_UDP_HOST, TEST_UDP_PORT)))
            
            # Initialize and subscribe
            await self.connector._init_udp_manager()
            await self.connector.udp_manager.start()
            await self.connector._setup_qtx_udp_subscriptions()
            
            # Create a trade message
            timestamp_ns = int(time.time() * 1e9)
            update_id = 67890
            header = struct.pack("<iiqqqq", 3, 1, update_id, timestamp_ns, 0, 0)  # type=3 (trade buy), index=1
            trade_data = struct.pack("<dd", 45050.0, 0.5)  # price, amount
            trade_message = header + trade_data
            
            # Process the message
            await self.connector.udp_manager._process_message(trade_message)
            
            # Verify message was added to trade queue
            trade_queue = self.connector._udp_queues[self.trading_pair]["trade"]
            self.assertEqual(trade_queue.qsize(), 1)
            
            # Get the message
            message = await trade_queue.get()
            self.assertIsInstance(message, OrderBookMessage)
            self.assertEqual(message.type, OrderBookMessageType.TRADE)
            self.assertEqual(message.content["trading_pair"], self.trading_pair)
            self.assertEqual(message.content["trade_type"], float(TradeType.BUY.value))
            self.assertEqual(message.content["price"], "45050.0")
            self.assertEqual(message.content["amount"], "0.5")
            
    async def test_order_book_snapshot_building(self):
        """Test building order book snapshot from collected messages"""
        with patch('socket.socket') as mock_socket_class:
            mock_socket_class.return_value = self.mock_socket
            
            # Setup subscription
            exchange_info = EXCHANGE_CONNECTOR_CLASSES[self.exchange_backend.lower()]
            qtx_symbol = f"{exchange_info['exchange_name_on_qtx']}:{self.trading_pair.lower().replace('-', '')}"
            self.mock_socket.receive_buffer.append((f"1:{qtx_symbol}".encode(), (TEST_UDP_HOST, TEST_UDP_PORT)))
            
            # Initialize and subscribe
            await self.connector._init_udp_manager()
            await self.connector.udp_manager.start()
            await self.connector._setup_qtx_udp_subscriptions()
            
            # Mock the UDP manager's build_orderbook_snapshot method
            snapshot_data = {
                "trading_pair": self.trading_pair,
                "update_id": 12345,
                "bids": [[45000.0, 1.5], [44900.0, 2.5]],
                "asks": [[45100.0, 2.0], [45200.0, 3.0]],
                "timestamp": time.time()
            }
            
            with patch.object(self.connector.udp_manager, 'build_orderbook_snapshot', 
                            return_value=snapshot_data) as mock_build:
                # Call the snapshot builder
                snapshot = await self.connector._build_qtx_orderbook_snapshot(self.trading_pair)
                
                # Verify snapshot format
                self.assertIsInstance(snapshot, OrderBookMessage)
                self.assertEqual(snapshot.type, OrderBookMessageType.SNAPSHOT)
                self.assertEqual(snapshot.content["trading_pair"], self.trading_pair)
                self.assertEqual(snapshot.content["update_id"], 12345)
                self.assertEqual(len(snapshot.content["bids"]), 2)
                self.assertEqual(len(snapshot.content["asks"]), 2)
                
                # Verify build_orderbook_snapshot was called
                mock_build.assert_called_once_with(self.trading_pair)
                
    async def test_message_consumption_from_queues(self):
        """Test that messages are properly consumed from queues"""
        with patch('socket.socket') as mock_socket_class:
            mock_socket_class.return_value = self.mock_socket
            
            # Setup subscription
            exchange_info = EXCHANGE_CONNECTOR_CLASSES[self.exchange_backend.lower()]
            qtx_symbol = f"{exchange_info['exchange_name_on_qtx']}:{self.trading_pair.lower().replace('-', '')}"
            self.mock_socket.receive_buffer.append((f"1:{qtx_symbol}".encode(), (TEST_UDP_HOST, TEST_UDP_PORT)))
            
            # Initialize and subscribe
            await self.connector._init_udp_manager()
            await self.connector.udp_manager.start()
            await self.connector._setup_qtx_udp_subscriptions()
            
            # Create test messages
            diff_message = OrderBookMessage(
                message_type=OrderBookMessageType.DIFF,
                content={
                    "trading_pair": self.trading_pair,
                    "update_id": 12345,
                    "bids": [[45000.0, 1.5]],
                    "asks": [[45100.0, 2.0]],
                },
                timestamp=time.time()
            )
            
            trade_message = OrderBookMessage(
                message_type=OrderBookMessageType.TRADE,
                content={
                    "trading_pair": self.trading_pair,
                    "trade_type": float(TradeType.BUY.value),
                    "price": "45050.0",
                    "amount": "0.5",
                },
                timestamp=time.time()
            )
            
            # Add messages to queues
            diff_queue = self.connector._udp_queues[self.trading_pair]["diff"]
            trade_queue = self.connector._udp_queues[self.trading_pair]["trade"]
            
            await diff_queue.put(diff_message)
            await trade_queue.put(trade_message)
            
            # Test diff consumption
            output_queue = asyncio.Queue()
            consume_task = asyncio.create_task(
                self.connector._consume_messages_from_queue(
                    self.trading_pair, diff_queue, output_queue, "diff"
                )
            )
            
            # Wait briefly for consumption
            await asyncio.sleep(0.1)
            
            # Cancel the task
            consume_task.cancel()
            try:
                await consume_task
            except asyncio.CancelledError:
                pass
            
            # Verify message was forwarded
            self.assertEqual(output_queue.qsize(), 1)
            forwarded_message = await output_queue.get()
            self.assertEqual(forwarded_message, diff_message)
            
    async def test_data_source_listener_methods(self):
        """Test that the overridden data source listener methods work correctly"""
        # Get the data source
        data_source = self.connector.order_book_tracker.data_source
        
        # Test listen_for_subscriptions override
        with patch.object(self.connector, '_setup_qtx_udp_subscriptions', new_callable=AsyncMock) as mock_setup:
            await data_source.listen_for_subscriptions()
            mock_setup.assert_called_once()
            
        # Test listen_for_order_book_diffs override
        output_queue = asyncio.Queue()
        with patch.object(self.connector, '_consume_diff_messages', new_callable=AsyncMock) as mock_consume:
            await data_source.listen_for_order_book_diffs(self.local_event_loop, output_queue)
            mock_consume.assert_called_once_with(output_queue)
            
        # Test listen_for_order_book_snapshots override
        with patch.object(self.connector, '_handle_orderbook_snapshots', new_callable=AsyncMock) as mock_handle:
            await data_source.listen_for_order_book_snapshots(self.local_event_loop, output_queue)
            mock_handle.assert_called_once_with(output_queue)
            
        # Test listen_for_trades override
        with patch.object(self.connector, '_consume_trade_messages', new_callable=AsyncMock) as mock_consume:
            await data_source.listen_for_trades(self.local_event_loop, output_queue)
            mock_consume.assert_called_once_with(output_queue)
            
        # Test _order_book_snapshot override
        expected_snapshot = OrderBookMessage(
            message_type=OrderBookMessageType.SNAPSHOT,
            content={"trading_pair": self.trading_pair},
            timestamp=time.time()
        )
        
        with patch.object(self.connector, '_build_qtx_orderbook_snapshot', 
                         return_value=expected_snapshot) as mock_build:
            result = await data_source._order_book_snapshot(self.trading_pair)
            mock_build.assert_called_once_with(self.trading_pair)
            self.assertEqual(result, expected_snapshot)
            
    async def test_network_lifecycle(self):
        """Test the network start/stop lifecycle"""
        with patch('socket.socket') as mock_socket_class:
            mock_socket_class.return_value = self.mock_socket
            
            # Mock the parent's start_network method
            with patch.object(self.connector.__class__.__bases__[0], 'start_network', 
                            new_callable=AsyncMock) as mock_parent_start:
                # Start network
                await self.connector.start_network()
                
                # Verify parent start_network was called
                mock_parent_start.assert_called_once()
                
                # Verify UDP manager is initialized
                self.assertIsNotNone(self.connector._udp_manager)
                
            # Stop network
            with patch.object(self.connector.__class__.__bases__[0], 'stop_network', 
                            new_callable=AsyncMock) as mock_parent_stop:
                await self.connector.stop_network()
                
                # Verify parent stop_network was called
                mock_parent_stop.assert_called_once()
                
                # Verify UDP socket was closed
                self.assertTrue(self.mock_socket.closed)
                
    async def test_error_handling_in_message_processing(self):
        """Test error handling when processing messages"""
        with patch('socket.socket') as mock_socket_class:
            mock_socket_class.return_value = self.mock_socket
            
            # Setup subscription
            exchange_info = EXCHANGE_CONNECTOR_CLASSES[self.exchange_backend.lower()]
            qtx_symbol = f"{exchange_info['exchange_name_on_qtx']}:{self.trading_pair.lower().replace('-', '')}"
            self.mock_socket.receive_buffer.append((f"1:{qtx_symbol}".encode(), (TEST_UDP_HOST, TEST_UDP_PORT)))
            
            # Initialize and subscribe
            await self.connector._init_udp_manager()
            await self.connector.udp_manager.start()
            await self.connector._setup_qtx_udp_subscriptions()
            
            # Test handling of malformed message
            malformed_message = b"invalid_data"
            
            # Should not raise exception
            await self.connector.udp_manager._process_message(malformed_message)
            
            # Verify no messages were added to queues
            diff_queue = self.connector._udp_queues[self.trading_pair]["diff"]
            trade_queue = self.connector._udp_queues[self.trading_pair]["trade"]
            self.assertEqual(diff_queue.qsize(), 0)
            self.assertEqual(trade_queue.qsize(), 0)
            
    async def test_multiple_trading_pairs(self):
        """Test handling multiple trading pairs"""
        # Create connector with multiple pairs
        trading_pairs = ["BTC-USDT", "ETH-USDT"]
        connector = QtxPerpetualDerivative(
            client_config_map=self.client_config_map,
            qtx_perpetual_host=TEST_UDP_HOST,
            qtx_perpetual_port=TEST_UDP_PORT,
            exchange_backend=self.exchange_backend,
            trading_pairs=trading_pairs,
            trading_required=False
        )
        
        with patch('socket.socket') as mock_socket_class:
            mock_socket_class.return_value = self.mock_socket
            
            # Prepare subscription responses
            exchange_info = EXCHANGE_CONNECTOR_CLASSES[self.exchange_backend.lower()]
            for i, pair in enumerate(trading_pairs):
                qtx_symbol = f"{exchange_info['exchange_name_on_qtx']}:{pair.lower().replace('-', '')}"
                self.mock_socket.receive_buffer.append((f"{i+1}:{qtx_symbol}".encode(), ("127.0.0.1", 8080)))
            
            # Initialize and subscribe
            await connector._init_udp_manager()
            await connector.udp_manager.start()
            await connector._setup_qtx_udp_subscriptions()
            
            # Verify queues were created for all pairs
            for pair in trading_pairs:
                self.assertIn(pair, connector._udp_queues)
                queues = connector._udp_queues[pair]
                self.assertIn("diff", queues)
                self.assertIn("trade", queues)


class TestQtxOrderBookIntegrationWithDifferentExchanges(IsolatedAsyncioWrapperTestCase):
    """Test QTX order book integration with different parent exchanges"""
    
    # Mapping of exchange names to their order book data source modules/classes
    EXCHANGE_ORDER_BOOK_DATA_SOURCES = {
        "binance": {
            "module": "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_api_order_book_data_source",
            "class": "BinancePerpetualAPIOrderBookDataSource"
        },
        "okx": {
            "module": "hummingbot.connector.derivative.okx_perpetual.okx_perpetual_api_order_book_data_source",
            "class": "OkxPerpetualAPIOrderBookDataSource"
        },
        "bitget": {
            "module": "hummingbot.connector.derivative.bitget_perpetual.bitget_perpetual_api_order_book_data_source",
            "class": "BitgetPerpetualAPIOrderBookDataSource"
        },
        "bybit": {
            "module": "hummingbot.connector.derivative.bybit_perpetual.bybit_perpetual_api_order_book_data_source",
            "class": "BybitPerpetualAPIOrderBookDataSource"
        },
        "kucoin": {
            "module": "hummingbot.connector.derivative.kucoin_perpetual.kucoin_perpetual_api_order_book_data_source",
            "class": "KucoinPerpetualAPIOrderBookDataSource"
        },
        "gate_io": {
            "module": "hummingbot.connector.derivative.gate_io_perpetual.gate_io_perpetual_api_order_book_data_source",
            "class": "GateIoPerpetualAPIOrderBookDataSource"
        }
    }
    
    def setUp(self):
        super().setUp()
        self.client_config_map = MagicMock()
        # Fix throttler initialization by adding rate_limits_share_pct
        self.client_config_map.rate_limits_share_pct = Decimal("100")
        self.trading_pair = "BTC-USDT"
        
    async def _reset_udp_manager(self):
        """Reset the UDP manager singleton for clean tests"""
        manager = await QtxPerpetualUDPManager.get_instance()
        if manager._connection_state.is_connected:
            await manager.stop()
        
        manager._subscriptions.clear()
        manager._index_to_pair.clear()
        manager._message_queues.clear()
        
    def _test_exchange_backend_integration(self, exchange_name: str):
        """Generic test method for any exchange backend integration"""
        self.run_async_with_timeout(self._reset_udp_manager())
        
        self.client_config_map.derivative_qtx_perpetual_exchange_backend = exchange_name
        
        # Mock the parent exchange's order book data source
        data_source_info = self.EXCHANGE_ORDER_BOOK_DATA_SOURCES[exchange_name]
        patch_path = f"{data_source_info['module']}.{data_source_info['class']}"
        
        with patch(patch_path):
            # Pass exchange-specific parameters based on the backend
            init_params = {
                "client_config_map": self.client_config_map,
                "qtx_perpetual_host": TEST_UDP_HOST,
                "qtx_perpetual_port": TEST_UDP_PORT,
                "exchange_backend": exchange_name,
                "trading_pairs": [self.trading_pair],
                "trading_required": False,
            }
            
            # Add exchange-specific mock parameters
            if exchange_name == "binance":
                init_params["binance_perpetual_api_key"] = "mock_key"
                init_params["binance_perpetual_api_secret"] = "mock_secret"
            elif exchange_name == "okx":
                init_params["okx_perpetual_api_key"] = "mock_key"
                init_params["okx_perpetual_secret_key"] = "mock_secret"
                init_params["okx_perpetual_passphrase"] = "mock_passphrase"
            elif exchange_name == "bitget":
                init_params["bitget_perpetual_api_key"] = "mock_key"
                init_params["bitget_perpetual_secret_key"] = "mock_secret"
                init_params["bitget_perpetual_passphrase"] = "mock_passphrase"
            elif exchange_name == "bybit":
                init_params["bybit_perpetual_api_key"] = "mock_key"
                init_params["bybit_perpetual_secret_key"] = "mock_secret"
            elif exchange_name == "kucoin":
                init_params["kucoin_perpetual_api_key"] = "mock_key"
                init_params["kucoin_perpetual_secret_key"] = "mock_secret"
                init_params["kucoin_perpetual_passphrase"] = "mock_passphrase"
            elif exchange_name == "gate_io":
                init_params["gate_io_perpetual_api_key"] = "mock_key"
                init_params["gate_io_perpetual_secret_key"] = "mock_secret"
                init_params["gate_io_perpetual_user_id"] = "mock_user_id"
            
            connector = QtxPerpetualDerivative(**init_params)
            
            # Verify correct parent class
            exchange_info = EXCHANGE_CONNECTOR_CLASSES[exchange_name]
            module = importlib.import_module(exchange_info["module"])
            parent_class = getattr(module, exchange_info["class"])
            
            self.assertIsInstance(connector, parent_class)
            self.assertEqual(connector.name, "qtx_perpetual")
            
            # Verify the connector has the correct exchange backend set
            self.assertEqual(connector._exchange_backend, exchange_name)
            
            # Verify QTX-specific methods exist
            self.assertTrue(hasattr(connector, "_setup_qtx_market_data"))
            self.assertTrue(hasattr(connector, "_setup_qtx_udp_subscriptions"))
            self.assertTrue(hasattr(connector, "_consume_diff_messages"))
            self.assertTrue(hasattr(connector, "_consume_trade_messages"))
            
    def test_binance_backend_integration(self):
        """Test integration with Binance as parent exchange"""
        self._test_exchange_backend_integration("binance")
        
    def test_okx_backend_integration(self):
        """Test integration with OKX as parent exchange"""
        self._test_exchange_backend_integration("okx")
        
    def test_bitget_backend_integration(self):
        """Test integration with Bitget as parent exchange"""
        self._test_exchange_backend_integration("bitget")
        
    def test_bybit_backend_integration(self):
        """Test integration with Bybit as parent exchange"""
        self._test_exchange_backend_integration("bybit")
        
    def test_kucoin_backend_integration(self):
        """Test integration with KuCoin as parent exchange"""
        self._test_exchange_backend_integration("kucoin")
        
    def test_gate_io_backend_integration(self):
        """Test integration with Gate.io as parent exchange"""
        self._test_exchange_backend_integration("gate_io")
        
    def test_all_exchanges_have_correct_qtx_name_mapping(self):
        """Test that all exchanges have the correct QTX name mapping"""
        expected_mappings = {
            "binance": "binance-futures",
            "okx": "okx-futures",
            "bitget": "bitget-futures",
            "bybit": "bybit-futures",
            "kucoin": "kucoin-futures",
            "gate_io": "gate-io-futures",
        }
        
        for exchange, expected_qtx_name in expected_mappings.items():
            exchange_info = EXCHANGE_CONNECTOR_CLASSES[exchange]
            self.assertEqual(
                exchange_info["exchange_name_on_qtx"], 
                expected_qtx_name,
                f"Exchange {exchange} has incorrect QTX name mapping"
            )
            
    async def test_exchange_specific_subscription_formats(self):
        """Test that each exchange generates the correct subscription format"""
        self.run_async_with_timeout(self._reset_udp_manager())
        
        for exchange_name in ["binance", "okx", "bitget", "bybit", "kucoin", "gate_io"]:
            # Create mock socket for this test
            mock_socket = MockUDPSocket()
            
            with patch('socket.socket', return_value=mock_socket):
                # Create connector for this exchange
                init_params = {
                    "client_config_map": self.client_config_map,
                    "qtx_perpetual_host": TEST_UDP_HOST,
                    "qtx_perpetual_port": TEST_UDP_PORT,
                    "exchange_backend": exchange_name,
                    "trading_pairs": [self.trading_pair],
                    "trading_required": False,
                }
                
                connector = QtxPerpetualDerivative(**init_params)
                
                # Initialize UDP manager
                await connector._init_udp_manager()
                udp_manager = connector.udp_manager
                
                # Verify the exchange name is set correctly
                exchange_info = EXCHANGE_CONNECTOR_CLASSES[exchange_name]
                self.assertEqual(udp_manager._exchange_name, exchange_info["exchange_name_on_qtx"])
                
                # Start and subscribe
                await udp_manager.start()
                await connector._setup_qtx_udp_subscriptions()
                
                # Verify the subscription format
                expected_symbol = f"{exchange_info['exchange_name_on_qtx']}:{self.trading_pair.lower().replace('-', '')}"
                sent_symbols = [data[0].decode() for data in mock_socket.sent_data if data[0] != b"ping"]
                self.assertIn(expected_symbol, sent_symbols, 
                             f"Exchange {exchange_name} did not send correct subscription format")
            
            # Reset UDP manager for next iteration
            await self._reset_udp_manager()


if __name__ == "__main__":
    import unittest
    unittest.main()