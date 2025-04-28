from typing import Dict, Optional

from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType


class QTXOrderBook(OrderBook):
    """
    Order book implementation for QTX market data source.
    Handles message formats from the QTX binary UDP protocol.
    """

    def __init__(self, dex=False):
        super().__init__(dex)
        self.snapshot_message_count = 0
        self._last_processed_timestamp = 0

    @classmethod
    def snapshot_message_from_exchange(
        cls, msg: Dict[str, any], timestamp: float, metadata: Optional[Dict] = None
    ) -> OrderBookMessage:
        """
        Creates a snapshot message with the order book snapshot message from QTX
        :param msg: the response from the QTX source containing order book snapshot
        :param timestamp: the snapshot timestamp
        :param metadata: a dictionary with extra information to add to the snapshot data
        :return: a snapshot message with the snapshot information received from QTX
        """
        if metadata:
            msg.update(metadata)

        return OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            {
                "trading_pair": msg["trading_pair"],
                "update_id": msg.get("update_id", int(timestamp * 1000)),
                "bids": msg.get("bids", []),
                "asks": msg.get("asks", []),
            },
            timestamp=timestamp,
        )

    @classmethod
    def diff_message_from_exchange(
        cls, msg: Dict[str, any], timestamp: Optional[float] = None, metadata: Optional[Dict] = None
    ) -> OrderBookMessage:
        """
        Creates a diff message with the changes in the order book received from QTX
        :param msg: the changes in the order book
        :param timestamp: the timestamp of the difference
        :param metadata: a dictionary with extra information to add to the difference data
        :return: a diff message with the changes in the order book
        """
        if metadata:
            msg.update(metadata)

        return OrderBookMessage(
            OrderBookMessageType.DIFF,
            {
                "trading_pair": msg["trading_pair"],
                "update_id": msg.get("update_id", int(timestamp * 1000)),
                "bids": msg.get("bids", []),
                "asks": msg.get("asks", []),
            },
            timestamp=timestamp,
        )

    @classmethod
    def trade_message_from_exchange(cls, msg: Dict[str, any], metadata: Optional[Dict] = None):
        """
        Creates a trade message with the information from the trade event sent by QTX
        :param msg: the trade event details sent by QTX
        :param metadata: a dictionary with extra information to add to trade message
        :return: a trade message with the details of the trade as provided by QTX
        """
        if metadata:
            msg.update(metadata)

        # Determine trade type from side
        trade_side = msg.get("side", "").upper()
        trade_type = float(TradeType.BUY.value if trade_side == "BUY" else TradeType.SELL.value)

        # Get timestamp or use current time
        ts = float(msg.get("timestamp", 0)) if "timestamp" in msg else float(msg.get("trade_id", 0)) / 1000

        return OrderBookMessage(
            OrderBookMessageType.TRADE,
            {
                "trading_pair": msg["trading_pair"],
                "trade_type": trade_type,
                "trade_id": msg.get("trade_id", "0"),
                "update_id": int(ts * 1000),
                "price": msg.get("price", "0"),
                "amount": msg.get("amount", "0"),
            },
            timestamp=ts,
        )

    def apply_snapshot(self, bids, asks, update_id):
        """
        Apply an order book snapshot to the order book.
        This override handles the parameters as expected by the OrderBookTracker.
        
        :param bids: the bids in the snapshot
        :param asks: the asks in the snapshot  
        :param update_id: the snapshot update id
        """
        # Construct a snapshot message and pass to the parent implementation
        snapshot_msg = OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            {
                "trading_pair": None,  # Will be set by OrderBookTracker
                "update_id": update_id,
                "bids": bids,
                "asks": asks,
            },
            timestamp=float(update_id) / 1000,
        )
        # Call the parent implementation with the expected arguments
        super().apply_snapshot(snapshot_msg.bids, snapshot_msg.asks, snapshot_msg.update_id)
        # Increment the snapshot counter
        self.snapshot_message_count += 1
        self._last_processed_timestamp = snapshot_msg.timestamp
