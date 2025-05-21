#!/usr/bin/env python

"""
Trading pair conversion utilities for QTX perpetual connector.
Handles conversions between Hummingbot format (BTC-USDT) and QTX UDP format (binance-futures:btcusdt).
"""

from typing import List, Optional


def convert_to_qtx_trading_pair(trading_pair: str, exchange_name: str = None) -> str:
    """
    Converts Hummingbot format to QTX UDP format (BTC-USDT -> binance-futures:btcusdt)
    
    :param trading_pair: Trading pair in Hummingbot format
    :param exchange_name: Exchange name required for QTX prefix
    :return: Trading pair in QTX format
    :raises: ValueError if exchange_name is missing
    """
    if exchange_name is None:
        raise ValueError("exchange_name must be provided for QTX trading pair conversion")

    formatted_pair = trading_pair.replace("-", "").lower()
    return f"{exchange_name}:{formatted_pair}"


def convert_from_qtx_trading_pair(qtx_symbol: str, trading_pairs: Optional[List[str]] = None) -> str:
    """
    Converts QTX UDP format to Hummingbot format (binance-futures:btcusdt -> BTC-USDT)
    
    :param qtx_symbol: Trading pair in QTX format
    :param trading_pairs: Optional list of known pairs for direct matching
    :return: Trading pair in Hummingbot format
    """
    # Extract symbol part
    if ":" in qtx_symbol:
        _, symbol = qtx_symbol.split(":", 1)
    else:
        symbol = qtx_symbol

    symbol = symbol.upper()

    # Try direct match with available pairs
    if trading_pairs:
        for tp in trading_pairs:
            if symbol == tp.replace("-", "").upper():
                return tp

    # Common quote currencies
    quote_currencies = ["USDT", "BUSD", "USDC", "USD", "DAI", "BTC", "ETH"]

    # Find split point using known quote currencies
    for quote in quote_currencies:
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            return f"{base}-{quote}"

    # Best-effort guess for unknown pairs
    if len(symbol) > 5:
        for split_point in [4, 3]:
            if len(symbol) > split_point:
                base = symbol[:-split_point]
                quote = symbol[-split_point:]
                return f"{base}-{quote}"

    return symbol
