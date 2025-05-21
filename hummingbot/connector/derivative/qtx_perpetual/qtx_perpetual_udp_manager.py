#!/usr/bin/env python

import asyncio
import logging
import select
import socket
import struct
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from hummingbot.connector.derivative.qtx_perpetual import (
    qtx_perpetual_constants as CONSTANTS,
    qtx_perpetual_trading_pair_utils as trading_pair_utils,
)
from hummingbot.logger import HummingbotLogger

# ---------------------------------------- UDP Manager Class ----------------------------------------

DEFAULT_UDP_BUFFER_SIZE = 65536


class QtxPerpetualUDPManager:
    """
    Manager for QTX Perpetual UDP connections.

    This class handles all UDP socket operations including:
    - Socket creation and configuration
    - Trading pair subscriptions
    - Message receiving and parsing
    - Connection status monitoring and reconnection
    """

    _logger: Optional[HummingbotLogger] = None

    def __init__(
        self,
        host: str = CONSTANTS.DEFAULT_UDP_HOST,
        port: int = CONSTANTS.DEFAULT_UDP_PORT,
        buffer_size: int = DEFAULT_UDP_BUFFER_SIZE,
        exchange_name_on_qtx: str = None,
    ):
        """Initialize the UDP manager."""
        # UDP connection settings
        self._host = host
        self._port = port
        self._buffer_size = buffer_size

        # Validate exchange name
        if exchange_name_on_qtx is None:
            self.logger().warning(
                "No exchange_name_on_qtx provided to UDP manager! QTX trading pair conversion may fail."
            )
        self._exchange_name_on_qtx = exchange_name_on_qtx

        # Socket status
        self._udp_socket = None
        self._is_connected = False
        self._listening_task = None
        self._cleanup_task = None

        # Subscription tracking
        self._subscription_indices = {}  # index -> trading_pair
        self._subscribed_pairs = set()
        self._empty_orderbook = {}  # trading_pair -> is_empty
        self._last_update_id = {}  # trading_pair -> last_update_id

        # Market data collection state tracking
        self._snapshot_collection_tasks = {}  # trading_pair -> asyncio.Task
        self._snapshot_collecting = set()  # Set of trading pairs currently being collected

        # Message callbacks - mapping message types to callback functions
        self._message_callbacks = {
            1: None,  # Ticker bid
            -1: None,  # Ticker ask
            2: None,  # Depth
            3: None,  # Trade buy
            -3: None,  # Trade sell
        }

        # Statistics
        self._receive_count = 0
        self._symbol_message_counts = {}
        self._last_stats_time = 0
        self._connection_start_time = 0

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    # ---------------------------------------- Connection Management ----------------------------------------
    async def connect(self) -> bool:
        """
        Establish a UDP socket connection

        :return: True when connection is successful
        :raises: ConnectionError if connection fails
        """
        # Only log basic connection info at DEBUG level
        self.logger().debug(
            "Connect called - is_connected=%s, has_socket=%s, subscribed_pairs=%d",
            self._is_connected, 
            self._udp_socket is not None,
            len(self._subscribed_pairs)
        )

        # Check if we're already connected - avoid redundant reconnections
        if self._is_connected and self._udp_socket is not None:
            try:
                # If we have active subscriptions, don't ping - we could confuse the socket buffer
                if self._subscribed_pairs:
                    self.logger().debug(
                        "UDP connection already active with %d subscriptions, skipping ping check",
                        len(self._subscribed_pairs)
                    )
                    return True

                # Verify connection is still active with a quick ping
                self.logger().debug("Testing UDP connection with ping")
                self._udp_socket.settimeout(2.0)  # Short timeout for check
                self._udp_socket.sendto(b"ping", (self._host, self._port))

                # Try to receive ping response to ensure buffer is clean
                try:
                    # Use select to check if there's data to read without blocking
                    ready_to_read, _, _ = select.select([self._udp_socket], [], [], 1.0)
                    if ready_to_read:
                        pong_response, addr = self._udp_socket.recvfrom(self._buffer_size)
                        self.logger().debug("Cleared ping response from socket buffer")
                except Exception as e:
                    self.logger().debug("No ping response or error receiving it: %s", e)

                self.logger().debug("UDP connection verified to %s:%d", self._host, self._port)
                return True
            except (socket.timeout, OSError) as e:
                # Connection is stale, proceed with reconnection
                self.logger().info("Existing connection is stale: %s. Reconnecting...", e)
                self._close_socket()
            except Exception as e:
                # Any other error, proceed with reconnection
                self.logger().warning(
                    "Error checking existing connection: %s. Recreating socket...", e
                )
                self._close_socket()
        elif self._is_connected:
            # Connected flag is set but socket is None (inconsistent state)
            self.logger().warning(
                "Connected flag is set but socket is None. Fixing inconsistent state..."
            )
            self._is_connected = False

        try:
            # Create a new UDP socket
            self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._buffer_size)

            # Set socket options for better reliability
            # Enable socket address reuse
            self._udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Bind to any address on an automatic port for receiving data
            self._udp_socket.bind(("0.0.0.0", 0))
            self.logger().debug("Created new UDP socket, bound to 0.0.0.0:0")

            # Ping the server to check connection
            self._udp_socket.settimeout(2.0)
            self.logger().debug("Sending ping to test connection")

            try:
                # Send ping and track send time for correlation with response
                send_time = time.time()
                self._udp_socket.sendto(b"ping", (self._host, self._port))

                # Fully drain the socket buffer to avoid mixing ping responses with later subscription ACKs
                # Wait up to 1 second for a response, but keep draining until socket has no more data or times out
                pong_received = False
                drain_attempts = 0
                max_drain_attempts = 5  # Maximum number of messages to drain

                while drain_attempts < max_drain_attempts:
                    drain_attempts += 1
                    try:
                        # Check if there's any data to read (non-blocking)
                        ready_to_read, _, _ = select.select([self._udp_socket], [], [], 0.2)
                        if not ready_to_read:
                            # If no data after first attempt and we already got a pong, we're done
                            if pong_received and drain_attempts > 1:
                                break
                            # If no data on first attempt, wait longer
                            if drain_attempts == 1:
                                # Wait up to 1 second for first response
                                ready_to_read, _, _ = select.select([self._udp_socket], [], [], 0.8)
                                if not ready_to_read:
                                    # If still no data, likely no response coming
                                    self.logger().debug(
                                        "No ping response after 1 second, assuming no response"
                                    )
                                    break

                        # Read the data if available
                        pong_response, addr = self._udp_socket.recvfrom(self._buffer_size)
                        response_time = time.time()
                        latency_ms = int((response_time - send_time) * 1000)

                        # Check if this looks like a pong response (text data that could be parsed as a number is likely an ACK)
                        is_likely_ack = False
                        try:
                            # If it parses as a number, likely an ACK not a pong
                            int(pong_response.decode("utf-8").strip())
                            is_likely_ack = True
                        except (UnicodeDecodeError, ValueError):
                            # Not numeric text, could be binary data or actual pong response
                            pass

                        if is_likely_ack:
                            self.logger().debug(
                                "Received ACK-like message during ping test - treating as connection confirmation"
                            )
                            # Put it back (not possible with UDP) or remember it for later processing
                            # For now, just break and assume we got the "pong" even though it's an ACK
                            pong_received = True
                            break
                        else:
                            self.logger().debug(
                                "Drained message from socket buffer, latency: %dms", 
                                latency_ms
                            )
                            pong_received = True

                    except socket.timeout:
                        # If we time out and already got a pong, that's fine
                        if pong_received:
                            self.logger().debug("Socket drained, no more messages")
                        else:
                            self.logger().debug(
                                "Timeout waiting for ping response (attempt %d)", 
                                drain_attempts
                            )
                        break
                    except Exception as e:
                        self.logger().debug("Error receiving ping response: %s", e)
                        # If we already got at least one response, assume socket is working
                        if pong_received:
                            break
                        # Otherwise, this is a connection failure
                        if drain_attempts >= 3:  # Give a few chances
                            raise

                # Regardless of pong response, we consider the connection established if send succeeds
                # Most QTX servers don't respond to pings with pongs, but the send not failing is enough
                self._is_connected = True
                self._connection_start_time = time.time()
                self.logger().debug(
                    "UDP connection established to %s:%d",
                    self._host, self._port
                )

                # Reset subscription tracking when creating a new connection
                if not self._subscribed_pairs:
                    self._subscription_indices = {}
                    self._empty_orderbook = {}
                    self._last_update_id = {}
                    self._symbol_message_counts = {}

                return True
            except (socket.timeout, ConnectionRefusedError) as e:
                self._close_socket()
                raise ConnectionError(f"Failed to connect to UDP server: {e}")

        except Exception as e:
            self._close_socket()
            raise ConnectionError(f"Error establishing UDP connection: {e}")

    def _close_socket(self):
        """Close the UDP socket safely"""
        if self._udp_socket is not None:
            try:
                # Set to non-blocking mode before closing to prevent any blocking calls
                self._udp_socket.setblocking(False)

                # Try to shutdown the socket if possible (may not be applicable to UDP)
                try:
                    # For UDP, we don't need to shutdown since it's connectionless
                    # But if socket has a shutdown method, try to use it
                    if hasattr(self._udp_socket, "shutdown"):
                        self._udp_socket.shutdown(socket.SHUT_RDWR)
                except Exception:
                    # Ignore shutdown errors, proceed with close
                    pass

                # Close the socket
                self._udp_socket.close()
                self.logger().debug("UDP socket closed successfully")
            except Exception as e:
                self.logger().error(f"Error closing UDP socket: {e}", exc_info=True)
            finally:
                # Ensure the socket is set to None and connection flag is reset
                self._udp_socket = None
                self._is_connected = False
                self.logger().debug("UDP socket reference cleared")

    # ---------------------------------------- End of Connection Management ----------------------------------------

    # ---------------------------------------- Subscription Management ----------------------------------------
    async def subscribe_to_trading_pairs(self, trading_pairs: List[str]) -> Tuple[bool, List[str]]:
        """
        Subscribe to multiple trading pairs via the UDP feed

        :param trading_pairs: List of trading pairs in Hummingbot format
        :return: Tuple of (success, successfully_subscribed_pairs)
        """
        # Track subscription attempt ID for debugging
        subscription_id = f"sub_{int(time.time())}"

        # Check if any trading pairs are already subscribed
        already_subscribed = [tp for tp in trading_pairs if tp in self._subscribed_pairs]
        new_pairs = [tp for tp in trading_pairs if tp not in self._subscribed_pairs]

        # Only log already subscribed pairs at debug level
        if already_subscribed:
            self.logger().debug(
                "Skipping %d already subscribed pairs",
                len(already_subscribed)
            )

        if not new_pairs:
            self.logger().debug(
                "No new pairs to subscribe to, all requested pairs already subscribed"
            )
            return True, already_subscribed

        self.logger().debug("Will attempt to subscribe to %d new pairs", len(new_pairs))

        # Make sure we're connected
        if not self._is_connected or self._udp_socket is None:
            self.logger().debug("No active connection for subscription, attempting to connect...")
            success = await self.connect()
            if not success:
                self.logger().error("Failed to establish connection for subscription")
                return len(already_subscribed) > 0, already_subscribed

        # Set socket to blocking mode for subscription phase
        self._udp_socket.setblocking(True)
        self._udp_socket.settimeout(2.0)

        successful_pairs = list(already_subscribed)  # Start with already subscribed pairs
        subscription_attempts = []

        # Check for existing exchange symbols
        exchange_symbols_to_pairs = {}
        for trading_pair in new_pairs:
            exchange_symbol = trading_pair_utils.convert_to_qtx_trading_pair(trading_pair, self._exchange_name_on_qtx)
            exchange_symbols_to_pairs[exchange_symbol] = trading_pair

        # Look for duplicates in QTX symbols
        for symbol, pair in list(exchange_symbols_to_pairs.items()):
            # Check if another pair maps to the same exchange symbol
            for existing_pair in self._subscribed_pairs:
                existing_symbol = trading_pair_utils.convert_to_qtx_trading_pair(
                    existing_pair, self._exchange_name_on_qtx
                )
                if existing_symbol == symbol:
                    self.logger().info(
                        f"[{subscription_id}] Trading pair {pair} maps to same QTX symbol ({symbol}) "
                        f"as already subscribed pair {existing_pair}, skipping"
                    )
                    successful_pairs.append(pair)
                    del exchange_symbols_to_pairs[symbol]
                    break

        # Subscribe to remaining trading pairs
        for exchange_symbol, trading_pair in exchange_symbols_to_pairs.items():
            try:
                self.logger().debug(
                    f"[{subscription_id}] Subscribing to QTX UDP feed: {exchange_symbol} for {trading_pair}"
                )
                subscription_attempts.append(trading_pair)

                # Send subscription request
                subscription_bytes = exchange_symbol.encode()
                self.logger().debug(
                    f"[{subscription_id}] Sending subscription request: {subscription_bytes!r} "
                    f"(hex: {subscription_bytes.hex()}) for {trading_pair}"
                )
                self._udp_socket.sendto(subscription_bytes, (self._host, self._port))

                # Wait for ACK (index number)
                deadline = time.time() + 2.0
                index = None
                ack_raw = None

                while time.time() < deadline:
                    try:
                        response, addr = self._udp_socket.recvfrom(self._buffer_size)
                        ack_raw = response
                        # Log raw response data
                        self.logger().debug(
                            f"[{subscription_id}] Received raw response from {addr}: {response!r} (hex: {response.hex()})"
                        )
                    except socket.timeout:
                        self.logger().error(f"[{subscription_id}] Timeout waiting for ACK for {exchange_symbol}")
                        break

                    # ignore anything not from the server
                    if addr[0] != self._host:
                        self.logger().warning(
                            f"[{subscription_id}] Received response from unexpected source: {addr[0]}, expected: {self._host}"
                        )
                        continue

                    # try to parse out a simple integer ACK
                    try:
                        index = int(response.decode("utf-8").strip())
                        self.logger().debug(f"[{subscription_id}] Got subscription ACK {index} for {exchange_symbol}")
                        break
                    except (UnicodeDecodeError, ValueError):
                        # Try to determine if this is a market data message
                        if len(response) >= 40:
                            try:
                                msg_type, idx, tx_ms, event_ms, local_ns, sn_id = struct.unpack(
                                    "<iiqqqq", response[:40]
                                )
                                self.logger().debug(
                                    f"Got market data during ACK-wait: type={msg_type}, index={idx}"
                                )
                            except struct.error:
                                self.logger().debug(
                                    f"Failed to parse header from non-text message: {response.hex()[:80]}..."
                                )
                        else:
                            self.logger().debug(
                                f"Not a valid subscription response: {response!r} (hex: {response.hex()})"
                            )
                        continue

                else:
                    # only hit if we never broke out of the while
                    self.logger().error(f"[{subscription_id}] No valid ACK for {exchange_symbol}, raw ACK: {ack_raw!r}")
                    if ack_raw:
                        self.logger().error(f"[{subscription_id}] Raw ACK hex: {ack_raw.hex()}")
                    continue

                if index is None:
                    self.logger().error(f"[{subscription_id}] No valid ACK for {exchange_symbol}, raw ACK: {ack_raw!r}")
                    if ack_raw:
                        self.logger().error(f"[{subscription_id}] Raw ACK hex: {ack_raw.hex()}")
                    continue

                # Check if this index is already used for a different trading pair
                if index in self._subscription_indices:
                    existing_pair = self._subscription_indices[index]
                    # If the same trading pair already has this index, we've somehow double-subscribed
                    if existing_pair == trading_pair:
                        self.logger().info(
                            f"[{subscription_id}] Trading pair {trading_pair} is already subscribed with index {index}, "
                            f"not registering again"
                        )
                        successful_pairs.append(trading_pair)
                        continue
                    else:
                        # This is an actual collision - different symbols with same index
                        self.logger().warning(
                            f"[{subscription_id}] Index collision detected: index {index} already mapped to {existing_pair}, "
                            f"now being assigned to {trading_pair}"
                        )

                # Store subscription information - like in the C code we check first
                # if NOT already subscribed to avoid duplicates
                if trading_pair not in self._subscribed_pairs:
                    self._subscription_indices[index] = trading_pair
                    self._subscribed_pairs.add(trading_pair)
                    self._empty_orderbook[trading_pair] = True
                    self._last_update_id[trading_pair] = int(time.time() * 1000)
                    self._symbol_message_counts[trading_pair] = 0

                    self.logger().info(
                        f"[{subscription_id}] Successfully subscribed to {exchange_symbol} with index {index}"
                    )
                else:
                    self.logger().info(
                        f"[{subscription_id}] Trading pair {trading_pair} is already in subscribed_pairs list, "
                        f"not adding again"
                    )

                # Add to successful subscriptions list
                successful_pairs.append(trading_pair)

            except Exception as e:
                self.logger().error(f"[{subscription_id}] Error subscribing to {trading_pair}: {e}", exc_info=True)

            # Small delay between subscriptions
            await asyncio.sleep(0.1)

        # Set socket back to non-blocking for receiving data
        self._udp_socket.setblocking(False)

        # Log detailed subscription results - only at DEBUG level
        if subscription_attempts and self.logger().isEnabledFor(logging.DEBUG):
            success_rate = len(successful_pairs) / len(trading_pairs) * 100
            self.logger().debug(
                f"[{subscription_id}] Subscription summary: {len(successful_pairs)}/{len(trading_pairs)} successful ({success_rate:.1f}%)"
            )

        return len(successful_pairs) > 0, successful_pairs

    async def unsubscribe_from_symbol(self, qtx_symbol: str) -> bool:
        """
        Unsubscribe from a single QTX symbol directly

        :param qtx_symbol: QTX formatted symbol (e.g., "binance-futures:btcusdt")
        :return: True if successful, False otherwise
        """
        # ID no longer needed for logging

        if not self._is_connected or self._udp_socket is None:
            self.logger().warning(f"Cannot unsubscribe from {qtx_symbol}: not connected")
            return False

        try:
            # Log socket state at debug level only
            self.logger().debug(
                f"Socket state before unsubscribe - connected: {self._is_connected}, "
                f"socket is None: {self._udp_socket is None}, "
                f"subscribed pairs: {self._subscribed_pairs}"
            )
            self.logger().debug(f"Current subscription indices: {self._subscription_indices}")

            # Set socket to blocking for unsubscribe
            self._udp_socket.setblocking(True)
            self._udp_socket.settimeout(2.0)  # Increased timeout for testing

            # Format unsubscribe message
            unsubscribe_msg = f"-{qtx_symbol}"
            unsubscribe_bytes = unsubscribe_msg.encode()
            self.logger().debug(f"Unsubscribing from symbol: {qtx_symbol}")
            self.logger().debug(f"Sending unsubscribe request: {unsubscribe_bytes!r} (hex: {unsubscribe_bytes.hex()})")

            # Before unsubscribing, we need to find the associated trading pair and indices for later cleanup
            # For the given QTX symbol, find the corresponding Hummingbot trading pair
            hb_pairs_for_cleanup = []
            indices_for_cleanup = []

            # Find Hummingbot trading pairs corresponding to this QTX symbol
            for hb_pair in list(self._subscribed_pairs):
                if trading_pair_utils.convert_to_qtx_trading_pair(hb_pair, self._exchange_name_on_qtx) == qtx_symbol:
                    hb_pairs_for_cleanup.append(hb_pair)
                    # Find all indices assigned to this trading pair
                    for idx, pair in list(self._subscription_indices.items()):
                        if pair == hb_pair:
                            indices_for_cleanup.append(idx)

            if indices_for_cleanup:
                self.logger().debug(f"Found indices {indices_for_cleanup} for QTX symbol {qtx_symbol} (trading pairs: {hb_pairs_for_cleanup})")

            # Try sending the unsubscribe message multiple times to ensure it's delivered
            max_attempts = 3
            unsubscribe_success = False
            unsubscribe_index = None

            for attempt in range(1, max_attempts + 1):
                # Send unsubscribe request
                self._udp_socket.sendto(unsubscribe_bytes, (self._host, self._port))

                # Wait for response
                try:
                    response, addr = self._udp_socket.recvfrom(self._buffer_size)
                    self.logger().debug(f"Received raw unsubscribe response from {addr}: {response!r} (hex: {response.hex()})")

                    try:
                        # Try to parse as text first - might be an ACK or success message
                        response_text = response.decode().strip()
                        self.logger().debug(f"Unsubscribe response for {qtx_symbol}: {response_text}")

                        # Try to extract an unsubscribe index if the response contains it
                        try:
                            unsubscribe_index = int(response_text)
                            self.logger().debug(f"Received unsubscribe index {unsubscribe_index} for {qtx_symbol}")
                            # If we got a numeric response, consider unsubscription successful
                            unsubscribe_success = True
                        except ValueError:
                            # Not a number but still a text response
                            if "unsubscribed" in response_text.lower():
                                self.logger().debug(f"Received unsubscribe confirmation for {qtx_symbol}: {response_text}")
                                unsubscribe_success = True  # noqa: F841
                            else:
                                self.logger().debug(f"Received text response but not unsubscribe confirmation: {response_text}")
                                # Keep trying - might be other messages in the queue
                                continue

                        # Successful response received
                        break
                    except UnicodeDecodeError:
                        # Try to parse as a binary message - might be market data
                        if len(response) >= 40:
                            try:
                                msg_type, index, *_ = struct.unpack("<iiqqqq", response[:40])
                                self.logger().debug(f"Received market data during unsubscribe: type={msg_type}, index={index}")
                                # Skip market data and continue waiting for unsubscribe response
                                continue
                            except struct.error:
                                pass

                        self.logger().debug(f"Received binary response for {qtx_symbol} (hex: {response.hex()})")
                        # Continue waiting for a proper text response
                        continue

                except socket.timeout:
                    self.logger().warning(f"Timeout waiting for unsubscribe response for {qtx_symbol} (attempt {attempt}/{max_attempts})")
                    # Only retry if we haven't reached max attempts
                    if attempt == max_attempts:
                        self.logger().error(f"Failed to get unsubscribe confirmation for {qtx_symbol} after {max_attempts} attempts")

            # Even if we didn't get a clear confirmation, proceed with cleanup
            # This is safer than leaving stale entries in the tracking structures

            # Clean up internal tracking for this symbol
            # First, remove the symbol from subscribed_pairs
            pairs_removed = set()
            for hb_pair in hb_pairs_for_cleanup:
                if hb_pair in self._subscribed_pairs:
                    self._subscribed_pairs.discard(hb_pair)
                    pairs_removed.add(hb_pair)
                    self._empty_orderbook.pop(hb_pair, None)
                    self._last_update_id.pop(hb_pair, None)
                    self._symbol_message_counts.pop(hb_pair, None)

            # Second, clean up all indices associated with this symbol
            indices_removed = set()
            for idx in indices_for_cleanup:
                if idx in self._subscription_indices:
                    self._subscription_indices.pop(idx)
                    indices_removed.add(idx)

            # Log cleanup results
            if pairs_removed or indices_removed:
                self.logger().debug(f"Cleaned up after unsubscribing from {qtx_symbol}: removed {len(pairs_removed)} trading pairs and {len(indices_removed)} indices")
                if self.logger().isEnabledFor(logging.DEBUG):
                    self.logger().debug(f"Removed pairs: {pairs_removed}")
                    self.logger().debug(f"Removed indices: {indices_removed}")

            # Check if the received unsubscribe index matches one of our indices
            if unsubscribe_index is not None and unsubscribe_index not in indices_removed:
                self.logger().warning(f"Received unsubscribe index {unsubscribe_index} for {qtx_symbol}, but this index was not in our tracked indices: {indices_for_cleanup}")

            # Set socket back to non-blocking
            try:
                self._udp_socket.setblocking(False)
            except Exception as e:
                self.logger().error(f"Error setting socket to non-blocking: {e}", exc_info=True)
                # Not a critical error, we can continue

            return True

        except Exception as e:
            self.logger().error(f"Error unsubscribing from {qtx_symbol}: {e}", exc_info=True)
            # Try to set socket back to non-blocking
            try:
                if self._udp_socket is not None:
                    self._udp_socket.setblocking(False)
            except Exception:
                pass
            return False

    async def unsubscribe_from_trading_pairs(self, trading_pairs: List[str]) -> bool:
        """
        Unsubscribe from trading pairs

        :param trading_pairs: List of trading pairs to unsubscribe from
        :return: True if successful, False otherwise
        """
        # Operation ID no longer needed for logging

        if not self._is_connected or self._udp_socket is None:
            self.logger().warning(f"Cannot unsubscribe: not connected or socket is None")
            return False

        try:
            self.logger().info(f"Unsubscribing from {len(trading_pairs)} trading pairs")

            # Group trading pairs by QTX symbol for more efficient unsubscription
            # Multiple Hummingbot trading pairs might map to the same QTX symbol
            qtx_symbol_to_pairs = {}
            for trading_pair in trading_pairs:
                if trading_pair not in self._subscribed_pairs:
                    self.logger().debug(f"Trading pair {trading_pair} not subscribed, skipping")
                    continue

                exchange_symbol = trading_pair_utils.convert_to_qtx_trading_pair(
                    trading_pair, self._exchange_name_on_qtx
                )
                if exchange_symbol not in qtx_symbol_to_pairs:
                    qtx_symbol_to_pairs[exchange_symbol] = []
                qtx_symbol_to_pairs[exchange_symbol].append(trading_pair)

            # Process unsubscriptions by QTX symbol (since that's how the server handles unsubscribes)
            success = True
            pairs_processed = []

            for exchange_symbol, pairs in qtx_symbol_to_pairs.items():
                self.logger().debug(f"Unsubscribing from QTX symbol {exchange_symbol} for pairs: {pairs}")
                result = await self.unsubscribe_from_symbol(exchange_symbol)

                # The unsubscribe_from_symbol method already handles cleanup of tracking structures
                # We just need to track success/failure and process results
                if result:
                    pairs_processed.extend(pairs)
                    self.logger().debug(f"Successfully unsubscribed from QTX symbol {exchange_symbol}")
                else:
                    success = False
                    self.logger().error(f"Failed to unsubscribe from QTX symbol {exchange_symbol}")

            # Log summary
            if pairs_processed:
                self.logger().info(f"Unsubscription complete: {len(pairs_processed)} trading pairs processed")
            else:
                self.logger().warning(f"No trading pairs were processed during unsubscription")

            return success

        except Exception as e:
            self.logger().error(f"Error during unsubscription: {e}", exc_info=True)
            return False

    async def unsubscribe_from_all(self):
        """Unsubscribe from all currently subscribed trading pairs"""
        if self._subscribed_pairs:
            await self.unsubscribe_from_trading_pairs(list(self._subscribed_pairs))

    # ---------------------------------------- Callback Registration ----------------------------------------
    def register_message_callback(self, msg_type: int, callback: Callable[[Dict[str, Any]], None]):
        """
        Register a callback function for a specific message type

        :param msg_type: Message type (1/-1 for ticker, 2 for depth, 3/-3 for trade)
        :param callback: Callback function to receive parsed messages
        """
        callback_name = callback.__qualname__ if hasattr(callback, "__qualname__") else str(callback)
        self.logger().debug(f"Registering callback for message type {msg_type}: {callback_name}")

        # Check if there's already a callback registered
        if self._message_callbacks.get(msg_type) is not None:
            old_callback = self._message_callbacks[msg_type]
            old_name = old_callback.__qualname__ if hasattr(old_callback, "__qualname__") else str(old_callback)
            self.logger().debug(
                f"Overriding existing callback for message type {msg_type}: {old_name} -> {callback_name}"
            )

        self._message_callbacks[msg_type] = callback

    @property
    def subscription_indices(self):
        """
        Get a copy of the subscription indices mapping.
        This allows the derivative connector to see which indices map to which trading pairs.
        """
        return self._subscription_indices.copy()

    def listen(self):
        """
        Alias for start_listening for backward compatibility
        """
        return self.start_listening()

    # ---------------------------------------- End of Callback Registration ----------------------------------------

    # This section was removed in favor of using trading_pair_utils directly
    # throughout the code, eliminating unnecessary indirection

    # ---------------------------------------- Lifecycle Management ----------------------------------------
    async def start_listening(self):
        """Start the listening task for incoming UDP messages"""
        # Check if already listening and task is still running
        if self._listening_task is not None:
            if not self._listening_task.done():
                self.logger().debug("UDP listener already running, no need to start again")
                return
            else:
                # Task is done but reference exists - clean it up
                self.logger().info("Cleaning up completed listener task reference")
                self._listening_task = None

        # Ensure we're connected before starting listener
        if not self._is_connected or self._udp_socket is None:
            self.logger().info("No active connection, attempting to connect before starting listener")
            try:
                success = await self.connect()
                if not success:
                    self.logger().error("Cannot start listening: failed to establish UDP connection")
                    return
            except Exception as e:
                self.logger().error(f"Error connecting before starting listener: {e}")
                return

        # Create new listening task
        self._listening_task = asyncio.create_task(self._listen_for_messages())
        self.logger().debug("Started UDP message listener")

    async def stop_listening(self):
        """Stop the listening task and clean up resources"""
        self.logger().debug("Beginning UDP listener shutdown sequence")

        # Cancel the listening task to prevent processing of new messages
        if self._listening_task is not None and not self._listening_task.done():
            self.logger().debug("Cancelling UDP listener task")
            self._listening_task.cancel()
            try:
                await self._listening_task
                self.logger().debug("UDP listener task cancelled successfully")
            except asyncio.CancelledError:
                self.logger().debug("UDP listener task cancelled")
            except Exception as e:
                self.logger().error(f"Error while cancelling UDP listener task: {e}", exc_info=True)
            finally:
                self._listening_task = None

        # Stop any continuous data collections
        if hasattr(self, "_continuous_collections") and self._continuous_collections:
            try:
                self.logger().debug("Stopping continuous data collections")
                await self.stop_all_continuous_collections()
            except Exception as e:
                self.logger().error(f"Error stopping continuous collections: {e}", exc_info=True)

        # Unsubscribe from all pairs
        if self._subscribed_pairs:
            try:
                self.logger().debug(f"Unsubscribing from {len(self._subscribed_pairs)} pairs")
                await self.unsubscribe_from_all()

                # Add a small delay to allow unsubscribe messages to be processed
                await asyncio.sleep(0.5)
            except Exception as e:
                self.logger().error(f"Error during unsubscription: {e}", exc_info=True)

        # Reset tracking data structures to ensure clean state
        self._subscription_indices = {}
        self._subscribed_pairs = set()
        self._empty_orderbook = {}
        self._last_update_id = {}
        self._symbol_message_counts = {}
        self._receive_count = 0

        # Clear continuous collection tracking
        if hasattr(self, "_continuous_collections"):
            self._continuous_collections = {}

        # Close the socket
        self._close_socket()

        # Ensure the connected flag is reset
        self._is_connected = False

        self.logger().debug("Stopped UDP message listener completely")

    # ---------------------------------------- End of Lifecycle Management ----------------------------------------

    # ---------------------------------------- End of Subscription Management ----------------------------------------

    # ---------------------------------------- Message Listening ----------------------------------------
    async def _listen_for_messages(self):
        """
        Main listening loop for incoming UDP messages
        Processes and dispatches messages to registered callbacks
        """
        self.logger().debug("Starting to listen for messages from QTX UDP feed")
        recv_buffer = bytearray(self._buffer_size)
        self._receive_count = 0
        self._last_stats_time = time.time()

        # Flag to track if listener is being cancelled
        is_cancelling = False

        try:
            while True:
                # We can't use cancel_requested() as it was added in Python 3.11
                # Just rely on CancelledError being raised when the task is cancelled

                try:
                    # Skip processing if socket is None or we're in cancellation
                    if self._udp_socket is None:
                        if is_cancelling:
                            break

                        self.logger().warning("UDP socket is None, attempting to reconnect...")
                        await asyncio.sleep(1)
                        success = await self.connect()
                        if not success:
                            continue

                    # Check if we should log statistics (every 5 minutes instead of every minute)
                    current_time = time.time()
                    if current_time - self._last_stats_time >= 300:  # 5 minutes
                        self._log_statistics()
                        self._last_stats_time = current_time

                    # Receive data from UDP socket (non-blocking way)
                    loop = asyncio.get_event_loop()
                    try:
                        # Use a timeout to make the executor call cancellable
                        async def with_timeout():
                            return await asyncio.wait_for(
                                loop.run_in_executor(None, lambda: self._udp_socket.recvfrom_into(recv_buffer)),
                                timeout=0.1,  # 100ms timeout
                            )

                        # Only attempt to receive if not cancelling and socket exists
                        if not is_cancelling and self._udp_socket is not None:
                            bytes_read, addr = await with_timeout()
                        else:
                            # Skip receiving data if we're shutting down
                            await asyncio.sleep(0.01)
                            continue

                    except asyncio.TimeoutError:
                        # Timed out reading socket - this is normal
                        await asyncio.sleep(0.001)
                        continue
                    except (BlockingIOError, socket.timeout):
                        # No data available
                        await asyncio.sleep(0.001)
                        continue
                    except ConnectionRefusedError:
                        self.logger().error("UDP connection refused. Attempting to reconnect...")
                        await asyncio.sleep(1)
                        await self.connect()
                        continue
                    except (ConnectionError, OSError) as e:
                        if self._udp_socket is None or is_cancelling:
                            # Socket was closed or we're shutting down, break
                            break
                        self.logger().error(f"Socket error: {e}. Attempting to reconnect...")
                        self._close_socket()  # Close current socket and set to None
                        await asyncio.sleep(1)
                        await self.connect()
                        continue

                    if bytes_read == 0:
                        await asyncio.sleep(0.001)
                        continue

                    # Process received data
                    data = recv_buffer[:bytes_read]
                    self._receive_count += 1

                    # Only log message count at major milestones
                    if self._receive_count % 100000 == 0 and self.logger().isEnabledFor(logging.DEBUG):
                        self.logger().debug(f"Received {self._receive_count} total UDP messages")

                    if len(data) >= 40:  # Minimum header size
                        await self._process_message(data)
                    else:
                        self.logger().warning(f"Received message too short for header ({len(data)} bytes)")

                    # Prevent CPU hogging
                    await asyncio.sleep(0.00001)

                except asyncio.CancelledError:
                    is_cancelling = True
                    self.logger().debug("UDP listener task cancellation detected")
                    break
                except Exception as e:
                    self.logger().error(f"Error processing UDP message: {e}", exc_info=True)
                    await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            self.logger().debug("UDP listener task cancelled from outer loop")
            # No need to re-raise, just exit cleanly
        except Exception as e:
            self.logger().error(f"Unexpected error in UDP listener: {e}", exc_info=True)
        finally:
            self.logger().debug("UDP listener task exiting")
            # Cleanup on exit if not already done
            if self._udp_socket is not None:
                self.logger().debug("Cleaning up socket from listener task")
                self._close_socket()

    # ---------------------------------------- End of Message Listening ----------------------------------------

    # ---------------------------------------- Message Processing ----------------------------------------
    async def _process_message(self, data: bytes):
        """
        Process a binary message from UDP feed

        :param data: Binary message data
        """
        try:
            # Only log detailed message info at DEBUG level
            if self.logger().isEnabledFor(logging.DEBUG):
                message_id = f"msg_{int(time.time() * 1000)}_{self._receive_count}"
                self.logger().debug(f"[{message_id}] Processing raw UDP message: {len(data)} bytes")
            else:
                message_id = ""  # Not used in non-debug logs

            # Parse the message header (40 bytes)
            if len(data) < 40:
                self.logger().warning(f"Message too short for proper header: {len(data)} bytes")
                return

            msg_type, index, tx_ms, event_ms, local_ns, sn_id = struct.unpack("<iiqqqq", data[:40])

            # Get the trading pair from the subscription index
            trading_pair = self._subscription_indices.get(index)
            # if trading_pair is None:
            #     # Changed from warning to debug: receiving messages for recently unsubscribed indices is expected
            #     self.logger().debug(f"No trading pair found for subscription index: {index} (likely stale message)")
            #     return

            # Only debug level detailed logging
            if self.logger().isEnabledFor(logging.DEBUG):
                # Debug log the message header values
                msg_type_name = {1: "TICKER_BID", -1: "TICKER_ASK", 2: "DEPTH", 3: "TRADE_BUY", -3: "TRADE_SELL"}.get(
                    msg_type, f"UNKNOWN({msg_type})"
                )
                self.logger().debug(
                    f"[{message_id}] Parsed UDP message header: type={msg_type} ({msg_type_name}), index={index}"
                )

                if trading_pair:
                    self.logger().debug(f"[{message_id}] Found trading pair for index {index}: {trading_pair}")

            # Update statistics for this trading pair
            if trading_pair and trading_pair in self._symbol_message_counts:
                self._symbol_message_counts[trading_pair] += 1
            elif not trading_pair:
                # Try to analyze the message payload even without a trading pair
                self.logger().warning(f"No trading pair found for subscription index: {index}")
                # We can't process this message further without a trading pair
                return

            # Process based on message type
            # Only log very basic info at normal levels, detailed info at debug
            if msg_type in (1, -1):  # Ticker (Bid = 1, Ask = -1)
                parsed_msg = self._parse_ticker_binary(data, trading_pair, msg_type > 0)
                if parsed_msg and self._message_callbacks[msg_type]:
                    await self._message_callbacks[msg_type](parsed_msg)
            elif msg_type == 2:  # Depth (Order Book)
                parsed_msg = self._parse_depth_binary(data, trading_pair)
                if parsed_msg and self._message_callbacks[2]:
                    await self._message_callbacks[2](parsed_msg)
            elif msg_type in (3, -3):  # Trade (Buy = 3, Sell = -3)
                parsed_msg = self._parse_trade_binary(data, trading_pair, msg_type > 0)
                if parsed_msg and self._message_callbacks[msg_type]:
                    await self._message_callbacks[msg_type](parsed_msg)
            else:
                self.logger().warning(f"Unknown message type {msg_type} for {trading_pair}")

        except Exception as e:
            self.logger().error(f"Error processing binary message: {e}", exc_info=True)

    # ---------------------------------------- End of Message Processing ----------------------------------------

    # ---------------------------------------- Binary Message Parsing ----------------------------------------
    def _parse_ticker_binary(self, data: bytes, trading_pair: str, is_bid: bool) -> Optional[Dict[str, Any]]:
        """Parse ticker data from binary format"""
        try:
            # Extract price and size (offset 40 for start of data after header)
            price, size = struct.unpack("<dd", data[40:56])

            # Create ticker message
            timestamp = time.time()
            update_id = int(timestamp * 1000)

            # Ensure update_id is always increasing for this trading pair
            if trading_pair in self._last_update_id:
                update_id = max(update_id, self._last_update_id[trading_pair] + 1)
            self._last_update_id[trading_pair] = update_id

            # For ticker data, create a diff message with single level
            msg = {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "timestamp": timestamp,
                "bids": [[price, size]] if is_bid else [],
                "asks": [] if is_bid else [[price, size]],
                "is_bid": is_bid,
            }

            return msg

        except Exception as e:
            self.logger().error(f"Error parsing ticker: {e}", exc_info=True)
            return None

    def _parse_depth_binary(self, data: bytes, trading_pair: str) -> Optional[Dict[str, Any]]:
        """Parse depth (order book) data from binary format"""
        try:
            # Get ask and bid counts (header is 40 bytes, followed by counts)
            if len(data) < 56:  # 40 bytes header + 16 bytes counts
                self.logger().warning(f"Depth data too short for proper header extension: {len(data)} bytes")
                return None

            asks_len, bids_len = struct.unpack("<qq", data[40:56])
            self.logger().debug(f"Parsing depth message for {trading_pair}: asks_len={asks_len}, bids_len={bids_len}")

            # Check if the data length makes sense given the count of price levels
            expected_data_length = 56 + (asks_len + bids_len) * 16
            if len(data) < expected_data_length:
                self.logger().warning(
                    f"Depth data too short for reported levels. Expected {expected_data_length} bytes, "
                    f"got {len(data)} bytes. asks_len={asks_len}, bids_len={bids_len}"
                )

            # Extract asks and bids
            asks = []
            bids = []
            offset = 56  # Start after the header and counts

            # Parse asks
            for i in range(asks_len):
                if offset + 16 <= len(data):
                    price, size = struct.unpack("<dd", data[offset : offset + 16])
                    asks.append([price, size])
                    offset += 16  # Each price/size pair is 16 bytes

            # Parse bids
            for i in range(bids_len):
                if offset + 16 <= len(data):
                    price, size = struct.unpack("<dd", data[offset : offset + 16])
                    bids.append([price, size])
                    offset += 16  # Each price/size pair is 16 bytes

            self.logger().debug(f"Parsed depth data: {len(asks)} asks, {len(bids)} bids")

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

            # Determine message type based on orderbook status
            is_empty = trading_pair in self._empty_orderbook and self._empty_orderbook[trading_pair]
            message_type = "snapshot" if is_empty else "diff"

            # If this is the first real data, mark the order book as no longer empty
            if is_empty:
                self._empty_orderbook[trading_pair] = False
                self.logger().debug(
                    f"Received first depth data for {trading_pair}: asks: {len(asks)}, bids: {len(bids)}"
                )

            msg = {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "timestamp": timestamp,
                "bids": bids,
                "asks": asks,
                "message_type": message_type,
            }

            # Log sample of the data we're returning (for known indices, we show more data in debug logs)
            if asks and bids:
                # For debug logs of known trading pairs, we'll show up to 5 levels
                sample_asks = asks[: min(5, len(asks))]
                sample_bids = bids[: min(5, len(bids))]
                self.logger().debug(
                    f"Depth message sample data for {trading_pair} (known index):\n"
                    f"  Top 5 asks (of {len(asks)}): {sample_asks}\n"
                    f"  Top 5 bids (of {len(bids)}): {sample_bids}"
                )

            return msg

        except Exception as e:
            self.logger().error(f"Error parsing depth for {trading_pair}: {e}", exc_info=True)
            return None

    def _parse_trade_binary(self, data: bytes, trading_pair: str, is_buy: bool) -> Optional[Dict[str, Any]]:
        """Parse trade data from binary format"""
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
                "timestamp": timestamp,
                "is_buy": is_buy,
            }

            return msg

        except Exception as e:
            self.logger().error(f"Error parsing trade: {e}", exc_info=True)
            return None

    # ---------------------------------------- End of Binary Message Parsing ----------------------------------------

    # ---------------------------------------- Market Data Collection ----------------------------------------
    async def collect_market_data(self, trading_pair: str, duration: float = 5.0) -> Dict[str, Any]:
        """
        Collects market data for a specified trading pair over a given duration.
        Unlike the previous implementation, this method doesn't cache the results by default.
        It simply collects and returns raw market data during the specified period.

        :param trading_pair: Trading pair in Hummingbot format (e.g. BTC-USDT)
        :param duration: Duration in seconds to collect data for (defaults to 5 seconds)
        :return: Dictionary with collected market data: bids, asks, timestamp, etc.
        """
        # Generate a unique operation ID for tracking
        operation_id = f"collect_{int(time.time() * 1000)}"

        # Check if we're already collecting for this trading pair
        if trading_pair in self._snapshot_collecting:
            self.logger().info(f"[{operation_id}] Already collecting data for {trading_pair}, waiting for completion")
            # Wait for the existing collection task to complete
            if trading_pair in self._snapshot_collection_tasks:
                try:
                    task = self._snapshot_collection_tasks[trading_pair]
                    if not task.done():
                        await task
                except Exception as e:
                    self.logger().error(
                        f"[{operation_id}] Error waiting for existing collection task: {e}", exc_info=True
                    )

        # Check if we need to subscribe to this trading pair
        if trading_pair not in self._subscribed_pairs:
            self.logger().debug(f"[{operation_id}] Trading pair {trading_pair} not subscribed, subscribing now")
            success, _ = await self.subscribe_to_trading_pairs([trading_pair])
            if not success:
                self.logger().error(f"[{operation_id}] Failed to subscribe to {trading_pair}, returning empty data")
                return {
                    "trading_pair": trading_pair,
                    "exchange_symbol": trading_pair_utils.convert_to_qtx_trading_pair(
                        trading_pair, self._exchange_name_on_qtx
                    ),
                    "bids": [],
                    "asks": [],
                    "update_id": int(time.time() * 1000),
                    "timestamp": time.time(),
                    "collection_duration": 0.0,
                    "message_count": 0,
                }

        # Mark this trading pair as being collected
        self._snapshot_collecting.add(trading_pair)

        try:
            # Initialize with empty bid/ask dictionaries
            bids_dict = {}  # price -> size
            asks_dict = {}  # price -> size
            collection_start = time.time()
            message_count = 0
            update_id = int(collection_start * 1000)
            exchange_symbol = trading_pair_utils.convert_to_qtx_trading_pair(trading_pair, self._exchange_name_on_qtx)

            # We don't use continuous/unlimited data collection anymore

            self.logger().debug(
                f"[{operation_id}] Starting to collect market data for {trading_pair} "
                f"(QTX symbol: {exchange_symbol}) for {duration} seconds"
            )

            # Create a new temporary depth callback to collect the data
            original_depth_callback = self._message_callbacks.get(2)  # Type 2 is depth
            received_data_event = asyncio.Event()

            async def data_collector(message: Dict[str, Any]):
                """Temporary callback to collect depth data during the collection period"""
                nonlocal message_count, update_id

                # Only process messages for our target trading pair
                if message.get("trading_pair") != trading_pair:
                    return

                # Parse data
                timestamp = message.get("timestamp", time.time())
                msg_update_id = message.get("update_id", int(timestamp * 1000))
                message_count += 1

                # Update the ID to the highest one seen
                update_id = max(update_id, msg_update_id)

                # Process bids - add or update the price level
                for price, size in message.get("bids", []):
                    # Convert to Decimal for precise comparison
                    price_dec = Decimal(str(price))
                    size_dec = Decimal(str(size))

                    if size_dec > 0:
                        bids_dict[price_dec] = size_dec
                    else:
                        # Remove the price level if size is 0
                        bids_dict.pop(price_dec, None)

                # Process asks - add or update the price level
                for price, size in message.get("asks", []):
                    # Convert to Decimal for precise comparison
                    price_dec = Decimal(str(price))
                    size_dec = Decimal(str(size))

                    if size_dec > 0:
                        asks_dict[price_dec] = size_dec
                    else:
                        # Remove the price level if size is 0
                        asks_dict.pop(price_dec, None)

                # Signal that we've received at least one message with data
                received_data_event.set()

                # We no longer use unlimited/continuous collection functionality
                pass

                # Log progress periodically
                if message_count % 5 == 0:
                    elapsed = time.time() - collection_start
                    remaining = max(0, duration - elapsed)
                    self.logger().debug(
                        f"[{operation_id}] Market data collection progress for {trading_pair}: "
                        f"{message_count} messages processed, {elapsed:.1f}s elapsed, {remaining:.1f}s remaining, "
                        f"{len(bids_dict)} bids, {len(asks_dict)} asks"
                    )

            # Register our temporary collector callback
            self.register_message_callback(2, data_collector)

            try:
                # Wait for the specified duration
                collection_task = asyncio.create_task(asyncio.sleep(duration))
                self._snapshot_collection_tasks[trading_pair] = collection_task

                # Wait for either the duration to complete or for the task to be cancelled
                try:
                    await collection_task
                except asyncio.CancelledError:
                    self.logger().debug(f"[{operation_id}] Data collection for {trading_pair} was cancelled")
                    raise

                # Wait for at least one message with data, with a timeout
                try:
                    await asyncio.wait_for(received_data_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self.logger().warning(
                        f"[{operation_id}] No depth data received for {trading_pair} after 5 seconds. "
                        f"Continuing with empty or partial data."
                    )

                # Collection completed - prepare the collected data
                collection_end = time.time()
                collection_duration = collection_end - collection_start

                # Convert dictionaries to sorted lists
                # Sort bids in descending order (highest price first)
                bids = [[float(price), float(size)] for price, size in bids_dict.items()]
                bids.sort(key=lambda x: float(x[0]), reverse=True)

                # Sort asks in ascending order (lowest price first)
                asks = [[float(price), float(size)] for price, size in asks_dict.items()]
                asks.sort(key=lambda x: float(x[0]))

                # Create the final data structure
                collected_data = {
                    "trading_pair": trading_pair,
                    "exchange_symbol": exchange_symbol,
                    "bids": bids,
                    "asks": asks,
                    "update_id": update_id,
                    "timestamp": collection_end,
                    "collection_duration": collection_duration,
                    "message_count": message_count,
                }

                self.logger().debug(
                    f"[{operation_id}] Completed market data collection for {trading_pair}: "
                    f"duration={collection_duration:.2f}s, "
                    f"processed {message_count} messages, "
                    f"collected {len(bids)} bids and {len(asks)} asks"
                )

                # Log sample of the collected data
                if bids and asks:
                    sample_bids = bids[: min(3, len(bids))]
                    sample_asks = asks[: min(3, len(asks))]
                    self.logger().debug(
                        f"[{operation_id}] Sample data for {trading_pair}:\n"
                        f"  Top bids: {sample_bids}\n"
                        f"  Top asks: {sample_asks}"
                    )

                # Clean up and return data
                return collected_data

            finally:
                # Restore the original callback
                if original_depth_callback:
                    self.register_message_callback(2, original_depth_callback)
                else:
                    # If there was no original callback, unregister our temporary one
                    self._message_callbacks[2] = None

        except Exception as e:
            self.logger().error(f"[{operation_id}] Error collecting market data for {trading_pair}: {e}", exc_info=True)
            return {
                "trading_pair": trading_pair,
                "exchange_symbol": trading_pair_utils.convert_to_qtx_trading_pair(
                    trading_pair, self._exchange_name_on_qtx
                ),
                "bids": [],
                "asks": [],
                "update_id": int(time.time() * 1000),
                "timestamp": time.time(),
                "collection_duration": 0.0,
                "message_count": 0,
            }

    # ---------------------------------------- End of Market Data Collection ----------------------------------------

    # ---------------------------------------- Statistics and Logging ----------------------------------------
    def _log_statistics(self):
        """Log statistics about the UDP connection and message processing"""
        if self._connection_start_time > 0:
            elapsed = time.time() - self._connection_start_time
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)

            # Log only total message count, but at DEBUG level instead of INFO
            self.logger().debug(
                f"UDP Stats: Connected for {int(hours)}h {int(minutes)}m {int(seconds)}s, "
                f"received {self._receive_count} total UDP messages"
            )

            # Log detailed per-symbol stats only for high-volume symbols and at DEBUG level
            if self.logger().isEnabledFor(logging.DEBUG):
                # Only log top 5 most active symbols to reduce verbosity
                top_symbols = sorted(
                    [(s, c) for s, c in self._symbol_message_counts.items() if not s.startswith("UNKNOWN_IDX_")],
                    key=lambda x: x[1],
                    reverse=True
                )[:5]
                
                if top_symbols:
                    for symbol, count in top_symbols:
                        if count > 0:  # Only log if there were messages
                            self.logger().debug(f"  {symbol}: {count} messages")

            # Reset counters for next period
            for symbol in self._symbol_message_counts:
                self._symbol_message_counts[symbol] = 0


# ---------------------------------------- End of QtxPerpetualUDPManager ----------------------------------------
