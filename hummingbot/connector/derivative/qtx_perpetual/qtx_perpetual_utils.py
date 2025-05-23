#!/usr/bin/env python

from pydantic import Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.connector.derivative.qtx_perpetual import qtx_perpetual_constants as CONSTANTS


class QtxPerpetualConfigMap(BaseConnectorConfigMap):
    """Configuration parameters for QTX Perpetual connector"""

    connector: str = "qtx_perpetual"
    exchange_backend: str = Field(
        default="binance",
        json_schema_extra={
            "prompt": lambda cm: "Enter the exchange backend to use for trading (binance, okx, bybit)",
            "prompt_on_new": True,
            "is_connect_key": True,
        },
    )
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
    qtx_place_order_shared_memory_name: str = Field(
        default="/place_order_kevinlee",
        json_schema_extra={
            "prompt": lambda cm: "Enter the QTX shared memory segment name:",
            "prompt_on_new": True,
            "is_connect_key": True,
        },
    )
    exchange_api_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: f"Enter your {cm.exchange_backend.upper()} API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    exchange_api_secret: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: f"Enter your {cm.exchange_backend.upper()} API secret",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )


KEYS = QtxPerpetualConfigMap.model_construct()
