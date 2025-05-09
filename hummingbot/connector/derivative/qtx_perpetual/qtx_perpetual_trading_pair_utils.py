#!/usr/bin/env python

"""
Centralized utilities for trading pair conversions in the QTX perpetual connector.

This module consolidates all trading pair conversion functions that were previously
scattered across web_utils.py and utils.py. This eliminates redundancy and ensures

consistent behavior throughout the connector.

These utilities are critical for translating between:
1. Hummingbot format (e.g., "BTC-USDT")
2. QTX UDP format (e.g., "binance-futures:btcusdt")
3. Binance API format (e.g., "BTCUSDT")
"""

from typing import List, Optional


def format_trading_pair(trading_pair: str) -> str:
    """
    Basic formatting: Converts Hummingbot trading pair to lowercase without separator
    Example: BTC-USDT -> btcusdt

    This is the foundation for other conversion functions.
    """
    return trading_pair.replace("-", "").lower()


def convert_to_binance_trading_pair(trading_pair: str) -> str:
    """
    Convert from Hummingbot trading pair format to Binance format.
    Example: BTC-USDT -> BTCUSDT

    This format is used for API calls to Binance.
    """
    return trading_pair.replace("-", "")


def convert_from_binance_trading_pair(binance_symbol: str, trading_pairs: Optional[List[str]] = None) -> str:
    """
    Convert from Binance format to Hummingbot trading pair format.
    Example: BTCUSDT -> BTC-USDT

    :param binance_symbol: Trading pair in Binance format
    :param trading_pairs: List of available trading pairs to match against (optional)
    :return: Trading pair in Hummingbot format
    """
    # First try direct match with trading_pairs if provided
    if trading_pairs:
        for tp in trading_pairs:
            if binance_symbol.upper() == tp.replace("-", "").upper():
                return tp

    # Common quote currencies in perpetual futures (from longest to shortest to avoid misidentification)
    quote_currencies = ["USDT", "BUSD", "USDC", "USD", "DAI", "BTC", "ETH"]

    # Convert to uppercase for standard processing
    binance_symbol = binance_symbol.upper()

    # Try to identify where to insert the dash by looking for common quote currencies
    for quote in quote_currencies:
        if binance_symbol.endswith(quote):
            base = binance_symbol[:-len(quote)]
            return f"{base}-{quote}"

    # If no clear split point is found, make a best guess
    # Usually the last 3-4 characters are the quote currency
    if len(binance_symbol) > 5:
        # Try to guess with common lengths (4 for USDT, 3 for BTC, etc.)
        for split_point in [4, 3]:
            if len(binance_symbol) > split_point:
                base = binance_symbol[:-split_point]
                quote = binance_symbol[-split_point:]
                return f"{base}-{quote}"

    # If all else fails, just return the original symbol
    return binance_symbol


def convert_to_qtx_trading_pair(trading_pair: str) -> str:
    """
    Convert from Hummingbot trading pair format to QTX UDP format.
    Example: BTC-USDT -> binance-futures:btcusdt

    This format is used specifically for QTX UDP market data subscription.
    """
    formatted_pair = format_trading_pair(trading_pair)
    return f"binance-futures:{formatted_pair}"


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

    # Convert using the Binance symbol conversion since underlying format is the same
    return convert_from_binance_trading_pair(symbol, trading_pairs)


# Aliases for backward compatibility - these names should be used with caution
# and eventually deprecated
convert_to_exchange_trading_pair = convert_to_qtx_trading_pair
convert_from_exchange_trading_pair = convert_from_qtx_trading_pair
