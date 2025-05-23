import asyncio
import ctypes
import gc
import json
import logging
import mmap
import re
import time
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType
from hummingbot.logger import HummingbotLogger

try:
    import posix_ipc
except ImportError:
    raise ImportError("The 'posix_ipc' module is required. Please install it using 'pip install posix_ipc'.")

# Shared memory buffer size constants
MAX_FLOAT_SIZE = 32
MAX_KEY_SIZE = 100
MAX_SYMBOL_SIZE = 100
MAX_MESSAGE_SIZE = 10000
MAX_QUEUE_SIZE = 100

# Order type constants
QTX_ORDER_TYPE_IOC = 0          # Immediate or Cancel
QTX_ORDER_TYPE_POST_ONLY = 1    # Post Only (maker only)
QTX_ORDER_TYPE_GTC = 2          # Good Till Cancelled
QTX_ORDER_TYPE_FOK = 3          # Fill or Kill

# Order side constants
QTX_SIDE_BUY = 1
QTX_SIDE_SELL = -1

# Position side constants
# NOTE: QTX only supports ONE-WAY mode - pos_side must always be BOTH (0)
QTX_POS_SIDE_LONG = 1           # Not used in ONE-WAY mode
QTX_POS_SIDE_BOTH = 0           # ONE-WAY mode (required for all QTX orders)
QTX_POS_SIDE_SHORT = -1         # Not used in ONE-WAY mode

# Price matching strategy constants
QTX_PRICE_MATCH_NONE = 0        # Use exact price
QTX_PRICE_MATCH_QUEUE = 1       # Match queue price
QTX_PRICE_MATCH_QUEUE_5 = 2     # Queue + 5 ticks
QTX_PRICE_MATCH_OPPONENT = 3    # Match opponent price
QTX_PRICE_MATCH_OPPONENT_5 = 4  # Opponent + 5 ticks


# Hummingbot → QTX Protocol Translation Layer
# 
# QTX uses numeric codes for order parameters while Hummingbot uses enums.
# These functions bridge the semantic gap between the two systems.

def _translate_trade_type_to_side(trade_type: TradeType) -> int:
    """QTX side encoding: 1=BUY, -1=SELL"""
    return QTX_SIDE_BUY if trade_type == TradeType.BUY else QTX_SIDE_SELL


def _translate_position_side(position_action: PositionAction, trade_type: TradeType) -> int:
    """
    Maps position intent to QTX's ONE-WAY mode position side encoding.
    
    In ONE-WAY mode, pos_side must ALWAYS be BOTH (0).
    The exchange automatically determines whether an order opens or closes
    a position based on the current position state and trade direction.
    
    BUY orders: Open/increase long position OR close/reduce short position
    SELL orders: Open/increase short position OR close/reduce long position
    """
    # In ONE-WAY mode, always use BOTH regardless of position action or trade type
    return QTX_POS_SIDE_BOTH


def _translate_order_type(order_type: OrderType) -> int:
    """
    Maps Hummingbot order types to QTX execution policies.
    
    Note: MARKET orders are architectural violations here - they should be 
    converted to aggressive LIMIT orders by the derivative connector.
    """
    order_type_map = {
        OrderType.LIMIT: QTX_ORDER_TYPE_GTC,
        OrderType.LIMIT_MAKER: QTX_ORDER_TYPE_POST_ONLY,
        OrderType.MARKET: QTX_ORDER_TYPE_GTC,  # Fallback - should not happen
    }
    return order_type_map.get(order_type, QTX_ORDER_TYPE_GTC)


class Order(ctypes.Structure):
    """
    Binary-compatible order structure for QTX shared memory protocol.
    
    WARNING: Field order and types must match QTX's C++ structure exactly.
    Any changes here require coordination with QTX system implementation.
    """
    _fields_ = [
        ("mode", ctypes.c_longlong),                    # 0: init key, 1: place order, -1: cancel order
        ("side", ctypes.c_longlong),                    # 1: BUY, -1: SELL
        ("pos_side", ctypes.c_longlong),                # 1: LONG, 0: BOTH, -1: SHORT
        ("client_order_id", ctypes.c_ulonglong),        # Client-defined order ID (numeric)
        ("order_type", ctypes.c_ulong),                 # Order type (IOC, POST_ONLY, GTC, FOK)
        ("price_match", ctypes.c_ulong),                # Price matching strategy
        ("place_ts_ns", ctypes.c_ulong),                # Order placement timestamp (nanoseconds)
        ("res_ts_ns", ctypes.c_ulong),                  # Response timestamp (set by manager)
        ("size", ctypes.c_char * MAX_FLOAT_SIZE),       # Order size as string
        ("price", ctypes.c_char * MAX_FLOAT_SIZE),      # Order price as string
        ("api_key", ctypes.c_char * MAX_KEY_SIZE),      # Exchange API key
        ("api_secret", ctypes.c_char * MAX_KEY_SIZE),   # Exchange API secret  
        ("symbol", ctypes.c_char * MAX_SYMBOL_SIZE),    # Trading pair symbol
        ("res_msg", ctypes.c_char * MAX_MESSAGE_SIZE),  # Response message from exchange
    ]


