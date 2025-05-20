#!/usr/bin/env python3
"""
Binance Futures Position and Order Checker
Retrieves and displays current positions and open orders
Part of binance_futures_utils collection
"""

import hmac
import hashlib
import time
import json
import os
import sys
from urllib.parse import urlencode
import requests
import datetime
from tabulate import tabulate  # You may need to install this: pip install tabulate

# ANSI color codes for better output
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
BOLD = '\033[1m'
ENDC = '\033[0m'

# Base URLs
FUTURES_BASE_URL = "https://fapi.binance.com"

# Taiwan timezone offset (UTC+8)
TAIWAN_TIMEZONE_OFFSET = 8 * 60 * 60  # 8 hours in seconds

def get_api_credentials():
    """Get API credentials from environment variables"""
    api_key = os.environ.get('BINANCE_API_KEY')
    api_secret = os.environ.get('BINANCE_API_SECRET')
    
    if not api_key or not api_secret:
        print(f"{RED}{BOLD}Error: API credentials not found in environment variables{ENDC}")
        print(f"Please set the following environment variables:")
        print(f"  export BINANCE_API_KEY='your_api_key'")
        print(f"  export BINANCE_API_SECRET='your_api_secret'")
        sys.exit(1)
    
    return api_key, api_secret

def sign_request(params, secret):
    """Generate HMAC SHA256 signature for a request"""
    query_string = urlencode(params)
    signature = hmac.new(
        secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return f"{query_string}&signature={signature}"

def make_signed_request(endpoint, params=None):
    """Make a signed request to Binance Futures API"""
    api_key, api_secret = get_api_credentials()
    
    if params is None:
        params = {}
    
    # Add timestamp to params
    params['timestamp'] = int(time.time() * 1000)
    
    # Generate signed query string
    signed_query = sign_request(params, api_secret)
    
    # Construct full URL
    url = f"{FUTURES_BASE_URL}{endpoint}?{signed_query}"
    
    # Set headers with API key
    headers = {'X-MBX-APIKEY': api_key}
    
    # Make request
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        return response.json()
    else:
        print(f"{RED}Error calling {endpoint}: Status {response.status_code}{ENDC}")
        print(f"Response: {response.text}")
        return None

def get_position_mode():
    """Get the current position mode (Hedge Mode or One-Way Mode)"""
    print(f"{BLUE}{BOLD}Checking Position Mode...{ENDC}")
    
    position_mode = make_signed_request("/fapi/v1/positionSide/dual")
    
    if position_mode:
        is_hedge_mode = position_mode.get('dualSidePosition', False)
        mode_name = "HEDGE MODE" if is_hedge_mode else "ONE-WAY MODE"
        mode_color = YELLOW if is_hedge_mode else BLUE
        print(f"Position Mode: {mode_color}{BOLD}{mode_name}{ENDC}")
        
        if is_hedge_mode:
            print(f"  {YELLOW}In Hedge Mode, you can hold both long and short positions simultaneously.{ENDC}")
            print(f"  {YELLOW}- BUY with Position Side LONG: Open or add to long position")
            print(f"  - SELL with Position Side LONG: Close or reduce long position")
            print(f"  - SELL with Position Side SHORT: Open or add to short position")
            print(f"  - BUY with Position Side SHORT: Close or reduce short position{ENDC}")
        else:
            print(f"  {BLUE}In One-Way Mode, you can only hold either a long or short position for each symbol.")
            print(f"  - BUY: Open long or reduce short position")
            print(f"  - SELL: Open short or reduce long position{ENDC}")
        
        print(f"\nReduce Only:")
        print(f"  YES: This order can only reduce your existing position, never increase it")
        print(f"  NO: This order can increase your position size or open a new position")
        
        return position_mode
    else:
        print(f"{RED}Failed to retrieve position mode{ENDC}")
        return None

def get_leverage_brackets():
    """Get leverage brackets for all symbols"""
    leverage_info = make_signed_request("/fapi/v1/leverageBracket")
    
    if leverage_info:
        # Create a mapping of symbol to max leverage
        leverage_map = {}
        for item in leverage_info:
            symbol = item.get('symbol')
            if symbol:
                brackets = item.get('brackets', [])
                if brackets:
                    max_leverage = brackets[0].get('initialLeverage', 20)
                    leverage_map[symbol] = max_leverage
        
        return leverage_map
    else:
        print(f"{RED}Failed to retrieve leverage information{ENDC}")
        return {}

def get_account_info():
    """Get account information from Binance Futures"""
    print(f"{BLUE}{BOLD}Checking Account Information...{ENDC}")
    
    account_info = make_signed_request("/fapi/v2/account")
    
    if account_info:
        print(f"{GREEN}Successfully retrieved account information{ENDC}")
        print(f"  Fee tier: {account_info.get('feeTier', 'Unknown')}")
        print(f"  Can trade: {account_info.get('canTrade', False)}")
        print(f"  Can deposit: {account_info.get('canDeposit', False)}")
        print(f"  Can withdraw: {account_info.get('canWithdraw', False)}")
        
        # Display account balances
        total_balance = float(account_info.get('totalWalletBalance', '0'))
        unrealized_pnl = float(account_info.get('totalUnrealizedProfit', '0'))
        margin_balance = float(account_info.get('totalMarginBalance', '0'))
        
        print(f"  Total Balance: {GREEN if total_balance > 0 else ''}${total_balance:.2f}{ENDC}")
        pnl_color = GREEN if unrealized_pnl > 0 else RED if unrealized_pnl < 0 else ""
        print(f"  Unrealized PnL: {pnl_color}${unrealized_pnl:.2f}{ENDC}")
        print(f"  Margin Balance: ${margin_balance:.2f}")
        
        # Available balance
        available_balance = float(account_info.get('availableBalance', '0'))
        print(f"  Available Balance: ${available_balance:.2f}")
        
        # Display assets with balances
        assets = account_info.get('assets', [])
        assets_with_balance = [a for a in assets if float(a.get('walletBalance', 0)) > 0]
        
        if assets_with_balance:
            print(f"\n{BLUE}{BOLD}Assets with Balance:{ENDC}")
            asset_data = []
            for asset in assets_with_balance:
                asset_data.append([
                    asset.get('asset', ''),
                    asset.get('walletBalance', '0'),
                    asset.get('unrealizedProfit', '0'),
                    asset.get('marginBalance', '0')
                ])
            print(tabulate(asset_data, headers=["Asset", "Wallet Balance", "Unrealized P/L", "Margin Balance"], 
                           tablefmt="pretty"))
        else:
            print(f"\n{YELLOW}No assets with balance found{ENDC}")
        
        return account_info
    else:
        print(f"{RED}Failed to retrieve account information{ENDC}")
        return None

def get_taiwan_time_str(timestamp_ms):
    """Convert millisecond timestamp to Taiwan time string (UTC+8)"""
    # Convert to seconds
    timestamp_seconds = timestamp_ms / 1000
    
    # Add Taiwan timezone offset
    taiwan_timestamp = timestamp_seconds + TAIWAN_TIMEZONE_OFFSET
    
    # Format time string
    taiwan_time = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(taiwan_timestamp))
    
    return taiwan_time

