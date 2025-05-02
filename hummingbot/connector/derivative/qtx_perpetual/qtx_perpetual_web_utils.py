#!/usr/bin/env python

import socket
from typing import Any, Dict, Optional

from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest
from hummingbot.core.web_assistant.rest_assistant import RESTAssistant
from hummingbot.core.web_assistant.rest_pre_processors import RESTPreProcessorBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


def build_api_factory(throttler: AsyncThrottler, auth: Optional[AuthBase] = None, time_synchronizer: Optional[Any] = None, domain: str = CONSTANTS.DOMAIN) -> WebAssistantsFactory:
    """
    Builds the web assistant factory for REST requests.
    :param throttler: The throttler to use for rate limiting
    :param auth: Authentication class
    :param time_synchronizer: Time synchronizer for timestamp validation (not used in this implementation)
    :param domain: The domain to connect to
    :return: A web assistant factory
    """
    # We don't use time_synchronizer as a pre-processor to avoid errors
    # This matches the approach used in the QTX spot connector
    
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        auth=auth,
        rest_pre_processors=None
    )
    return api_factory


def create_rest_assistant(api_factory: Optional[WebAssistantsFactory] = None) -> RESTAssistant:
    """
    Creates a REST assistant using the given API factory
    :param api_factory: The API factory to use for creating the assistant
    :return: A REST assistant
    """
    rest_assistant = None
    if api_factory is not None:
        rest_assistant = api_factory.get_rest_assistant()
    return rest_assistant


async def api_request(path: str,
                      api_factory: Optional[WebAssistantsFactory] = None,
                      rest_method: RESTMethod = RESTMethod.GET,
                      params: Optional[Dict[str, Any]] = None,
                      data: Optional[Dict[str, Any]] = None,
                      is_auth_required: bool = False) -> Dict[str, Any]:
    """
    Sends an API request to the exchange
    :param path: The path of the API endpoint
    :param api_factory: The API factory to use for the request
    :param rest_method: The REST method to use
    :param params: The parameters to send in the request
    :param data: The data to send in the request
    :param is_auth_required: Whether authentication is required for this endpoint
    :return: The response from the API
    """
    rest_assistant = await create_rest_assistant(api_factory)
    if rest_assistant is None:
        raise ValueError("REST assistant is not available.")
    
    url = f"{CONSTANTS.BASE_PATH}{path}"
    
    request = RESTRequest(
        method=rest_method,
        url=url,
        params=params,
        data=data,
        is_auth_required=is_auth_required
    )
    
    response = await rest_assistant.call(request)
    
    if response.status != 200:
        error_response = await response.json()
        raise IOError(f"Error executing request {path}. HTTP status is {response.status}. "
                      f"Response: {error_response}")
    
    return await response.json()


async def get_current_server_time(throttler: AsyncThrottler, domain: str = CONSTANTS.DOMAIN) -> float:
    """
    Gets the current server time from the exchange
    :param throttler: The throttler to use for rate limiting
    :param domain: The domain to connect to
    :return: The server time in seconds
    """
    api_factory = build_api_factory(throttler=throttler, domain=domain)
    response = await api_request(
        path=CONSTANTS.PING_PATH_URL,
        api_factory=api_factory
    )
    
    return float(response["serverTime"]) / 1000


def get_udp_socket(host: str, port: int) -> socket.socket:
    """
    Creates a UDP socket for market data connection
    :param host: The host to connect to
    :param port: The port to connect to
    :return: A UDP socket
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(CONSTANTS.SOCKET_TIMEOUT)
    sock.connect((host, port))
    return sock


# REST API Utilities

def public_rest_url(path_url: str, domain: str = CONSTANTS.DOMAIN) -> str:
    """
    Creates a full URL for public REST endpoints
    
    :param path_url: The endpoint path to append to the base URL
    :param domain: The domain of the QTX API server
    :return: The full URL for the endpoint
    """
    # Placeholder implementation - would be replaced with actual API URL in production
    return f"http://{domain}/api{path_url}"


def private_rest_url(path_url: str, domain: str = CONSTANTS.DOMAIN) -> str:
    """
    Creates a full URL for private (authenticated) REST endpoints
    
    :param path_url: The endpoint path to append to the base URL
    :param domain: The domain of the QTX API server
    :return: The full URL for the endpoint
    """
    # Private REST endpoints use the same URL format as public ones for QTX
    return public_rest_url(path_url=path_url, domain=domain)


def websocket_url(endpoint: str = "", domain: str = CONSTANTS.DOMAIN) -> str:
    """
    Creates a WebSocket URL for connecting to the exchange
    
    :param endpoint: The specific endpoint to use
    :param domain: The domain of the QTX WebSocket server
    :return: The WebSocket URL
    """
    # Placeholder implementation - would be replaced with actual WebSocket URL in production
    return f"ws://{domain}/ws{endpoint}"
