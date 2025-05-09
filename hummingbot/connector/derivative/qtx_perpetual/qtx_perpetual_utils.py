#!/usr/bin/env python

from decimal import Decimal
from typing import Any, Dict

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

BROKER_ID = "QTX_Perpetual"


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
