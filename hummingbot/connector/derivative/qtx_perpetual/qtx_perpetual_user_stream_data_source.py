#!/usr/bin/env python

import asyncio
from typing import TYPE_CHECKING, Optional

from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_derivative import QtxPerpetualDerivative


class QtxPerpetualUserStreamDataSource(UserStreamTrackerDataSource):
    """
    A minimal pass-through: it does NOT create its own listen key or websocket.
    Instead, it listens for user events from the binance_connector._user_stream_tracker's queue
    and forwards them to QTX's output queue.
    """

    _logger: Optional[HummingbotLogger] = None

    def __init__(self, qtx_connector: "QtxPerpetualDerivative"):
        super().__init__()
        self._qtx_connector = qtx_connector

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = super().logger()
        return cls._logger

    async def listen_for_user_stream(self, output: asyncio.Queue):
        # If QTX is not trading, no user stream is needed
        if not self._qtx_connector.is_trading_required:
            self.logger().debug("QTX not trading required, skipping user stream.")
            return

        binance_connector = self._qtx_connector.binance_connector
        if binance_connector is None:
            self.logger().error("No binance connector, cannot forward user stream events.")
            return

        self.logger().info("Delegating user stream events from binance connector to QTX output queue.")
        while True:
            data = await binance_connector._user_stream_tracker.user_stream.get()
            output.put_nowait(data)
