#!/usr/bin/env python
import asyncio
from typing import List, Optional

from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.logger import HummingbotLogger


class QTXAPIUserStreamDataSource(UserStreamTrackerDataSource):
    """
    A simulated user stream data source for QTX connector.
    
    This implementation satisfies the interface requirements for UserStreamTrackerDataSource
    but does not actually connect to any real user stream since QTX is a market data only connector.
    """
    
    _logger: Optional[HummingbotLogger] = None
    
    def __init__(self):
        super().__init__()
    
    async def listen_for_user_stream(self, output: asyncio.Queue):
        """
        Simulates a user stream listener. 
        Since this is a market data only connector, this method just keeps running
        without emitting any events.
        """
        while True:
            try:
                # Just keep the coroutine alive without doing anything
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # Pass through cancellation
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in user stream listener: {e}", exc_info=True)
                # Small delay before retrying
                await asyncio.sleep(5.0) 