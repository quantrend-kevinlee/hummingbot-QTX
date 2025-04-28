import asyncio
import time
from typing import Optional

from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.logger import HummingbotLogger


class QTXAPIUserStreamDataSource(UserStreamTrackerDataSource):
    """API UserStreamDataSource for QTX connector (Simulated)."""

    _logger: Optional[HummingbotLogger] = None

    def __init__(self):
        super().__init__()
        self._last_recv_time: float = 0

    @property
    def last_recv_time(self) -> float:
        # Return current time to always appear "live"
        # Alternatively, could potentially sync with order book source time if needed
        return time.time()

    async def listen_for_user_stream(self, output: asyncio.Queue):
        """Listen for user stream."""
        self.logger().info("User stream listener started (Simulated - does nothing).")
        while True:
            await asyncio.sleep(3600) # Sleep indefinitely until cancelled

    # Optionally override start/stop if base class implementations cause issues
    # async def start(self):
    #     pass # Minimal implementation
    #
    # async def stop(self):
    #     pass # Minimal implementation 