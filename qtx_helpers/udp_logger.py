#!/usr/bin/env python3
"""
udp_logger.py

Enhanced standalone script to connect to a QTX UDP feed, subscribe to symbols, receive and log messages.
Features:
- Logs both raw (hex) and parsed message data
- Tracks sequence numbers for each symbol
- Logs ACK messages
- Supports both time duration and max message count limits
- Detailed statistics per message type

Usage: python udp_logger.py
Default settings: connects to 172.30.3.142:8080, subscribes to kucoin-futures:XBTUSDTM, runs for 300s
"""
import argparse
import logging
import os
import socket
import statistics
import struct
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

# Configuration constants
MAX_SYMBOLS = 100
MAX_SYMBOL_LEN = 64
UDP_SIZE = 65536


@dataclass
class Subscription:
    """Represents a symbol subscription with tracking data"""

    symbol: str
    index: int
    message_count: int = 0
    last_sequence: int = 0
    message_types: dict = None
    ticker_latencies: list = None
    depth_latencies: list = None
    trade_latencies: list = None
    last_sequences_by_type: dict = None
    missed_sequences_by_type: dict = None

    def __post_init__(self):
        if self.message_types is None:
            self.message_types = defaultdict(int)
        if self.ticker_latencies is None:
            self.ticker_latencies = []
        if self.depth_latencies is None:
            self.depth_latencies = []
        if self.trade_latencies is None:
            self.trade_latencies = []
        if self.last_sequences_by_type is None:
            self.last_sequences_by_type = defaultdict(int)
        if self.missed_sequences_by_type is None:
            self.missed_sequences_by_type = defaultdict(int)


def print_status(subscriptions_list):
    """Print the current subscription status"""
    logging.info("=== Current Subscription Status ===")
    logging.info(f"Total symbols: {len(subscriptions_list)}")
    for sub in subscriptions_list:
        logging.info(f"Symbol: {sub.symbol} (index: {sub.index})")
    logging.info("==============================")


def print_message_stats(subscriptions_list):
    """Print detailed message statistics for all subscriptions"""
    logging.info("\n=== Message Statistics ===")

    total_by_type = defaultdict(int)
    for sub in subscriptions_list:
        logging.info(f"Symbol: {sub.symbol} (index: {sub.index})")
        logging.info(f"  Total messages: {sub.message_count}")

        # Print message type breakdown
        for msg_type, count in sub.message_types.items():
            type_name = "UNKNOWN"
            if abs(msg_type) == 1:
                type_name = "TICKER (BID)" if msg_type > 0 else "TICKER (ASK)"
            elif msg_type == 2:
                type_name = "DEPTH"
            elif abs(msg_type) == 3:
                type_name = "TRADE (BUY)" if msg_type > 0 else "TRADE (SELL)"

            logging.info(f"  Type {msg_type} ({type_name}): {count} messages")
            total_by_type[msg_type] += count

        # Print missed sequences by type for this symbol
        if sub.missed_sequences_by_type:
            logging.info("  Missed sequences by type:")
            for msg_type, missed in sub.missed_sequences_by_type.items():
                type_name = "UNKNOWN"
                if abs(msg_type) == 1:
                    type_name = "TICKER (BID)" if msg_type > 0 else "TICKER (ASK)"
                elif msg_type == 2:
                    type_name = "DEPTH"
                elif abs(msg_type) == 3:
                    type_name = "TRADE (BUY)" if msg_type > 0 else "TRADE (SELL)"
                
                logging.info(f"    Type {msg_type} ({type_name}): {missed} missed")

        logging.info("")

    # Print totals across all symbols
    logging.info("=== Totals Across All Symbols ===")
    total_messages = sum(sub.message_count for sub in subscriptions_list)
    logging.info(f"Total messages: {total_messages}")

    for msg_type, count in total_by_type.items():
        type_name = "UNKNOWN"
        if abs(msg_type) == 1:
            type_name = "TICKER (BID)" if msg_type > 0 else "TICKER (ASK)"
        elif msg_type == 2:
            type_name = "DEPTH"
        elif abs(msg_type) == 3:
            type_name = "TRADE (BUY)" if msg_type > 0 else "TRADE (SELL)"

        logging.info(f"Type {msg_type} ({type_name}): {count} messages")

    logging.info("==============================")


