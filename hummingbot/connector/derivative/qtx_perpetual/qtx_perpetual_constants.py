#!/usr/bin/env python

from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_constants as BINANCE_CONSTANTS

# QTX Constants - only keep QTX-specific constants here
EXCHANGE_NAME = "qtx_perpetual"
DEFAULT_DOMAIN = BINANCE_CONSTANTS.DOMAIN  # Using Binance mainnet as default for trading
DEFAULT_UDP_HOST = "172.30.2.221"  # Default local UDP host for QTX market data
DEFAULT_UDP_PORT = 8080  # Default UDP port for QTX market data
DEFAULT_UDP_BUFFER_SIZE = 65536  # UDP buffer size

# Message types for UDP feed
DIFF_MESSAGE_TYPE = "diff"
TRADE_MESSAGE_TYPE = "trade"

# Inherit necessary Binance constants
MAX_ORDER_ID_LEN = BINANCE_CONSTANTS.MAX_ORDER_ID_LEN
BROKER_ID = BINANCE_CONSTANTS.BROKER_ID
FUNDING_SETTLEMENT_DURATION = BINANCE_CONSTANTS.FUNDING_SETTLEMENT_DURATION

# Map Binance order states - keep this for convenience
ORDER_STATE = BINANCE_CONSTANTS.ORDER_STATE

# Import BINANCE_CONSTANTS error codes for consistent error handling
ORDER_NOT_EXIST_ERROR_CODE = BINANCE_CONSTANTS.ORDER_NOT_EXIST_ERROR_CODE
ORDER_NOT_EXIST_MESSAGE = BINANCE_CONSTANTS.ORDER_NOT_EXIST_MESSAGE
UNKNOWN_ORDER_ERROR_CODE = BINANCE_CONSTANTS.UNKNOWN_ORDER_ERROR_CODE
UNKNOWN_ORDER_MESSAGE = BINANCE_CONSTANTS.UNKNOWN_ORDER_MESSAGE

# We use Binance's rate limits directly to ensure compatibility
# This is important because we delegate all trading operations to Binance
RATE_LIMITS = BINANCE_CONSTANTS.RATE_LIMITS
