#!/usr/bin/env python

# QTX Constants - only keep QTX-specific constants here
EXCHANGE_NAME = "qtx_perpetual"
DEFAULT_UDP_HOST = "172.30.2.221"  # Default local UDP host for QTX market data
DEFAULT_UDP_PORT = 8080  # Default UDP port for QTX market data

# Message types for UDP feed
DIFF_MESSAGE_TYPE = "diff"
TRADE_MESSAGE_TYPE = "trade"

# Order management constants
MAX_ORDER_ID_LEN = 32
BROKER_ID = "QTX"
FUNDING_SETTLEMENT_DURATION = 8 * 60 * 60  # 8 hours in seconds

# Order states - generic mapping that works for multiple exchanges
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

# Error codes - generic error handling
ORDER_NOT_EXIST_ERROR_CODE = -2013
ORDER_NOT_EXIST_MESSAGE = "Order does not exist"
UNKNOWN_ORDER_ERROR_CODE = -7000
UNKNOWN_ORDER_MESSAGE = "Unknown order sent"

# Rate limits - these will be handled by the parent exchange
RATE_LIMITS = []  # Empty as rate limiting is handled by parent exchange
