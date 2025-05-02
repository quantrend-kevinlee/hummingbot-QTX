#!/usr/bin/env python

import hashlib
import hmac
from typing import Any

from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


class QtxPerpetualBinanceAuth(AuthBase):
    """
    Auth class handling Binance authentication for the hybrid connector.
    QTX market data doesn't require authentication since it uses an open UDP port.
    """

    def __init__(
        self,
        binance_api_key: str = None,
        binance_api_secret: str = None,
        time_provider: Any = None,
    ):
        self.binance_api_key = binance_api_key
        self.binance_api_secret = binance_api_secret
        self._time_provider = time_provider

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Authenticates REST requests for Binance API
        """
        # Check if this is a Binance request
        # Handle both URL objects and string URLs
        url_str = str(request.url)
        if "binance" in url_str or "fapi.binance.com" in url_str:
            return await self._binance_rest_authenticate(request)
        return request

    async def _binance_rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Authenticates Binance REST requests
        """
        if self.binance_api_key is None or self.binance_api_secret is None:
            return request

        # Add API key to headers
        request.headers = {
            **request.headers,
            "X-MBX-APIKEY": self.binance_api_key,
        }

        # For Binance, we need to add timestamp and signature to params
        if request.params is None:
            request.params = {}

        # Add timestamp
        timestamp = str(int(self._time_provider.time() * 1000))
        request.params["timestamp"] = timestamp

        # Build signature
        query_string = "&".join([f"{key}={value}" for key, value in sorted(request.params.items())])
        signature = hmac.new(
            self.binance_api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # Add signature to params
        request.params["signature"] = signature

        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """
        Authenticates WebSocket requests, needed only for Binance
        """
        # Check if this is a Binance WebSocket (URL contains binance)
        if request.url and "binance" in request.url:
            # Binance futures WebSocket authentication is handled via listen key in the URL
            # No additional authentication needed in the payload
            pass
        return request
