#!/usr/bin/env python

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from hummingbot.connector.derivative.qtx_perpetual import (
    qtx_perpetual_constants as CONSTANTS,
    qtx_perpetual_web_utils as web_utils,
)
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.logger import HummingbotLogger


class QtxPerpetualUserStreamDataSource(UserStreamTrackerDataSource):
    """
    UserStreamTrackerDataSource implementation for QTX Perpetual.
    This class is responsible for maintaining the connection to the user stream WebSocket.
    """
    _logger: Optional[HummingbotLogger] = None

    def __init__(
        self,
        auth: AuthBase,
        connector: Any,
        api_factory: WebAssistantsFactory,
        domain: str = CONSTANTS.DOMAIN,
    ):
        super().__init__()
        self._auth = auth
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._ws_assistant = None
        self._listen_key = None
        self._last_listen_key_ping_ts = 0

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    async def _get_listen_key(self) -> str:
        """
        Gets a listen key from the exchange
        :return: The listen key
        """
        try:
            response = await web_utils.api_request(
                path=CONSTANTS.BINANCE_USER_STREAM_ENDPOINT,
                api_factory=self._api_factory,
                rest_method="POST",
                is_auth_required=True
            )
            return response["listenKey"]
        except Exception as e:
            self.logger().error(f"Error getting listen key: {e}", exc_info=True)
            raise

    async def _ping_listen_key(self) -> bool:
        """
        Pings the listen key to keep it alive
        :return: True if successful, False otherwise
        """
        try:
            await web_utils.api_request(
                path=CONSTANTS.BINANCE_USER_STREAM_ENDPOINT,
                api_factory=self._api_factory,
                rest_method="PUT",
                params={"listenKey": self._listen_key},
                is_auth_required=True
            )
            return True
        except Exception as e:
            self.logger().error(f"Error pinging listen key: {e}", exc_info=True)
            return False

    async def _manage_listen_key_task_loop(self):
        """
        Periodically pings the listen key to keep it alive
        """
        while True:
            try:
                now = int(time.time())
                if self._listen_key is None:
                    self._listen_key = await self._get_listen_key()
                    self._last_listen_key_ping_ts = now
                    self.logger().info(f"Obtained listen key: {self._listen_key}")
                elif now - self._last_listen_key_ping_ts >= 1800:  # Ping every 30 minutes
                    success = await self._ping_listen_key()
                    if success:
                        self._last_listen_key_ping_ts = now
                        self.logger().info(f"Successfully pinged listen key: {self._listen_key}")
                    else:
                        self._listen_key = None
                        self.logger().info("Listen key ping failed, will request a new one")
                
                # Sleep for 10 minutes
                await asyncio.sleep(600)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error managing listen key: {e}", exc_info=True)
                self._listen_key = None
                await asyncio.sleep(5)

    async def _create_websocket_connection(self) -> None:
        """
        Creates a WebSocket connection to the user stream
        """
        self._ws_assistant = await self._api_factory.get_ws_assistant()
        
        if self._listen_key is None:
            self._listen_key = await self._get_listen_key()
            self._last_listen_key_ping_ts = int(time.time())
        
        ws_url = f"{CONSTANTS.PERPETUAL_WS_URL}{CONSTANTS.PRIVATE_WS_ENDPOINT}?listenKey={self._listen_key}"
        await self._ws_assistant.connect(ws_url=ws_url, message_timeout=CONSTANTS.HEARTBEAT_TIME_INTERVAL)
        self.logger().info("Connected to user stream WebSocket")

    async def _process_websocket_messages(self, websocket_assistant, queue: asyncio.Queue):
        """
        Processes WebSocket messages and puts them in the queue
        """
        while True:
            try:
                message = await websocket_assistant.receive()
                data = message.data
                
                # Process different event types
                event_type = data.get("e")
                
                if event_type == "ORDER_TRADE_UPDATE":
                    # Order update
                    await queue.put(data)
                elif event_type == "ACCOUNT_UPDATE":
                    # Account update (balances, positions)
                    await queue.put(data)
                elif event_type == "MARGIN_CALL":
                    # Margin call
                    await queue.put(data)
                elif event_type == "ACCOUNT_CONFIG_UPDATE":
                    # Account config update (leverage, position mode)
                    await queue.put(data)
                else:
                    # Unknown event type
                    self.logger().debug(f"Received unknown event type: {event_type}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Error processing WebSocket message: {e}", exc_info=True)
                # Close the connection and reconnect
                await self._ws_assistant.disconnect()
                await self._create_websocket_connection()

    async def listen_for_user_stream(self, output: asyncio.Queue):
        """
        Connects to the user stream WebSocket and listens for messages
        """
        # Start listen key management task
        listen_key_task = None
        
        try:
            listen_key_task = asyncio.create_task(self._manage_listen_key_task_loop())
            
            while True:
                try:
                    # Create WebSocket connection
                    await self._create_websocket_connection()
                    
                    # Process messages
                    await self._process_websocket_messages(self._ws_assistant, output)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.logger().error(f"Error listening for user stream: {e}", exc_info=True)
                    # Close the connection and reconnect
                    if self._ws_assistant:
                        await self._ws_assistant.disconnect()
                        self._ws_assistant = None
                    await asyncio.sleep(5)
        finally:
            # Clean up
            if listen_key_task is not None:
                listen_key_task.cancel()
            
            if self._ws_assistant:
                await self._ws_assistant.disconnect()
                self._ws_assistant = None
