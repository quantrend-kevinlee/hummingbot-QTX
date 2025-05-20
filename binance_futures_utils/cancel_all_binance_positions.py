#!/usr/bin/env python3
"""
Binance Futures Position Cancellation Script
Closes all open positions on Binance Futures
Part of binance_futures_utils collection
"""

import asyncio
import logging
import os
import sys
from decimal import Decimal
from typing import Dict, Any, List, Optional
import hmac
import hashlib
import time
import json
from urllib.parse import urlencode
import aiohttp  # Using aiohttp for async requests

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

FUTURES_BASE_URL = "https://fapi.binance.com"

def get_api_credentials():
    """Get API credentials from environment variables"""
    api_key = os.environ.get('BINANCE_API_KEY')
    api_secret = os.environ.get('BINANCE_API_SECRET')
    
    if not api_key or not api_secret:
        logger.error("API credentials not found in environment variables")
        logger.error("Please set the following environment variables:")
        logger.error("  export BINANCE_API_KEY='your_api_key'")
        logger.error("  export BINANCE_API_SECRET='your_api_secret'")
        sys.exit(1)
    
    return api_key, api_secret

def _sign_request(params: Dict[str, Any], secret: str) -> str:
    """Generate HMAC SHA256 signature for a request."""
    query_string = urlencode(params)
    signature = hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return signature

async def _make_signed_request(
    session: aiohttp.ClientSession, method: str, endpoint: str, params: Optional[Dict[str, Any]] = None
) -> Optional[Any]:
    """Make a signed request to Binance Futures API using aiohttp."""
    api_key, api_secret = get_api_credentials()
    
    if params is None:
        params = {}

    params["timestamp"] = int(time.time() * 1000)
    signature = _sign_request(params, api_secret)
    params["signature"] = signature

    headers = {"X-MBX-APIKEY": api_key}
    url = f"{FUTURES_BASE_URL}{endpoint}"

    try:
        if method.upper() == "GET":
            async with session.get(url, headers=headers, params=params) as response:
                response_text = await response.text()
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(
                        f"Error calling {method} {endpoint}: Status {response.status}, Response: {response_text}"
                    )
                    return None
        elif method.upper() == "POST":
            async with session.post(url, headers=headers, data=params) as response:
                response_text = await response.text()
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(
                        f"Error calling {method} {endpoint}: Status {response.status}, Response: {response_text}"
                    )
                    return None
        else:
            logger.error(f"Unsupported HTTP method: {method}")
            return None
    except aiohttp.ClientError as e:
        logger.error(f"HTTP Client Error calling {method} {endpoint}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error during request to {method} {endpoint}: {e}")
        return None

async def cancel_all_positions():
    """
    Connects to Binance Futures, fetches all open positions, and attempts to close them using aiohttp.
    """
    async with aiohttp.ClientSession() as session:
        try:
            logger.info("Fetching account information and open positions...")
            positions_info: Optional[List[Dict[str, Any]]] = await _make_signed_request(
                session, "GET", "/fapi/v2/positionRisk"
            )

            if positions_info is None:
                logger.error("Failed to retrieve position information.")
                return

            open_positions = [pos for pos in positions_info if Decimal(pos.get("positionAmt", "0")) != Decimal("0")]

            if not open_positions:
                logger.info("No open positions found.")
                return

            logger.info(f"Found {len(open_positions)} open position(s) to close.")

            for position in open_positions:
                symbol = position["symbol"]
                position_amt_str = position["positionAmt"]
                position_amt = Decimal(position_amt_str)

                side_to_close = "SELL" if position_amt > 0 else "BUY"
                quantity_to_close = abs(position_amt)
                current_position_side = "LONG" if position_amt > 0 else "SHORT"

                logger.info(
                    f"Attempting to close {current_position_side} position for {symbol}: Quantity {quantity_to_close}"
                )

                # Get the position mode
                account_info = await _make_signed_request(session, "GET", "/fapi/v1/positionSide/dual")
                hedge_mode = account_info and account_info.get("dualSidePosition", False)
                
                # Prepare order parameters based on position mode
                if hedge_mode:
                    # In hedge mode, we need positionSide parameter
                    order_params = {
                        "symbol": symbol,
                        "side": side_to_close,
                        "type": "MARKET",
                        "quantity": str(quantity_to_close),
                        "positionSide": current_position_side
                    }
                else:
                    # In one-way mode, we use reduceOnly
                    order_params = {
                        "symbol": symbol,
                        "side": side_to_close,
                        "type": "MARKET",
                        "quantity": str(quantity_to_close),
                        "reduceOnly": "true"
                    }

                order_result = await _make_signed_request(session, "POST", "/fapi/v1/order", params=order_params)

                if order_result and order_result.get("orderId"):
                    logger.info(
                        f"Successfully placed MARKET {side_to_close} order for {symbol} to close position. Order ID: {order_result['orderId']}"
                    )
                else:
                    logger.error(f"Failed to place order for {symbol}. Response: {order_result}")

        except Exception as e:
            logger.error(f"An unexpected error occurred in cancel_all_positions: {e}", exc_info=True)
        finally:
            logger.info("Processing complete. Ensure API key and secret were correctly set if issues persist.")

async def main():
    await cancel_all_positions()

if __name__ == "__main__":
    asyncio.run(main())