#!/usr/bin/env python3
"""
qtx_udp_logger.py

Standalone script to connect to a QTX UDP feed, subscribe to symbols, receive messages for a given duration,
log the raw bytes (hex) along with a timestamp, then exit.
Usage: python qtx_udp_logger.py --host 172.30.2.221 --port 8080 --duration 5.0 --symbols binance:ethusdt,binance:btcusdt,binance:solusdt
"""
import argparse
import binascii
import logging
import socket
import struct
import sys
import time

# Configuration constants
MAX_SYMBOLS = 100
MAX_SYMBOL_LEN = 64
UDP_SIZE = 65536
DEFAULT_SYMBOLS = ["binance:ethusdt", "binance:btcusdt"]

class Subscription:
    """Represents a symbol subscription"""
    def __init__(self, symbol, index):
        self.symbol = symbol
        self.index = index


def print_status(subscriptions_list):
    """Print the current subscription status"""
    logging.info("=== Current Status ===")
    logging.info(f"Total symbols: {len(subscriptions_list)}")
    for sub in subscriptions_list:
        logging.info(f"Symbol: {sub.symbol} (index: {sub.index})")
    logging.info("==================")


def main():
    parser = argparse.ArgumentParser(description="QTX UDP Feed Logger")
    parser.add_argument("--host", type=str, default="172.30.2.221", help="QTX UDP host IP address")
    parser.add_argument("--port", type=int, default=8080, help="QTX UDP port")
    parser.add_argument("--duration", type=float, default=5.0, help="Duration in seconds to capture data")
    parser.add_argument("--buffer", type=int, default=UDP_SIZE, help="UDP receive buffer size")
    parser.add_argument("--output", type=str, default="qtx_udp_feed.log", help="Path to output log file")
    parser.add_argument("--symbols", type=str, default="binance:ethusdt,binance:btcusdt",
                        help="Comma-separated list of symbols to subscribe to")
    parser.add_argument("--min-symbols", type=int, default=0, 
                        help="Minimum number of successful symbol subscriptions required to continue")
    args = parser.parse_args()

    # Configure logging
    handlers = [logging.StreamHandler()]
    if args.output:
        handlers.append(logging.FileHandler(args.output, mode="w"))
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S", handlers=handlers
    )

    logging.info(f"Starting UDP logger to {args.host}:{args.port} for {args.duration}s")

    # Create a UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(True)  # Keep socket blocking during subscription phase
    sock.settimeout(2.0)    # Set timeout for subscription responses

    # Bind to any address on an automatic port
    sock.bind(('0.0.0.0', 0))

    # Subscribe to symbols
    symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    subscriptions_list = []  # List of Subscription objects
    subscription_count = 0   # Track subscription count

    for symbol in symbols:
        logging.info(f"Subscribing to symbol: {symbol}")
        try:
            # Send subscription request
            sock.sendto(symbol.encode(), (args.host, args.port))

            # ── Start ACK-loop ──
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    response, addr = sock.recvfrom(args.buffer)
                except socket.timeout:
                    logging.error(f"Timeout waiting for ACK for {symbol}")
                    break
                # ignore any packet not from the server
                if addr[0] != args.host:
                    continue
                # try decode as ASCII index
                try:
                    index = int(response.decode('utf-8').strip())
                    logging.info(f"Got text ACK {index} for {symbol}")
                    break
                except (UnicodeDecodeError, ValueError):
                    # not an index (probably market data) → keep looping
                    continue
            else:
                # loop fell through without break → no valid ACK
                logging.error(f"No valid ACK for {symbol} before timeout")
                continue
            # ── End ACK-loop ──

            # Store the subscription with its assigned index
            subscription = Subscription(symbol, index)
            subscriptions_list.append(subscription)
            subscription_count += 1
            logging.info(f"Successfully subscribed to {symbol} with index {index}")

        except socket.timeout:
            logging.error(f"Timeout while waiting for subscription response for {symbol}")
        except Exception as e:
            logging.error(f"Error subscribing to {symbol}: {e}")
        
        # Small delay before next subscription attempt
        time.sleep(0.1)

    if not subscriptions_list:
        logging.error("No successful subscriptions, exiting")
        sock.close()
        return 1

    # Print status of all subscriptions
    print_status(subscriptions_list)
    
    # Check if we meet the minimum required successful subscriptions
    if args.min_symbols > 0 and len(subscriptions_list) < args.min_symbols:
        logging.error(f"Only {len(subscriptions_list)} symbols subscribed successfully, minimum required is {args.min_symbols}")
        logging.error("Exiting due to insufficient successful subscriptions")
        sock.close()
        return 1
    
    # Create index to subscription mapping for quick lookups
    index_to_subscription = {sub.index: sub for sub in subscriptions_list}
    
    # Set to non-blocking for the data receiving phase
    sock.setblocking(False)
    
    start_ts = time.time()
    receive_count = 0
    symbol_message_counts = {sub.symbol: 0 for sub in subscriptions_list}
    
    logging.info("\nEntering main loop to receive market data...")
    logging.info(f"Will capture data for {args.duration} seconds...\n")
    
    try:
        while time.time() - start_ts < args.duration:
            try:
                data, addr = sock.recvfrom(args.buffer)
                if data:
                    receive_count += 1
                    
                    # Basic parsing of the message structure
                    if len(data) >= 48:  # Minimum size for Msg struct
                        try:
                            # Parse header
                            msg_type, index, tx_ms, event_ms, local_ns, sn_id = struct.unpack("<iiqqqq", data[:40])
                            
                            # Find the corresponding subscription
                            symbol = None
                            for sub in subscriptions_list:
                                if sub.index == index:
                                    symbol = sub.symbol
                                    break
                            
                            if symbol:
                                # Update message count for this symbol
                                symbol_message_counts[symbol] = symbol_message_counts.get(symbol, 0) + 1
                                
                                # Calculate latency
                                now = time.time_ns()
                                latency = now - local_ns
                                
                                # Process message based on message type
                                if msg_type == 2:  # Depth data
                                    if len(data) >= 56:  # Has additional header data
                                        asks_len, bids_len = struct.unpack("<qq", data[40:56])
                                        
                                        # Log depth info
                                        logging.info(f"{symbol}: depth, asks={asks_len}, bids={bids_len}, latency={latency} ns")
                                        
                                        if len(data) >= 56 + (asks_len + bids_len) * 16:
                                            levels_offset = 56
                                            
                                            # Print asks
                                            max_levels = 3
                                            asks_str = ""
                                            for i in range(min(asks_len, max_levels)):
                                                offset = levels_offset + i * 16
                                                price, size = struct.unpack("<dd", data[offset:offset+16])
                                                asks_str += f"{price:.8g}:{size:.8g} "
                                            logging.info(f"  asks: {asks_str.strip()}")
                                            
                                            # Print bids
                                            bids_str = ""
                                            for i in range(min(bids_len, max_levels)):
                                                offset = levels_offset + (asks_len + i) * 16
                                                price, size = struct.unpack("<dd", data[offset:offset+16])
                                                bids_str += f"{price:.8g}:{size:.8g} "
                                            logging.info(f"  bids: {bids_str.strip()}")
                                        else:
                                            logging.warning(f"Depth message too short for declared levels")
                                    else:
                                        logging.warning(f"Depth message (type 2) is too short for depth header")
                                
                                elif abs(msg_type) == 1:  # Ticker
                                    price, size = struct.unpack("<dd", data[40:56])
                                    side = "bid" if msg_type > 0 else "ask"
                                    # Log ticker data
                                    logging.info(f"{symbol}: ticker, {side}, price={price:.8g}, size={size:.8g}, latency={latency} ns")
                                
                                elif abs(msg_type) == 3:  # Trade
                                    price, size = struct.unpack("<dd", data[40:56])
                                    side = "buy" if msg_type > 0 else "sell"
                                    # Log trade data
                                    logging.info(f"{symbol}: trade, {side}, price={price:.8g}, size={size:.8g}, latency={latency} ns")
                                
                                else:
                                    logging.warning(f"{symbol}: unknown message type {msg_type}")
                            else:
                                logging.warning(f"Received message for unknown index: {index}")

                        except struct.error as se:
                            logging.error(f"Struct unpacking error: {se}")
                        except Exception as e:
                            logging.error(f"Error parsing message: {e}")
                    else:
                        logging.warning(f"Received message too short for header ({len(data)} bytes)")
                            
            except BlockingIOError:
                # No data available right now
                pass
            except Exception as e:
                logging.error(f"Error receiving UDP data: {e}")
                
            # Sleep to avoid busy loop
            time.sleep(0.001)

    except KeyboardInterrupt:
        logging.info("Received keyboard interrupt, exiting...")
    finally:
        logging.info(f"\nReceived {receive_count} messages. Exiting...")
        
        # Show per-symbol statistics
        for symbol, count in symbol_message_counts.items():
            logging.info(f"Symbol {symbol}: {count} messages received")
        
        # Unsubscribe from all symbols
        logging.info("Unsubscribing all symbols...")
        for sub in reversed(subscriptions_list):
            unsubscribe_msg = f"-{sub.symbol}"
            logging.info(f"Unsubscribing from symbol: {sub.symbol}")
            try:
                sock.setblocking(True)
                sock.settimeout(1.0)
                sock.sendto(unsubscribe_msg.encode(), (args.host, args.port))
                response, _ = sock.recvfrom(args.buffer)
                try:
                    response_text = response.decode().strip()
                    logging.info(f"Unsubscribe response for {sub.symbol}: {response_text}")
                except UnicodeDecodeError:
                    logging.info(f"Unsubscribe response for {sub.symbol} (binary): {response.hex()}")
            except Exception as e:
                logging.error(f"Error unsubscribing from {sub.symbol}: {e}")
        
        logging.info("Closing socket")
        sock.close()
    
    logging.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
