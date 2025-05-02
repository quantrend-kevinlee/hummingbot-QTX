#!/usr/bin/env python

from decimal import Decimal
from typing import Any, Dict, List

from pydantic import Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_constants as BINANCE_CONSTANTS
from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS
from hummingbot.core.data_type.common import OrderType, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.trade_fee import TradeFeeSchema
from hummingbot.core.utils.tracking_nonce import get_tracking_nonce

DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0002"),
    taker_percent_fee_decimal=Decimal("0.0004"),
    buy_percent_fee_deducted_from_returns=True,
)

CENTRALIZED = True

EXAMPLE_PAIR = "BTC-USDT"

BROKER_ID = "QTX"


class QtxPerpetualConfigMap(BaseConnectorConfigMap):
    connector: str = "qtx_perpetual"
    qtx_perpetual_host: str = Field(
        default=CONSTANTS.DEFAULT_UDP_HOST,
        json_schema_extra={
            "prompt": lambda cm: "Enter your QTX Perpetual UDP host IP address",
            "prompt_on_new": True,
            "is_connect_key": True,
        },
    )
    qtx_perpetual_port: int = Field(
        default=CONSTANTS.DEFAULT_UDP_PORT,
        json_schema_extra={
            "prompt": lambda cm: "Enter your QTX Perpetual UDP port",
            "prompt_on_new": True,
            "is_connect_key": True,
        },
    )
    binance_api_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": "Enter your Binance API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    binance_api_secret: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": "Enter your Binance API secret",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )


KEYS = QtxPerpetualConfigMap.model_construct()


def get_client_order_id(order_side: TradeType, trading_pair: str) -> str:
    """
    Creates a client order id for a new order
    :param order_side: the side of the order (BUY or SELL)
    :param trading_pair: the trading pair the order will be operating with
    :return: an identifier for the new order to be used in the client
    """
    nonce = get_tracking_nonce()
    side_str = "B" if order_side is TradeType.BUY else "S"
    return f"{CONSTANTS.BROKER_ID}{side_str}{nonce}"


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    """
    Verifies if the exchange information response contains the required information
    :param exchange_info: the response from the exchange
    :return: True if the exchange information is valid
    """
    return all(key in exchange_info.keys() for key in ["timezone", "serverTime", "symbols"])


def format_trading_pair(trading_pair: str) -> str:
    """
    Format trading pair to match exchange's requirements.
    Example: BTC-USDT -> btcusdt
    """
    return trading_pair.replace("-", "").lower()


def convert_to_exchange_trading_pair(hb_trading_pair: str) -> str:
    """
    Convert from Hummingbot trading pair format to QTX perpetual UDP format.
    Example: BTC-USDT -> binance-futures:btcusdt
    """
    formatted_pair = format_trading_pair(hb_trading_pair)
    return f"binance-futures:{formatted_pair}"


def convert_from_exchange_trading_pair(exchange_trading_pair: str) -> str:
    """
    Convert trading pair from exchange format to client format.
    Example: binance-futures:btcusdt -> BTC-USDT
    """
    # Remove the exchange prefix if present
    if ":" in exchange_trading_pair:
        _, exchange_trading_pair = exchange_trading_pair.split(":", 1)

    # Convert to lowercase for consistent processing
    exchange_trading_pair = exchange_trading_pair.lower()

    # Attempt to identify base and quote currencies
    for quote in ["usdt", "busd", "usdc", "usd", "dai", "btc", "eth"]:
        if exchange_trading_pair.endswith(quote):
            base = exchange_trading_pair[: -len(quote)]
            return f"{base.upper()}-{quote.upper()}"

    # If we can't identify the split, return the original with a warning
    import logging

    logging.getLogger(__name__).warning(f"Could not parse exchange symbol: {exchange_trading_pair}. Returning as is.")
    return exchange_trading_pair.upper()


def get_exchange_order_type(order_type: OrderType) -> str:
    """
    Convert OrderType to exchange order type string.
    """
    if order_type is OrderType.LIMIT:
        return "LIMIT"
    elif order_type is OrderType.MARKET:
        return "MARKET"
    elif order_type is OrderType.LIMIT_MAKER:
        return "LIMIT_MAKER"


