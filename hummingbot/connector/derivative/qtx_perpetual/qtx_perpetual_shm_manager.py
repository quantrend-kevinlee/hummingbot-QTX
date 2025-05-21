import asyncio
import ctypes
import gc
import math
import mmap
import re
import time
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from hummingbot.logger import HummingbotLogger

# Import posix_ipc
try:
    import posix_ipc
except ImportError:
    raise ImportError("The 'posix_ipc' module is required. Please install it using 'pip install posix_ipc'.")

# Constants for QTX Shared Memory Order Management
MAX_FLOAT_SIZE = 32
MAX_KEY_SIZE = 100
MAX_SYMBOL_SIZE = 100
MAX_MESSAGE_SIZE = 10000
MAX_QUEUE_SIZE = 100

# QTX Order Types
QTX_ORDER_TYPE_IOC = 0
QTX_ORDER_TYPE_POST_ONLY = 1
QTX_ORDER_TYPE_GTC = 2
QTX_ORDER_TYPE_FOK = 3

# QTX Side Constants
QTX_SIDE_BUY = 1
QTX_SIDE_SELL = -1

# QTX Position Side Constants
QTX_POS_SIDE_LONG = 1
QTX_POS_SIDE_BOTH = 0
QTX_POS_SIDE_SHORT = -1

# QTX Price Match Constants
QTX_PRICE_MATCH_NONE = 0
QTX_PRICE_MATCH_QUEUE = 1
QTX_PRICE_MATCH_QUEUE_5 = 2
QTX_PRICE_MATCH_OPPONENT = 3
QTX_PRICE_MATCH_OPPONENT_5 = 4


# ctypes structure for Order
class Order(ctypes.Structure):
    _fields_ = [
        ("mode", ctypes.c_longlong),  # 0: init key, 1: place order, -1: cancel order
        ("side", ctypes.c_longlong),  # 1: BUY, -1: SELL
        ("pos_side", ctypes.c_longlong),  # 1: LONG, 0: BOTH, -1: SHORT
        ("client_order_id", ctypes.c_ulonglong),  # Client-defined order ID
        ("order_type", ctypes.c_ulong),  # 0: IOC, 1: POST_ONLY, 2: GTC, 3: FOK
        ("price_match", ctypes.c_ulong),  # 0: NONE, 1: QUEUE, 2: QUEUE_5, 3: OPPONENT, 4: OPPONENT_5
        ("place_ts_ns", ctypes.c_ulong),  # Timestamp of order placement (set by client or manager)
        ("res_ts_ns", ctypes.c_ulong),  # Timestamp of response (set by manager)
        ("size", ctypes.c_char * MAX_FLOAT_SIZE),
        ("price", ctypes.c_char * MAX_FLOAT_SIZE),
        ("api_key", ctypes.c_char * MAX_KEY_SIZE),
        ("api_secret", ctypes.c_char * MAX_KEY_SIZE),
        ("symbol", ctypes.c_char * MAX_SYMBOL_SIZE),
        ("res_msg", ctypes.c_char * MAX_MESSAGE_SIZE),
    ]


ORDER_SIZE = ctypes.sizeof(Order)


# ctypes structure for Queue
class Queue(ctypes.Structure):
    _fields_ = [
        ("from_idx", ctypes.c_longlong),
        ("to_idx", ctypes.c_longlong),
        ("orders", Order * MAX_QUEUE_SIZE),
    ]


SHM_SIZE = ctypes.sizeof(Queue)