def get_positions():
    """Get current positions from Binance Futures"""
    print(f"\n{BLUE}{BOLD}Checking Current Positions...{ENDC}")
    
    # Get position information
    position_info = make_signed_request("/fapi/v2/positionRisk")
    
    if position_info:
        # Get account position mode
        position_mode = make_signed_request("/fapi/v1/positionSide/dual")
        is_hedge_mode = position_mode and position_mode.get('dualSidePosition', False)
        
        # Filter positions with non-zero amount
        active_positions = [p for p in position_info if float(p.get('positionAmt', 0)) != 0]
        
        if active_positions:
            print(f"{GREEN}Found {len(active_positions)} active positions:{ENDC}")
            position_data = []
            for position in active_positions:
                # Calculate unrealized PnL percentage
                entry_price = float(position.get('entryPrice', 0))
                mark_price = float(position.get('markPrice', 0))
                position_amt = float(position.get('positionAmt', 0))
                leverage = float(position.get('leverage', 1))
                
                side = "LONG" if position_amt > 0 else "SHORT"
                unrealized_pnl = float(position.get('unRealizedProfit', 0))
                
                # Calculate PnL percentage based on position direction
                if entry_price > 0 and position_amt != 0:
                    if side == "LONG":
                        pnl_pct = ((mark_price - entry_price) / entry_price) * 100 * leverage
                    else:
                        pnl_pct = ((entry_price - mark_price) / entry_price) * 100 * leverage
                else:
                    pnl_pct = 0
                
                # Format direction with color
                direction = f"{GREEN}{side}{ENDC}" if side == "LONG" else f"{RED}{side}{ENDC}"
                
                # Format PnL with color
                pnl_color = GREEN if unrealized_pnl > 0 else RED if unrealized_pnl < 0 else ""
                pnl_formatted = f"{pnl_color}{unrealized_pnl} ({pnl_pct:.2f}%){ENDC}"
                
                # Get position side if in hedge mode
                position_side = position.get('positionSide', 'BOTH')
                position_side_formatted = f"{YELLOW}{position_side}{ENDC}" if is_hedge_mode else "BOTH"
                
                # Add margin type info
                margin_type = position.get('marginType', '')
                if margin_type.upper() == 'ISOLATED':
                    margin_type_formatted = f"{YELLOW}ISOLATED{ENDC}"
                    isolated_margin = position.get('isolatedMargin', '0')
                    isolated_wallet = position.get('isolatedWallet', '0')
                    margin_info = f"{margin_type_formatted} ({isolated_margin})"
                else:
                    margin_type_formatted = f"{BLUE}CROSS{ENDC}"
                    margin_info = margin_type_formatted
                
                # Update time in Taiwan time
                update_time_ms = int(position.get('updateTime', 0))
                update_time_str = get_taiwan_time_str(update_time_ms)
                
                position_data.append([
                    position.get('symbol', ''),
                    direction,
                    position_side_formatted,
                    abs(position_amt),
                    position.get('entryPrice', '0'),
                    position.get('markPrice', '0'),
                    pnl_formatted,
                    position.get('leverage', '1') + "x",
                    margin_info,
                    position.get('liquidationPrice', 'N/A'),
                    update_time_str
                ])
            
            print(tabulate(position_data, 
                          headers=["Symbol", "Side", "Position Side", "Size", "Entry Price", "Mark Price", 
                                  "Unrealized PnL", "Leverage", "Margin Type", "Liquidation Price", "Update Time (TW)"], 
                          tablefmt="pretty"))
        else:
            print(f"{YELLOW}No active positions found{ENDC}")
        
        return position_info
    else:
        print(f"{RED}Failed to retrieve position information{ENDC}")
        return None

