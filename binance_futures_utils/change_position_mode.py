#!/usr/bin/env python3

import hashlib
import hmac
import os
import sys
import time
from urllib.parse import urlencode

import requests

# ANSI color codes for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
BOLD = "\033[1m"
ENDC = "\033[0m"

FUTURES_BASE_URL = "https://fapi.binance.com"


def get_api_credentials():
    # Retrieve API credentials from environment variables
    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        print(f"{RED}{BOLD}Error: API credentials not found in environment variables{ENDC}")
        print("  export BINANCE_API_KEY='your_api_key'")
        print("  export BINANCE_API_SECRET='your_api_secret'")
        sys.exit(1)
    return api_key, api_secret


def sign_request(params, secret):
    # Generate HMAC SHA256 signature for Binance API
    query_string = urlencode(params)
    signature = hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query_string}&signature={signature}"


def make_signed_request(endpoint, params=None, method="GET"):
    # Make a signed request to the Binance Futures API
    api_key, api_secret = get_api_credentials()
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    signed_query = sign_request(params, api_secret)
    url = f"{FUTURES_BASE_URL}{endpoint}?{signed_query}"
    headers = {"X-MBX-APIKEY": api_key}
    
    if method == "GET":
        response = requests.get(url, headers=headers)
    elif method == "POST":
        response = requests.post(url, headers=headers)
    elif method == "DELETE":
        response = requests.delete(url, headers=headers)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")
    
    if response.status_code == 200:
        return response.json()
    else:
        print(f"{RED}Error calling {endpoint}: Status {response.status_code}{ENDC}")
        print(f"Response: {response.text}")
        return None


def get_current_position_mode():
    """Get the current position mode"""
    result = make_signed_request("/fapi/v1/positionSide/dual")
    if result:
        is_hedge_mode = result.get("dualSidePosition", False)
        return "HEDGE" if is_hedge_mode else "ONE-WAY"
    return None


def close_all_positions():
    """Close all open positions before changing mode"""
    print(f"\n{YELLOW}Checking for open positions...{ENDC}")
    
    # Get all positions
    positions = make_signed_request("/fapi/v2/positionRisk")
    if not positions:
        print(f"{RED}Failed to get positions{ENDC}")
        return False
    
    # Filter active positions
    active_positions = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
    
    if not active_positions:
        print(f"{GREEN}No open positions found{ENDC}")
        return True
    
    print(f"{YELLOW}Found {len(active_positions)} open positions that need to be closed:{ENDC}")
    for pos in active_positions:
        symbol = pos.get("symbol", "")
        amount = float(pos.get("positionAmt", 0))
        side = "LONG" if amount > 0 else "SHORT"
        print(f"  - {symbol}: {side} {abs(amount)} contracts")
    
    return False


def cancel_all_orders():
    """Cancel all open orders before changing mode"""
    print(f"\n{YELLOW}Checking for open orders...{ENDC}")
    
    # Get all open orders
    orders = make_signed_request("/fapi/v1/openOrders")
    if orders is None:
        print(f"{RED}Failed to get open orders{ENDC}")
        return False
    
    if not orders:
        print(f"{GREEN}No open orders found{ENDC}")
        return True
    
    print(f"{YELLOW}Found {len(orders)} open orders{ENDC}")
    print(f"{YELLOW}Cancelling all open orders...{ENDC}")
    
    # Cancel all orders
    result = make_signed_request("/fapi/v1/allOpenOrders", method="DELETE")
    if result:
        print(f"{GREEN}Successfully cancelled all open orders{ENDC}")
        return True
    else:
        print(f"{RED}Failed to cancel all orders{ENDC}")
        return False


def change_position_mode(target_mode):
    """Change position mode to target mode (ONE-WAY or HEDGE)"""
    # Convert to boolean for API
    dual_side_position = True if target_mode == "HEDGE" else False
    
    print(f"\n{YELLOW}Changing position mode to {target_mode}...{ENDC}")
    
    params = {"dualSidePosition": "true" if dual_side_position else "false"}
    result = make_signed_request("/fapi/v1/positionSide/dual", params, method="POST")
    
    if result:
        print(f"{GREEN}Successfully changed position mode to {target_mode}{ENDC}")
        return True
    else:
        print(f"{RED}Failed to change position mode{ENDC}")
        return False


def main():
    """Main function to change position mode"""
    print(f"{BLUE}{BOLD}=== Binance Futures Position Mode Changer ==={ENDC}")
    
    # Get current mode
    current_mode = get_current_position_mode()
    if not current_mode:
        print(f"{RED}Failed to get current position mode{ENDC}")
        sys.exit(1)
    
    print(f"\nCurrent position mode: {YELLOW}{BOLD}{current_mode}{ENDC}")
    
    # Ask user what mode they want
    print(f"\n{BLUE}Select target position mode:{ENDC}")
    print(f"  1. {BLUE}ONE-WAY{ENDC} (single position per symbol)")
    print(f"  2. {YELLOW}HEDGE{ENDC} (separate LONG and SHORT positions)")
    print(f"  3. {RED}Cancel{ENDC}")
    
    choice = input(f"\n{BOLD}Enter your choice (1-3): {ENDC}").strip()
    
    if choice == "1":
        target_mode = "ONE-WAY"
    elif choice == "2":
        target_mode = "HEDGE"
    elif choice == "3":
        print(f"{YELLOW}Operation cancelled{ENDC}")
        sys.exit(0)
    else:
        print(f"{RED}Invalid choice{ENDC}")
        sys.exit(1)
    
    # Check if already in target mode
    if current_mode == target_mode:
        print(f"\n{YELLOW}Already in {target_mode} mode. No change needed.{ENDC}")
        sys.exit(0)
    
    print(f"\n{BOLD}Switching from {current_mode} to {target_mode} mode{ENDC}")
    
    # Pre-checks
    print(f"\n{BLUE}{BOLD}Pre-flight checks:{ENDC}")
    
    # Cancel all orders first
    if not cancel_all_orders():
        print(f"{RED}Please cancel all orders manually before changing position mode{ENDC}")
        sys.exit(1)
    
    # Check for open positions
    if not close_all_positions():
        print(f"\n{RED}{BOLD}ERROR: Cannot change position mode with open positions{ENDC}")
        print(f"{YELLOW}Please close all positions before changing mode{ENDC}")
        print(f"\nYou can use the cancel_all_binance_positions.py script to close all positions")
        sys.exit(1)
    
    # Confirm with user
    print(f"\n{YELLOW}{BOLD}WARNING:{ENDC}")
    print(f"  - This will change your account's position mode from {current_mode} to {target_mode}")
    print(f"  - This affects how orders and positions are managed")
    print(f"  - Make sure you understand the implications")
    
    confirm = input(f"\n{BOLD}Are you sure you want to proceed? (yes/no): {ENDC}").strip().lower()
    
    if confirm != "yes":
        print(f"{YELLOW}Operation cancelled{ENDC}")
        sys.exit(0)
    
    # Change the mode
    if change_position_mode(target_mode):
        # Verify the change
        new_mode = get_current_position_mode()
        if new_mode == target_mode:
            print(f"\n{GREEN}{BOLD}✓ Position mode successfully changed to {target_mode}{ENDC}")
        else:
            print(f"\n{RED}Warning: Mode change reported success but verification failed{ENDC}")
            print(f"Current mode: {new_mode}")
    else:
        print(f"\n{RED}{BOLD}Failed to change position mode{ENDC}")
        sys.exit(1)


if __name__ == "__main__":
    main()