def calculate_latency_stats(latencies):
    """Calculate latency statistics for a list of latencies"""
    if not latencies:
        return None
    
    avg_latency_ms = sum(latencies) / len(latencies)
    median_latency_ms = statistics.median(latencies)
    p1_latency_ms = statistics.quantiles(latencies, n=100)[0]   # 1st percentile
    p25_latency_ms = statistics.quantiles(latencies, n=4)[0]    # 25th percentile (Q1)
    p75_latency_ms = statistics.quantiles(latencies, n=4)[2]    # 75th percentile (Q3)
    p99_latency_ms = statistics.quantiles(latencies, n=100)[98] # 99th percentile
    
    return {
        'count': len(latencies),
        'avg': avg_latency_ms,
        'median': median_latency_ms,
        'p1': p1_latency_ms,
        'p25': p25_latency_ms,
        'p75': p75_latency_ms,
        'p99': p99_latency_ms
    }


def print_latency_stats(stats, label):
    """Print formatted latency statistics"""
    if stats:
        logging.info(
            f"{label} ({stats['count']} messages) - "
            f"Avg: {stats['avg']:.2f}ms, Median: {stats['median']:.2f}ms, "
            f"1%: {stats['p1']:.2f}ms, 25%: {stats['p25']:.2f}ms, "
            f"75%: {stats['p75']:.2f}ms, 99%: {stats['p99']:.2f}ms"
        )
    else:
        logging.info(f"{label}: No messages received")


def print_per_symbol_latency_stats(subscriptions_list):
    """Print latency statistics for each symbol"""
    logging.info("\n=== Per-Symbol Latency Statistics ===")
    
    for sub in subscriptions_list:
        logging.info(f"\nSymbol: {sub.symbol}")
        
        # Calculate overall latency for this symbol
        all_latencies = []
        all_latencies.extend(sub.ticker_latencies)
        all_latencies.extend(sub.depth_latencies)
        all_latencies.extend(sub.trade_latencies)
        
        if all_latencies:
            overall_stats = calculate_latency_stats(all_latencies)
            print_latency_stats(overall_stats, f"  Overall for {sub.symbol}")
        
        # Print per-type latencies for this symbol
        ticker_stats = calculate_latency_stats(sub.ticker_latencies)
        depth_stats = calculate_latency_stats(sub.depth_latencies)
        trade_stats = calculate_latency_stats(sub.trade_latencies)
        
        print_latency_stats(ticker_stats, f"  TICKER for {sub.symbol}")
        print_latency_stats(depth_stats, f"  DEPTH for {sub.symbol}")
        print_latency_stats(trade_stats, f"  TRADE for {sub.symbol}")
    
    logging.info("\n===============================================")


def parse_depth_message(data, offset=56):
    """Parse depth message data and return formatted string"""
    asks_len, bids_len = struct.unpack("<qq", data[40:56])

    result = {}
    result["asks_len"] = asks_len
    result["bids_len"] = bids_len
    result["asks"] = []
    result["bids"] = []

    # Parse asks
    for i in range(asks_len):
        price_offset = offset + i * 16
        price, size = struct.unpack("<dd", data[price_offset : price_offset + 16])
        result["asks"].append((price, size))

    # Parse bids
    for i in range(bids_len):
        price_offset = offset + (asks_len + i) * 16
        price, size = struct.unpack("<dd", data[price_offset : price_offset + 16])
        result["bids"].append((price, size))

    return result


def format_price_levels(levels, max_levels=5):
    """Format price levels for display"""
    result = ""
    for i, (price, size) in enumerate(levels[:max_levels]):
        result += f"{price:.8g}:{size:.8g} "
        if i >= max_levels - 1 and len(levels) > max_levels:
            result += f"... ({len(levels) - max_levels} more)"
            break
    return result.strip()


