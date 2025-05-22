#!/usr/bin/env python
"""
Comprehensive test suite for QTX perpetual's shared memory (SHM) manager.

This test suite covers the complete lifecycle of shared memory communication used
for high-performance order execution between Hummingbot and the QTX order manager.

Testing Strategy:
- Comprehensive mocking to avoid system resource dependencies
- Covers all order types, sides, and position sides for complete coverage
- Tests both JSON and text response parsing for robustness
- Includes timeout and error handling scenarios
- Uses IsolatedAsyncioWrapperTestCase for proper async test execution
"""
import asyncio
import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
import ctypes
import math

from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase

from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager import (
    QtxPerpetualSharedMemoryManager,
    Order,
    Queue,
    QTX_ORDER_TYPE_IOC,
    QTX_ORDER_TYPE_POST_ONLY,
    QTX_ORDER_TYPE_GTC,
    QTX_SIDE_BUY,
    QTX_SIDE_SELL,
    QTX_POS_SIDE_LONG,
    QTX_POS_SIDE_BOTH,
    QTX_POS_SIDE_SHORT,
)


class TestQtxPerpetualSharedMemoryManager(IsolatedAsyncioWrapperTestCase):
    """
    Test suite for QTX perpetual shared memory manager.

    Design Considerations:
    - Uses extensive mocking to isolate shared memory operations from system dependencies
    - Time mocking strategy handles logging system interference
    - API key initialization setup prevents test hangs in connect() operations
    - Tests cover universal error patterns that work across different exchanges
    """

    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.api_key = "test_api_key_12345"
        cls.api_secret = "test_api_secret_67890"
        cls.shm_name = "/test_shm_segment"
        cls.exchange_name_on_qtx = "binance-futures"

    async def asyncSetUp(self) -> None:
        """Set up mocking environment."""
        await super().asyncSetUp()

        # System mocks

        self.posix_ipc_patcher = patch(
            "hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.posix_ipc"
        )
        self.mock_posix_ipc = self.posix_ipc_patcher.start()

        # Mock posix_ipc exceptions
        self.mock_posix_ipc.Error = type("Error", (Exception,), {})
        self.mock_posix_ipc.ExistentialError = type("ExistentialError", (self.mock_posix_ipc.Error,), {})
        self.mock_posix_ipc.BusyError = type("BusyError", (self.mock_posix_ipc.Error,), {})
        self.mock_posix_ipc.PermissionsError = type("PermissionsError", (self.mock_posix_ipc.Error,), {})
        self.mock_posix_ipc.O_RDWR = 0x0002

        self.mock_shm_instance = MagicMock()
        self.mock_posix_ipc.SharedMemory.return_value = self.mock_shm_instance
        self.mock_shm_instance.fd = 123

        # Mock memory mapping
        self.mmap_patcher = patch("hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.mmap")
        self.mock_mmap = self.mmap_patcher.start()
        self.mock_mmap_instance = MagicMock()
        self.mock_mmap.mmap.return_value = self.mock_mmap_instance

        # === Queue and Order Structure Mocks ===

        self.queue_patcher = patch(
            "hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.Queue.from_buffer"
        )
        self.mock_queue_from_buffer = self.queue_patcher.start()

        self.mock_queue = MagicMock()
        self.mock_queue_from_buffer.return_value = self.mock_queue

        # Queue indices - direct attribute access avoids PropertyMock complexity
        self.queue_to_idx = 0
        self.queue_from_idx = 0
        self.mock_queue.to_idx = self.queue_to_idx
        self.mock_queue.from_idx = self.queue_from_idx

        # Mock order array with complete field initialization
        self.mock_orders = [MagicMock() for _ in range(10)]
        for i, mock_order in enumerate(self.mock_orders):
            # CRITICAL: All orders need non-zero res_ts_ns to avoid infinite polling loops
            # Tests with multiple consecutive order placements increment queue indices
            mock_order.res_ts_ns = 1631234567123456789 + i  # Unique timestamp per order
            mock_order.res_msg = b'{"status": 200, "result": {"orderId": "12345678", "status": "NEW"}}'
            mock_order.mode = 0
            mock_order.side = 0
            mock_order.pos_side = 0
            mock_order.client_order_id = 0
            mock_order.order_type = 0
            mock_order.price_match = 0
            mock_order.size = b""
            mock_order.price = b""
            mock_order.symbol = b""
            mock_order.api_key = b""
            mock_order.api_secret = b""

        # First order configured for API key initialization
        self.mock_orders[0].res_msg = b"API key initialized successfully"

        self.mock_queue.orders = self.mock_orders

        # === System Operation Mocks ===

        self.memset_patcher = patch(
            "hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.ctypes.memset"
        )
        self.mock_memset = self.memset_patcher.start()

        self.addressof_patcher = patch(
            "hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.ctypes.addressof"
        )
        self.mock_addressof = self.addressof_patcher.start()
        self.mock_addressof.return_value = 0x12345678

        self.sleep_patcher = patch(
            "hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.asyncio.sleep"
        )
        self.mock_sleep = self.sleep_patcher.start()
        self.mock_sleep.return_value = None

        # CRITICAL: Time mocking strategy
        # Global time.time() mocking affects logging system - use consistent lambda
        self.time_patcher = patch("hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.time.time")
        self.mock_time = self.time_patcher.start()
        self.mock_time.return_value = 1631234567.123
        self.mock_time.side_effect = lambda: 1631234567.123

        self.gc_patcher = patch("hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.gc.collect")
        self.mock_gc_collect = self.gc_patcher.start()

        # Mock logger to prevent interference during exception handling
        self.logger_patcher = patch.object(QtxPerpetualSharedMemoryManager, "logger")
        self.mock_logger_method = self.logger_patcher.start()
        self.mock_logger = MagicMock()
        self.mock_logger_method.return_value = self.mock_logger

        # Create system under test
        self.manager = QtxPerpetualSharedMemoryManager(
            api_key=self.api_key,
            api_secret=self.api_secret,
            shm_name=self.shm_name,
            exchange_name_on_qtx=self.exchange_name_on_qtx,
        )

    async def asyncTearDown(self) -> None:
        """Clean up all patches and resources"""
        try:
            self.posix_ipc_patcher.stop()
            self.mmap_patcher.stop()
            self.queue_patcher.stop()
            self.memset_patcher.stop()
            self.addressof_patcher.stop()
            self.sleep_patcher.stop()
            self.time_patcher.stop()
            self.gc_patcher.stop()
            self.logger_patcher.stop()
        except:
            pass

        await super().asyncTearDown()

    # ============================================================================
    # INITIALIZATION AND CONFIGURATION TESTS
    # ============================================================================

    def test_initialization_valid_parameters(self):
        """Test proper initialization with valid parameters"""
        self.assertEqual(self.manager._api_key, self.api_key)
        self.assertEqual(self.manager._api_secret, self.api_secret)
        self.assertEqual(self.manager._shm_name, self.shm_name)
        self.assertEqual(self.manager._exchange_name_on_qtx, self.exchange_name_on_qtx)
        self.assertFalse(self.manager._is_connected)
        self.assertFalse(self.manager._api_key_initialized)
        self.assertEqual(self.manager._pending_orders, {})
        self.assertIsNone(self.manager._last_connection_error)
        self.assertEqual(self.manager._connection_attempts, 0)

    def test_initialization_missing_shm_name(self):
        """Test initialization validation for shared memory name"""
        with self.assertRaises(ValueError) as context:
            QtxPerpetualSharedMemoryManager(
                api_key=self.api_key,
                api_secret=self.api_secret,
                shm_name="",
                exchange_name_on_qtx=self.exchange_name_on_qtx,
            )
        self.assertIn("Shared memory name", str(context.exception))

    def test_initialization_missing_api_key(self):
        """Test initialization validation for API key"""
        with self.assertRaises(ValueError) as context:
            QtxPerpetualSharedMemoryManager(
                api_key="",
                api_secret=self.api_secret,
                shm_name=self.shm_name,
                exchange_name_on_qtx=self.exchange_name_on_qtx,
            )
        self.assertIn("API key must be provided", str(context.exception))

    def test_initialization_missing_api_secret(self):
        """Test initialization validation for API secret"""
        with self.assertRaises(ValueError) as context:
            QtxPerpetualSharedMemoryManager(
                api_key=self.api_key,
                api_secret=None,
                shm_name=self.shm_name,
                exchange_name_on_qtx=self.exchange_name_on_qtx,
            )
        self.assertIn("API secret must be provided", str(context.exception))

    def test_initialization_missing_exchange_name(self):
        """Test initialization validation for exchange name"""
        with self.assertRaises(ValueError) as context:
            QtxPerpetualSharedMemoryManager(
                api_key=self.api_key,
                api_secret=self.api_secret,
                shm_name=self.shm_name,
                exchange_name_on_qtx="",
            )
        self.assertIn("Exchange name on QTX", str(context.exception))

    def test_direct_instantiation_non_singleton(self):
        """Test multiple instance creation (non-singleton behavior)"""
        second_instance = QtxPerpetualSharedMemoryManager(
            api_key=self.api_key,
            api_secret=self.api_secret,
            shm_name=self.shm_name,
            exchange_name_on_qtx=self.exchange_name_on_qtx,
        )

        self.assertIsNot(self.manager, second_instance)
        self.assertEqual(self.manager._api_key, second_instance._api_key)

    def test_is_connected_property(self):
        """Test the is_connected property"""
        self.assertFalse(self.manager.is_connected)
        self.manager._is_connected = True
        self.assertTrue(self.manager.is_connected)

    # ============================================================================
    # CONNECTION LIFECYCLE MANAGEMENT TESTS
    # ============================================================================

    async def test_connect_success(self):
        """Test successful connection to shared memory"""
        result = await self.manager.connect()

        self.mock_posix_ipc.SharedMemory.assert_called_once_with(self.shm_name, flags=self.mock_posix_ipc.O_RDWR)
        self.mock_mmap.mmap.assert_called_once()
        self.mock_queue_from_buffer.assert_called_once_with(self.mock_mmap_instance)

        self.assertTrue(result)
        self.assertTrue(self.manager._is_connected)
        self.assertIsNone(self.manager._last_connection_error)
        self.assertEqual(self.manager._connection_attempts, 0)

    async def test_connect_already_connected(self):
        """Test connect when already connected"""
        self.manager._is_connected = True
        self.manager._memory_map = self.mock_mmap_instance

        result = await self.manager.connect()

        self.assertTrue(result)
        self.mock_posix_ipc.SharedMemory.assert_not_called()

    async def test_connect_shared_memory_not_exist(self):
        """Test connection failure when shared memory doesn't exist"""
        self.mock_posix_ipc.ExistentialError = Exception
        self.mock_posix_ipc.SharedMemory.side_effect = self.mock_posix_ipc.ExistentialError("No such file")

        with self.assertRaises(ConnectionError) as context:
            await self.manager.connect()

        self.assertIn("does not exist", str(context.exception))
        self.assertFalse(self.manager._is_connected)
        self.assertIsNotNone(self.manager._last_connection_error)
        self.assertEqual(self.manager._connection_attempts, 1)

    async def test_connect_general_exception(self):
        """Test connection failure with general exception"""
        self.mock_posix_ipc.SharedMemory.side_effect = RuntimeError("General error")

        with self.assertRaises(ConnectionError) as context:
            await self.manager.connect()

        self.assertIn("Error establishing shared memory connection", str(context.exception))
        self.assertFalse(self.manager._is_connected)
        self.assertIsNotNone(self.manager._last_connection_error)

    async def test_disconnect_when_connected(self):
        """Test disconnect when connected"""
        self.manager._is_connected = True
        self.manager._memory_map = self.mock_mmap_instance
        self.manager._shm = self.mock_shm_instance
        self.manager._queue_ptr = self.mock_queue
        self.manager._api_key_initialized = True
        self.manager._pending_orders = {"test": "order"}

        await self.manager.disconnect()

        self.mock_mmap_instance.close.assert_called_once()
        self.mock_shm_instance.close_fd.assert_called_once()
        self.mock_gc_collect.assert_called_once()

        self.assertFalse(self.manager._is_connected)
        self.assertFalse(self.manager._api_key_initialized)
        self.assertEqual(self.manager._pending_orders, {})

    async def test_disconnect_when_not_connected(self):
        """Test disconnect when not connected"""
        self.assertFalse(self.manager._is_connected)

        await self.manager.disconnect()

        self.mock_mmap_instance.close.assert_not_called()
        self.mock_shm_instance.close_fd.assert_not_called()

    def test_cleanup_resources_exception_handling(self):
        """Test cleanup resources handles exceptions gracefully"""
        self.manager._memory_map = MagicMock()
        self.manager._memory_map.close.side_effect = Exception("Close failed")
        self.manager._shm = MagicMock()
        self.manager._shm.close_fd.side_effect = Exception("Close FD failed")
        self.manager._queue_ptr = self.mock_queue

        # Should not raise exceptions
        self.manager._cleanup_resources()

        self.assertFalse(self.manager._is_connected)
        self.assertIsNone(self.manager._memory_map)
        self.assertIsNone(self.manager._shm)
        self.assertIsNone(self.manager._queue_ptr)

    # ============================================================================
    # API KEY MANAGEMENT TESTS
    # ============================================================================

    async def test_initialize_api_key_success(self):
        """Test successful API key initialization"""
        self.manager._is_connected = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 1631234567123456789

        result = await self.manager.initialize_api_key()

        self.assertTrue(result)
        self.assertTrue(self.manager._api_key_initialized)

        # Verify order parameters were set correctly
        self.assertEqual(mock_order.mode, 0)
        self.assertEqual(mock_order.api_key, self.api_key.encode("utf-8"))
        self.assertEqual(mock_order.api_secret, self.api_secret.encode("utf-8"))

    async def test_initialize_api_key_missing_credentials(self):
        """Test API key initialization with missing credentials"""
        self.manager._api_key = ""
        self.manager._api_secret = ""

        result = await self.manager.initialize_api_key()

        self.assertFalse(result)
        self.assertFalse(self.manager._api_key_initialized)

    async def test_initialize_api_key_not_connected(self):
        """Test API key initialization when not connected"""
        self.manager._is_connected = False

        with patch.object(self.manager, "connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = False

            result = await self.manager.initialize_api_key()

            self.assertFalse(result)
            mock_connect.assert_called_once()

    async def test_initialize_api_key_timeout(self):
        """Test API key initialization timeout"""
        self.manager._is_connected = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 0

        start_time = 1000.0
        timeout_time = start_time + 15.0
        self.mock_time.side_effect = [start_time, timeout_time]

        result = await self.manager.initialize_api_key()

        self.assertFalse(result)
        self.assertFalse(self.manager._api_key_initialized)

    async def test_initialize_api_key_already_initialized(self):
        """Test API key initialization when already initialized"""
        self.manager._api_key_initialized = True

        result = await self.manager.initialize_api_key()

        self.assertTrue(result)
        self.mock_memset.assert_not_called()

    # ============================================================================
    # ORDER PLACEMENT TESTS
    # Test comprehensive order parameter combinations
    # ============================================================================

    async def test_place_order_success(self):
        """Test successful order placement with comprehensive parameter validation"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 1631234567123456789
        mock_order.res_msg = (
            b'{"status": 200, "result": {"orderId": "12345678", "status": "NEW", "updateTime": 1631234567000}}'
        )

        client_order_id = "test_order_123"
        symbol = "BTCUSDT"
        trade_type = TradeType.BUY
        position_action = PositionAction.OPEN
        order_type = OrderType.LIMIT
        price = Decimal("50000.0")
        size = Decimal("0.5")

        success, result = await self.manager.place_order(
            client_order_id=client_order_id,
            symbol=symbol,
            trade_type=trade_type,
            position_action=position_action,
            order_type=order_type,
            price=price,
            size=size,
        )

        self.assertTrue(success)
        self.assertEqual(result.get("exchange_order_id"), "12345678")
        self.assertEqual(result.get("status"), "NEW")
        self.assertIn("transaction_time", result)

        # Verify order parameters were set
        self.assertEqual(mock_order.mode, 1)
        self.assertEqual(mock_order.side, QTX_SIDE_BUY)
        self.assertEqual(mock_order.pos_side, QTX_POS_SIDE_LONG)
        self.assertEqual(mock_order.order_type, QTX_ORDER_TYPE_GTC)
        self.assertEqual(mock_order.size, str(size).encode("utf-8"))
        self.assertEqual(mock_order.price, str(price).encode("utf-8"))
        self.assertEqual(mock_order.symbol, symbol.encode("utf-8"))
        
        # Verify timestamp was set (place_ts_ns should be current time in nanoseconds)
        expected_timestamp_ns = int(self.mock_time.return_value * 1_000_000_000)
        self.assertEqual(mock_order.place_ts_ns, expected_timestamp_ns)

    async def test_place_order_with_market_order_price(self):
        """Test market order placement with price provided by derivative connector"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 1631234567123456789
        mock_order.res_msg = b'{"status": 200, "result": {"orderId": "12345678", "status": "NEW"}}'

        # Simulate derivative connector providing aggressive price for market order
        aggressive_price = Decimal("55000.0")  # 10% above market price of 50000

        success, result = await self.manager.place_order(
            client_order_id="test_order_123",
            symbol="BTCUSDT",
            trade_type=TradeType.BUY,
            position_action=PositionAction.OPEN,
            order_type=OrderType.MARKET,
            price=aggressive_price,  # Derivative connector provides aggressive price
            size=Decimal("0.5"),
        )

        # Should use the price provided by derivative connector
        self.assertEqual(mock_order.price, str(aggressive_price).encode("utf-8"))
        self.assertTrue(success)

    async def test_place_order_invalid_symbol(self):
        """Test order placement validation for empty symbol"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        success, result = await self.manager.place_order(
            client_order_id="test_order_123",
            symbol="",
            trade_type=TradeType.BUY,
            position_action=PositionAction.OPEN,
            order_type=OrderType.LIMIT,
            price=Decimal("50000.0"),
            size=Decimal("0.5"),
        )

        self.assertFalse(success)
        self.assertIn("Empty trading pair symbol", result["error"])

    async def test_place_order_missing_api_credentials(self):
        """Test order placement with missing API credentials"""
        self.manager._api_key = ""
        self.manager._api_secret = ""

        success, result = await self.manager.place_order(
            client_order_id="test_order_123",
            symbol="BTCUSDT",
            trade_type=TradeType.BUY,
            position_action=PositionAction.OPEN,
            order_type=OrderType.LIMIT,
            price=Decimal("50000.0"),
            size=Decimal("0.5"),
        )

        self.assertFalse(success)
        self.assertIn("API key or secret is missing", result["error"])

    async def test_place_order_not_connected(self):
        """Test order placement when not connected"""
        self.manager._is_connected = False

        with patch.object(self.manager, "connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = False

            success, result = await self.manager.place_order(
                client_order_id="test_order_123",
                symbol="BTCUSDT",
                trade_type=TradeType.BUY,
                position_action=PositionAction.OPEN,
                order_type=OrderType.LIMIT,
                price=Decimal("50000.0"),
                size=Decimal("0.5"),
            )

            self.assertFalse(success)
            self.assertIn("Failed to connect to shared memory", result["error"])

    async def test_place_order_timeout(self):
        """Test order placement timeout handling"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 0

        start_time = 1000.0
        timeout_time = start_time + 15.0
        # Mock multiple time.time() calls: timestamp setting, start_time, timeout check
        self.mock_time.side_effect = [start_time, start_time, timeout_time]

        success, result = await self.manager.place_order(
            client_order_id="test_order_123",
            symbol="BTCUSDT",
            trade_type=TradeType.BUY,
            position_action=PositionAction.OPEN,
            order_type=OrderType.LIMIT,
            price=Decimal("50000.0"),
            size=Decimal("0.5"),
        )

        self.assertFalse(success)
        self.assertIn("Timeout waiting for order placement response", result["error"])

    async def test_place_order_client_order_id_conversion(self):
        """Test client order ID conversion from string to integer"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 1631234567123456789
        mock_order.res_msg = b'{"status": 200, "result": {"orderId": "12345678", "status": "NEW"}}'

        # Test numeric string conversion
        await self.manager.place_order(
            client_order_id="123456",
            symbol="BTCUSDT",
            trade_type=TradeType.BUY,
            position_action=PositionAction.OPEN,
            order_type=OrderType.LIMIT,
            price=Decimal("50000.0"),
            size=Decimal("0.5"),
        )

        self.assertEqual(mock_order.client_order_id, 123456)

        # Test non-numeric string (should be hashed)
        await self.manager.place_order(
            client_order_id="abc_order_123",
            symbol="BTCUSDT",
            trade_type=TradeType.BUY,
            position_action=PositionAction.OPEN,
            order_type=OrderType.LIMIT,
            price=Decimal("50000.0"),
            size=Decimal("0.5"),
        )

        self.assertNotEqual(mock_order.client_order_id, 0)

    # ============================================================================
    # ORDER CANCELLATION TESTS
    # ============================================================================

    async def test_cancel_order_success(self):
        """Test successful order cancellation"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 1631234567123456789
        mock_order.res_msg = (
            b'{"status": 200, "result": {"orderId": "12345678", "status": "CANCELED", "updateTime": 1631234567000}}'
        )

        success, result = await self.manager.cancel_order(client_order_id="test_order_123", symbol="BTCUSDT")

        self.assertTrue(success)
        self.assertEqual(result.get("status"), "CANCELED")
        self.assertIn("transaction_time", result)

        # Verify cancel parameters were set
        self.assertEqual(mock_order.mode, -1)
        self.assertEqual(mock_order.symbol, b"BTCUSDT")

    async def test_cancel_order_not_found_json(self):
        """Test order cancellation when order not found (JSON response)"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 1631234567123456789
        mock_order.res_msg = b'{"code": -2013, "msg": "Order does not exist"}'

        success, result = await self.manager.cancel_order(client_order_id="nonexistent_order", symbol="BTCUSDT")

        # Should be treated as success (already cancelled)
        self.assertTrue(success)
        self.assertEqual(result.get("status"), "CANCELED")
        self.assertIn("already cancelled", result.get("note", ""))

    async def test_cancel_order_not_found_text(self):
        """Test order cancellation when order not found (text response)"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 1631234567123456789
        mock_order.res_msg = b"Error: Order not found or already cancelled"

        success, result = await self.manager.cancel_order(client_order_id="nonexistent_order", symbol="BTCUSDT")

        # Text contains "cancelled" so is treated as successful cancellation
        self.assertTrue(success)
        self.assertEqual(result.get("status"), "CANCELED")
        self.assertIsNone(result.get("note"))

    async def test_cancel_order_invalid_symbol(self):
        """Test order cancellation validation for empty symbol"""
        success, result = await self.manager.cancel_order(client_order_id="test_order_123", symbol="")

        self.assertFalse(success)
        self.assertIn("Empty trading pair symbol", result["error"])

    async def test_cancel_order_invalid_client_order_id(self):
        """Test order cancellation validation for empty client order ID"""
        success, result = await self.manager.cancel_order(client_order_id="", symbol="BTCUSDT")

        self.assertFalse(success)
        self.assertIn("Empty client order ID", result["error"])

    async def test_cancel_order_timeout(self):
        """Test order cancellation timeout handling"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 0

        start_time = 1000.0
        timeout_time = start_time + 15.0
        self.mock_time.side_effect = [start_time, timeout_time]

        success, result = await self.manager.cancel_order(client_order_id="test_order_123", symbol="BTCUSDT")

        self.assertFalse(success)
        self.assertIn("Timeout waiting for order cancellation response", result["error"])

    # ============================================================================
    # RESPONSE PARSING AND VALIDATION TESTS
    # Test both JSON and text response parsing with universal error patterns
    # ============================================================================

    def test_is_order_not_found_error_patterns(self):
        """Test universal order not found error detection"""
        # Universal patterns that work across exchanges
        true_cases = [
            "Order does not exist",
            "order not found",
            "Order not exist",
            "invalid order",
            "unknown order",
            "Order already cancelled",
            "Order already canceled",
            "order expired",
            "no such order",
            "order id not found",
            "order already filled",
            "order rejected",
        ]

        for case in true_cases:
            with self.subTest(case=case):
                self.assertTrue(self.manager._is_order_not_found_error(case))
                self.assertTrue(self.manager._is_order_not_found_error(case.upper()))

        false_cases = ["Insufficient balance", "Price too high", "Network error", "Connection timeout", "", None]

        for case in false_cases:
            with self.subTest(case=case):
                self.assertFalse(self.manager._is_order_not_found_error(case))

    def test_is_successful_json_response(self):
        """Test JSON response success detection"""
        success_response = {"status": 200, "result": {"orderId": "123"}}
        self.assertTrue(self.manager._is_successful_json_response(success_response))

        fail_responses = [
            {"status": 400, "error": "Bad request"},
            {"code": -1000, "msg": "Error"},
            {"status": 200},  # Missing result
            {},
            None,
            "not a dict",
        ]

        for response in fail_responses:
            with self.subTest(response=response):
                self.assertFalse(self.manager._is_successful_json_response(response))

    def test_is_successful_text_response(self):
        """Test text response success detection"""
        success_responses = ["Order executed successfully", "Success: Order placed", "EXECUTED"]

        for response in success_responses:
            with self.subTest(response=response):
                self.assertTrue(self.manager._is_successful_text_response(response))

        fail_responses = ["Error: Invalid symbol", "Failed to place order", "", None]

        for response in fail_responses:
            with self.subTest(response=response):
                self.assertFalse(self.manager._is_successful_text_response(response))

    def test_is_successful_cancel_response(self):
        """Test cancel response success detection"""
        success_responses = [
            "Order canceled successfully",
            "Order cancelled successfully",
            "Success: Order cancelled",
            "CANCELED",
        ]

        for response in success_responses:
            with self.subTest(response=response):
                self.assertTrue(self.manager._is_successful_cancel_response(response))

        fail_responses = ["Error: Cannot cancel order", "Failed to cancel", "", None]

        for response in fail_responses:
            with self.subTest(response=response):
                self.assertFalse(self.manager._is_successful_cancel_response(response))

    def test_extract_timestamp_from_response(self):
        """Test timestamp extraction from response data"""
        # Test with updateTime (milliseconds)
        response_with_update_time = {"updateTime": 1631234567000}
        timestamp = self.manager._extract_timestamp_from_response(response_with_update_time)
        self.assertEqual(timestamp, 1631234567.0)

        # Test with time (milliseconds)
        response_with_time = {"time": 1631234567000}
        timestamp = self.manager._extract_timestamp_from_response(response_with_time)
        self.assertEqual(timestamp, 1631234567.0)

        # Test with time (seconds)
        response_with_seconds = {"time": 1631234567}
        timestamp = self.manager._extract_timestamp_from_response(response_with_seconds)
        self.assertEqual(timestamp, 1631234567)

        # Test with no timestamp (should use current time)
        response_no_time = {"orderId": "123"}
        timestamp = self.manager._extract_timestamp_from_response(response_no_time)
        self.assertEqual(timestamp, self.mock_time.return_value)

    def test_parse_order_response_json_success(self):
        """Test parsing successful JSON order response"""
        response = '{"status": 200, "result": {"orderId": "12345678", "status": "NEW", "symbol": "BTCUSDT", "price": "50000.0", "executedQty": "0.5", "updateTime": 1631234567000}}'

        success, result = self.manager._parse_order_response(response, "test_order_123")

        self.assertTrue(success)
        self.assertEqual(result["exchange_order_id"], "12345678")
        self.assertEqual(result["status"], "NEW")
        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertEqual(result["price"], "50000.0")
        self.assertEqual(result["executed_qty"], "0.5")
        self.assertEqual(result["transaction_time"], 1631234567.0)
        self.assertEqual(result["client_order_id"], "test_order_123")
        self.assertEqual(result["raw_response"], response)

    def test_parse_order_response_json_failure(self):
        """Test parsing failed JSON order response"""
        response = '{"code": -1000, "msg": "Invalid symbol"}'

        success, result = self.manager._parse_order_response(response, "test_order_123")

        self.assertFalse(success)
        self.assertEqual(result["error"], "Invalid symbol")
        self.assertEqual(result["error_code"], -1000)
        self.assertEqual(result["client_order_id"], "test_order_123")

    def test_parse_order_response_text_success(self):
        """Test parsing successful text order response"""
        response = "Order executed successfully: orderId: 12345678, status: NEW, symbol: BTCUSDT, price: 50000.0, updateTime: 1631234567000"

        success, result = self.manager._parse_order_response(response, "test_order_123")

        self.assertTrue(success)
        self.assertEqual(result["exchange_order_id"], "12345678")
        self.assertEqual(result["status"], "NEW")
        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertEqual(result["price"], "50000.0")
        self.assertEqual(result["transaction_time"], 1631234567.0)

    def test_parse_order_response_text_failure(self):
        """Test parsing failed text order response"""
        response = "Error: Invalid symbol, code: -1000"

        success, result = self.manager._parse_order_response(response, "test_order_123")

        self.assertFalse(success)
        self.assertIn("Error", result["error"])
        self.assertEqual(result["error_code"], -1000)

    def test_parse_cancel_response_json_success(self):
        """Test parsing successful JSON cancel response"""
        response = (
            '{"status": 200, "result": {"orderId": "12345678", "status": "CANCELED", "updateTime": 1631234567000}}'
        )

        success, result = self.manager._parse_cancel_response(response, "test_order_123")

        self.assertTrue(success)
        self.assertEqual(result["status"], "CANCELED")
        self.assertEqual(result["exchange_order_id"], "12345678")
        self.assertEqual(result["transaction_time"], 1631234567.0)

    def test_parse_cancel_response_json_not_found(self):
        """Test parsing JSON cancel response for order not found"""
        response = '{"code": -2013, "msg": "Order does not exist"}'

        success, result = self.manager._parse_cancel_response(response, "test_order_123")

        self.assertTrue(success)
        self.assertEqual(result["status"], "CANCELED")
        self.assertIn("already cancelled", result["note"])

    def test_parse_cancel_response_text_success(self):
        """Test parsing successful text cancel response"""
        response = "Order cancelled successfully: orderId: 12345678, status: CANCELED, updateTime: 1631234567000"

        success, result = self.manager._parse_cancel_response(response, "test_order_123")

        self.assertTrue(success)
        self.assertEqual(result["status"], "CANCELED")
        self.assertEqual(result["exchange_order_id"], "12345678")
        self.assertEqual(result["transaction_time"], 1631234567.0)

    def test_parse_cancel_response_text_not_found(self):
        """Test parsing text cancel response for order not found"""
        response = "Error: Order not found or already cancelled"

        success, result = self.manager._parse_cancel_response(response, "test_order_123")

        # Should be treated as success because response contains "cancelled"
        self.assertTrue(success)
        self.assertEqual(result["status"], "CANCELED")
        self.assertNotIn("note", result)

    # ============================================================================
    # PARAMETER TRANSLATION TESTS
    # ============================================================================

    def test_position_side_translation_one_way_mode(self):
        """Test position side translation for ONE-WAY mode operations"""
        from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager import _translate_position_side
        
        # Test OPEN operations
        # OPEN + BUY = Opening LONG position
        pos_side = _translate_position_side(PositionAction.OPEN, TradeType.BUY)
        self.assertEqual(pos_side, QTX_POS_SIDE_LONG)
        
        # OPEN + SELL = Opening SHORT position
        pos_side = _translate_position_side(PositionAction.OPEN, TradeType.SELL)
        self.assertEqual(pos_side, QTX_POS_SIDE_SHORT)
        
        # Test CLOSE operations
        # CLOSE + BUY = Closing SHORT position (buy to cover)
        pos_side = _translate_position_side(PositionAction.CLOSE, TradeType.BUY)
        self.assertEqual(pos_side, QTX_POS_SIDE_SHORT)
        
        # CLOSE + SELL = Closing LONG position (sell to close)
        pos_side = _translate_position_side(PositionAction.CLOSE, TradeType.SELL)
        self.assertEqual(pos_side, QTX_POS_SIDE_LONG)
        
        # Test NIL operations (defaults to OPEN behavior)
        pos_side = _translate_position_side(PositionAction.NIL, TradeType.BUY)
        self.assertEqual(pos_side, QTX_POS_SIDE_LONG)
        
        pos_side = _translate_position_side(PositionAction.NIL, TradeType.SELL)
        self.assertEqual(pos_side, QTX_POS_SIDE_SHORT)

    async def test_place_order_position_side_combinations(self):
        """Test all position side combinations in order placement"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        test_cases = [
            # (position_action, trade_type, expected_pos_side, description)
            (PositionAction.OPEN, TradeType.BUY, QTX_POS_SIDE_LONG, "Open long position"),
            (PositionAction.OPEN, TradeType.SELL, QTX_POS_SIDE_SHORT, "Open short position"), 
            (PositionAction.CLOSE, TradeType.BUY, QTX_POS_SIDE_SHORT, "Close short position"),
            (PositionAction.CLOSE, TradeType.SELL, QTX_POS_SIDE_LONG, "Close long position"),
        ]

        for i, (position_action, trade_type, expected_pos_side, description) in enumerate(test_cases):
            with self.subTest(case=description):
                mock_order = self.mock_orders[i % len(self.mock_orders)]
                mock_order.res_ts_ns = 1631234567123456789 + i
                mock_order.res_msg = b'{"status": 200, "result": {"orderId": "12345678", "status": "NEW"}}'

                success, result = await self.manager.place_order(
                    client_order_id=f"test_order_{i}",
                    symbol="BTCUSDT",
                    trade_type=trade_type,
                    position_action=position_action,
                    order_type=OrderType.LIMIT,
                    price=Decimal("50000.0"),
                    size=Decimal("0.5"),
                )

                self.assertTrue(success, f"Order placement failed for {description}")
                self.assertEqual(mock_order.pos_side, expected_pos_side, 
                               f"Position side mismatch for {description}")

    # ============================================================================
    # ERROR HANDLING AND EDGE CASES
    # ============================================================================

    def test_logger_singleton(self):
        """Test logger is properly initialized as singleton"""
        logger1 = self.manager.logger()
        logger2 = self.manager.logger()
        self.assertIs(logger1, logger2)

    def test_parse_response_invalid_json(self):
        """Test response parsing with invalid JSON falls back to text parsing"""
        invalid_json = '{"invalid": json}'

        success, result = self.manager._parse_order_response(invalid_json, "test_order")

        self.assertFalse(success)
        self.assertEqual(result["raw_response"], invalid_json)

    async def test_pending_orders_cleanup(self):
        """Test that pending orders are properly cleaned up after order placement"""
        self.manager._is_connected = True
        self.manager._api_key_initialized = True
        self.manager._queue_ptr = self.mock_queue

        mock_order = self.mock_orders[0]
        mock_order.res_ts_ns = 1631234567123456789
        mock_order.res_msg = b'{"status": 200, "result": {"orderId": "12345678", "status": "NEW"}}'

        client_order_id = "test_order_123"

        success, result = await self.manager.place_order(
            client_order_id=client_order_id,
            symbol="BTCUSDT",
            trade_type=TradeType.BUY,
            position_action=PositionAction.OPEN,
            order_type=OrderType.LIMIT,
            price=Decimal("50000.0"),
            size=Decimal("0.5"),
        )

        # Verify pending order was cleaned up after completion
        self.assertNotIn(client_order_id, self.manager._pending_orders)


if __name__ == "__main__":
    unittest.main()
