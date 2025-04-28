#!/usr/bin/env python3
"""
qtx_udp_logger.py

Standalone script to connect to a QTX UDP feed, subscribe to symbols, receive messages for a given duration,
log the raw bytes (hex) along with a timestamp, then exit.
Usage: python qtx_udp_logger.py --host 172.30.2.221 --port 8080 --duration 5.0 --symbols binance-futures:btcusdt,binance:btcusdt
"""
import argparse
import logging
import socket
import time
import struct
import binascii


def main():
    parser = argparse.ArgumentParser(description="QTX UDP Feed Logger")
    parser.add_argument("--host", type=str, default="172.30.2.221", help="QTX UDP host IP address")
    parser.add_argument("--port", type=int, default=8080, help="QTX UDP port")
    parser.add_argument("--duration", type=float, default=5.0, help="Duration in seconds to capture data")
    parser.add_argument("--buffer", type=int, default=65536, help="UDP receive buffer size")
    parser.add_argument("--output", type=str, default=None, help="Optional path to output log file")
    parser.add_argument("--symbols", type=str, default="binance:btcusdt", 
                        help="Comma-separated list of symbols to subscribe to")
    args = parser.parse_args()

    # Configure logging
    handlers = [logging.StreamHandler()]
    if args.output:
        handlers.append(logging.FileHandler(args.output, mode="w"))
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S", handlers=handlers
    )

    logging.info(f"Starting UDP logger to {args.host}:{args.port} for {args.duration}s")

    # Create a UDP socket (using blocking mode for simplicity)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)  # 1 second timeout
    
    # Bind to any address on an automatic port
    sock.bind(('0.0.0.0', 0))
    
    # Subscribe to symbols
    symbols = args.symbols.split(',')
    subscriptions = {}
    
    for symbol in symbols:
        logging.info(f"Subscribing to symbol: {symbol}")
        # Send subscription request
        sock.sendto(symbol.encode(), (args.host, args.port))
        
        try:
            # Receive response
            response, _ = sock.recvfrom(args.buffer)
            if response:
                # Parse response (expecting an index number as response)
                index = int(response.decode().strip())
                subscriptions[index] = symbol
                logging.info(f"Successfully subscribed to {symbol} with index {index}")
        except socket.timeout:
            logging.error(f"Timeout while waiting for subscription response for {symbol}")
        except Exception as e:
            logging.error(f"Error subscribing to {symbol}: {e}")

    if not subscriptions:
        logging.error("No successful subscriptions, exiting")
        sock.close()
        return

    logging.info(f"Subscribed to {len(subscriptions)} symbols: {subscriptions}")
    
    # Set to non-blocking for the data receiving phase
    sock.setblocking(False)
    
    start_ts = time.time()
    receive_count = 0
    
    while time.time() - start_ts < args.duration:
        try:
            data, addr = sock.recvfrom(args.buffer)
            if data:
                receive_count += 1
                # Log raw data as hex string
                logging.info(f"Received {len(data)} bytes: {data.hex()}")
                
                # Basic parsing of the message structure (similar to the C code)
                if len(data) >= 48:  # Minimum size for Msg struct
                    try:
                        # Parse header (similar to Msg struct in C)
                        msg_type, index, tx_ms, event_ms, local_ns, sn_id = struct.unpack("<iiqqqq", data[:40])
                        
                        symbol = subscriptions.get(index, f"unknown-{index}")
                        
                        if abs(msg_type) == 1:  # Ticker
                            price, size = struct.unpack("<dd", data[40:56])
                            side = "bid" if msg_type > 0 else "ask"
                            logging.info(f"{symbol}: ticker, {side}, {price:.8g}, {size:.8g}")
                        
                        elif abs(msg_type) == 3:  # Trade
                            price, size = struct.unpack("<dd", data[40:56])
                            side = "buy" if msg_type > 0 else "sell"
                            logging.info(f"{symbol}: trade, {side}, {price:.8g}, {size:.8g}")
                        
                        elif msg_type == 2:  # Depth data
                            if len(data) >= 56:  # Has additional header data
                                asks_len, bids_len = struct.unpack("<qq", data[40:56])
                                logging.info(f"{symbol}: depth, asks: {asks_len}, bids: {bids_len}")
                                
                                if len(data) >= 56 + (asks_len + bids_len) * 16:
                                    levels_offset = 56
                                    asks = []
                                    bids = []
                                    
                                    # Parse asks
                                    for i in range(asks_len):
                                        offset = levels_offset + i * 16
                                        price, size = struct.unpack("<dd", data[offset:offset+16])
                                        asks.append((price, size))
                                    
                                    # Parse bids
                                    for i in range(bids_len):
                                        offset = levels_offset + (asks_len + i) * 16
                                        price, size = struct.unpack("<dd", data[offset:offset+16])
                                        bids.append((price, size))
                                    
                                    logging.info(f"asks: {asks}")
                                    logging.info(f"bids: {bids}")
                    except Exception as e:
                        logging.error(f"Error parsing message: {e}")
                        
        except BlockingIOError:
            # No data available right now
            pass
        except Exception as e:
            logging.error(f"Error receiving UDP data: {e}")
        time.sleep(0.01)

    logging.info(f"Capture complete, received {receive_count} messages. Unsubscribing...")
    
    # Unsubscribe from all symbols
    for symbol in symbols:
        unsubscribe_msg = f"-{symbol}"
        logging.info(f"Unsubscribing from symbol: {symbol}")
        sock.sendto(unsubscribe_msg.encode(), (args.host, args.port))
        try:
            # Set to blocking temporarily to receive response
            sock.setblocking(True)
            sock.settimeout(1.0)
            response, _ = sock.recvfrom(args.buffer)
            logging.info(f"Unsubscribe response for {symbol}: {response.decode().strip()}")
        except Exception as e:
            logging.error(f"Error unsubscribing from {symbol}: {e}")
    
    logging.info("Closing socket")
    sock.close()


if __name__ == "__main__":
    main()
