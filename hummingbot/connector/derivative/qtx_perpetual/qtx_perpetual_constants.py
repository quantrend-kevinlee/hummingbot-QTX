#!/usr/bin/env python

# Connection settings
EXCHANGE_NAME = "qtx_perpetual"
DEFAULT_UDP_HOST = "172.30.2.221"
DEFAULT_UDP_PORT = 8080

# Message types
DIFF_MESSAGE_TYPE = "diff"
TRADE_MESSAGE_TYPE = "trade"

# Order management
MAX_ORDER_ID_LEN = 32
BROKER_ID = "QTX"
FUNDING_SETTLEMENT_DURATION = 8 * 60 * 60  # 8 hours in seconds

# Order state mapping
ORDER_STATE = {
    "NEW": "OPEN",
    "PARTIALLY_FILLED": "OPEN",
    "FILLED": "EXECUTED",
    "CANCELED": "CANCELED",
    "PENDING_CANCEL": "CANCELING",
    "REJECTED": "FAILED",
    "EXPIRED": "FAILED",
    "UNKNOWN": "UNKNOWN",
}

# Error codes
ORDER_NOT_EXIST_ERROR_CODE = -2013
ORDER_NOT_EXIST_MESSAGE = "Order does not exist"
UNKNOWN_ORDER_ERROR_CODE = -7000
UNKNOWN_ORDER_MESSAGE = "Unknown order sent"

# Rate limits (handled by parent exchange)
RATE_LIMITS = []