def get_open_orders():
    """Get open orders from Binance Futures"""
    print(f"\n{BLUE}{BOLD}Checking Open Orders...{ENDC}")
    
    # Get position mode
    position_mode = make_signed_request("/fapi/v1/positionSide/dual")
    is_hedge_mode = position_mode and position_mode.get('dualSidePosition', False)
    
    # Get open orders
    open_orders = make_signed_request("/fapi/v1/openOrders")
    
    if open_orders is not None:  # Could be an empty list []
        if open_orders:
            print(f"{GREEN}Found {len(open_orders)} open orders:{ENDC}")
            order_data = []
            for order in open_orders:
                # Format side with color
                side = order.get('side', '')
                side_formatted = f"{GREEN}{side}{ENDC}" if side == "BUY" else f"{RED}{side}{ENDC}"
                
                # Format order status
                status = order.get('status', '')
                status_color = YELLOW if status in ["NEW", "PARTIALLY_FILLED"] else GREEN
                status_formatted = f"{status_color}{status}{ENDC}"
                
                # Position side for hedge mode
                position_side = order.get('positionSide', 'BOTH')
                position_side_formatted = f"{YELLOW}{position_side}{ENDC}" if is_hedge_mode else "BOTH"
                
                # Reduce only
                reduce_only = order.get('reduceOnly', False)
                reduce_only_str = f"{YELLOW}YES{ENDC}" if reduce_only else "NO"
                
                # Time in Taiwan time
                time_ms = int(order.get('time', 0))
                time_str = get_taiwan_time_str(time_ms)
                
                order_data.append([
                    time_str,
                    order.get('symbol', ''),
                    side_formatted,
                    position_side_formatted,
                    order.get('type', ''),
                    order.get('price', '0'),
                    order.get('origQty', '0'),
                    order.get('executedQty', '0'),
                    reduce_only_str,
                    status_formatted,
                    order.get('orderId', '')
                ])
            
            print(tabulate(order_data, 
                           headers=["Time (TW)", "Symbol", "Side", "Position Side", "Type", "Price", "Original Qty", 
                                    "Executed Qty", "Reduce Only", "Status", "Order ID"], 
                           tablefmt="pretty"))
        else:
            print(f"{YELLOW}No open orders found{ENDC}")
        
        return open_orders
    else:
        print(f"{RED}Failed to retrieve open orders{ENDC}")
        return None

