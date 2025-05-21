#!/usr/bin/env python

"""
Centralized utilities for trading pair conversions in the QTX perpetual connector.

This module handles conversions between:
1. Hummingbot format (e.g., "BTC-USDT")
2. QTX UDP format (e.g., "binance-futures:btcusdt")

Note: Conversions to/from exchange-specific formats (e.g., Binance's "BTCUSDT")
should be handled by the respective exchange's utility modules.
"""

from typing import List, Optional


def convert_to_qtx_trading_pair(trading_pair: str, exchange_name: str = None) -> str:
    """
    Convert from Hummingbot trading pair format to QTX UDP format.
    Example: BTC-USDT -> binance-futures:btcusdt

    This format is used specifically for QTX UDP market data subscription.

    :param trading_pair: Trading pair in Hummingbot format (e.g., "BTC-USDT")
    :param exchange_name: Exchange name to use in the prefix (REQUIRED, no default)
                         Must be the full exchange name as used by QTX (e.g., "binance-futures")
    :return: Trading pair in QTX format
    :raises: ValueError if exchange_name is not provided
    """
    if exchange_name is None:
        raise ValueError("exchange_name must be provided for QTX trading pair conversion")

    # Remove dash and convert to lowercase for QTX format
    formatted_pair = trading_pair.replace("-", "").lower()
    return f"{exchange_name}:{formatted_pair}"


def convert_from_qtx_trading_pair(qtx_symbol: str, trading_pairs: Optional[List[str]] = None) -> str:
    """
    Convert from QTX UDP format to Hummingbot trading pair format.
    Example: binance-futures:btcusdt -> BTC-USDT

    This handles QTX's unique format with exchange prefix.

    :param qtx_symbol: Trading pair in QTX format
    :param trading_pairs: List of available trading pairs to match against (optional)
    :return: Trading pair in Hummingbot format
    """
    # Extract the symbol part if it has an exchange prefix
    if ":" in qtx_symbol:
        _, symbol = qtx_symbol.split(":", 1)
    else:
        symbol = qtx_symbol

    # Convert to uppercase for processing
    symbol = symbol.upper()

    # First try direct match with trading_pairs if provided
    if trading_pairs:
        for tp in trading_pairs:
            if symbol == tp.replace("-", "").upper():
                return tp

    # Common quote currencies in perpetual futures (from longest to shortest to avoid misidentification)
    quote_currencies = ["USDT", "BUSD", "USDC", "USD", "DAI", "BTC", "ETH"]

    # Try to identify where to insert the dash by looking for common quote currencies
    for quote in quote_currencies:
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            return f"{base}-{quote}"

    # If no clear split point is found, make a best guess
    # Usually the last 3-4 characters are the quote currency
    if len(symbol) > 5:
        # Try to guess with common lengths (4 for USDT, 3 for BTC, etc.)
        for split_point in [4, 3]:
            if len(symbol) > split_point:
                base = symbol[:-split_point]
                quote = symbol[-split_point:]
                return f"{base}-{quote}"

    # If all else fails, return the symbol with uppercase
    return symbol
