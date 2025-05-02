#!/usr/bin/env python

import asyncio
import logging
import time
from typing import Optional

from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_constants as BINANCE_CONSTANTS
from hummingbot.connector.derivative.qtx_perpetual import (
    qtx_perpetual_constants as CONSTANTS,
    qtx_perpetual_web_utils as web_utils,
)
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger


class QtxPerpetualUserStreamDataSource(UserStreamTrackerDataSource):
    """
    This class is the implementation of UserStreamTrackerDataSource that uses Binance's WebSocket user stream.
    Since QTX doesn't have authentication and we're delegating trading to Binance,
    this class acts as a proxy to Binance's user stream.
    """
    _logger: Optional[HummingbotLogger] = None

    def __init__(
        self,
        auth,
        connector,
        api_factory: WebAssistantsFactory,
        domain: str = CONSTANTS.DEFAULT_DOMAIN
    ):
        super().__init__()
        self._auth = auth
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._ws_assistant: Optional[WSAssistant] = None
        self._listen_key: Optional[str] = None
        self._listen_key_initialized_event = asyncio.Event()
        self._last_listen_key_ping_ts = 0

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    async def _get_listen_key(self):
        """
        Get a listen key from Binance for the user stream
        """
        try:
            if not self._connector._trading_required:
                # No trading, no listen key needed
                self._listen_key = "dummy_listen_key"
                self._listen_key_initialized_event.set()
                return

            # Get listen key from Binance connector
            if hasattr(self._connector, "_binance_connector"):
                # Delegate to binance connector
                listen_key = await self._connector._binance_connector._api_request(
                    method="POST",
                    path_url=BINANCE_CONSTANTS.BINANCE_USER_STREAM_ENDPOINT,
                    is_auth_required=True
                )
                self._listen_key = listen_key["listenKey"]
                self._listen_key_initialized_event.set()
                self.logger().info(f"Obtained Binance listen key: {self._listen_key}")
            else:
                self.logger().error("Binance connector not available for user stream")
                self._listen_key = "dummy_listen_key"
                self._listen_key_initialized_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().error(f"Error getting Binance listen key: {e}", exc_info=True)
            self._listen_key = "dummy_listen_key"
            self._listen_key_initialized_event.set()

    async def _ping_listen_key(self) -> bool:
        """
        Ping the listen key to keep it alive
        """
        try:
            if not self._connector._trading_required or not self._listen_key:
                return True

            if hasattr(self._connector, "_binance_connector"):
                # Delegate to binance connector
                await self._connector._binance_connector._api_request(
                    method="PUT",
                    path_url=BINANCE_CONSTANTS.BINANCE_USER_STREAM_ENDPOINT,
                    is_auth_required=True
                )
                self._last_listen_key_ping_ts = int(time.time())
                return True
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().error(f"Error pinging Binance listen key: {e}", exc_info=True)
            return False

    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        Get a websocket assistant connected to the Binance user stream
        """
        if self._ws_assistant is not None:
            return self._ws_assistant

        # Wait for listen key to be initialized
        await self._listen_key_initialized_event.wait()

        # Create websocket connection to Binance
        ws_url = web_utils.build_ws_url(f"/{self._listen_key}", self._domain)
        self._ws_assistant = await self._api_factory.get_ws_assistant()
        await self._ws_assistant.connect(ws_url)
        return self._ws_assistant

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        """
        Subscribe to user stream channels
        No subscription needed for Binance user stream as it's already done through the listen key
        """
        pass

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant, queue: asyncio.Queue):
        """
        Process messages from the websocket and put them into the queue
        """
        while True:
            try:
                # Ping the listen key every ~30 minutes
                now = int(time.time())
                if self._last_listen_key_ping_ts > 0 and now - self._last_listen_key_ping_ts > 1800:
                    ping_success = await self._ping_listen_key()
                    if not ping_success:
                        # If ping fails, reconnect
                        raise ConnectionError("Binance listen key ping failed")

                message = await websocket_assistant.receive()
                data = message.data
                queue.put_nowait(data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error processing websocket message: {e}", exc_info=True)
                # Close the connection to force a reconnect
                await self._ws_assistant.disconnect()
                self._ws_assistant = None
                # Re-initialize listen key
                await self._get_listen_key()
                raise

    async def listen_for_user_stream(self, output: asyncio.Queue):
        """
        Listen for user stream messages
        """
        while True:
            try:
                # Initialize listen key if needed
                if self._listen_key is None:
                    await self._get_listen_key()

                ws = await self._connected_websocket_assistant()
                await self._subscribe_channels(ws)
                await self._process_websocket_messages(ws, output)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error listening for user stream: {e}", exc_info=True)
                # Sleep to avoid tight loop on error
                await asyncio.sleep(5)
