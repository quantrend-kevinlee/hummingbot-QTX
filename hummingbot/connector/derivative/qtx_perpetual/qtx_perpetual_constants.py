#!/usr/bin/env python

from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

EXCHANGE_NAME = "qtx_perpetual"
BROKER_ID = "x-qtx"
MAX_ORDER_ID_LEN = 32

DOMAIN = EXCHANGE_NAME
TESTNET_DOMAIN = "qtx_perpetual_testnet"

# Default UDP connection settings (from C SDK)
DEFAULT_UDP_HOST = "172.30.2.221"
DEFAULT_UDP_PORT = 8080
DEFAULT_UDP_BUFFER_SIZE = 65536

# Message types (from C SDK)
MSG_TYPE_L1_BID = 1
MSG_TYPE_L1_ASK = -1
MSG_TYPE_L2_BID = 2
MSG_TYPE_L2_ASK = -2
MSG_TYPE_TRADE_BUY = 3
MSG_TYPE_TRADE_SELL = -3

# Max number of symbols and symbol length
MAX_SYMBOLS = 100
MAX_SYMBOL_LEN = 64

# Event types for message queue routing
SNAPSHOT_EVENT_TYPE = "snapshot"
DIFF_EVENT_TYPE = "orderbook"
TRADE_EVENT_TYPE = "trade"
FUNDING_INFO_EVENT_TYPE = "funding_info"

# Message types for QTX
SNAPSHOT_MESSAGE_TYPE = "snapshot"
DIFF_MESSAGE_TYPE = "orderbook"
TRADE_MESSAGE_TYPE = "trade"
FUNDING_INFO_MESSAGE_TYPE = "funding_info"

# Socket timeout values
SOCKET_TIMEOUT = 30.0  # seconds
RECONNECT_DELAY = 5.0  # seconds

# API Endpoints (placeholder for future implementation)
BASE_PATH = "/api/v1"
PING_PATH_URL = "/ping"
EXCHANGE_INFO_PATH_URL = "/info"
ORDER_PATH_URL = "/order"

# Perpetual specific endpoints
POSITION_INFORMATION_PATH_URL = "/position"
FUNDING_INFO_PATH_URL = "/funding"
SET_LEVERAGE_PATH_URL = "/leverage"
CHANGE_POSITION_MODE_PATH_URL = "/positionMode"
INCOME_HISTORY_PATH_URL = "/income"

# Auth related
API_KEY_HEADER = "X-QTX-APIKEY"

# Order states
ORDER_STATE = {
    "NEW": OrderState.OPEN,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELED,
    "PENDING_CANCEL": OrderState.PENDING_CANCEL,
    "REJECTED": OrderState.FAILED,
    "EXPIRED": OrderState.CANCELED,
}

# Order error codes
ORDER_NOT_EXIST_ERROR_CODE = 1001
ORDER_NOT_EXIST_MESSAGE = "Order does not exist"
UNKNOWN_ORDER_ERROR_CODE = 1002
UNKNOWN_ORDER_MESSAGE = "Unknown order"

# Funding Settlement Time Span
FUNDING_SETTLEMENT_DURATION = (0, 30)  # seconds before snapshot, seconds after snapshot

# Position Mode
ONE_WAY_MODE = "ONE_WAY"
HEDGE_MODE = "HEDGE"

# Time in force options
TIME_IN_FORCE_GTC = "GTC"  # Good till cancelled
TIME_IN_FORCE_IOC = "IOC"  # Immediate or cancel
TIME_IN_FORCE_FOK = "FOK"  # Fill or kill
TIME_IN_FORCE_GTX = "GTX"  # Good till crossing

# Rate Limit Type
REQUEST_WEIGHT = "REQUEST_WEIGHT"
ORDERS_1MIN = "ORDERS_1MIN"
ORDERS_1SEC = "ORDERS_1SEC"

# Rate Limit time intervals
ONE_HOUR = 3600
ONE_MINUTE = 60
ONE_SECOND = 1
ONE_DAY = 86400

MAX_REQUEST = 2400

# Rate limits - following a similar structure to Binance
RATE_LIMITS = [
    # Pool Limits
    RateLimit(limit_id=REQUEST_WEIGHT, limit=2400, time_interval=ONE_MINUTE),
    RateLimit(limit_id=ORDERS_1MIN, limit=1200, time_interval=ONE_MINUTE),
    RateLimit(limit_id=ORDERS_1SEC, limit=300, time_interval=10),
    # Weight Limits for individual endpoints
    RateLimit(
        limit_id=SNAPSHOT_EVENT_TYPE,
        limit=MAX_REQUEST,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, weight=20)],
    ),
    RateLimit(
        limit_id=PING_PATH_URL,
        limit=MAX_REQUEST,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, weight=1)],
    ),
    RateLimit(
        limit_id=EXCHANGE_INFO_PATH_URL,
        limit=MAX_REQUEST,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, weight=40)],
    ),
    RateLimit(
        limit_id=ORDER_PATH_URL,
        limit=MAX_REQUEST,
        time_interval=ONE_MINUTE,
        linked_limits=[
            LinkedLimitWeightPair(REQUEST_WEIGHT, weight=1),
            LinkedLimitWeightPair(ORDERS_1MIN, weight=1),
            LinkedLimitWeightPair(ORDERS_1SEC, weight=1),
        ],
    ),
    RateLimit(
        limit_id=POSITION_INFORMATION_PATH_URL,
        limit=MAX_REQUEST,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, weight=5)],
    ),
    RateLimit(
        limit_id=FUNDING_INFO_PATH_URL,
        limit=MAX_REQUEST,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, weight=1)],
    ),
    RateLimit(
        limit_id=SET_LEVERAGE_PATH_URL,
        limit=MAX_REQUEST,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, weight=1)],
    ),
    RateLimit(
        limit_id=CHANGE_POSITION_MODE_PATH_URL,
        limit=MAX_REQUEST,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, weight=1)],
    ),
    RateLimit(
        limit_id=INCOME_HISTORY_PATH_URL,
        limit=MAX_REQUEST,
        time_interval=ONE_MINUTE,
        linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, weight=30)],
    ),
]

# WebSocket channels
DIFF_STREAM_ID = 1
TRADE_STREAM_ID = 2
FUNDING_INFO_STREAM_ID = 3
HEARTBEAT_TIME_INTERVAL = 30.0
