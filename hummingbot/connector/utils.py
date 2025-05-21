import gzip
import json
import os
import platform
import random
import time
from collections import namedtuple
from hashlib import md5
from typing import Any, Callable, Dict, Optional, Tuple

from hexbytes import HexBytes

from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.api_throttler.async_throttler_base import AsyncThrottlerBase
from hummingbot.core.utils.tracking_nonce import NonceCreator
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSResponse
from hummingbot.core.web_assistant.rest_pre_processors import RESTPreProcessorBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_post_processors import WSPostProcessorBase

TradeFillOrderDetails = namedtuple("TradeFillOrderDetails", "market exchange_trade_id symbol")


def build_api_factory(throttler: AsyncThrottlerBase) -> WebAssistantsFactory:
    throttler = throttler or AsyncThrottler(rate_limits=[])
    api_factory = WebAssistantsFactory(throttler=throttler)
    return api_factory


def split_hb_trading_pair(trading_pair: str) -> Tuple[str, str]:
    base, quote = trading_pair.split("-")
    return base, quote


def combine_to_hb_trading_pair(base: str, quote: str) -> str:
    trading_pair = f"{base}-{quote}"
    return trading_pair


def validate_trading_pair(trading_pair: str) -> bool:
    valid = False
    if "-" in trading_pair and len(trading_pair.split("-")) == 2:
        valid = True
    return valid


def _bot_instance_id() -> str:
    return md5(f"{platform.uname()}_pid:{os.getpid()}_ppid:{os.getppid()}".encode("utf-8")).hexdigest()


def get_new_client_order_id(
    is_buy: bool, trading_pair: str, hbot_order_id_prefix: str = "", max_id_len: Optional[int] = None
) -> str:
    """
    Creates a pure integer client order id for a new order
    :param is_buy: True if the order is a buy order, False otherwise
    :param trading_pair: the trading pair the order will be operating with (not used in integer ID generation)
    :param hbot_order_id_prefix: The hummingbot-specific identifier (ignored to generate pure integer)
    :param max_id_len: The maximum length of the ID string (applied to the final string representation)
    :return: a pure integer identifier for the new order to be used in the client
    """
    # Generate components for a unique ID
    timestamp = int(time.time() * 1000)  # millisecond precision
    random_component = random.randint(1000, 9999)  # 4-digit random number
    side_digit = 1 if is_buy else 2  # 1 for buy, 2 for sell
    # Create a numeric ID in format: SIDE + TIMESTAMP + RANDOM
    # This creates IDs like: 11684231490001234
    numeric_id = int(f"{side_digit}{timestamp}{random_component}")
    # Handle long long size limitations (typically 2^63-1 or 9,223,372,036,854,775,807)
    # Ensure our ID doesn't exceed this limit for C/C++ shared memory compatibility
    max_c_long_long = (2**63) - 1
    numeric_id = min(numeric_id, max_c_long_long)
    # Convert to string and apply max_id_len if necessary
    result = str(numeric_id)
    if max_id_len is not None and len(result) > max_id_len:
        # If truncating, keep the most significant parts (from the beginning)
        result = result[:max_id_len]
    return result


def get_new_numeric_client_order_id(nonce_creator: NonceCreator, max_id_bit_count: Optional[int] = None) -> int:
    hexa_hash = _bot_instance_id()
    host_part = int(hexa_hash, 16)
    client_order_id = int(f"{host_part}{nonce_creator.get_tracking_nonce()}")
    if max_id_bit_count:
        max_int = 2**max_id_bit_count - 1
        client_order_id &= max_int
    return client_order_id


class TimeSynchronizerRESTPreProcessor(RESTPreProcessorBase):
    """
    This pre processor is intended to be used in those connectors that require synchronization with the server time
    to accept API requests. It ensures the synchronizer has at least one server time sample before being used.
    """

    def __init__(self, synchronizer: TimeSynchronizer, time_provider: Callable):
        super().__init__()
        self._synchronizer = synchronizer
        self._time_provider = time_provider

    async def pre_process(self, request: RESTRequest) -> RESTRequest:
        await self._synchronizer.update_server_time_if_not_initialized(time_provider=self._time_provider())
        return request


class GZipCompressionWSPostProcessor(WSPostProcessorBase):
    """
    Performs the necessary response processing from both public and private websocket streams.
    """

    async def post_process(self, response: WSResponse) -> WSResponse:
        if not isinstance(response.data, bytes):
            # Unlike Market WebSocket, the return data of Account and Order Websocket are not compressed by GZIP.
            return response
        encoded_msg: bytes = gzip.decompress(response.data)
        msg: Dict[str, Any] = json.loads(encoded_msg.decode("utf-8"))

        return WSResponse(data=msg)


def to_0x_hex(signature: HexBytes | bytes) -> str:
    """
    Convert a string to a 0x-prefixed hex string.
    """
    if hasattr(signature, "to_0x_hex"):
        return signature.to_0x_hex()

    return hex if (hex := signature.hex()).startswith("0x") else f"0x{hex}"
