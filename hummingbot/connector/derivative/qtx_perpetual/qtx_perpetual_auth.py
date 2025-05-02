#!/usr/bin/env python

import hashlib
import hmac
import time
from typing import Any, Dict

from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


class QtxPerpetualAuth(AuthBase):
    """
    Auth class required by QTX Perpetual API
    """

    def __init__(self, api_key: str, secret_key: str, time_provider: Any):
        self._api_key: str = api_key
        self._secret_key: str = secret_key
        self._time_provider = time_provider

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Adds the server time and the signature to the request, required for authenticated interactions
        :param request: the request to be authenticated
        """
        if self._api_key is None or self._secret_key is None:
            return request

        # Timestamp for requests
        timestamp = int(self._time_provider.time() * 1000)
        request.headers = {
            **request.headers,
            CONSTANTS.API_KEY_HEADER: self._api_key,
        }

        # Build signature
        signature_payload = f"{timestamp}{request.method}{request.url.path}"
        if request.params:
            signature_payload += "".join([f"{key}={value}" for key, value in sorted(request.params.items())])
        if request.data:
            signature_payload += request.data

        # Create signature using HMAC-SHA256
        signature = hmac.new(
            self._secret_key.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        # Add timestamp and signature to headers
        request.headers["X-QTX-TIMESTAMP"] = str(timestamp)
        request.headers["X-QTX-SIGNATURE"] = signature

        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """
        This method is intended to authenticate websocket connections
        """
        if self._api_key is None or self._secret_key is None:
            return request

        # Similar to REST authentication but for WebSocket
        timestamp = int(self._time_provider.time() * 1000)
        
        # Create auth params
        auth_params = {
            "api_key": self._api_key,
            "timestamp": timestamp,
        }
        
        # Build signature
        signature_payload = f"{timestamp}{self._api_key}"
        signature = hmac.new(
            self._secret_key.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        auth_params["signature"] = signature
        
        # Add auth params to request
        if request.payload is None:
            request.payload = {}
        request.payload["auth"] = auth_params
        
        return request
