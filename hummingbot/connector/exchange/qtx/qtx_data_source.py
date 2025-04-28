import asyncio
import random
import time

class QTXDataSource:
    async def listen_for_subscriptions(self):
        """
        Mock method that simulates the reception of data from exchange WebSocket API. Generates sample data for
        testing purposes.
        """
        while True:
            try:
                self.logger().debug("Simulating WebSocket connection for QTX exchange...")
                
                # Initialize snapshots immediately for all trading pairs
                for trading_pair in self._trading_pairs:
                    snapshot_timestamp = time.time()
                    snapshot_data = {
                        "msg_type": 2,
                        "trading_pair": trading_pair,
                        "event_ms": int(snapshot_timestamp * 1000),
                        "sn_id": int(snapshot_timestamp * 1000),
                        "bids": [[10000.0, 5.0], [9990.0, 4.0], [9980.0, 3.0], [9970.0, 2.0], [9960.0, 1.0]],
                        "asks": [[10010.0, 1.0], [10020.0, 2.0], [10030.0, 3.0], [10040.0, 4.0], [10050.0, 5.0]]
                    }
                    self.logger().info(f"Generating initial order book snapshot for {trading_pair}")
                    await self._process_order_book_snapshot(snapshot_data, trading_pair)
                    # Notify we're ready for this trading pair
                    order_book_ready_event = self._order_book_snapshot_ready_events.get(trading_pair)
                    if order_book_ready_event and not order_book_ready_event.is_set():
                        order_book_ready_event.set()
                
                # Wait a moment to ensure snapshots are processed
                await asyncio.sleep(0.5)
                
                # After initialization, continue with random updates
                while True:
                    # Generate order book updates
                    for trading_pair in self._trading_pairs:
                        update_timestamp = time.time()
                        random_side = random.choice(["bids", "asks"])
                        
                        if random_side == "bids":
                            random_price = round(10000.0 * (1 - random.uniform(0, 0.01)), 1)
                            update_data = {
                                "msg_type": 1,
                                "trading_pair": trading_pair,
                                "event_ms": int(update_timestamp * 1000),
                                "sn_id": int(update_timestamp * 1000),
                                "bids": [[random_price, random.uniform(0.1, 5.0)]],
                                "asks": []
                            }
                        else:
                            random_price = round(10000.0 * (1 + random.uniform(0, 0.01)), 1)
                            update_data = {
                                "msg_type": 1,
                                "trading_pair": trading_pair,
                                "event_ms": int(update_timestamp * 1000),
                                "sn_id": int(update_timestamp * 1000),
                                "bids": [],
                                "asks": [[random_price, random.uniform(0.1, 5.0)]]
                            }
                        
                        await self._process_order_book_diff(update_data, trading_pair)
                    
                    # Generate random trades
                    for trading_pair in self._trading_pairs:
                        if random.random() < 0.3:  # 30% chance to generate a trade
                            trade_timestamp = time.time()
                            side = random.choice(["buy", "sell"])
                            price = round(10000.0 * (1 + random.uniform(-0.005, 0.005)), 1)
                            amount = round(random.uniform(0.001, 1.0), 6)
                            
                            trade_data = {
                                "msg_type": 3,  # Trade data
                                "trading_pair": trading_pair,
                                "event_ms": int(trade_timestamp * 1000),
                                "price": price,
                                "quantity": amount,
                                "side": side
                            }
                            
                            await self._process_trade(trade_data, trading_pair)
                    
                    # Wait before generating the next batch of data
                    await asyncio.sleep(1.0)
                    
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in QTX WebSocket connection: {e}", exc_info=True)
                await asyncio.sleep(5.0) 