class QtxPerpetualSharedMemoryManager:
    """
    Manager for QTX Perpetual shared memory operations.

    This class handles:
    - Shared memory initialization and cleanup
    - API key initialization
    - Order placement and cancellation
    - Response tracking
    """

    _instance = None
    _logger: Optional[HummingbotLogger] = None

    @classmethod
    def get_instance(cls, *args, **kwargs):
        """Singleton pattern implementation"""
        if cls._instance is None:
            cls._instance = cls(*args, **kwargs)
        return cls._instance

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            from hummingbot.logger import HummingbotLogger

            cls._logger = HummingbotLogger(__name__)
        return cls._logger

    @property
    def is_connected(self) -> bool:
        """Returns whether the shared memory manager is connected to the shared memory segment"""
        return self._is_connected

    def __init__(
        self, api_key: str = None, api_secret: str = None, shm_name: str = None, exchange_name_on_qtx: str = None
    ):
        """
        Initialize the shared memory manager

        :param api_key: API key for authentication
        :param api_secret: API secret for authentication
        :param shm_name: Name of the shared memory segment (required, e.g., "/place_order_kevinlee")
        :param exchange_name_on_qtx: Exchange name to use for QTX (REQUIRED, no default)
        :raises: ValueError if shm_name is not provided
        """
        if not shm_name:
            raise ValueError("Shared memory name (shm_name) must be provided")

        if not api_key or not api_secret:
            self.logger().warning("API key or secret is missing. Order placement will fail without valid credentials.")

        self._api_key = api_key
        self._api_secret = api_secret
        self._shm_name = shm_name

        if exchange_name_on_qtx is None:
            self.logger().warning(
                "No exchange_name_on_qtx provided to SHM manager! QTX trading pair conversion may fail."
            )
        self._exchange_name_on_qtx = exchange_name_on_qtx

        # Shared memory objects
        self._shm = None
        self._memory_map = None
        self._queue_ptr = None

        # State tracking
        self._is_connected = False
        self._api_key_initialized = False
        self._pending_orders = {}  # client_order_id -> order reference

        # Connection tracking
        self._last_connection_error = None
        self._connection_attempts = 0

    # --- Connection Management ---

    async def connect(self) -> bool:
        """
        Establish a connection to the shared memory segment

        :return: True when connection is successful
        :raises: ConnectionError if connection fails
        """
        # Check if we're already connected
        if self._is_connected and self._memory_map is not None:
            return True

        # Track connection attempts
        self._connection_attempts += 1

        try:
            # Open the shared memory segment - O_RDWR allows reading and writing
            self._shm = posix_ipc.SharedMemory(self._shm_name, flags=posix_ipc.O_RDWR)

            # Map the shared memory
            self._memory_map = mmap.mmap(self._shm.fd, SHM_SIZE, access=mmap.ACCESS_WRITE)

            # Cast the mapped memory to our Queue structure pointer
            self._queue_ptr = Queue.from_buffer(self._memory_map)

            self.logger().info(f"Connected to QTX order manager via shared memory '{self._shm_name}'")

            self._is_connected = True
            self._last_connection_error = None
            self._connection_attempts = 0

            # Initialize API key if provided
            if self._api_key and self._api_secret and not self._api_key_initialized:
                key_initialized = await self.initialize_api_key()
                if not key_initialized:
                    self.logger().warning("API key initialization failed")
            elif not self._api_key or not self._api_secret:
                self.logger().warning("API key/secret missing")

            return True

        except posix_ipc.ExistentialError:
            error_msg = f"Shared memory segment '{self._shm_name}' does not exist"
            self.logger().error(f"Error: {error_msg}")
            self._last_connection_error = error_msg
            self._cleanup_resources()
            raise ConnectionError(error_msg)
        except Exception as e:
            error_msg = f"Error establishing shared memory connection: {e}"
            self.logger().error(f"{error_msg}", exc_info=True)
            self._last_connection_error = str(e)
            self._cleanup_resources()
            raise ConnectionError(error_msg)

    def _cleanup_resources(self):
        """Clean up shared memory resources"""
        # Clean up shared memory resources

        # Delete references to the buffer in reverse order
        if hasattr(self, "_queue_ptr") and self._queue_ptr is not None:
            self._queue_ptr = None

        # Force garbage collection to clean up lingering ctypes references
        gc.collect()

        if hasattr(self, "_memory_map") and self._memory_map is not None:
            try:
                self._memory_map.close()
            except Exception:
                pass
            self._memory_map = None

        if hasattr(self, "_shm") and self._shm is not None:
            try:
                self._shm.close_fd()
            except Exception:
                pass
            self._shm = None

        self._is_connected = False
        self.logger().info("Shared memory resources cleaned up")

    async def disconnect(self):
        """Disconnect from shared memory and cleanup resources"""
        if not self._is_connected:
            return

        self.logger().info("Disconnecting from shared memory")

        # Clean up resources
        self._cleanup_resources()

        # Reset state
        self._api_key_initialized = False
        self._pending_orders = {}

        self.logger().info("Disconnected from shared memory successfully")

    # --- API Key Management ---

    async def initialize_api_key(self) -> bool:
        """
        Initialize API key in the shared memory

        :return: True if successful, False otherwise
        """
        # Validate API key and secret are present
        if not self._api_key or not self._api_secret:
            self.logger().error(
                "Cannot initialize API key: API key or secret is missing. "
                "Order execution will fail until credentials are provided."
            )
            return False

        # Ensure we're connected to shared memory
        if not self._is_connected:
            try:
                success = await self.connect()
                if not success:
                    self.logger().error("Cannot initialize API key: Failed to connect to shared memory")
                    return False
            except Exception as e:
                self.logger().error(f"Cannot initialize API key: Connection error: {e}")
                return False

        if self._api_key_initialized:
            return True

        try:
            # Get a new index for the API key initialization
            key_idx = self._queue_ptr.to_idx
            key_order_ref = self._queue_ptr.orders[key_idx]

            # Zero out the memory for this order
            ctypes.memset(ctypes.addressof(key_order_ref), 0, ORDER_SIZE)

            # Set the API key initialization parameters
            key_order_ref.mode = 0  # init key mode
            key_order_ref.api_key = self._api_key.encode("utf-8")
            key_order_ref.api_secret = self._api_secret.encode("utf-8")

            self.logger().info("Sending API key initialization request")

            # Notify the order manager by incrementing the queue's to_idx
            self._queue_ptr.to_idx = (self._queue_ptr.to_idx + 1) % MAX_QUEUE_SIZE

            # Wait for response using polling approach (same as reference client)
            self.logger().debug("Waiting for API key initialization response...")
            timeout_seconds = 10.0
            start_time = time.time()

            try:
                # Poll for response by checking res_ts_ns
                while key_order_ref.res_ts_ns == 0:
                    # Check for timeout
                    if time.time() - start_time > timeout_seconds:
                        raise asyncio.TimeoutError("API key initialization timed out")

                    # Sleep briefly to avoid CPU spinning
                    await asyncio.sleep(0.01)  # 10ms sleep

                # Process response once received - just log it but don't try to parse
                response = key_order_ref.res_msg.decode(errors="ignore")
                self.logger().info(f"API Key initialization response received: {response}")

                # Reference client doesn't parse the response content - it just assumes success if res_ts_ns is set
                # We'll follow the same approach for maximum compatibility
                self._api_key_initialized = True
                return True

            except asyncio.TimeoutError:
                self.logger().error("API key initialization timeout")
                return False

        except Exception as e:
            self.logger().error(f"Error initializing API key: {e}", exc_info=True)
            return False

    # --- Order Management ---

    async def place_order(
        self,
        client_order_id: str,
        symbol: str,
        side: int,  # 1 for BUY, -1 for SELL
        position_side: int,  # 1 for LONG, 0 for BOTH, -1 for SHORT
        order_type: int,  # 0: IOC, 1: POST_ONLY, 2: GTC, 3: FOK
        price: Decimal,
        size: Decimal,
        price_match: int = 0,  # 0: NONE, 1: QUEUE, 2: QUEUE_5, 3: OPPONENT, 4: OPPONENT_5
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Place an order using shared memory

        :param client_order_id: Unique client order ID
        :param symbol: Trading pair symbol in exchange format (e.g., BTCUSDT)
        :param side: Order side (1 for BUY, -1 for SELL)
        :param position_side: Position side (1 for LONG, 0 for BOTH, -1 for SHORT)
        :param order_type: Order type (0: IOC, 1: POST_ONLY, 2: GTC, 3: FOK)
        :param price: Order price
        :param size: Order size
        :param price_match: Price matching strategy (0: exact price)
        :return: Tuple of (success, response_data)
        """
        # Parameter validation
        if not symbol or not symbol.strip():
            self.logger().error("Cannot place order: Empty trading pair symbol provided")
            return False, {"error": "Empty trading pair symbol provided"}

        # No validation for size or price - passing through directly to shared memory
        # Strategy code is responsible for parameter validation

        # Check API key availability
        if not self._api_key or not self._api_secret:
            self.logger().error("Cannot place order: API key or secret is missing")
            return False, {"error": "API key or secret is missing"}

        # Ensure connection
        if not self._is_connected:
            try:
                success = await self.connect()
                if not success:
                    return False, {"error": "Failed to connect to shared memory"}
            except Exception as e:
                self.logger().error(f"Order placement failed: Connection error: {e}")
                return False, {"error": f"Connection error: {str(e)}"}

        # Ensure API key is initialized
        if not self._api_key_initialized:
            try:
                success = await self.initialize_api_key()
                if not success:
                    return False, {"error": "Failed to initialize API key. Order placement aborted."}
            except Exception as e:
                self.logger().error(f"Order placement failed: API key initialization error: {e}")
                return False, {"error": f"API key initialization error: {str(e)}"}

        try:
            # Convert client_order_id string to integer
            try:
                client_order_id_int = int(client_order_id)
            except ValueError:
                # If not a pure integer, hash it to get a numeric value
                client_order_id_int = abs(hash(client_order_id)) % (2**64 - 1)

            # Get a new index for the order placement
            place_idx = self._queue_ptr.to_idx
            place_order_ref = self._queue_ptr.orders[place_idx]

            # Zero out the memory for this order
            ctypes.memset(ctypes.addressof(place_order_ref), 0, ORDER_SIZE)

            # Set the order parameters
            place_order_ref.mode = 1  # place order mode
            place_order_ref.side = side
            place_order_ref.pos_side = position_side
            place_order_ref.client_order_id = client_order_id_int
            place_order_ref.order_type = order_type
            place_order_ref.price_match = price_match
            place_order_ref.size = str(size).encode("utf-8")

            # Handle NaN or None price for market orders
            # For IOC market orders (order_type == 0) or any order with NaN/None price
            is_nan_price = False

            # Check for NaN in multiple ways to catch all edge cases
            if price is None:
                is_nan_price = True
            elif isinstance(price, float) and math.isnan(price):
                is_nan_price = True
            elif price != price:  # NaN comparison
                is_nan_price = True
            elif hasattr(price, "is_nan") and price.is_nan():
                is_nan_price = True

            # If it's a market order (IOC) with no price, or price is NaN, use a fixed non-zero price
            # Binance requires prices above minimum thresholds even for market orders
            if order_type == QTX_ORDER_TYPE_IOC or is_nan_price:
                place_order_ref.price = "100.0".encode(
                    "utf-8"
                )  # Use 100.0 to circumvent Binance's minimum price threshold
                self.logger().info(f"Setting market order price to 100.0 for order {client_order_id}")
            else:
                place_order_ref.price = str(price).encode("utf-8")

            place_order_ref.symbol = symbol.encode("utf-8")

            # Store reference to the order for tracking, if needed
            self._pending_orders[client_order_id] = place_order_ref

            self.logger().info(f"Placing order: {symbol}, size={size}, client_order_id={client_order_id}")

            # Notify the order manager by incrementing the queue's to_idx
            self._queue_ptr.to_idx = (self._queue_ptr.to_idx + 1) % MAX_QUEUE_SIZE

            # Wait for response using polling approach (same as reference client)
            self.logger().debug(f"Waiting for order placement response for {client_order_id}...")
            timeout_seconds = 10.0
            start_time = time.time()

            try:
                # Poll for response by checking res_ts_ns
                while place_order_ref.res_ts_ns == 0:
                    # Check for timeout
                    if time.time() - start_time > timeout_seconds:
                        raise asyncio.TimeoutError("Order placement timed out")

                    # Sleep briefly to avoid CPU spinning
                    await asyncio.sleep(0.01)  # 10ms sleep

                # Get response data once received
                response = place_order_ref.res_msg.decode(errors="ignore")
                self.logger().debug(f"Order placement response received for {client_order_id}: {response}")

                # Parse response to determine success and extract info
                success, response_data = self._parse_order_response(response, client_order_id)

                if success:
                    self.logger().info(f"Order placed successfully: {client_order_id} - {response_data}")
                else:
                    self.logger().error(f"Order placement failed: {client_order_id} - {response_data}")

                return success, response_data

            except asyncio.TimeoutError:
                self.logger().error(f"Timeout waiting for order placement response: {client_order_id}")
                return False, {"error": "Timeout waiting for order placement response"}

        except Exception as e:
            self.logger().error(f"Error placing order: {e}", exc_info=True)
            return False, {"error": f"Error placing order: {str(e)}"}
        finally:
            # Clean up tracking references if needed
            if client_order_id in self._pending_orders:
                del self._pending_orders[client_order_id]

    async def cancel_order(self, client_order_id: str, symbol: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Cancel an order using shared memory

        :param client_order_id: Client order ID to cancel
        :param symbol: Trading pair symbol in exchange format
        :return: Tuple of (success, response_data)
        """
        # Parameter validation
        if not symbol or not symbol.strip():
            self.logger().error("Cannot cancel order: Empty trading pair symbol provided")
            return False, {"error": "Empty trading pair symbol provided"}

        if not client_order_id or not client_order_id.strip():
            self.logger().error("Cannot cancel order: Empty client order ID provided")
            return False, {"error": "Empty client order ID provided"}

        # Check API key availability (cancellation also requires API key)
        if not self._api_key or not self._api_secret:
            self.logger().error("Cannot cancel order: API key or secret is missing")
            return False, {"error": "API key or secret is missing"}

        # Ensure connection
        if not self._is_connected:
            try:
                success = await self.connect()
                if not success:
                    return False, {"error": "Failed to connect to shared memory"}
            except Exception as e:
                self.logger().error(f"Order cancellation failed: Connection error: {e}")
                return False, {"error": f"Connection error: {str(e)}"}

        # Ensure API key is initialized
        if not self._api_key_initialized:
            try:
                success = await self.initialize_api_key()
                if not success:
                    return False, {"error": "Failed to initialize API key. Order cancellation aborted."}
            except Exception as e:
                self.logger().error(f"Order cancellation failed: API key initialization error: {e}")
                return False, {"error": f"API key initialization error: {str(e)}"}

        cancel_track_id = f"cancel_{client_order_id}"
        try:
            # Convert client_order_id string to integer
            try:
                client_order_id_int = int(client_order_id)
            except ValueError:
                # If not a pure integer, hash it to get a numeric value
                client_order_id_int = abs(hash(client_order_id)) % (2**64 - 1)

            # Get a new index for the cancellation request
            cancel_idx = self._queue_ptr.to_idx
            cancel_order_ref = self._queue_ptr.orders[cancel_idx]

            # Zero out the memory for this order
            ctypes.memset(ctypes.addressof(cancel_order_ref), 0, ORDER_SIZE)

            # Set the cancellation parameters
            cancel_order_ref.mode = -1  # cancel order mode
            cancel_order_ref.client_order_id = client_order_id_int
            cancel_order_ref.symbol = symbol.encode("utf-8")

            # Store reference to the cancel request for tracking, if needed
            self._pending_orders[cancel_track_id] = cancel_order_ref

            self.logger().info(f"Canceling order with client ID: {client_order_id} for symbol: {symbol}")

            # Notify the order manager by incrementing the queue's to_idx
            self._queue_ptr.to_idx = (self._queue_ptr.to_idx + 1) % MAX_QUEUE_SIZE

            # Wait for response using polling approach (same as reference client)
            self.logger().debug(f"Waiting for order cancellation response for {client_order_id}...")
            timeout_seconds = 10.0
            start_time = time.time()

            try:
                # Poll for response by checking res_ts_ns
                while cancel_order_ref.res_ts_ns == 0:
                    # Check for timeout
                    if time.time() - start_time > timeout_seconds:
                        raise asyncio.TimeoutError("Order cancellation timed out")

                    # Sleep briefly to avoid CPU spinning
                    await asyncio.sleep(0.01)  # 10ms sleep

                # Get response data once received
                response = cancel_order_ref.res_msg.decode(errors="ignore")
                self.logger().debug(f"Order cancellation response received for {client_order_id}: {response}")

                # Parse response to determine success and extract info
                success, response_data = self._parse_cancel_response(response, client_order_id)

                if success:
                    self.logger().info(f"Order cancelled successfully: {client_order_id} - {response_data}")
                else:
                    self.logger().error(f"Order cancellation failed: {client_order_id} - {response_data}")

                return success, response_data

            except asyncio.TimeoutError:
                self.logger().error(f"Timeout waiting for order cancellation response: {client_order_id}")
                return False, {"error": "Timeout waiting for order cancellation response"}

        except Exception as e:
            self.logger().error(f"Error canceling order: {e}", exc_info=True)
            return False, {"error": f"Error canceling order: {str(e)}"}
        finally:
            # Clean up tracking references
            if cancel_track_id in self._pending_orders:
                del self._pending_orders[cancel_track_id]

    # --- Response Parsing Methods ---

    def _parse_order_response(self, response: str, client_order_id: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Parse order placement response

        :param response: Response message from QTX
        :param client_order_id: Client order ID
        :return: Tuple of (success, response_data)
        """
        response_data = {"raw_response": response, "client_order_id": client_order_id}
        success: bool

        # Check for success indicators in the response
        if "success" in response.lower() or "executed" in response.lower():
            success = True

            # Extract exchange order ID if present
            # The exact format depends on the QTX response format
            exchange_id_match = re.search(r"order_id[\":\s]+([0-9a-zA-Z]+)", response)
            if exchange_id_match:
                response_data["exchange_order_id"] = exchange_id_match.group(1)
            else:
                # If no exchange order ID is found, generate a deterministic one based on client order ID
                # Using a prefix to identify it as QTX-generated
                response_data["exchange_order_id"] = f"qtx_{abs(hash(client_order_id))}"
                self.logger().warning(
                    f"Could not extract exchange order ID from response for {client_order_id}. "
                    f"Using generated ID: {response_data['exchange_order_id']}"
                )

            # Extract order status if present
            status_match = re.search(r"status[\":\s]+([a-zA-Z_]+)", response)
            if status_match:
                response_data["status"] = status_match.group(1)
            else:
                # Default to NEW status if not found
                response_data["status"] = "NEW"

            # Extract trading pair if present
            symbol_match = re.search(r"symbol[\":\s]+([a-zA-Z0-9]+)", response)
            if symbol_match:
                response_data["symbol"] = symbol_match.group(1)

            # Extract price if present
            price_match = re.search(r"price[\":\s]+([\d\.]+)", response)
            if price_match:
                response_data["price"] = price_match.group(1)

            # Extract quantity if present
            qty_match = re.search(r"(quantity|amount|qty)[\":\s]+([\d\.]+)", response)
            if qty_match:
                response_data["quantity"] = qty_match.group(2)

            # Extract executed quantity if present (for filled orders)
            exec_qty_match = re.search(r"(executed_qty|executedQty|filled)[\":\s]+([\d\.]+)", response)
            if exec_qty_match:
                response_data["executed_qty"] = exec_qty_match.group(2)

            # Extract trade ID if present
            trade_id_match = re.search(r"(trade_id|tradeId)[\":\s]+([0-9a-zA-Z]+)", response)
            if trade_id_match:
                response_data["trade_id"] = trade_id_match.group(2)

            # Extract fee information if present
            fee_amount_match = re.search(r"(fee_amount|feeAmount|fee)[\":\s]+([\d\.]+)", response)
            if fee_amount_match:
                response_data["fee_amount"] = fee_amount_match.group(2)

            fee_asset_match = re.search(r"(fee_asset|feeAsset|feeCoin)[\":\s]+([a-zA-Z]+)", response)
            if fee_asset_match:
                response_data["fee_asset"] = fee_asset_match.group(2)

            # Extract timestamp if present
            time_match = re.search(r"time[\":\s]+(\d+)", response)
            if time_match:
                # Convert to seconds if in milliseconds
                timestamp = int(time_match.group(1))
                if timestamp > 1000000000000:  # If timestamp is in milliseconds
                    timestamp = timestamp / 1000
                response_data["transaction_time"] = timestamp
            else:
                # Use current time if not found
                response_data["transaction_time"] = time.time()

            return success, response_data
        else:
            # Order placement failed
            success = False
            error_msg = response

            # Extract error reason if possible
            error_match = re.search(r"error[\":\s]+(.*)", response)
            if error_match:
                error_msg = error_match.group(1)

            # Extract error code if present
            error_code_match = re.search(r"code[\":\s]+(-?\d+)", response)
            if error_code_match:
                response_data["error_code"] = int(error_code_match.group(1))

            response_data["error"] = error_msg.strip()
            return success, response_data

    def _parse_cancel_response(self, response: str, client_order_id: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Parse order cancellation response

        :param response: Response message from QTX
        :param client_order_id: Client order ID
        :return: Tuple of (success, response_data)
        """
        response_data = {"raw_response": response, "client_order_id": client_order_id}

        # Check for success indicators in the response
        if "success" in response.lower() or "canceled" in response.lower() or "cancelled" in response.lower():
            # Extract status if present
            status_match = re.search(r"status[\":\s]+([a-zA-Z_]+)", response)
            if status_match:
                response_data["status"] = status_match.group(1)
            else:
                # Default to CANCELED status
                response_data["status"] = "CANCELED"

            # Extract exchange order ID if present
            exchange_id_match = re.search(r"order_id[\":\s]+([0-9a-zA-Z]+)", response)
            if exchange_id_match:
                response_data["exchange_order_id"] = exchange_id_match.group(1)

            # Extract timestamp if present
            time_match = re.search(r"time[\":\s]+(\d+)", response)
            if time_match:
                # Convert to seconds if in milliseconds
                timestamp = int(time_match.group(1))
                if timestamp > 1000000000000:  # If timestamp is in milliseconds
                    timestamp = timestamp / 1000
                response_data["transaction_time"] = timestamp
            else:
                # Use current time if not found
                response_data["transaction_time"] = time.time()

            return True, response_data
        else:
            # Check for "order does not exist" or similar, which we can also treat as success
            if "not exist" in response.lower() or "not found" in response.lower():
                response_data["note"] = "Order already cancelled or does not exist"
                response_data["status"] = "CANCELED"
                response_data["transaction_time"] = time.time()
                return True, response_data

            # Cancellation failed
            error_msg = response

            # Extract error reason if possible
            error_match = re.search(r"error[\":\s]+(.*)", response)
            if error_match:
                error_msg = error_match.group(1)

            # Extract error code if present
            error_code_match = re.search(r"code[\":\s]+(-?\d+)", response)
            if error_code_match:
                response_data["error_code"] = int(error_code_match.group(1))

            response_data["error"] = error_msg.strip()
            return False, response_data
