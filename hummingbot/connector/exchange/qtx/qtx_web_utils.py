"""
QTX utilities for UDP communication based on the reference C SDK.
This module provides functions for UDP-based market data access.
"""

import socket
import struct
import time
from typing import Any, Dict, Optional, Tuple, List, Union, AsyncIterable
import aiohttp  # Import aiohttp as a module instead

import hummingbot.connector.exchange.qtx.qtx_constants as CONSTANTS
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSJSONRequest
from hummingbot.core.web_assistant.rest_assistant import RESTAssistant
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.connector.time_synchronizer import TimeSynchronizer


def create_udp_socket(host: str = None, port: int = None) -> socket.socket:
    """
    Creates a non-blocking UDP socket for communication with QTX data source.

    :param host: The UDP server host address
    :param port: The UDP server port
    :return: The created socket
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    # Set up the socket with the provided host and port
    if host is not None and port is not None:
        sock.connect((host, port))

    return sock


def close_udp_socket(sock: socket.socket) -> None:
    """
    Safely closes a UDP socket

    :param sock: Socket to close
    """
    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass


def subscribe_symbol(sock: socket.socket, symbol: str) -> int:
    """
    Subscribes to a trading pair using the UDP protocol
    (Follows the C SDK pattern that sends just the symbol name)

    :param sock: UDP socket to send the message through
    :param symbol: Trading pair symbol to subscribe to (e.g., "binance-futures:btcusdt")
    :return: Symbol index if successful, -1 otherwise
    """
    try:
        # Send the raw symbol name (no JSON, matching C SDK)
        server_addr = (CONSTANTS.DEFAULT_UDP_HOST, CONSTANTS.DEFAULT_UDP_PORT)
        sock.sendto(symbol.encode(), server_addr)

        # Set socket to blocking temporarily for the response
        sock.setblocking(True)
        sock.settimeout(5.0)  # 5 second timeout

        # Receive response
        try:
            data, addr = sock.recvfrom(CONSTANTS.DEFAULT_UDP_BUFFER_SIZE)
            # Response should be the symbol index
            if data:
                index = int(data.decode().strip())
                return index
        except socket.timeout:
            return -1
        finally:
            # Reset to non-blocking
            sock.setblocking(False)
    except Exception:
        return -1


def unsubscribe_symbol(sock: socket.socket, symbol: str) -> bool:
    """
    Unsubscribes from a trading pair
    (Follows the C SDK pattern that sends "-" + symbol name)

    :param sock: UDP socket to send the message through
    :param symbol: Trading pair symbol to unsubscribe from
    :return: True if successful, False otherwise
    """
    try:
        # Format is "-" + symbol (matching C SDK)
        unsubscribe_msg = f"-{symbol}"
        server_addr = (CONSTANTS.DEFAULT_UDP_HOST, CONSTANTS.DEFAULT_UDP_PORT)
        sock.sendto(unsubscribe_msg.encode(), server_addr)

        # Set socket to blocking temporarily for the response
        sock.setblocking(True)
        sock.settimeout(5.0)  # 5 second timeout

        # Receive response
        try:
            data, addr = sock.recvfrom(CONSTANTS.DEFAULT_UDP_BUFFER_SIZE)
            return data is not None
        except socket.timeout:
            return False
        finally:
            # Reset to non-blocking
            sock.setblocking(False)
    except Exception:
        return False


def parse_market_message(raw_data: bytes) -> Dict[str, Any]:
    """
    Parses binary market data messages based on the C SDK message format

    :param raw_data: Raw binary data received from UDP
    :return: Parsed message as a dictionary
    """
    # Based on the C SDK Msg struct
    if len(raw_data) < 48:  # Minimum size for the basic message
        return {}

    # Parse the message header (matching C SDK struct)
    # msg_type (int), index (int), tx_ms (long), event_ms (long),
    # local_ns (long), sn_id (long), price(double), size(double)
    header_format = "iilllldd"
    msg_type, index, tx_ms, event_ms, local_ns, sn_id, price, size = struct.unpack(header_format, raw_data[:48])

    # Initialize basic message
    message = {
        "msg_type": msg_type,
        "index": index,
        "tx_ms": tx_ms,
        "event_ms": event_ms,
        "local_ns": local_ns,
        "sn_id": sn_id,
        "price": price,
        "size": size,
    }

    # For L2 order book data (msg_type == 2 or msg_type == -2)
    if abs(msg_type) == 2 and len(raw_data) > 48:
        # This is a depth message - parse additional fields
        # In the C SDK, for L2 data, we'd need to handle the levels list
        # This is a simplified implementation - in practice you'd need to
        # parse the exact binary format of your system
        try:
            # Get the number of levels from the message
            # This assumes a format where asks_len and bids_len are at specific offsets
            # You'll need to adjust this based on your actual message format
            offset = 48  # After the basic message fields
            asks_len, bids_len = struct.unpack("ii", raw_data[offset : offset + 8])
            offset += 8

            # Parse price and size for each level
            asks = []
            bids = []

            # Parse asks
            for i in range(asks_len):
                price, size = struct.unpack("dd", raw_data[offset : offset + 16])
                asks.append([price, size])
                offset += 16

            # Parse bids
            for i in range(bids_len):
                price, size = struct.unpack("dd", raw_data[offset : offset + 16])
                bids.append([price, size])
                offset += 16

            message["asks"] = asks
            message["bids"] = bids
        except Exception:
            # In case of parsing error, return what we have
            pass

    return message


def receive_udp_message(
    sock: socket.socket, buffer_size: int = CONSTANTS.DEFAULT_UDP_BUFFER_SIZE
) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple]]:
    """
    Non-blocking receive of UDP message

    :param sock: UDP socket to receive from
    :param buffer_size: Size of the buffer for receiving data
    :return: Tuple of (parsed_message, sender_address) or (None, None) if no data
    """
    try:
        # Non-blocking receive
        data, addr = sock.recvfrom(buffer_size)

        if data:
            # Parse the message based on the C SDK format
            message = parse_market_message(data)
            return message, addr

    except BlockingIOError:
        # No data available right now
        pass
    except Exception:
        # Other errors
        pass

    return None, None


def get_current_timestamp_ns() -> int:
    """
    Returns the current timestamp in nanoseconds.
    Mimics the C SDK function for consistency.

    :return: Current timestamp in nanoseconds
    """
    return int(time.time() * 1_000_000_000)


# REST API Utilities

def public_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_UDP_HOST) -> str:
    """
    Creates a full URL for public REST endpoints
    
    :param path_url: The endpoint path to append to the base URL
    :param domain: The domain of the QTX API server
    :return: The full URL for the endpoint
    """
    # Placeholder implementation - would be replaced with actual API URL in production
    return f"http://{domain}:{CONSTANTS.DEFAULT_UDP_PORT}{CONSTANTS.BASE_PATH}{path_url}"


def private_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_UDP_HOST) -> str:
    """
    Creates a full URL for private (authenticated) REST endpoints
    
    :param path_url: The endpoint path to append to the base URL
    :param domain: The domain of the QTX API server
    :return: The full URL for the endpoint
    """
    # For now, use the same URL format as public endpoints
    return public_rest_url(path_url, domain)


def websocket_url(endpoint: str = '', domain: str = CONSTANTS.DEFAULT_UDP_HOST) -> str:
    """
    Creates a WebSocket URL for connecting to the exchange
    
    :param endpoint: The specific endpoint to use
    :param domain: The domain of the QTX WebSocket server
    :return: The WebSocket URL
    """
    # Placeholder implementation - would be replaced with actual WS URL in production
    return f"ws://{domain}:{CONSTANTS.DEFAULT_UDP_PORT}/ws{endpoint}"


def build_api_factory(
    throttler: Optional[AsyncThrottler] = None,
    time_synchronizer: Optional[TimeSynchronizer] = None,
    domain: str = CONSTANTS.DEFAULT_UDP_HOST,
    auth: Optional[AuthBase] = None,
) -> WebAssistantsFactory:
    """
    Builds the web assistant factory to be used by the connector
    
    :param throttler: The throttler to use for API rate limits
    :param time_synchronizer: The time synchronizer to use for clock synchronization
    :param domain: The domain of the QTX server
    :param auth: The authentication instance
    :return: A WebAssistantsFactory instance
    """
    throttler = throttler or AsyncThrottler(CONSTANTS.RATE_LIMITS)
    time_synchronizer = time_synchronizer or TimeSynchronizer()
    
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        auth=auth,
        rest_pre_processors=[],
        ws_pre_processors=[],
        rest_post_processors=[],
        ws_post_processors=[],
    )
    
    return api_factory


async def api_request(
    path: str,
    api_factory: Optional[WebAssistantsFactory] = None,
    throttler: Optional[AsyncThrottler] = None,
    time_synchronizer: Optional[TimeSynchronizer] = None,
    domain: str = CONSTANTS.DEFAULT_UDP_HOST,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    method: RESTMethod = RESTMethod.GET,
    is_auth_required: bool = False,
    return_err: bool = False,
    limit_id: Optional[str] = None,
    timeout: float = CONSTANTS.SOCKET_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Sends an API request to the QTX exchange
    
    :param path: The path of the API request
    :param api_factory: The WebAssistantsFactory to use
    :param throttler: The AsyncThrottler to use for rate limits
    :param time_synchronizer: The TimeSynchronizer to use for clock synchronization
    :param domain: The domain of the QTX server
    :param params: The query parameters for the request
    :param data: The data for the request body
    :param method: The HTTP method to use
    :param is_auth_required: Whether authentication is required for this request
    :param return_err: Whether to return the error response instead of raising an exception
    :param limit_id: The ID to use for rate limiting
    :param timeout: The timeout for the request
    :param headers: The headers to include in the request
    :return: The API response
    """
    # Placeholder for REST API implementation - for now, return simulated data
    # In a complete implementation, this would send actual REST API requests
    
    # Mock successful authentication response
    if path == CONSTANTS.PING_PATH_URL:
        return {"status": "ok"}
    elif path == CONSTANTS.EXCHANGE_INFO_PATH_URL:
        return {
            "timezone": "UTC",
            "serverTime": int(time.time() * 1000),
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "baseAssetPrecision": 8,
                    "quotePrecision": 8,
                    "orderTypes": ["LIMIT", "MARKET"],
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "minPrice": "0.00000100",
                            "maxPrice": "100000.00000000",
                            "tickSize": "0.00000100"
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.00100000",
                            "maxQty": "100000.00000000",
                            "stepSize": "0.00100000"
                        },
                        {
                            "filterType": "MIN_NOTIONAL",
                            "minNotional": "0.00100000"
                        }
                    ]
                }
            ]
        }
    
    # Mock response for other requests
    return {"success": True, "message": "Simulated API response"}