def get_order_history(limit=10):
    """Get recent order history from Binance Futures"""
    print(f"\n{BLUE}{BOLD}Checking Recent Order History (last {limit} orders)...{ENDC}")
    
    # Get position mode
    position_mode = make_signed_request("/fapi/v1/positionSide/dual")
    is_hedge_mode = position_mode and position_mode.get('dualSidePosition', False)
    
    # Get order history
    params = {'limit': limit}
    order_history = make_signed_request("/fapi/v1/allOrders", params)
    
    if order_history:
        # Sort by time, most recent first
        order_history.sort(key=lambda x: int(x.get('time', 0)), reverse=True)
        
        # Limit to specified number
        recent_orders = order_history[:limit]
        
        if recent_orders:
            print(f"{GREEN}Found {len(recent_orders)} recent orders:{ENDC}")
            order_data = []
            for order in recent_orders:
                # Format side with color
                side = order.get('side', '')
                side_formatted = f"{GREEN}{side}{ENDC}" if side == "BUY" else f"{RED}{side}{ENDC}"
                
                # Format order status
                status = order.get('status', '')
                status_color = GREEN if status in ["FILLED", "CANCELED"] else YELLOW
                status_formatted = f"{status_color}{status}{ENDC}"
                
                # Position side for hedge mode
                position_side = order.get('positionSide', 'BOTH')
                position_side_formatted = f"{YELLOW}{position_side}{ENDC}" if is_hedge_mode else "BOTH"
                
                # Reduce only
                reduce_only = order.get('reduceOnly', False)
                reduce_only_str = f"{YELLOW}YES{ENDC}" if reduce_only else "NO"
                
                # Format time in Taiwan time
                time_ms = int(order.get('time', 0))
                time_str = get_taiwan_time_str(time_ms)
                
                order_data.append([
                    time_str,
                    order.get('symbol', ''),
                    side_formatted,
                    position_side_formatted,
                    order.get('type', ''),
                    order.get('price', '0'),
                    order.get('origQty', '0'),
                    order.get('executedQty', '0'),
                    reduce_only_str,
                    status_formatted
                ])
            
            print(tabulate(order_data, 
                           headers=["Time (TW)", "Symbol", "Side", "Position Side", "Type", "Price", "Original Qty", 
                                    "Executed Qty", "Reduce Only", "Status"], 
                           tablefmt="pretty"))
        else:
            print(f"{YELLOW}No recent orders found{ENDC}")
        
        return recent_orders
    else:
        print(f"{RED}Failed to retrieve order history{ENDC}")
        return None

def main():
    """Main function to check positions and orders"""
    print(f"{BLUE}{BOLD}=== Binance Futures Position and Order Checker ==={ENDC}")
    
    # Check if we have tabulate installed, if not provide installation instructions
    try:
        import tabulate
    except ImportError:
        print(f"{YELLOW}The tabulate library is not installed. For better formatting, install it with:{ENDC}")
        print("pip install tabulate")
        print("Continuing with basic formatting...")
    
    # Get position mode first
    position_mode = get_position_mode()
    
    # Get account, position and order information
    account_info = get_account_info()
    positions = get_positions()
    open_orders = get_open_orders()
    recent_orders = get_order_history(10)
    
    print(f"\n{BLUE}{BOLD}=== Check complete ==={ENDC}")

if __name__ == "__main__":
    main()