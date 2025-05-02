#!/usr/bin/env python

import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.client.config.config_methods import using_exchange
from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS
from hummingbot.core.data_type.common import OrderType, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder
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
    qtx_perpetual_api_key: SecretStr = Field(
        default="fake_api_key",
        json_schema_extra={
            "prompt": "Enter your QTX Perpetual API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    qtx_perpetual_api_secret: SecretStr = Field(
        default="fake_secret_key",
        json_schema_extra={
            "prompt": "Enter your QTX Perpetual API secret",
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


def convert_to_exchange_trading_pair(hb_trading_pair: str) -> str:
    """
    Convert from Hummingbot trading pair format to exchange format.
    Example: BTC-USDT -> BTCUSDT
    """
    return format_trading_pair(hb_trading_pair)


def build_api_factory_without_time_synchronizer_pre_processor(throttler):
    """
    Builds an API factory without time synchronizer pre-processor.
    This is used for endpoints that don't require time synchronization.
    """
    from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_web_utils as web_utils

    return web_utils.build_api_factory(throttler=throttler)


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
        return CONSTANTS.TIME_IN_FORCE_GTC
    elif order_type is OrderType.MARKET:
        return CONSTANTS.TIME_IN_FORCE_GTC
    elif order_type is OrderType.LIMIT_MAKER:
        return CONSTANTS.TIME_IN_FORCE_GTX
    else:
        raise ValueError(f"Unsupported order type: {order_type}")


def get_position_mode_from_exchange_value(position_mode: str) -> PositionMode:
    """
    Convert exchange position mode string to PositionMode enum.
    """
    if position_mode == CONSTANTS.ONE_WAY_MODE:
        return PositionMode.ONEWAY
    elif position_mode == CONSTANTS.HEDGE_MODE:
        return PositionMode.HEDGE
    else:
        raise ValueError(f"Unsupported position mode: {position_mode}")


def get_position_mode_exchange_value(position_mode: PositionMode) -> str:
    """
    Convert PositionMode enum to exchange position mode string.
    """
    if position_mode is PositionMode.ONEWAY:
        return CONSTANTS.ONE_WAY_MODE
    elif position_mode is PositionMode.HEDGE:
        return CONSTANTS.HEDGE_MODE


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
    if status in CONSTANTS.ORDER_STATE:
        return CONSTANTS.ORDER_STATE[status]
    else:
        raise ValueError(f"Order status {status} is not supported.")