def main():
    parser = argparse.ArgumentParser(description="Enhanced QTX UDP Feed Logger")
    parser.add_argument("--host", type=str, default="172.30.3.142", help="QTX UDP host IP address")
    parser.add_argument("--port", type=int, default=8080, help="QTX UDP port")
    parser.add_argument(
        "--duration", type=float, default=300.0, help="Duration in seconds to capture data (0 for unlimited)"
    )
    parser.add_argument(
        "--max-messages", type=int, default=100, help="Maximum number of messages to capture (0 for unlimited)"
    )
    parser.add_argument("--buffer", type=int, default=UDP_SIZE, help="UDP receive buffer size")
    parser.add_argument("--output", type=str, default="./logs/udp_logger.log", help="Path to output log file")
    parser.add_argument(
        "--symbols",
        type=str,
        default="binance-futures:btcusdt",
        help="Comma-separated list of symbols to subscribe to",
    )
    parser.add_argument(
        "--min-symbols",
        type=int,
        default=1,
        help="Minimum number of successful symbol subscriptions required to continue",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging (including raw hex dumps)")
    parser.add_argument(
        "--local-port",
        type=int,
        default=8088,
        help="Local UDP port to bind (0 = choose a random free port)",
    )
    args = parser.parse_args()

    # Configure logging
    handlers = [logging.StreamHandler()]
    if args.output:
        if not os.path.exists(os.path.dirname(args.output)):
            os.makedirs(os.path.dirname(args.output))
        handlers.append(logging.FileHandler(args.output, mode="w"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    logging.info(f"Starting UDP logger to {args.host}:{args.port}")
    if args.duration > 0:
        logging.info(f"Will run for {args.duration}s")
    if args.max_messages > 0:
        logging.info(f"Will capture up to {args.max_messages} messages")

    # Create a UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(True)  # Keep socket blocking during subscription phase
    sock.settimeout(2.0)  # Set timeout for subscription responses

    # Bind to any address on an automatic port
    sock.bind(("0.0.0.0", args.local_port))
    local_addr = sock.getsockname()
    logging.info(f"Bound to local address: {local_addr[0]}:{local_addr[1]}")

    # Subscribe to symbols
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    subscriptions_list = []  # List of Subscription objects

    for symbol in symbols:
        logging.info(f"Subscribing to symbol: {symbol}")
        try:
            # Send subscription request
            sub_request = symbol.encode()
            sock.sendto(sub_request, (args.host, args.port))

            if args.verbose:
                logging.info(f"Sent subscription request: {sub_request!r} (hex: {sub_request.hex()})")

            # ── Start ACK-loop ──
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    response, addr = sock.recvfrom(args.buffer)
                except socket.timeout:
                    logging.error(f"Timeout waiting for ACK for {symbol}")
                    break

                # Log raw response
                if args.verbose:
                    logging.info(f"Received raw response from {addr}: {response!r} (hex: {response.hex()})")

                # ignore any packet not from the server
                if addr[0] != args.host:
                    logging.warning(f"Ignoring response from unexpected address: {addr}")
                    continue

                # try decode as ASCII index:symbol response
                try:
                    response_str = response.decode("utf-8").strip()
                    # Parse new format: "index:symbol" (e.g., "9:binance-futures:ethusdt")
                    index_str, returned_symbol = response_str.split(":", 1)
                    index = int(index_str)
                    logging.info(f"Got text ACK {index}:{returned_symbol} for {symbol}")
                    if returned_symbol != symbol:
                        logging.warning(f"Subscribed to {symbol} but received confirmation for {returned_symbol}")
                    break
                except (UnicodeDecodeError, ValueError):
                    # Try to determine if this is a market data message
                    if len(response) >= 40:
                        try:
                            msg_type, idx, tx_ms, event_ms, local_ns, sn_id = struct.unpack("<iiqqqq", response[:40])
                            logging.info(f"Got market data during ACK-wait: type={msg_type}, index={idx}")
                        except struct.error:
                            pass
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
        logging.error(
            f"Only {len(subscriptions_list)} symbols subscribed successfully, minimum required is {args.min_symbols}"
        )
        logging.error("Exiting due to insufficient successful subscriptions")
        sock.close()
        return 1

    # Create index→subscription mapping for quick look-ups
    index_to_subscription = {sub.index: sub for sub in subscriptions_list}

    # Set to non-blocking for the data receiving phase
    sock.setblocking(False)

    start_ts = time.time()
    total_messages = 0

    # Tracking statistics
    missed_sequences = defaultdict(int)
    latencies = []
    
    # Tracking latencies by message type
    ticker_latencies = []  # Type ±1
    depth_latencies = []   # Type 2
    trade_latencies = []   # Type ±3

    logging.info("\nEntering main loop to receive market data...")

    try:
        while True:
            # Check if we should exit based on duration or message count
            elapsed = time.time() - start_ts
            if args.duration > 0 and elapsed >= args.duration:
                logging.info(f"Duration limit of {args.duration}s reached")
                break

            if args.max_messages > 0 and total_messages >= args.max_messages:
                logging.info(f"Message count limit of {args.max_messages} reached")
                break

            try:
                data, addr = sock.recvfrom(args.buffer)
                if data:
                    total_messages += 1

                    # Log the raw message if in verbose mode
                    if args.verbose:
                        logging.info(f"Message #{total_messages} RAW [{len(data)} bytes]: {data.hex()}")

                    # Basic parsing of the message structure
                    if len(data) >= 40:  # Minimum size for header
                        try:
                            # Parse header
                            msg_type, index, tx_ms, event_ms, local_ns, seq_num = struct.unpack("<iiqqqq", data[:40])

                            # Find the corresponding subscription
                            sub = index_to_subscription.get(index)

                            if sub:
                                symbol = sub.symbol
                                sub.message_count += 1
                                sub.message_types[msg_type] += 1

                                # Calculate latency (end-to-end from KuCoin server to client)
                                now_ms = time.time() * 1000  # Current time in milliseconds
                                latency_ms = now_ms - event_ms  # True end-to-end latency
                                latencies.append(latency_ms)
                                
                                # Track latency by message type (overall and per-symbol)
                                if abs(msg_type) == 1:  # TICKER
                                    ticker_latencies.append(latency_ms)
                                    sub.ticker_latencies.append(latency_ms)
                                elif msg_type == 2:  # DEPTH
                                    depth_latencies.append(latency_ms)
                                    sub.depth_latencies.append(latency_ms)
                                elif abs(msg_type) == 3:  # TRADE
                                    trade_latencies.append(latency_ms)
                                    sub.trade_latencies.append(latency_ms)

                                # Check for missing sequence numbers per message type
                                last_seq_for_type = sub.last_sequences_by_type[msg_type]
                                if last_seq_for_type > 0 and seq_num > last_seq_for_type + 1:
                                    gap = seq_num - last_seq_for_type - 1
                                    sub.missed_sequences_by_type[msg_type] += gap
                                    missed_sequences[symbol] += gap
                                    logging.warning(
                                        f"Sequence gap for {symbol} type {msg_type}: last={last_seq_for_type}, current={seq_num}, missed={gap}"
                                    )

                                sub.last_sequences_by_type[msg_type] = seq_num
                                sub.last_sequence = seq_num

                                # Process message based on type
                                msg_info = f"#{total_messages} [{symbol}] type={msg_type}, seq={seq_num}, latency={latency_ms:.2f}ms"

                                if msg_type == 2:  # Depth
                                    if len(data) >= 56:  # Has additional header
                                        depth_data = parse_depth_message(data)
                                        asks_str = format_price_levels(depth_data["asks"])
                                        bids_str = format_price_levels(depth_data["bids"])

                                        logging.info(
                                            f"DEPTH {msg_info}, asks={depth_data['asks_len']}, bids={depth_data['bids_len']}"
                                        )
                                        logging.info(f"  Asks: {asks_str}")
                                        logging.info(f"  Bids: {bids_str}")
                                    else:
                                        logging.warning(f"DEPTH {msg_info} - Message too short for depth header")

                                elif abs(msg_type) == 1:  # Ticker
                                    if len(data) >= 56:
                                        price, size = struct.unpack("<dd", data[40:56])
                                        side = "BID" if msg_type > 0 else "ASK"
                                        logging.info(
                                            f"TICKER {msg_info}, side={side}, price={price:.8g}, size={size:.8g}"
                                        )
                                    else:
                                        logging.warning(f"TICKER {msg_info} - Message too short for price/size")

                                elif abs(msg_type) == 3:  # Trade
                                    if len(data) >= 56:
                                        price, size = struct.unpack("<dd", data[40:56])
                                        side = "BUY" if msg_type > 0 else "SELL"
                                        logging.info(
                                            f"TRADE {msg_info}, side={side}, price={price:.8g}, size={size:.8g}"
                                        )
                                    else:
                                        logging.warning(f"TRADE {msg_info} - Message too short for price/size")

                                else:
                                    logging.warning(f"UNKNOWN {msg_info}")
                                    if args.verbose:
                                        logging.info(f"Unknown message content: {data.hex()}")
                            else:
                                logging.warning(f"Message #{total_messages} for unknown index: {index}")
                                if args.verbose:
                                    logging.info(f"Unknown index message: {data.hex()}")

                        except struct.error as se:
                            logging.error(f"Struct unpacking error in message #{total_messages}: {se}")
                            if args.verbose:
                                logging.info(f"Malformed message: {data.hex()}")
                        except Exception as e:
                            logging.error(f"Error parsing message #{total_messages}: {e}")
                    else:
                        logging.warning(f"Message #{total_messages} too short ({len(data)} bytes)")

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
        elapsed = time.time() - start_ts
        logging.info(f"\nRun completed in {elapsed:.2f} seconds")
        logging.info(f"Total messages received: {total_messages}")

        # Calculate and print overall latency statistics
        if latencies:
            overall_stats = calculate_latency_stats(latencies)
            print_latency_stats(overall_stats, "Overall Latency")

        # Calculate and print overall latency statistics by message type
        logging.info("\n=== Overall Latency Statistics by Message Type ===")
        ticker_stats = calculate_latency_stats(ticker_latencies)
        depth_stats = calculate_latency_stats(depth_latencies)
        trade_stats = calculate_latency_stats(trade_latencies)
        
        print_latency_stats(ticker_stats, "TICKER Messages (All Symbols)")
        print_latency_stats(depth_stats, "DEPTH Messages (All Symbols)")
        print_latency_stats(trade_stats, "TRADE Messages (All Symbols)")
        logging.info("===============================================")

        # Print per-symbol latency statistics
        print_per_symbol_latency_stats(subscriptions_list)

        # Print missed sequences
        if missed_sequences:
            logging.info("\nTotal missed sequences by symbol:")
            for symbol, count in missed_sequences.items():
                logging.info(f"  {symbol}: {count} missed messages")

        # Print detailed statistics
        print_message_stats(subscriptions_list)

        # Unsubscribe from all symbols
        logging.info("\nUnsubscribing all symbols...")
        for sub in reversed(subscriptions_list):
            unsubscribe_msg = f"-{sub.symbol}"
            logging.info(f"Unsubscribing from symbol: {sub.symbol}")
            try:
                sock.setblocking(True)
                sock.settimeout(1.0)
                sock.sendto(unsubscribe_msg.encode(), (args.host, args.port))

                # Wait for confirmation
                try:
                    response, _ = sock.recvfrom(args.buffer)
                    try:
                        response_text = response.decode().strip()
                        logging.info(f"Unsubscribe response for {sub.symbol}: {response_text}")
                    except UnicodeDecodeError:
                        logging.info(f"Unsubscribe response for {sub.symbol} (binary): {response.hex()}")
                except socket.timeout:
                    logging.warning(f"No unsubscribe confirmation received for {sub.symbol}")
            except Exception as e:
                logging.error(f"Error unsubscribing from {sub.symbol}: {e}")

        logging.info("Closing socket")
        sock.close()

    logging.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