ORDER_SIZE = ctypes.sizeof(Order)


class Queue(ctypes.Structure):
    """
    Lock-free circular buffer for high-frequency order communication.
    
    Producer (Hummingbot) writes at to_idx, Consumer (QTX) reads from from_idx.
    Synchronization relies on atomic index updates and response timestamps.
    """
    _fields_ = [
        ("from_idx", ctypes.c_longlong),    # Consumer index (read by manager)
        ("to_idx", ctypes.c_longlong),      # Producer index (written by client)
        ("orders", Order * MAX_QUEUE_SIZE), # Circular buffer of orders
    ]


SHM_SIZE = ctypes.sizeof(Queue)


class QtxPerpetualSharedMemoryManager:
    """
    High-performance IPC bridge to QTX order execution system.
    
    Responsibilities:
    - Translate Hummingbot semantics to QTX protocol
    - Manage shared memory lifecycle and error recovery
    - Provide synchronous API over asynchronous IPC
    
    Architecture: This is a pure translation layer. Business logic like
    market order conversion belongs in the derivative connector.
    """

    _logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    @property
    def is_connected(self) -> bool:
        """Returns whether the shared memory manager is connected to the shared memory segment"""
        return self._is_connected

    def __init__(
        self, api_key: str = None, api_secret: str = None, shm_name: str = None, exchange_name_on_qtx: str = None
    ):
        """
        Initialize shared memory manager with required credentials and settings.
        
        Args:
            api_key: Exchange API key for authentication
            api_secret: Exchange API secret for authentication  
            shm_name: Shared memory segment name (e.g., "/place_order_kevinlee")
            exchange_name_on_qtx: Exchange identifier for QTX system
        
        Raises:
            ValueError: If any required parameter is missing or empty
        """
        if not shm_name:
            raise ValueError("Shared memory name (shm_name) must be provided and cannot be empty")
        
        if not api_key:
            raise ValueError("API key must be provided and cannot be empty")
            
        if not api_secret:
            raise ValueError("API secret must be provided and cannot be empty")
            
        if not exchange_name_on_qtx:
            raise ValueError("Exchange name on QTX (exchange_name_on_qtx) must be provided and cannot be empty")

        self._api_key = api_key
        self._api_secret = api_secret
        self._shm_name = shm_name

        self._exchange_name_on_qtx = exchange_name_on_qtx

        self._shm = None
        self._memory_map = None
        self._queue_ptr = None
        self._is_connected = False
        self._api_key_initialized = False
        self._pending_orders = {}
        self._last_connection_error = None
        self._connection_attempts = 0

    # Connection Management

    async def connect(self) -> bool:
        """
        Connect to the shared memory segment and initialize the queue.
        
        Returns:
            bool: True if connection successful
            
        Raises:
            ConnectionError: If shared memory segment doesn't exist or connection fails
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

            self.logger().debug(f"Connected to QTX order manager via shared memory '{self._shm_name}'")

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
        self.logger().debug("Shared memory resources cleaned up")

    async def disconnect(self):
        """Disconnect from shared memory and cleanup resources"""
        if not self._is_connected:
            return

        self.logger().debug("Disconnecting from shared memory")

        # Clean up resources
        self._cleanup_resources()

        # Reset state
        self._api_key_initialized = False
        self._pending_orders = {}

        self.logger().debug("Disconnected from shared memory successfully")

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

            self.logger().debug("Sending API key initialization request")

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
                self.logger().debug(f"API Key initialization response received: {response}")

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
        trade_type: TradeType,
        position_action: PositionAction,
        order_type: OrderType,
        price: Decimal,
        size: Decimal,
        price_match: int = 0,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Submit order to QTX via shared memory IPC.
        
        Contract: Expects pre-processed parameters (no MARKET orders, valid prices).
        Returns: (success: bool, response_data: dict) where response_data contains
        either order details (success) or error information (failure).
        """
        # Translate parameters to QTX format
        side = _translate_trade_type_to_side(trade_type)
        position_side = _translate_position_side(position_action, trade_type)
        order_type_int = _translate_order_type(order_type)
        
        # Parameter validation
        if not symbol or not symbol.strip():
            self.logger().error("Cannot place order: Empty trading pair symbol provided")
            return False, {"error": "Empty trading pair symbol provided"}

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
            # Convert client_order_id to integer
            try:
                client_order_id_int = int(client_order_id)
            except ValueError:
                client_order_id_int = abs(hash(client_order_id)) % (2**64 - 1)

            # Get order slot and initialize
            place_idx = self._queue_ptr.to_idx
            place_order_ref = self._queue_ptr.orders[place_idx]
            ctypes.memset(ctypes.addressof(place_order_ref), 0, ORDER_SIZE)

            # Set order parameters
            place_order_ref.mode = 1
            place_order_ref.side = side
            place_order_ref.pos_side = position_side
            place_order_ref.client_order_id = client_order_id_int
            place_order_ref.order_type = order_type_int
            place_order_ref.price_match = price_match
            place_order_ref.size = str(size).encode("utf-8")
            
            # Set placement timestamp in nanoseconds
            place_order_ref.place_ts_ns = int(time.time() * 1_000_000_000)

            # Use the provided price (derivative connector handles market order pricing)
            place_order_ref.price = str(price).encode("utf-8")
            place_order_ref.symbol = symbol.encode("utf-8")

            # Track pending order
            self._pending_orders[client_order_id] = place_order_ref

            self.logger().debug(f"Placing order: {symbol}, size={size}, price={price}, client_order_id={client_order_id}")

            # Submit order to queue
            self._queue_ptr.to_idx = (self._queue_ptr.to_idx + 1) % MAX_QUEUE_SIZE

            # Wait for response
            self.logger().debug(f"Waiting for order placement response for {client_order_id}...")
            timeout_seconds = 10.0
            start_time = time.time()

            try:
                while place_order_ref.res_ts_ns == 0:
                    if time.time() - start_time > timeout_seconds:
                        raise asyncio.TimeoutError("Order placement timed out")
                    await asyncio.sleep(0.01)

                # Process response
                response = place_order_ref.res_msg.decode(errors="ignore")
                self.logger().debug(f"Order placement response received for {client_order_id}: {response}")

                # Parse response to determine success and extract info
                success, response_data = self._parse_order_response(response, client_order_id)

                if success:
                    self.logger().debug(f"Order placed successfully: {client_order_id} - {response_data}")
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
            # Clean up tracking
            if client_order_id in self._pending_orders:
                del self._pending_orders[client_order_id]

    async def cancel_order(self, client_order_id: str, symbol: str) -> Tuple[bool, Dict[str, Any]]:
        """Cancel an order using shared memory communication with QTX order manager."""
        # Parameter validation
        if not symbol or not symbol.strip():
            self.logger().error("Cannot cancel order: Empty trading pair symbol provided")
            return False, {"error": "Empty trading pair symbol provided"}

        if not client_order_id or not client_order_id.strip():
            self.logger().error("Cannot cancel order: Empty client order ID provided")
            return False, {"error": "Empty client order ID provided"}

        # Check API key availability
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

            # Set cancellation parameters
            cancel_order_ref.mode = -1
            cancel_order_ref.client_order_id = client_order_id_int
            cancel_order_ref.symbol = symbol.encode("utf-8")

            # Track cancel request
            self._pending_orders[cancel_track_id] = cancel_order_ref

            self.logger().debug(f"Canceling order with client ID: {client_order_id} for symbol: {symbol}")

            # Submit cancellation to queue
            self._queue_ptr.to_idx = (self._queue_ptr.to_idx + 1) % MAX_QUEUE_SIZE

            # Wait for response
            self.logger().debug(f"Waiting for order cancellation response for {client_order_id}...")
            timeout_seconds = 10.0
            start_time = time.time()

            try:
                while cancel_order_ref.res_ts_ns == 0:
                    if time.time() - start_time > timeout_seconds:
                        raise asyncio.TimeoutError("Order cancellation timed out")
                    await asyncio.sleep(0.01)

                # Process response
                response = cancel_order_ref.res_msg.decode(errors="ignore")
                self.logger().debug(f"Order cancellation response received for {client_order_id}: {response}")

                # Parse response to determine success and extract info
                success, response_data = self._parse_cancel_response(response, client_order_id)

                if success:
                    self.logger().debug(f"Order cancelled successfully: {client_order_id} - {response_data}")
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

    def _is_order_not_found_error(self, error_msg: str, error_code: Optional[int] = None) -> bool:
        """Check if error indicates order not found using universal patterns."""
        if not error_msg:
            return False

        error_msg_lower = error_msg.lower()

        # Common order not found patterns
        not_found_patterns = [
            "not exist",
            "not found",
            "does not exist",
            "order not found",
            "invalid order",
            "unknown order",
            "order does not exist",
            "no such order",
            "order id not found",
            "order already filled",
            "order already cancelled",
            "order already canceled",  # US spelling
            "order expired",
            "order rejected",
        ]

        return any(pattern in error_msg_lower for pattern in not_found_patterns)

    def _is_successful_json_response(self, json_response: dict) -> bool:
        """Check if JSON response indicates success."""
        return (isinstance(json_response, dict) 
                and json_response.get("status") == 200 
                and "result" in json_response)

    def _is_successful_text_response(self, response: str) -> bool:
        """Check if text response indicates success using pattern matching."""
        if not response:
            return False
            
        response_lower = response.lower()
        success_patterns = ["success", "executed"]
        return any(pattern in response_lower for pattern in success_patterns)

    def _is_successful_cancel_response(self, response: str) -> bool:
        """Check if text response indicates successful cancellation."""
        if not response:
            return False
            
        response_lower = response.lower()
        cancel_success_patterns = ["success", "canceled", "cancelled"]
        return any(pattern in response_lower for pattern in cancel_success_patterns)

    def _extract_timestamp_from_response(self, response_data: dict) -> float:
        """
        Extracts timestamp from response data with proper conversion.
        Handles both updateTime and time fields, converting milliseconds to seconds.
        
        :param response_data: Dictionary containing response data
        :return: Timestamp in seconds (float)
        """
        # Try updateTime first, then time
        timestamp_fields = ["updateTime", "time"]
        
        for field in timestamp_fields:
            if field in response_data:
                timestamp = response_data[field]
                # Convert from milliseconds to seconds if needed
                if timestamp > 1000000000000:  # If timestamp is in milliseconds
                    timestamp = timestamp / 1000
                return timestamp
        
        # Use current time if not found
        return time.time()

    def _parse_order_response(self, response: str, client_order_id: str) -> Tuple[bool, Dict[str, Any]]:
        """Parse order placement response."""
        response_data = {"raw_response": response, "client_order_id": client_order_id}
        success: bool

        # Try to parse the response as JSON first, as the binance-futures response is in JSON format
        try:
            json_response = json.loads(response)
            # Check for success using extracted method
            if self._is_successful_json_response(json_response):
                success = True
                result = json_response["result"]

                # Extract exchange order ID (orderId field in Binance response)
                if "orderId" in result:
                    response_data["exchange_order_id"] = str(result["orderId"])
                else:
                    # If no exchange order ID found, use client_order_id as fallback
                    response_data["exchange_order_id"] = client_order_id
                    self.logger().warning(
                        f"Could not extract exchange order ID from response for {client_order_id}. "
                        f"Using client_order_id as fallback."
                    )

                # Extract order status
                if "status" in result:
                    response_data["status"] = result["status"]
                else:
                    # Default to NEW status if not found
                    response_data["status"] = "NEW"

                # Extract symbol/trading pair
                if "symbol" in result:
                    response_data["symbol"] = result["symbol"]

                # Extract price
                if "price" in result:
                    response_data["price"] = result["price"]

                # Extract executed quantity (for filled orders)
                if "executedQty" in result:
                    response_data["executed_qty"] = result["executedQty"]

                # Extract timestamp - look for updateTime or time
                if "updateTime" in result:
                    # Convert to seconds if in milliseconds
                    timestamp = result["updateTime"]
                    if timestamp > 1000000000000:  # If timestamp is in milliseconds
                        timestamp = timestamp / 1000
                    response_data["transaction_time"] = timestamp
                elif "time" in result:
                    timestamp = result["time"]
                    if timestamp > 1000000000000:  # If timestamp is in milliseconds
                        timestamp = timestamp / 1000
                    response_data["transaction_time"] = timestamp
                else:
                    # Use current time if not found
                    response_data["transaction_time"] = time.time()

                return success, response_data
            else:
                # JSON parsing worked but response indicates failure
                success = False
                error_msg = "Unknown error"

                # Extract error message if present
                if "error" in json_response:
                    error_msg = json_response.get("error", "Unknown error")
                elif "msg" in json_response:
                    error_msg = json_response.get("msg", "Unknown error")

                # Extract error code if present
                if "code" in json_response:
                    response_data["error_code"] = json_response["code"]

                response_data["error"] = error_msg
                return success, response_data

        except (json.JSONDecodeError, ValueError, TypeError):
            # If JSON parsing failed, fall back to regex-based parsing
            # This is the original behavior for non-JSON responses

            # Check for success indicators using extracted method
            if self._is_successful_text_response(response):
                success = True

                # Extract exchange order ID if present
                # Try multiple patterns for different exchange formats
                exchange_id_match = re.search(r"order[iI]d[\":\s]+([0-9a-zA-Z]+)", response)
                if exchange_id_match:
                    # Use the exact exchange order ID without adding any prefix
                    response_data["exchange_order_id"] = exchange_id_match.group(1)
                else:
                    # If no exchange order ID is found, use client_order_id as fallback without any prefix
                    response_data["exchange_order_id"] = client_order_id
                    self.logger().warning(
                        f"Could not extract exchange order ID from response for {client_order_id}. "
                        f"Using client_order_id as fallback."
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

                # Extract timestamp if present - look for updateTime or time
                time_match = re.search(r"(updateTime|time)[\":\s]+(\d+)", response)
                if time_match:
                    # Convert to seconds if in milliseconds
                    timestamp = int(time_match.group(2))
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
        """Parse order cancellation response."""
        response_data = {"raw_response": response, "client_order_id": client_order_id}

        # Try to parse the response as JSON first, as the binance-futures response is in JSON format
        try:
            json_response = json.loads(response)

            # Check for success using extracted method
            if self._is_successful_json_response(json_response):
                result = json_response["result"]

                # Extract status
                if "status" in result:
                    response_data["status"] = result["status"]
                else:
                    # Default to CANCELED status
                    response_data["status"] = "CANCELED"

                # Extract exchange order ID if present
                if "orderId" in result:
                    response_data["exchange_order_id"] = str(result["orderId"])

                # Extract timestamp - look for updateTime or time
                if "updateTime" in result:
                    timestamp = result["updateTime"]
                    if timestamp > 1000000000000:  # If timestamp is in milliseconds
                        timestamp = timestamp / 1000
                    response_data["transaction_time"] = timestamp
                elif "time" in result:
                    timestamp = result["time"]
                    if timestamp > 1000000000000:  # If timestamp is in milliseconds
                        timestamp = timestamp / 1000
                    response_data["transaction_time"] = timestamp
                else:
                    # Use current time if not found
                    response_data["transaction_time"] = time.time()

                return True, response_data
            else:
                # Check if it's an "order not found" type response, which we can also treat as success
                error_code = json_response.get("code", 0)
                error_msg = json_response.get("msg", "")

                if self._is_order_not_found_error(error_msg, error_code):
                    self.logger().debug(
                        f"Order {client_order_id} not found (error_code: {error_code}, msg: '{error_msg}'). Treating as already cancelled."
                    )
                    response_data["note"] = "Order already cancelled or does not exist"
                    response_data["status"] = "CANCELED"
                    response_data["transaction_time"] = time.time()
                    return True, response_data

                # JSON parsing worked but response indicates failure
                response_data["error"] = json_response.get("msg", "Unknown error")
                if "code" in json_response:
                    response_data["error_code"] = json_response["code"]

                return False, response_data

        except (json.JSONDecodeError, ValueError, TypeError):
            # If JSON parsing failed, fall back to regex-based parsing
            # Check for success indicators using extracted method
            if self._is_successful_cancel_response(response):
                # Extract status if present
                status_match = re.search(r"status[\":\s]+([a-zA-Z_]+)", response)
                if status_match:
                    response_data["status"] = status_match.group(1)
                else:
                    # Default to CANCELED status
                    response_data["status"] = "CANCELED"

                # Extract exchange order ID if present - try both formats
                exchange_id_match = re.search(r"order[iI]d[\":\s]+([0-9a-zA-Z]+)", response)
                if exchange_id_match:
                    response_data["exchange_order_id"] = exchange_id_match.group(1)

                # Extract timestamp if present - look for updateTime or time
                time_match = re.search(r"(updateTime|time)[\":\s]+(\d+)", response)
                if time_match:
                    # Convert to seconds if in milliseconds
                    timestamp = int(time_match.group(2))
                    if timestamp > 1000000000000:  # If timestamp is in milliseconds
                        timestamp = timestamp / 1000
                    response_data["transaction_time"] = timestamp
                else:
                    # Use current time if not found
                    response_data["transaction_time"] = time.time()

                return True, response_data
            else:
                # Check for "order does not exist" or similar, which we can also treat as success
                if self._is_order_not_found_error(response):
                    self.logger().debug(
                        f"Order {client_order_id} not found in response: '{response}'. Treating as already cancelled."
                    )
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