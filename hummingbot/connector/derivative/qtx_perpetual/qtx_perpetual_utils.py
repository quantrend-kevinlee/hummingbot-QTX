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
            "prompt": lambda cm: "Enter the exchange backend to use for trading (binance, okx, bitget, bybit, kucoin, gate_io)",
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

    # Binance fields
    binance_perpetual_api_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your Binance API key" if cm.exchange_backend == "binance" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    binance_perpetual_api_secret: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your Binance API secret" if cm.exchange_backend == "binance" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    # OKX fields
    okx_perpetual_api_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your OKX API key" if cm.exchange_backend == "okx" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    okx_perpetual_secret_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your OKX secret key" if cm.exchange_backend == "okx" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    okx_perpetual_passphrase: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your OKX passphrase" if cm.exchange_backend == "okx" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    # Bitget fields
    bitget_perpetual_api_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your Bitget API key" if cm.exchange_backend == "bitget" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    bitget_perpetual_secret_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your Bitget secret key" if cm.exchange_backend == "bitget" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    bitget_perpetual_passphrase: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your Bitget passphrase" if cm.exchange_backend == "bitget" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    # Bybit fields
    bybit_perpetual_api_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your Bybit API key" if cm.exchange_backend == "bybit" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    bybit_perpetual_secret_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your Bybit secret key" if cm.exchange_backend == "bybit" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    # KuCoin fields
    kucoin_perpetual_api_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your KuCoin API key" if cm.exchange_backend == "kucoin" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    kucoin_perpetual_secret_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your KuCoin secret key" if cm.exchange_backend == "kucoin" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    kucoin_perpetual_passphrase: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your KuCoin passphrase" if cm.exchange_backend == "kucoin" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    # Gate.io fields
    gate_io_perpetual_api_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your Gate.io API key" if cm.exchange_backend == "gate_io" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    gate_io_perpetual_secret_key: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your Gate.io secret key" if cm.exchange_backend == "gate_io" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    gate_io_perpetual_user_id: SecretStr = Field(
        default="",
        json_schema_extra={
            "prompt": lambda cm: "Enter your Gate.io user ID" if cm.exchange_backend == "gate_io" else None,
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )


KEYS = QtxPerpetualConfigMap.model_construct()