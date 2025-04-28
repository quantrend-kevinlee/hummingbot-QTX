# QTX specific constants for market data only
HBOT_ORDER_ID_PREFIX = "QTX"
MAX_ORDER_ID_LEN = 32

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

# Message types for QTX
SNAPSHOT_MESSAGE_TYPE = "snapshot"
DIFF_MESSAGE_TYPE = "orderbook"
TRADE_MESSAGE_TYPE = "trade"

# Socket timeout values
SOCKET_TIMEOUT = 30.0  # seconds
RECONNECT_DELAY = 5.0  # seconds

# API Endpoints (placeholder for future implementation)
BASE_PATH = "/api/v1"
PING_PATH_URL = "/ping"
EXCHANGE_INFO_PATH_URL = "/info"
ORDER_PATH_URL = "/order"

# Auth related 
API_KEY_HEADER = "X-QTX-APIKEY"

# Order states
ORDER_STATE = {
    "NEW": "NEW",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED",
    "FILLED": "FILLED",
    "CANCELED": "CANCELED",
    "PENDING_CANCEL": "PENDING_CANCEL",
    "REJECTED": "REJECTED",
    "EXPIRED": "EXPIRED"
}

# Order error codes
ORDER_NOT_EXIST_ERROR_CODE = 1001
ORDER_NOT_EXIST_MESSAGE = "Order does not exist"
UNKNOWN_ORDER_ERROR_CODE = 1002
UNKNOWN_ORDER_MESSAGE = "Unknown order"

# Rate limits
RATE_LIMITS = [
    # Pool for public endpoints
    {
        "limit_id": "public",
        "limit": 100,
        "time_interval": 1  # 1 second
    },
    # Pool for private endpoints
    {
        "limit_id": "private",
        "limit": 10,
        "time_interval": 1  # 1 second
    },
    # Pool for specific endpoints
    {
        "limit_id": PING_PATH_URL,
        "limit": 50,
        "time_interval": 1  # 1 second
    },
    {
        "limit_id": EXCHANGE_INFO_PATH_URL,
        "limit": 10,
        "time_interval": 1  # 1 second
    },
    {
        "limit_id": ORDER_PATH_URL,
        "limit": 5,
        "time_interval": 1  # 1 second
    }
]
