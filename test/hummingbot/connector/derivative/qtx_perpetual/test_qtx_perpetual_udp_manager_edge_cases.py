#!/usr/bin/env python

"""
Edge case and performance tests for QtxPerpetualUDPManager
"""

import asyncio
import socket
import struct
import sys
import time
import unittest
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_udp_manager import (
    ConnectionState,
    QtxPerpetualUDPManager,
    SubscriptionState,
)
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType


class QtxPerpetualUDPManagerEdgeCaseTests(IsolatedAsyncioWrapperTestCase):
    """Edge case tests for the QtxPerpetualUDPManager"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.log_records = []
        
        # Get the singleton instance
        self.udp_manager = await QtxPerpetualUDPManager.get_instance()
        
        # Reset the manager state for clean tests
        await self._reset_manager_state()
        
        # Configure the manager
        self.udp_manager.configure(exchange_name="binance-futures")
        
        # Set up logging
        self.udp_manager.logger().setLevel(1)
        self.udp_manager.logger().addHandler(self)
        self.logger = self.udp_manager.logger()
    
    async def _reset_manager_state(self):
        """Reset the singleton manager to a clean state"""
        if self.udp_manager._connection_state.is_connected:
            await asyncio.wait_for(self.udp_manager.stop(), timeout=5.0)
        
        self.udp_manager._subscriptions.clear()
        self.udp_manager._index_to_pair.clear()
        self.udp_manager._message_queues.clear()
        self.udp_manager._last_update_id.clear()
    
    async def asyncTearDown(self) -> None:
        if hasattr(self, "udp_manager"):
            try:
                if self.udp_manager._listen_task is not None and not self.udp_manager._listen_task.done():
                    await asyncio.wait_for(self.udp_manager.stop(), timeout=5.0)
                await asyncio.wait_for(self.udp_manager.stop(), timeout=5.0)
            except asyncio.TimeoutError:
                self.logger.error("Cleanup operations timed out")
            except Exception as e:
                self.logger.error(f"Error during test cleanup: {e}")
        
        await super().asyncTearDown()
    
    def handle(self, record):
        self.log_records.append(record)
    
    level = 0  # Accept all log levels
    
    # ========== Edge Case Tests ==========
    
    @patch('socket.socket')
    async def test_connection_timeout(self, mock_socket_class):
        """Test handling of connection timeout"""
        mock_socket = MagicMock()
        mock_socket.setblocking = MagicMock()
        mock_socket.settimeout = MagicMock()
        mock_socket.sendto = MagicMock(side_effect=socket.timeout("Connection timed out"))
        mock_socket_class.return_value = mock_socket
        
        # Test connection with timeout protection
        try:
            connected = await asyncio.wait_for(self.udp_manager.connect(), timeout=5.0)
        except asyncio.TimeoutError:
            self.fail("Connection test should not hang - it should handle timeout gracefully")
        
        self.assertFalse(connected)
        self.assertFalse(self.udp_manager._connection_state.is_connected)
    
    @patch('socket.socket')
    async def test_malformed_message_handling(self, mock_socket_class):
        """Test handling of malformed UDP messages"""
        mock_socket = MagicMock()
        mock_socket.setblocking = MagicMock()
        mock_socket.settimeout = MagicMock()
        mock_socket.sendto = MagicMock(return_value=10)
        
        timestamp_ns = int(time.time() * 1e9)
        malformed_messages = [
            b"",  # Empty message
            b"TOO_SHORT",  # Too short for header
            b"\x00" * 5,  # Invalid header format
            b"\x00" * 39,  # Just under minimum header size
            # Valid header but incomplete body for ticker
            struct.pack("<iiqqqq", 1, 100, 12345, timestamp_ns, timestamp_ns, timestamp_ns) + b"SHORT",
            # Unknown message type
            struct.pack("<iiqqqq", 999, 100, 12345, timestamp_ns, timestamp_ns, timestamp_ns) + 
            struct.pack("<dd", 50000.0, 1.0),
        ]
        
        mock_socket.recvfrom = MagicMock(
            side_effect=[(msg, ("127.0.0.1", 8080)) for msg in malformed_messages]
        )
        mock_socket_class.return_value = mock_socket
        
        await self.udp_manager.connect()
        self.udp_manager._udp_socket = mock_socket
        self.udp_manager._connection_state.is_connected = True
        
        # Subscribe to a trading pair
        self.udp_manager._subscriptions["BTC-USDT"] = SubscriptionState(
            trading_pair="BTC-USDT",
            qtx_symbol="binance-futures:btcusdt",
            index=100,
            is_active=True
        )
        self.udp_manager._index_to_pair[100] = "BTC-USDT"
        
        # Get message queue to capture messages
        diff_queue = self.udp_manager.get_message_queue("BTC-USDT", "diff")
        
        listen_task = asyncio.create_task(self.udp_manager._listen_for_messages())
        
        await asyncio.sleep(0.1)
        
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass
        
        # No valid messages should be in the queue
        self.assertTrue(diff_queue.empty())
        
        warning_logs = [r for r in self.log_records if r.levelname in ["WARNING", "ERROR"]]
        self.assertGreater(len(warning_logs), 0)
    
    async def test_subscription_with_no_exchange_name(self):
        """Test subscription when exchange name is not set"""
        self.udp_manager._exchange_name = None
        
        success, _ = await self.udp_manager.subscribe_to_trading_pairs(["BTC-USDT"])
        
        self.assertFalse(success)
        self.assertEqual(len(self.udp_manager._subscriptions), 0)
    
    async def test_rapid_subscribe_unsubscribe(self):
        """Test rapid subscription and unsubscription cycles"""
        mock_socket = MagicMock()
        mock_socket.setblocking = MagicMock()
        mock_socket.settimeout = MagicMock()
        mock_socket.sendto = MagicMock(return_value=10)
        
        index_counter = 100
        def mock_recvfrom(*args):
            nonlocal index_counter
            response = f"{index_counter}:binance-futures:btcusdt".encode("utf-8")
            index_counter += 1
            return (response, ("127.0.0.1", 8080))
        
        mock_socket.recvfrom = MagicMock(side_effect=mock_recvfrom)
        
        with patch('socket.socket', return_value=mock_socket):
            await self.udp_manager.connect()
            self.udp_manager._connection_state.is_connected = True
            
            for i in range(10):
                success, _ = await self.udp_manager.subscribe_to_trading_pairs(["BTC-USDT"])
                self.assertTrue(success)
                self.assertIn("BTC-USDT", self.udp_manager._subscriptions)
                
                success = await self.udp_manager.unsubscribe_from_trading_pairs(["BTC-USDT"])
                self.assertTrue(success)
                self.assertNotIn("BTC-USDT", self.udp_manager._subscriptions)
    
    async def test_concurrent_subscriptions(self):
        """Test concurrent subscription requests"""
        mock_socket = MagicMock()
        mock_socket.setblocking = MagicMock()
        mock_socket.settimeout = MagicMock()
        mock_socket.sendto = MagicMock(return_value=10)
        
        index_counter = 100
        def mock_recvfrom(*args):
            nonlocal index_counter
            response = f"{index_counter}:binance-futures:symbol".encode("utf-8")
            index_counter += 1
            return (response, ("127.0.0.1", 8080))
        
        mock_socket.recvfrom = MagicMock(side_effect=mock_recvfrom)
        
        with patch('socket.socket', return_value=mock_socket):
            await self.udp_manager.connect()
            self.udp_manager._connection_state.is_connected = True
            
            pairs = [f"{base}-USDT" for base in ["BTC", "ETH", "SOL", "DOGE", "ADA"]]
            tasks = []
            
            for pair in pairs:
                task = asyncio.create_task(self.udp_manager.subscribe_to_trading_pairs([pair]))
                tasks.append(task)
            
            results = await asyncio.gather(*tasks)
            
            self.assertTrue(all(result[0] for result in results))
            
            for pair in pairs:
                self.assertIn(pair, self.udp_manager._subscriptions)
    
    async def test_message_processing_under_load(self):
        """Test message processing under high load conditions"""
        self.udp_manager._subscriptions["BTC-USDT"] = SubscriptionState(
            trading_pair="BTC-USDT",
            qtx_symbol="binance-futures:btcusdt",
            index=100,
            is_active=True
        )
        self.udp_manager._index_to_pair[100] = "BTC-USDT"
        
        # Get message queue to capture messages
        diff_queue = self.udp_manager.get_message_queue("BTC-USDT", "diff")
        
        timestamp_ns = int(time.time() * 1e9)
        start_time = time.time()
        
        # Process 1000 ticker messages
        for i in range(1000):
            header = struct.pack(
                "<iiqqqq", 1, 100, i, 
                timestamp_ns + i * 1000, timestamp_ns + i * 1000, timestamp_ns + i * 1000
            )
            body = struct.pack("<dd", 50000.0 + i, 1.0 + i * 0.1)
            message_data = header + body
            
            await self.udp_manager._process_message(message_data)
        
        processing_time = time.time() - start_time
        
        # Count messages in queue
        processed_messages = 0
        while not diff_queue.empty():
            await diff_queue.get()
            processed_messages += 1
        
        self.assertEqual(processed_messages, 1000)
        
        rate = processed_messages / processing_time if processing_time > 0 else 0
        self.logger.info(
            f"Processed {processed_messages} messages in {processing_time:.3f}s ({rate:.0f} msg/s)"
        )
        
        self.assertGreater(rate, 1000)
    
    async def test_connection_recovery_with_subscriptions(self):
        """Test that subscriptions are maintained after connection recovery"""
        mock_socket = MagicMock()
        mock_socket.setblocking = MagicMock()
        mock_socket.settimeout = MagicMock()
        mock_socket.sendto = MagicMock(return_value=10)
        
        index_counter = 100
        def mock_recvfrom(*args):
            nonlocal index_counter
            response = f"{index_counter}:binance-futures:symbol".encode("utf-8")
            index_counter += 1
            return (response, ("127.0.0.1", 8080))
        
        mock_socket.recvfrom = MagicMock(side_effect=mock_recvfrom)
        
        with patch('socket.socket', return_value=mock_socket):
            await asyncio.wait_for(self.udp_manager.connect(), timeout=5.0)
            self.udp_manager._connection_state.is_connected = True
            
            await asyncio.wait_for(
                self.udp_manager.subscribe_to_trading_pairs(["BTC-USDT", "ETH-USDT"]), 
                timeout=10.0
            )
            
            self.assertEqual(len(self.udp_manager._subscriptions), 2)
            
            # Simulate connection loss
            self.udp_manager._connection_state.is_connected = False
            self.udp_manager._connection_state.consecutive_failures = 3
            
            await asyncio.wait_for(self.udp_manager.connect(), timeout=5.0)
            
            self.assertEqual(len(self.udp_manager._subscriptions), 2)
            self.assertIn("BTC-USDT", self.udp_manager._subscriptions)
            self.assertIn("ETH-USDT", self.udp_manager._subscriptions)
    
    async def test_queue_overflow_protection(self):
        """Test that queue overflow is handled gracefully"""
        self.udp_manager._subscriptions["BTC-USDT"] = SubscriptionState(
            trading_pair="BTC-USDT",
            qtx_symbol="binance-futures:btcusdt",
            index=100,
            is_active=True
        )
        self.udp_manager._index_to_pair[100] = "BTC-USDT"
        
        # Get message queue
        diff_queue = self.udp_manager.get_message_queue("BTC-USDT", "diff")
        
        timestamp_ns = int(time.time() * 1e9)
        
        # Fill queue beyond capacity
        for i in range(1200):  # Queue max is 1000
            header = struct.pack("<iiqqqq", 1, 100, i, timestamp_ns, timestamp_ns, timestamp_ns)
            body = struct.pack("<dd", 50000.0, 1.0)
            message_data = header + body
            
            await self.udp_manager._process_message(message_data)
        
        # Queue should not exceed its max size
        queue_size = diff_queue.qsize()
        self.assertLessEqual(queue_size, 1000)
        self.logger.info(f"Queue size after overflow test: {queue_size}")
    
    async def test_invalid_message_type_validation(self):
        """Test validation of invalid message types for get_message_queue"""
        try:
            self.udp_manager.get_message_queue("BTC-USDT", "invalid_type")
            self.fail("Should have raised ValueError for invalid message type")
        except ValueError as e:
            self.assertIn("Invalid message_type", str(e))
        
        try:
            self.udp_manager.get_message_queue("BTC-USDT", "snapshot")
            self.fail("Should have raised ValueError for snapshot message type")
        except ValueError as e:
            self.assertIn("Invalid message_type", str(e))
    
    # ========== Performance Benchmark Tests ==========
    
    async def test_message_parsing_performance(self):
        """Benchmark message parsing performance"""
        messages = []
        timestamp_ns = int(time.time() * 1e9)
        
        # Generate test messages
        for i in range(2000):
            if i % 3 == 0:
                # Ticker message
                msg_data = struct.pack(
                    "<iiqqqq", 
                    1, 100, i, timestamp_ns, timestamp_ns + 1000, timestamp_ns + 2000
                ) + struct.pack("<dd", 50000.0 + i * 0.1, 1.0 + i * 0.01)
                messages.append(msg_data)
            elif i % 3 == 1:
                # Depth message
                header = struct.pack(
                    "<iiqqqq", 2, 100, i, timestamp_ns, timestamp_ns + 1000, timestamp_ns + 2000
                )
                counts = struct.pack("<qq", 2, 2)
                asks = struct.pack("<dd", 50001.0, 1.0) + struct.pack("<dd", 50002.0, 2.0)
                bids = struct.pack("<dd", 49999.0, 1.0) + struct.pack("<dd", 49998.0, 2.0)
                messages.append(header + counts + asks + bids)
            else:
                # Trade message
                msg_data = struct.pack(
                    "<iiqqqq", 
                    3, 100, i, timestamp_ns, timestamp_ns + 1000, timestamp_ns + 2000
                ) + struct.pack("<dd", 50000.0, 0.5)
                messages.append(msg_data)
        
        self.udp_manager._subscriptions["BTC-USDT"] = SubscriptionState(
            trading_pair="BTC-USDT",
            qtx_symbol="binance-futures:btcusdt",
            index=100,
            is_active=True
        )
        self.udp_manager._index_to_pair[100] = "BTC-USDT"
        
        # Get queues to capture messages
        diff_queue = self.udp_manager.get_message_queue("BTC-USDT", "diff")
        trade_queue = self.udp_manager.get_message_queue("BTC-USDT", "trade")
        
        start_time = time.time()
        
        for message in messages:
            await self.udp_manager._process_message(message)
        
        elapsed = time.time() - start_time
        
        # Count processed messages
        diff_count = diff_queue.qsize()
        trade_count = trade_queue.qsize()
        total_processed = diff_count + trade_count
        
        # Calculate expected queued messages accounting for queue capacity limits
        expected_diff_total = len([i for i in range(len(messages)) if i % 3 in [0, 1]])
        expected_trade_total = len([i for i in range(len(messages)) if i % 3 == 2])
        
        # Account for queue size limits
        expected_diff = min(expected_diff_total, 1000)
        expected_trade = min(expected_trade_total, 1000)
        expected_queued = expected_diff + expected_trade
        
        rate = len(messages) / elapsed if elapsed > 0 else 0
        avg_time = elapsed / len(messages) * 1000000 if len(messages) > 0 else 0
        
        self.logger.info(f"\nMessage Parsing Performance:")
        self.logger.info(f"  Total messages: {len(messages)}")
        self.logger.info(f"  Queued messages: {total_processed} (diff: {diff_count}, trade: {trade_count})")
        self.logger.info(f"  Expected queued: {expected_queued}")
        self.logger.info(f"  Total time: {elapsed:.3f}s")
        self.logger.info(f"  Parsing rate: {rate:.0f} messages/second")
        self.logger.info(f"  Average time per message: {avg_time:.1f} microseconds")
        
        self.assertGreater(rate, 10000)
        self.assertEqual(total_processed, expected_queued)
    
    async def test_subscription_performance(self):
        """Benchmark subscription performance"""
        mock_socket = MagicMock()
        mock_socket.setblocking = MagicMock()
        mock_socket.settimeout = MagicMock()
        mock_socket.sendto = MagicMock(return_value=10)
        
        index_counter = 100
        def mock_recvfrom(*args):
            nonlocal index_counter
            response = f"{index_counter}:binance-futures:symbol".encode("utf-8")
            index_counter += 1
            return (response, ("127.0.0.1", 8080))
        
        mock_socket.recvfrom = MagicMock(side_effect=mock_recvfrom)
        
        with patch('socket.socket', return_value=mock_socket):
            await self.udp_manager.connect()
            self.udp_manager._connection_state.is_connected = True
            
            pairs = [f"TOKEN{i}-USDT" for i in range(100)]
            
            start_time = time.time()
            
            batch_size = 10
            for i in range(0, len(pairs), batch_size):
                batch = pairs[i:i + batch_size]
                await self.udp_manager.subscribe_to_trading_pairs(batch)
            
            elapsed = time.time() - start_time
            
            rate = len(pairs) / elapsed
            
            self.logger.info(f"\nSubscription Performance:")
            self.logger.info(f"  Total pairs: {len(pairs)}")
            self.logger.info(f"  Total time: {elapsed:.3f}s")
            self.logger.info(f"  Subscription rate: {rate:.0f} pairs/second")
            
            self.assertEqual(len(self.udp_manager._subscriptions), len(pairs))
    
    async def test_memory_efficiency(self):
        """Test memory efficiency with many subscriptions"""
        mock_socket = MagicMock()
        mock_socket.setblocking = MagicMock()
        mock_socket.settimeout = MagicMock()
        mock_socket.sendto = MagicMock(return_value=10)
        
        index_counter = 100
        def mock_recvfrom(*args):
            nonlocal index_counter
            response = f"{index_counter}:binance-futures:symbol".encode("utf-8")
            index_counter += 1
            return (response, ("127.0.0.1", 8080))
        
        mock_socket.recvfrom = MagicMock(side_effect=mock_recvfrom)
        
        with patch('socket.socket', return_value=mock_socket):
            await self.udp_manager.connect()
            self.udp_manager._connection_state.is_connected = True
            
            initial_size = sys.getsizeof(self.udp_manager._subscriptions)
            
            pairs = [f"TOKEN{i}-USDT" for i in range(1000)]
            await self.udp_manager.subscribe_to_trading_pairs(pairs)
            
            final_size = sys.getsizeof(self.udp_manager._subscriptions)
            per_subscription = (final_size - initial_size) / len(pairs)
            
            self.logger.info(f"\nMemory Efficiency:")
            self.logger.info(f"  Subscriptions: {len(pairs)}")
            self.logger.info(f"  Total memory: {final_size} bytes")
            self.logger.info(f"  Per subscription: {per_subscription:.1f} bytes")
            
            self.assertLess(per_subscription, 1000)
    
    async def test_build_orderbook_snapshot_performance(self):
        """Test performance of building orderbook snapshots"""
        mock_socket = MagicMock()
        mock_socket.setblocking = MagicMock()
        mock_socket.settimeout = MagicMock()
        mock_socket.sendto = MagicMock(return_value=10)
        mock_socket.recvfrom = MagicMock(
            return_value=(b"100:binance-futures:btcusdt", ("127.0.0.1", 8080))
        )
        
        with patch('socket.socket', return_value=mock_socket):
            await self.udp_manager.connect()
            self.udp_manager._connection_state.is_connected = True
            await self.udp_manager.subscribe_to_trading_pairs(["BTC-USDT"])
            await self.udp_manager.start()
            
            # Fill queue with messages
            diff_queue = self.udp_manager.get_message_queue("BTC-USDT", "diff")
            timestamp_ns = int(time.time() * 1e9)
            
            for i in range(100):
                header = struct.pack("<iiqqqq", 2, 100, i, timestamp_ns, timestamp_ns, timestamp_ns)
                counts = struct.pack("<qq", 5, 5)
                asks = b""
                bids = b""
                for j in range(5):
                    asks += struct.pack("<dd", 50000.0 + j, 1.0 + j * 0.1)
                    bids += struct.pack("<dd", 49999.0 - j, 1.0 + j * 0.1)
                message_data = header + counts + asks + bids
                await self.udp_manager._process_message(message_data)
            
            start_time = time.time()
            snapshot = await self.udp_manager.build_orderbook_snapshot("BTC-USDT", duration=1.0)
            elapsed = time.time() - start_time
            
            self.logger.info(f"\nSnapshot Building Performance:")
            self.logger.info(f"  Build time: {elapsed:.3f}s")
            self.logger.info(f"  Bids: {len(snapshot['bids'])}")
            self.logger.info(f"  Asks: {len(snapshot['asks'])}")
            
            self.assertLess(elapsed, 2.0)
            self.assertGreater(len(snapshot['bids']), 0)
            self.assertGreater(len(snapshot['asks']), 0)


class QtxPerpetualUDPManagerStressTests(IsolatedAsyncioWrapperTestCase):
    """Stress tests for the QtxPerpetualUDPManager"""
    
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.udp_manager = await QtxPerpetualUDPManager.get_instance()
        await self._reset_manager_state()
        self.udp_manager.configure(exchange_name="binance-futures")
    
    async def _reset_manager_state(self):
        """Reset the singleton manager to a clean state"""
        if self.udp_manager._connection_state.is_connected:
            await asyncio.wait_for(self.udp_manager.stop(), timeout=5.0)
        
        self.udp_manager._subscriptions.clear()
        self.udp_manager._index_to_pair.clear()
        self.udp_manager._message_queues.clear()
        self.udp_manager._last_update_id.clear()
    
    async def test_sustained_high_throughput(self):
        """Test sustained high throughput message processing"""
        # Set up subscriptions
        for i in range(10):
            index = 100 + i
            pair = f"TOKEN{i}-USDT"
            self.udp_manager._subscriptions[pair] = SubscriptionState(
                trading_pair=pair,
                qtx_symbol=f"binance-futures:token{i}usdt",
                index=index,
                is_active=True
            )
            self.udp_manager._index_to_pair[index] = pair
        
        # Get queues to capture messages
        message_queues = {}
        for i in range(10):
            pair = f"TOKEN{i}-USDT"
            message_queues[pair] = {
                "diff": self.udp_manager.get_message_queue(pair, "diff"),
                "trade": self.udp_manager.get_message_queue(pair, "trade")
            }
        
        start_time = time.time()
        timestamp_ns = int(time.time() * 1e9)
        
        # Process 5000 messages
        for msg_count in range(5000):
            index = 100 + (msg_count % 10)
            
            if msg_count % 3 == 0:
                # Ticker
                header = struct.pack(
                    "<iiqqqq", 1, index, msg_count, 
                    timestamp_ns, timestamp_ns, timestamp_ns
                )
                body = struct.pack("<dd", 50000.0 + msg_count * 0.1, 1.0)
                message_data = header + body
            elif msg_count % 3 == 1:
                # Depth
                header = struct.pack(
                    "<iiqqqq", 2, index, msg_count, 
                    timestamp_ns, timestamp_ns, timestamp_ns
                )
                counts = struct.pack("<qq", 1, 1)
                asks = struct.pack("<dd", 50001.0, 1.0)
                bids = struct.pack("<dd", 49999.0, 1.0)
                message_data = header + counts + asks + bids
            else:
                # Trade
                header = struct.pack(
                    "<iiqqqq", 3, index, msg_count, 
                    timestamp_ns, timestamp_ns, timestamp_ns
                )
                body = struct.pack("<dd", 50000.0, 0.5)
                message_data = header + body
            
            await self.udp_manager._process_message(message_data)
            timestamp_ns += 10 * 1000000  # 10ms between messages
        
        elapsed = time.time() - start_time
        
        # Count total messages processed
        total_messages = 0
        for pair, queues in message_queues.items():
            total_messages += queues["diff"].qsize() + queues["trade"].qsize()
        
        rate = total_messages / elapsed if elapsed > 0 else 0
        
        print(f"\nSustained Throughput Test:")
        print(f"  Duration: {elapsed:.1f}s")
        print(f"  Total messages: {total_messages}")
        print(f"  Average rate: {rate:.0f} messages/second")
        
        self.assertGreater(rate, 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)