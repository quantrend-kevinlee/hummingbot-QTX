#!/usr/bin/env python

from typing import Any

from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_constants as BINANCE_CONSTANTS
from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS


async def get_current_server_time(throttler: Any = None, domain: str = None) -> float:
    """
    Get the current server time by delegating directly to Binance's implementation.

    Since QTX uses Binance for order execution, we need to synchronize with Binance's server time.
    This is crucial for properly signed requests to Binance's API.

    :param throttler: The throttler to use (passed to Binance's implementation)
    :param domain: The domain to use (passed to Binance's implementation)
    :return: Current server time in seconds
    :raises: ValueError if the server time cannot be retrieved
    """
    # Import here to avoid circular imports
    from hummingbot.connector.derivative.binance_perpetual.binance_perpetual_web_utils import (
        get_current_server_time as binance_get_current_server_time,
    )

    try:
        # Directly delegate to Binance's implementation
        return await binance_get_current_server_time(
            throttler=throttler,  # Pass the throttler to Binance's implementation
            domain=domain or BINANCE_CONSTANTS.DOMAIN,
        )
    except Exception as e:
        raise ValueError(f"Failed to get Binance server time, using local time as fallback: {str(e)}") from e


def build_api_url(path: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Build full URL for Binance API endpoints by delegating to Binance implementation

    This function ensures consistency with Binance's URL structure and domain handling.

    :param path: API endpoint path
    :param domain: Domain to use
    :return: Full URL for the endpoint
    """
    # Import the necessary function from binance_perpetual_web_utils if needed
    from hummingbot.connector.derivative.binance_perpetual.binance_perpetual_web_utils import (
        build_api_url as binance_build_api_url,
    )

    # Delegate to Binance's implementation
    return binance_build_api_url(path, domain)


def build_ws_url(stream_path: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Build WebSocket URL for Binance streams by delegating to Binance implementation

    This function ensures consistency with Binance's WebSocket URL structure and domain handling.

    :param stream_path: WebSocket stream path
    :param domain: Domain to use
    :return: Full WebSocket URL
    """
    # Import the necessary function from binance_perpetual_web_utils if needed
    from hummingbot.connector.derivative.binance_perpetual.binance_perpetual_web_utils import (
        build_ws_url as binance_build_ws_url,
    )

    # Delegate to Binance's implementation
    return binance_build_ws_url(stream_path, domain)


def public_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Create a REST API URL for public endpoints by delegating to Binance's implementation

    This delegation ensures that all REST API requests are properly routed to Binance's API,
    which is critical for the hybrid connector approach (QTX market data + Binance trading).

    :param path_url: The specific endpoint path
    :param domain: The domain to use
    :return: The full URL from Binance's implementation
    """
    # Import here to avoid circular imports
    from hummingbot.connector.derivative.binance_perpetual.binance_perpetual_web_utils import (
        public_rest_url as binance_public_rest_url,
    )

    # Delegate directly to Binance's implementation
    return binance_public_rest_url(path_url, domain)


def private_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Create a REST API URL for private/authenticated endpoints by delegating to Binance's implementation

    This delegation ensures that all REST API requests are properly routed to Binance's API,
    which is critical for the hybrid connector approach (QTX market data + Binance trading).

    :param path_url: The specific endpoint path
    :param domain: The domain to use
    :return: The full URL from Binance's implementation
    """
    # Import here to avoid circular imports
    from hummingbot.connector.derivative.binance_perpetual.binance_perpetual_web_utils import (
        private_rest_url as binance_private_rest_url,
    )

    # Delegate directly to Binance's implementation
    return binance_private_rest_url(path_url, domain)
