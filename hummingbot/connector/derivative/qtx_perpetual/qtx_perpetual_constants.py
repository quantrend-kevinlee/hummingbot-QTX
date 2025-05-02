#!/usr/bin/env python

from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_constants as BINANCE_CONSTANTS
from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit

# QTX Constants - only keep QTX-specific constants here
EXCHANGE_NAME = "qtx_perpetual"
DEFAULT_DOMAIN = BINANCE_CONSTANTS.DOMAIN  # Using Binance mainnet as default for trading
DEFAULT_UDP_HOST = "172.30.2.221"  # Default local UDP host for QTX market data
DEFAULT_UDP_PORT = 8080  # Default UDP port for QTX market data
DEFAULT_UDP_BUFFER_SIZE = 8192  # UDP buffer size
ORDERBOOK_WARMUP_TIME = 10.0  # Seconds to wait for the orderbook to build up before marking connector as ready

# Message types for UDP feed
DIFF_MESSAGE_TYPE = "diff"
TRADE_MESSAGE_TYPE = "trade"

# Inherit necessary Binance constants
MAX_ORDER_ID_LEN = BINANCE_CONSTANTS.MAX_ORDER_ID_LEN
BROKER_ID = BINANCE_CONSTANTS.BROKER_ID
FUNDING_SETTLEMENT_DURATION = BINANCE_CONSTANTS.FUNDING_SETTLEMENT_DURATION

# Map Binance order states - keep this for convenience
ORDER_STATE = BINANCE_CONSTANTS.ORDER_STATE

# We need to define our own rate limits since QTX may have different quotas
# If QTX uses the same limits as Binance, consider importing BINANCE_CONSTANTS.RATE_LIMITS directly
RATE_LIMITS = [
    # Tier 1 (IP weight <= 2400)
    RateLimit(limit_id=BINANCE_CONSTANTS.EXCHANGE_INFO_URL, limit=20, time_interval=60.0),
    RateLimit(limit_id=BINANCE_CONSTANTS.PING_URL, limit=5, time_interval=60.0),
    RateLimit(limit_id=BINANCE_CONSTANTS.TICKER_PRICE_URL, limit=100, time_interval=60.0),
    RateLimit(limit_id=BINANCE_CONSTANTS.TICKER_PRICE_URL, limit=100, time_interval=60.0),
    RateLimit(limit_id=BINANCE_CONSTANTS.TICKER_PRICE_CHANGE_URL, limit=40, time_interval=60.0),
    RateLimit(limit_id=BINANCE_CONSTANTS.BINANCE_USER_STREAM_ENDPOINT, limit=5, time_interval=60.0),
    RateLimit(limit_id=BINANCE_CONSTANTS.ACCOUNT_INFO_URL, limit=20, time_interval=60.0),
    RateLimit(limit_id=BINANCE_CONSTANTS.POSITION_INFORMATION_URL, limit=20, time_interval=60.0),
    RateLimit(limit_id=BINANCE_CONSTANTS.ORDER_URL, limit=40, time_interval=60.0),

    # Weight limit
    RateLimit(limit_id="API_WEIGHT", limit=2400, time_interval=60.0, linked_limits=[
        LinkedLimitWeightPair(BINANCE_CONSTANTS.EXCHANGE_INFO_URL, 40),
        LinkedLimitWeightPair(BINANCE_CONSTANTS.PING_URL, 2),
        LinkedLimitWeightPair(BINANCE_CONSTANTS.TICKER_PRICE_URL, 2),
        LinkedLimitWeightPair(BINANCE_CONSTANTS.TICKER_PRICE_URL, 2),
        LinkedLimitWeightPair(BINANCE_CONSTANTS.TICKER_PRICE_CHANGE_URL, 1),
        LinkedLimitWeightPair(BINANCE_CONSTANTS.BINANCE_USER_STREAM_ENDPOINT, 1),
        LinkedLimitWeightPair(BINANCE_CONSTANTS.ACCOUNT_INFO_URL, 5),
        LinkedLimitWeightPair(BINANCE_CONSTANTS.POSITION_INFORMATION_URL, 5),
        LinkedLimitWeightPair(BINANCE_CONSTANTS.ORDER_URL, 1),
    ]),
]
