from decimal import Decimal
from typing import Any, Dict

from pydantic import ConfigDict, Field

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.connector.exchange.qtx import qtx_constants as CONSTANTS
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

CENTRALIZED = True
EXAMPLE_PAIR = "BTC-USDT"  # Example trading pair for QTX

# Currently using Binance's fee structure as a placeholder until QTX provides its own fee structure
DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("1"),
    taker_percent_fee_decimal=Decimal("1"),
    buy_percent_fee_deducted_from_returns=True,
)


def is_trading_pair_valid(symbol_info: Dict[str, Any]) -> bool:
    """
    Verifies if a trading pair is enabled to operate with based on the QTX info
    :param symbol_info: the information for a trading pair from QTX
    :return: True if the trading pair is enabled, False otherwise
    """
    # This is a placeholder. Implement according to QTX's data format
    # For now, we'll assume all pairs are valid if they have a status field set to "TRADING"
    return True


def format_trading_pair(trading_pair: str) -> str:
    """
    Format the trading pair to match QTX expected format.
    :param trading_pair: the trading pair in Hummingbot format (e.g., "BTC-USDT")
    :return: the trading pair in QTX format (e.g., "btcusdt")
    """
    # Remove the hyphen and convert to lowercase
    trading_pair = trading_pair.replace("-", "").lower()
    return trading_pair


def convert_from_exchange_trading_pair(exchange_symbol: str) -> str:
    """
    Convert from QTX format to Hummingbot format.
    This is a simplified implementation that assumes the exchange symbol can be properly split.

    :param exchange_symbol: Symbol in exchange format (e.g., "btcusdt" or possibly with exchange prefix)
    :return: Trading pair in Hummingbot format (e.g., "BTC-USDT")
    """
    # Remove any exchange prefix if present
    if ":" in exchange_symbol:
        _, exchange_symbol = exchange_symbol.split(":", 1)

    # Attempt to identify base and quote currencies
    # Common quote currencies to try (from longest to shortest to avoid misidentification)
    common_quotes = ["usdt", "busd", "usdc", "dai", "wbtc", "eth", "btc", "bnb"]

    exchange_symbol = exchange_symbol.lower()
    for quote in common_quotes:
        if exchange_symbol.endswith(quote):
            base = exchange_symbol[: -len(quote)]
            return f"{base.upper()}-{quote.upper()}"

    # If we can't identify the split, return the original with a warning
    # In a real implementation, you would use the trading_pairs list or other references
    import logging

    logging.getLogger(__name__).warning(f"Could not parse exchange symbol: {exchange_symbol}. Returning as is.")
    return exchange_symbol.upper()


class QTXConfigMap(BaseConnectorConfigMap):
    connector: str = "qtx"

    qtx_host: str = Field(
        default=CONSTANTS.DEFAULT_UDP_HOST,
        client_data=None,
        json_schema_extra={
            "prompt": lambda cm: "Enter your QTX UDP host IP address",
            "prompt_on_new": True,
            "is_connect_key": True,
        },
    )
    qtx_port: int = Field(
        default=CONSTANTS.DEFAULT_UDP_PORT,
        client_data=None,
        json_schema_extra={
            "prompt": lambda cm: "Enter your QTX UDP port",
            "prompt_on_new": True,
            "is_connect_key": True,
        },
    )

    qtx_api_key: str = Field(
        default="fake_api_key",
        client_data=None,
        secret=True,
        json_schema_extra={
            "prompt": lambda cm: "Enter your QTX API key",
            "prompt_on_new": True,
            "is_connect_key": True,
        },
    )
    qtx_secret_key: str = Field(
        default="fake_secret_key",
        client_data=None,
        secret=True,
        json_schema_extra={
            "prompt": lambda cm: "Enter your QTX secret key",
            "prompt_on_new": True,
            "is_connect_key": True,
        },
    )

    model_config = ConfigDict(title="qtx")


KEYS = QTXConfigMap.model_construct()
