import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_derivative import QtxPerpetualDerivative


class QtxPerpetualUserStreamDataSource(UserStreamTrackerDataSource):
    """
    A minimal pass-through: it does NOT create its own listen key or websocket.
    Instead, it 'listens' for user events from the binance_connector._user_stream_tracker's queue
    and forwards them to QTX's output queue.

    The code below adds:
     - _connected_websocket_assistant() / _subscribe_channels() stubs so base-class logic can still be invoked
     - A last_recv_time property that references the binance sub-connector’s last_recv_time if available
     - A re-try loop in listen_for_user_stream
    """

    _logger: Optional[HummingbotLogger] = None

    def __init__(self, qtx_connector: "QtxPerpetualDerivative"):
        super().__init__()
        self._qtx_connector = qtx_connector
        self._local_last_recv_time = 0.0

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = super().logger()
        return cls._logger

    @property
    def last_recv_time(self) -> float:
        """
        We attempt to read the binance sub-connector's last_recv_time if it exists, otherwise fall
        back to a local timestamp that we set whenever we get data from the sub-connector's queue.
        """
        binance_connector = self._qtx_connector.binance_connector
        if binance_connector is not None:
            # binance's user stream data source
            binance_ds = binance_connector._user_stream_tracker.data_source
            return binance_ds.last_recv_time if binance_ds is not None else self._local_last_recv_time
        else:
            self.logger().warning("No binance connector, cannot forward last_recv_time from binance.")
            return self._local_last_recv_time

    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        Normally, the base class calls this to create & connect a WS for user stream.
        In QTX, we have no direct user-stream WebSocket. We simply pass-through from binance’s queue.

        If you want to let the base-class logic run (listen_for_user_stream tries to connect?), you can simply
        return None or raise an exception. For demonstration, let's just raise to clarify we do not have a WS.
        """
        raise NotImplementedError(
            "QTX has no direct user-stream WebSocket. The logic is delegated to Binance sub-connector."
        )

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        """
        Since we have no direct WebSocket channels for QTX user stream, do nothing or raise a NotImplementedError.
        """
        raise NotImplementedError(
            "QTX has no direct subscription to user-stream webSocket. The logic is delegated to Binance sub-connector."
        )

    async def listen_for_user_stream(self, output: asyncio.Queue):
        """
        We override the base method to implement a robust re-try loop reading
        from binance_connector._user_stream_tracker.user_stream queue.

        If any errors happen, we log and retry after a short delay.
        """
        self.logger().info("QTX STARTING listen_for_user_stream() with output queue ID: %s", id(output))
        while True:
            try:
                # If QTX is not trading, just sleep and do nothing
                if not self._qtx_connector.is_trading_required:
                    self.logger().info("QTX not trading required, skipping user stream.")
                    await asyncio.sleep(60)
                    continue

                # Check if we have binance connector
                binance_connector = self._qtx_connector.binance_connector
                if binance_connector is None:
                    self.logger().warning("No binance connector, cannot forward user stream events.")
                    await asyncio.sleep(5)
                    continue

                # DEBUGGING: Check state of binance user stream - only log at DEBUG level now
                binance_user_stream = binance_connector._user_stream_tracker.user_stream

                # Log initial delegation notice at debug level only
                if self.logger().isEnabledFor(logging.DEBUG):
                    self.logger().debug(
                        f"Delegating user stream events from binance connector to QTX output queue. "
                        f"Queue size: {binance_user_stream.qsize()}"
                    )

                while True:
                    # Only log at debug level to reduce noise
                    if self.logger().isEnabledFor(logging.DEBUG):
                        self.logger().debug(f"Waiting on binance_user_stream.get() - Queue size: {binance_user_stream.qsize()}")
                    # Add timeout to detect if queue is blocked
                    try:
                        # Use wait_for with a timeout to detect if queue is stuck
                        data = await asyncio.wait_for(
                            binance_connector._user_stream_tracker.user_stream.get(),
                            timeout=300  # 5 minutes timeout for diagnostic purposes
                        )
                        # We update local recv time if we retrieved data successfully
                        self._local_last_recv_time = time.time()
                        # Only log at debug level to reduce noise
                        if self.logger().isEnabledFor(logging.DEBUG):
                            self.logger().debug("Got data from binance user stream. Forwarding to QTX output.")
                        # Forward the event
                        output.put_nowait(data)
                    except asyncio.TimeoutError:
                        self.logger().warning("No data received from Binance user stream for 5 minutes. This is unusual.")
                        continue

            except asyncio.CancelledError:
                self.logger().info("QTX listen_for_user_stream CancelledError received.")
                raise
            except Exception as e:
                self.logger().exception(f"QTX user stream error: {str(e)}. Retrying in 5 seconds...")
                await asyncio.sleep(5)
            finally:
                self.logger().info("QTX listen_for_user_stream finally block reached.")
                # In some connectors we might do extra cleanup here, if we had an actual WS.
                # For now we do nothing.
                pass
