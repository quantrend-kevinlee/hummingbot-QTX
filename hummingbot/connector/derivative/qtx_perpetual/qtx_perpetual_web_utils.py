#!/usr/bin/env python

import socket

from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_constants as BINANCE_CONSTANTS
from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS


def get_udp_socket(host: str = CONSTANTS.DEFAULT_UDP_HOST, port: int = CONSTANTS.DEFAULT_UDP_PORT) -> socket.socket:
    """
    Create and configure a UDP socket for market data connection

    :param host: UDP host to connect to
    :param port: UDP port to connect to
    :return: Configured socket
    """
    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Set socket options
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, CONSTANTS.DEFAULT_UDP_BUFFER_SIZE)

    # Don't bind to specific port, let the OS choose
    sock.bind(("0.0.0.0", 0))

    # Return configured socket
    return sock


def build_api_url(path: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Build full URL for Binance API endpoints

    :param path: API endpoint path
    :param domain: Domain to use
    :return: Full URL for the endpoint
    """
    if domain == BINANCE_CONSTANTS.TESTNET_DOMAIN:
        api_url = BINANCE_CONSTANTS.TESTNET_BASE_URL
    else:
        api_url = BINANCE_CONSTANTS.PERPETUAL_BASE_URL

    url = f"{api_url}{path}"
    return url


def build_ws_url(stream_path: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Build WebSocket URL for Binance streams

    :param stream_path: WebSocket stream path
    :param domain: Domain to use
    :return: Full WebSocket URL
    """
    if domain == BINANCE_CONSTANTS.TESTNET_DOMAIN:
        ws_url = BINANCE_CONSTANTS.TESTNET_WS_URL
    else:
        ws_url = BINANCE_CONSTANTS.PERPETUAL_WS_URL

    url = f"{ws_url}{stream_path}"
    return url


def public_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Create a REST API URL for public endpoints

    :param path_url: The specific endpoint path
    :param domain: The domain to use
    :return: The full URL
    """
    return build_api_url(path_url, domain)


def private_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Create a REST API URL for private/authenticated endpoints

    :param path_url: The specific endpoint path
    :param domain: The domain to use
    :return: The full URL
    """
    return build_api_url(path_url, domain)
