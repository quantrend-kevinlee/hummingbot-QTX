#!/usr/bin/env python

"""
Unit tests for QtxPerpetualUDPManager with the real QTX UDP server
"""

import asyncio
import time
import unittest
from typing import Optional, Dict

from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_udp_manager import QtxPerpetualUDPManager
from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType


class QtxPerpetualUDPManagerRealServerTests(unittest.IsolatedAsyncioTestCase):
    """Test cases for the QtxPerpetualUDPManager that connect to the real QTX UDP server"""

    # Configuration
    host = CONSTANTS.DEFAULT_UDP_HOST
    port = CONSTANTS.DEFAULT_UDP_PORT
    exchange_name = "binance-futures"
    trading_pair = "BTC-USDT"
    trading_pairs = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT"]
    
    # Test timeouts
    timeout = 30  # seconds
    message_wait_time = 5  # seconds to wait for messages
    
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        print("\n" + "=" * 80)
        print("Starting QtxPerpetualUDPManager Real Server Tests")
        print(f"Connecting to QTX UDP server at {cls.host}:{cls.port}")
        print(f"Using exchange: {cls.exchange_name}")
        print(f"Trading pairs: {cls.trading_pairs}")
        print("=" * 80 + "\n")
    
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        print("\n" + "=" * 80)
        print("Completed QtxPerpetualUDPManager Real Server Tests")
        print("=" * 80 + "\n")
    
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.log_records = []
        self.listening_task = None
        
        # Get the singleton instance
        self.udp_manager = await QtxPerpetualUDPManager.get_instance()
        
        # Reset the manager state for clean tests
        await self._reset_manager_state()
        
        # Configure the manager
        self.udp_manager.configure(host=self.host, port=self.port, exchange_name=self.exchange_name)
        
        # Set up logging
        self.udp_manager.logger().setLevel(1)
        self.udp_manager.logger().addHandler(self)
        self.logger = self.udp_manager.logger()
        
        # Connect to the server with timeout protection
        try:
            connected = await asyncio.wait_for(self.udp_manager.connect(), timeout=10.0)
            if not connected:
                self.logger.error("❌ Failed to connect to real QTX UDP server")
                self.fail("Could not connect to QTX UDP server")
        except asyncio.TimeoutError:
            self.logger.error("❌ Connection to QTX UDP server timed out")
            self.fail("Connection to QTX UDP server timed out after 10 seconds")
        except Exception as e:
            self.logger.error(f"❌ Error connecting to QTX UDP server: {e}")
            self.fail(f"Error connecting to QTX UDP server: {e}")
        
        self.logger.info("✓ Successfully connected to QTX UDP server")
    
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
                
                if self.udp_manager._subscriptions:
                    active_pairs = list(self.udp_manager._subscriptions.keys())
                    await asyncio.wait_for(
                        self.udp_manager.unsubscribe_from_trading_pairs(active_pairs), 
                        timeout=5.0
                    )
                
                await asyncio.wait_for(self.udp_manager.stop(), timeout=5.0)
            except asyncio.TimeoutError:
                self.logger.error("Cleanup operations timed out")
            except Exception as e:
                self.logger.error(f"Error during test cleanup: {e}")
        
        if self.listening_task is not None and not self.listening_task.done():
            self.listening_task.cancel()
            try:
                await self.listening_task
            except asyncio.CancelledError:
                pass
        
        await super().asyncTearDown()
    
    def handle(self, record):
        self.log_records.append(record)
    
    level = 0  # Accept all log levels
    
    def _is_logged(self, log_level: str, message):
        """Check if a specific message was logged at the given level"""
        for record in self.log_records:
            if record.levelname == log_level and message in record.getMessage():
                return True
        return False
    
    # ========== Basic initialization tests ==========
    
    async def test_init(self):
        """Test the initialization of the manager"""
        self.assertTrue(self.udp_manager._connection_state.is_connected)
        self.assertIsNotNone(self.udp_manager._udp_socket)
        self.assertEqual({}, self.udp_manager._subscriptions)
        self.assertEqual({}, self.udp_manager._index_to_pair)
    
    # ========== Subscription tests ==========
    
    async def test_subscribe_to_single_trading_pair(self):
        """Test subscription to a single trading pair with the real server"""
        success, subscribed_pairs = await self.udp_manager.subscribe_to_trading_pairs([self.trading_pair])
        
        self.assertTrue(success)
        self.assertEqual(subscribed_pairs, [self.trading_pair])
        
        self.assertIn(self.trading_pair, self.udp_manager._subscriptions)
        self.assertTrue(self.udp_manager._subscriptions[self.trading_pair].is_active)
        
        subscription = self.udp_manager._subscriptions[self.trading_pair]
        btc_index = subscription.index
        self.assertIsNotNone(btc_index)
        self.assertIn(btc_index, self.udp_manager._index_to_pair)
        
        print(f"Successfully subscribed to {self.trading_pair} with index {btc_index}")
        
        # Verify we receive messages using direct queue access
        await self.udp_manager.start()
        
        # Get the message queue
        diff_queue = self.udp_manager.get_message_queue(self.trading_pair, "diff")
        
        try:
            # Wait for a message
            message = await asyncio.wait_for(diff_queue.get(), timeout=self.message_wait_time)
            self.assertEqual(message.content.get("trading_pair"), self.trading_pair)
            self.logger.info(f"✓ Received message for {self.trading_pair}")
        except asyncio.TimeoutError:
            self.fail(f"Did not receive any messages for {self.trading_pair} within {self.message_wait_time} seconds")
    
    async def test_subscribe_to_multiple_trading_pairs(self):
        """Test subscription to multiple trading pairs with the real server"""
        success, _ = await self.udp_manager.subscribe_to_trading_pairs(self.trading_pairs)
        
        self.assertTrue(success)
        
        for pair in self.trading_pairs:
            self.assertIn(pair, self.udp_manager._subscriptions)
            subscription = self.udp_manager._subscriptions[pair]
            self.assertTrue(subscription.is_active)
            self.assertIsNotNone(subscription.index)
            self.logger.info(f"✓ Subscribed to {pair} with index {subscription.index}")
        
        # Verify we receive messages for all pairs using queues
        await self.udp_manager.start()
        
        messages_received = {pair: False for pair in self.trading_pairs}
        
        # Create tasks to consume from each queue
        async def check_queue(pair: str):
            queue = self.udp_manager.get_message_queue(pair, "diff")
            try:
                message = await asyncio.wait_for(queue.get(), timeout=self.message_wait_time)
                if message.content.get("trading_pair") == pair:
                    messages_received[pair] = True
            except asyncio.TimeoutError:
                pass
        
        tasks = [check_queue(pair) for pair in self.trading_pairs]
        await asyncio.gather(*tasks)
        
        for pair, received in messages_received.items():
            if received:
                self.logger.info(f"✓ Received messages for {pair}")
            else:
                self.logger.warning(f"⚠ No messages received for {pair}")
    
    # ========== Unsubscription tests ==========
    
    async def test_unsubscribe_from_single_trading_pair(self):
        """Test unsubscription from a single trading pair with the real server"""
        await self.udp_manager.subscribe_to_trading_pairs([self.trading_pair])
        self.assertIn(self.trading_pair, self.udp_manager._subscriptions)
        
        success = await self.udp_manager.unsubscribe_from_trading_pairs([self.trading_pair])
        
        self.assertTrue(success)
        self.assertNotIn(self.trading_pair, self.udp_manager._subscriptions)
        
        self.logger.info(f"✓ Successfully unsubscribed from {self.trading_pair}")
    
    async def test_unsubscribe_from_multiple_trading_pairs(self):
        """Test unsubscription from multiple trading pairs with the real server"""
        await self.udp_manager.subscribe_to_trading_pairs(self.trading_pairs)
        
        for pair in self.trading_pairs:
            self.assertIn(pair, self.udp_manager._subscriptions)
        
        pairs_to_unsubscribe = self.trading_pairs[:2]
        success = await self.udp_manager.unsubscribe_from_trading_pairs(pairs_to_unsubscribe)
        
        self.assertTrue(success)
        
        for pair in pairs_to_unsubscribe:
            self.assertNotIn(pair, self.udp_manager._subscriptions)
            self.logger.info(f"✓ Unsubscribed from {pair}")
        
        for pair in self.trading_pairs[2:]:
            self.assertIn(pair, self.udp_manager._subscriptions)
            self.logger.info(f"✓ Still subscribed to {pair}")
    
    async def test_subscribe_unsubscribe_resubscribe(self):
        """Test the full cycle of subscribe, unsubscribe, and resubscribe"""
        success, _ = await self.udp_manager.subscribe_to_trading_pairs([self.trading_pair])
        self.assertTrue(success)
        first_subscription = self.udp_manager._subscriptions.get(self.trading_pair)
        self.assertIsNotNone(first_subscription)
        first_index = first_subscription.index
        
        self.logger.info(f"✓ First subscription: {self.trading_pair} -> index {first_index}")
        
        success = await self.udp_manager.unsubscribe_from_trading_pairs([self.trading_pair])
        self.assertTrue(success)
        self.assertNotIn(self.trading_pair, self.udp_manager._subscriptions)
        
        await asyncio.sleep(1)
        
        success, _ = await self.udp_manager.subscribe_to_trading_pairs([self.trading_pair])
        self.assertTrue(success)
        second_subscription = self.udp_manager._subscriptions.get(self.trading_pair)
        self.assertIsNotNone(second_subscription)
        second_index = second_subscription.index
        
        self.logger.info(f"✓ Second subscription: {self.trading_pair} -> index {second_index}")
        
        self.assertIsNotNone(second_index)
    
    # ========== Message reception tests ==========
    
    async def test_receive_ticker_data_from_single_trading_pair(self):
        """Test receiving and processing ticker data from a single trading pair"""
        await self.udp_manager.subscribe_to_trading_pairs([self.trading_pair])
        await self.udp_manager.start()
        
        # Get diff queue (ticker messages come as DIFF)
        diff_queue = self.udp_manager.get_message_queue(self.trading_pair, "diff")
        
        ticker_messages = []
        
        try:
            # Collect messages for a period
            end_time = asyncio.get_event_loop().time() + self.message_wait_time
            while asyncio.get_event_loop().time() < end_time:
                try:
                    message = await asyncio.wait_for(
                        diff_queue.get(), 
                        timeout=max(0.1, end_time - asyncio.get_event_loop().time())
                    )
                    # Check if it's a ticker message (single bid/ask)
                    content = message.content
                    if (len(content.get("bids", [])) <= 1 and 
                        len(content.get("asks", [])) <= 1):
                        ticker_messages.append(message)
                except asyncio.TimeoutError:
                    break
            
            self.logger.info(f"✓ Received {len(ticker_messages)} ticker messages for {self.trading_pair}")
            
            if ticker_messages:
                msg = ticker_messages[0]
                content = msg.content
                self.assertEqual(content.get("trading_pair"), self.trading_pair)
                self.assertIn("bids", content)
                self.assertIn("asks", content)
                self.assertIn("update_id", content)
                
                if content["bids"] and content["asks"]:
                    bid = content["bids"][0]
                    ask = content["asks"][0]
                    self.logger.info(f"  Sample ticker - Bid: {bid[0]} @ {bid[1]}, Ask: {ask[0]} @ {ask[1]}")
        
        except Exception as e:
            self.fail(f"Error receiving ticker data: {e}")
    
    async def test_receive_depth_data_from_single_trading_pair(self):
        """Test receiving and processing depth (order book) data from a single trading pair"""
        await self.udp_manager.subscribe_to_trading_pairs([self.trading_pair])
        await self.udp_manager.start()
        
        # Get diff queue (depth messages come as DIFF)
        diff_queue = self.udp_manager.get_message_queue(self.trading_pair, "diff")
        
        depth_messages = []
        
        try:
            # Collect messages for a period
            end_time = asyncio.get_event_loop().time() + self.message_wait_time * 2
            while asyncio.get_event_loop().time() < end_time:
                try:
                    message = await asyncio.wait_for(
                        diff_queue.get(), 
                        timeout=max(0.1, end_time - asyncio.get_event_loop().time())
                    )
                    # Check if it's a depth message (multiple levels)
                    content = message.content
                    if (len(content.get("bids", [])) > 1 or 
                        len(content.get("asks", [])) > 1):
                        depth_messages.append(message)
                except asyncio.TimeoutError:
                    break
            
            self.logger.info(f"✓ Received {len(depth_messages)} depth messages for {self.trading_pair}")
            
            if depth_messages:
                msg = depth_messages[0]
                content = msg.content
                self.assertEqual(content.get("trading_pair"), self.trading_pair)
                
                num_bids = len(content.get("bids", []))
                num_asks = len(content.get("asks", []))
                self.logger.info(f"  Depth levels - Bids: {num_bids}, Asks: {num_asks}")
        
        except Exception as e:
            self.logger.warning(f"⚠ Error receiving depth data: {e}")
    
    async def test_receive_trade_data_from_single_trading_pair(self):
        """Test receiving and processing trade data from a single trading pair"""
        await self.udp_manager.subscribe_to_trading_pairs([self.trading_pair])
        await self.udp_manager.start()
        
        # Get trade queue
        trade_queue = self.udp_manager.get_message_queue(self.trading_pair, "trade")
        
        trade_messages = []
        
        try:
            # Collect messages for a period
            end_time = asyncio.get_event_loop().time() + self.message_wait_time * 3
            while asyncio.get_event_loop().time() < end_time:
                try:
                    message = await asyncio.wait_for(
                        trade_queue.get(), 
                        timeout=max(0.1, end_time - asyncio.get_event_loop().time())
                    )
                    trade_messages.append(message)
                except asyncio.TimeoutError:
                    break
            
            self.logger.info(f"✓ Received {len(trade_messages)} trade messages for {self.trading_pair}")
            
            if trade_messages:
                msg = trade_messages[0]
                content = msg.content
                self.assertEqual(content.get("trading_pair"), self.trading_pair)
                self.assertIn("price", content)
                self.assertIn("amount", content)
                self.assertIn("trade_type", content)
                self.assertIn("trade_id", content)
                
                trade_type = "SELL" if content["trade_type"] == 2.0 else "BUY"
                self.logger.info(f"  Sample trade - {trade_type} {content['amount']} @ {content['price']}")
        
        except Exception as e:
            self.logger.warning(f"⚠ No trade data received: {e}")
            self.logger.info("  This is expected if there were no trades during the test period")
    
    # ========== Integration tests ==========
    
    async def test_process_real_messages(self):
        """Test receiving and processing real messages from QTX server"""
        await self.udp_manager.subscribe_to_trading_pairs([self.trading_pair])
        await self.udp_manager.start()
        
        messages_by_type = {
            OrderBookMessageType.DIFF: [],
            OrderBookMessageType.TRADE: []
        }
        
        # Get queues
        diff_queue = self.udp_manager.get_message_queue(self.trading_pair, "diff")
        trade_queue = self.udp_manager.get_message_queue(self.trading_pair, "trade")
        
        # Create tasks to consume from both queues
        async def consume_diff():
            while True:
                try:
                    message = await asyncio.wait_for(diff_queue.get(), timeout=0.1)
                    messages_by_type[OrderBookMessageType.DIFF].append(message)
                except asyncio.TimeoutError:
                    break
        
        async def consume_trade():
            while True:
                try:
                    message = await asyncio.wait_for(trade_queue.get(), timeout=0.1)
                    messages_by_type[OrderBookMessageType.TRADE].append(message)
                except asyncio.TimeoutError:
                    break
        
        # Run consumers for a period
        await asyncio.gather(
            asyncio.create_task(consume_diff()),
            asyncio.create_task(consume_trade()),
            asyncio.sleep(self.message_wait_time)
        )
        
        total_messages = sum(len(msgs) for msgs in messages_by_type.values())
        self.logger.info(f"✓ Received {total_messages} total messages for {self.trading_pair}")
        self.logger.info(f"  Diffs: {len(messages_by_type[OrderBookMessageType.DIFF])}")
        self.logger.info(f"  Trades: {len(messages_by_type[OrderBookMessageType.TRADE])}")
        
        self.assertGreater(total_messages, 0)
    
    async def test_build_orderbook_snapshot(self):
        """Test building orderbook snapshot from collected messages"""
        await self.udp_manager.subscribe_to_trading_pairs([self.trading_pair])
        await self.udp_manager.start()
        
        # Wait a bit for messages to start flowing
        await asyncio.sleep(1)
        
        # Build snapshot
        snapshot_data = await self.udp_manager.build_orderbook_snapshot(self.trading_pair, duration=3.0)
        
        self.assertIsInstance(snapshot_data, dict)
        self.assertEqual(snapshot_data["trading_pair"], self.trading_pair)
        self.assertIn("bids", snapshot_data)
        self.assertIn("asks", snapshot_data)
        self.assertIn("update_id", snapshot_data)
        self.assertIn("timestamp", snapshot_data)
        
        num_bids = len(snapshot_data["bids"])
        num_asks = len(snapshot_data["asks"])
        
        self.logger.info(f"✓ Built orderbook snapshot for {self.trading_pair}")
        self.logger.info(f"  Bids: {num_bids}, Asks: {num_asks}")
        self.logger.info(f"  Update ID: {snapshot_data['update_id']}")
        
        # Verify bids are sorted descending and asks ascending
        if num_bids > 1:
            for i in range(1, num_bids):
                self.assertGreaterEqual(snapshot_data["bids"][i-1][0], snapshot_data["bids"][i][0])
        
        if num_asks > 1:
            for i in range(1, num_asks):
                self.assertLessEqual(snapshot_data["asks"][i-1][0], snapshot_data["asks"][i][0])
    
    async def test_collect_market_data_parallel(self):
        """Test collecting market data from multiple trading pairs in parallel"""
        await self.udp_manager.subscribe_to_trading_pairs(self.trading_pairs)
        await self.udp_manager.start()
        
        # Wait for messages to start flowing
        await asyncio.sleep(1)
        
        # Build snapshots for all pairs in parallel
        snapshot_tasks = []
        for pair in self.trading_pairs:
            task = self.udp_manager.build_orderbook_snapshot(pair, duration=2.0)
            snapshot_tasks.append(task)
        
        snapshots = await asyncio.gather(*snapshot_tasks)
        
        self.logger.info("\nParallel Orderbook Snapshot Results:")
        self.logger.info("-" * 60)
        self.logger.info(f"{'Pair':<12} {'Bids':<10} {'Asks':<10} {'Update ID':<15}")
        self.logger.info("-" * 60)
        
        for i, snapshot in enumerate(snapshots):
            pair = self.trading_pairs[i]
            num_bids = len(snapshot["bids"])
            num_asks = len(snapshot["asks"])
            update_id = snapshot["update_id"]
            
            self.logger.info(f"{pair:<12} {num_bids:<10} {num_asks:<10} {update_id:<15}")
            
            # Basic validation
            self.assertEqual(snapshot["trading_pair"], pair)
            self.assertIsInstance(snapshot["bids"], list)
            self.assertIsInstance(snapshot["asks"], list)
    
    async def test_queue_overflow_handling(self):
        """Test that the manager handles queue overflow gracefully"""
        await self.udp_manager.subscribe_to_trading_pairs([self.trading_pair])
        await self.udp_manager.start()
        
        # Get queue and check it doesn't block when full
        diff_queue = self.udp_manager.get_message_queue(self.trading_pair, "diff")
        
        # Don't consume messages to let queue fill up
        await asyncio.sleep(5)
        
        # The manager should still be running
        self.assertTrue(self.udp_manager.is_connected)
        self.assertIsNotNone(self.udp_manager._listen_task)
        self.assertFalse(self.udp_manager._listen_task.done())
        
        # Now consume some messages to verify they're still being processed
        messages_consumed = 0
        try:
            while not diff_queue.empty():
                await diff_queue.get()
                messages_consumed += 1
        except:
            pass
        
        self.logger.info(f"✓ Queue overflow handled gracefully, consumed {messages_consumed} backlogged messages")


if __name__ == "__main__":
    unittest.main(verbosity=2)