def get_exchange_time_in_force(order_type: OrderType) -> str:
    """
    Convert OrderType to exchange time in force.
    """
    if order_type is OrderType.LIMIT:
        return BINANCE_CONSTANTS.TIME_IN_FORCE_GTC
    elif order_type is OrderType.MARKET:
        return BINANCE_CONSTANTS.TIME_IN_FORCE_GTC
    elif order_type is OrderType.LIMIT_MAKER:
        return BINANCE_CONSTANTS.TIME_IN_FORCE_GTX
    else:
        raise ValueError(f"Unsupported order type: {order_type}")


def get_position_mode_from_exchange_value(position_mode: str) -> PositionMode:
    """
    Convert exchange position mode string to PositionMode enum.
    """
    if position_mode == "ONE_WAY":
        return PositionMode.ONEWAY
    elif position_mode == "HEDGE":
        return PositionMode.HEDGE
    else:
        raise ValueError(f"Unsupported position mode: {position_mode}")


def get_position_mode_exchange_value(position_mode: PositionMode) -> str:
    """
    Convert PositionMode enum to exchange position mode string.
    """
    if position_mode is PositionMode.ONEWAY:
        return "ONE_WAY"
    elif position_mode is PositionMode.HEDGE:
        return "HEDGE"


def get_position_side_from_exchange_value(position_side: str) -> PositionSide:
    """
    Convert exchange position side string to PositionSide enum.
    """
    if position_side == "LONG":
        return PositionSide.LONG
    elif position_side == "SHORT":
        return PositionSide.SHORT
    elif position_side == "BOTH":
        return PositionSide.BOTH
    else:
        raise ValueError(f"Unsupported position side: {position_side}")


def get_position_side_exchange_value(position_side: PositionSide) -> str:
    """
    Convert PositionSide enum to exchange position side string.
    """
    if position_side is PositionSide.LONG:
        return "LONG"
    elif position_side is PositionSide.SHORT:
        return "SHORT"
    elif position_side is PositionSide.BOTH:
        return "BOTH"


def get_order_status_from_exchange_value(status: str) -> str:
    """
    Convert exchange order status string to internal order status string.
    """
    return CONSTANTS.ORDER_STATE.get(status, "UNKNOWN")


# Binance-specific utility functions

def convert_to_binance_trading_pair(trading_pair: str) -> str:
    """
    Converts Hummingbot trading pair format to Binance format
    Example: BTC-USDT -> BTCUSDT
    """
    return trading_pair.replace("-", "")


def convert_from_binance_trading_pair(binance_symbol: str, trading_pairs: List[str]) -> str:
    """
    Converts Binance symbol to Hummingbot trading pair format
    Example: BTCUSDT -> BTC-USDT

    :param binance_symbol: The Binance symbol to convert
    :param trading_pairs: List of available trading pairs to match against
    :return: The converted trading pair in Hummingbot format
    """
    # First try direct conversion by checking if removing the dash matches
    for tp in trading_pairs:
        if binance_symbol == tp.replace("-", ""):
            return tp

    # If no direct match, try to identify base and quote
    # Common quote currencies in perpetual futures
    quote_currencies = ["USDT", "BUSD", "USDC", "USD", "BTC", "ETH"]

    for quote in quote_currencies:
        if binance_symbol.endswith(quote):
            base = binance_symbol[:-len(quote)]
            candidate = f"{base}-{quote}"
            if candidate in trading_pairs:
                return candidate

    # If we still can't find a match, try a more sophisticated approach
    # by checking all possible splits of the symbol
    for i in range(1, len(binance_symbol)):
        base = binance_symbol[:i]
        quote = binance_symbol[i:]
        candidate = f"{base}-{quote}"
        if candidate in trading_pairs:
            return candidate

    # If all else fails, return a best guess
    for quote in quote_currencies:
        if binance_symbol.endswith(quote):
            base = binance_symbol[:-len(quote)]
            return f"{base}-{quote}"

    # Last resort: just insert a dash before the last 4 characters
    return f"{binance_symbol[:-4]}-{binance_symbol[-4:]}"


def map_binance_order_status(binance_status: str) -> str:
    """
    Maps Binance order status to internal order status
    """
    # Binance uses the same status strings as our internal constants
    return binance_status


def get_binance_position_mode(position_mode: PositionMode) -> bool:
    """
    Converts PositionMode enum to Binance position mode value
    For Binance, dualSidePosition=true means HEDGE mode, false means ONE-WAY

    :param position_mode: The position mode to convert
    :return: Boolean value for Binance API
    """
    return position_mode == PositionMode.HEDGE
