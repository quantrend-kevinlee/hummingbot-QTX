#!/usr/bin/env python

import asyncio
import socket
import time
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, AsyncIterable, Dict, List, Optional, Tuple

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_constants as BINANCE_CONSTANTS
from hummingbot.connector.derivative.binance_perpetual.binance_perpetual_derivative import BinancePerpetualDerivative
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.derivative.qtx_perpetual import (
    qtx_perpetual_constants as CONSTANTS,
    qtx_perpetual_web_utils as web_utils,
)
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_api_order_book_data_source import (
    QtxPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_auth import QtxPerpetualBinanceAuth
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_user_stream_data_source import (
    QtxPerpetualUserStreamDataSource,
)
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter


class QtxPerpetualDerivative(PerpetualDerivativePyBase):
    """
    QtxPerpetualDerivative connects to the QTX UDP market data source for real-time market data.
    Trading functionality is delegated to Binance's API for order execution on the mainnet.
    This hybrid approach leverages QTX's market data with Binance's order execution capabilities.
    """

    web_utils = web_utils
    SHORT_POLL_INTERVAL = 5.0
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    LONG_POLL_INTERVAL = 120.0

    def __init__(
        self,
        client_config_map: "ClientConfigAdapter",
        qtx_perpetual_host: str = CONSTANTS.DEFAULT_UDP_HOST,
        qtx_perpetual_port: int = CONSTANTS.DEFAULT_UDP_PORT,
        binance_api_key: str = None,  # Binance API key
        binance_api_secret: str = None,  # Binance API secret
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):
        # QTX connection settings for market data
        self._qtx_perpetual_host = qtx_perpetual_host
        self._qtx_perpetual_port = qtx_perpetual_port

        # Binance credentials for order execution (mainnet)
        self.binance_api_key = binance_api_key
        self.binance_api_secret = binance_api_secret

        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._domain = domain
        self._funding_payment_span = CONSTANTS.FUNDING_SETTLEMENT_DURATION

        # Initialize state for position tracking and funding info
        self._positions_cache: Dict[str, Dict[PositionSide, Position]] = defaultdict(dict)
        self._funding_info = {}

        # Warm-up related variables
        self._ready = False
        self._warm_up_start_time = 0

        # Initialize the base class
        super().__init__(client_config_map)

        # Initialize Binance connector AFTER super().__init__ to ensure throttler is created
        self._binance_derivative = None
        if trading_required and binance_api_key and binance_api_secret:
            self._init_binance_connector()

    def _init_binance_connector(self):
        """
        Initialize the Binance connector for order execution delegation
        """
        self._binance_derivative = BinancePerpetualDerivative(
            client_config_map=self._client_config,
            binance_perpetual_api_key=self.binance_api_key,
            binance_perpetual_api_secret=self.binance_api_secret,
            trading_pairs=self._trading_pairs,
            trading_required=self._trading_required,
            domain=self._domain
        )
        # Share the throttler to coordinate rate limiting
        self._binance_derivative._throttler = self._throttler

    @property
    def _binance_connector(self) -> BinancePerpetualDerivative:
        """
        Returns the Binance connector instance, initializing it if necessary
        """
        if self._binance_derivative is None and self._trading_required:
            if self.binance_api_key and self.binance_api_secret:
                self._init_binance_connector()
            else:
                raise ValueError("Binance API credentials required for trading")
        return self._binance_derivative

    @property
    def name(self) -> str:
        return CONSTANTS.EXCHANGE_NAME

    @property
    def authenticator(self) -> QtxPerpetualBinanceAuth:
        # Only need Binance authentication, QTX market data doesn't require auth
        return QtxPerpetualBinanceAuth(
            binance_api_key=self.binance_api_key,
            binance_api_secret=self.binance_api_secret,
            time_provider=self._time_synchronizer
        )

    @property
    def rate_limits_rules(self) -> List[RateLimit]:
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def client_order_id_max_length(self) -> int:
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self) -> str:
        return CONSTANTS.BROKER_ID

    @property
    def trading_rules_request_path(self) -> str:
        return BINANCE_CONSTANTS.EXCHANGE_INFO_URL

    @property
    def trading_pairs_request_path(self) -> str:
        return BINANCE_CONSTANTS.EXCHANGE_INFO_URL

    @property
    def check_network_request_path(self) -> str:
        return BINANCE_CONSTANTS.PING_URL

    @property
    def ready(self) -> bool:
        """
        Checks if the connector is ready for trading.
        Returns True if:
        1. All markets are ready
        2. The warm-up period has elapsed (preventing trading during orderbook initialization)
        """
        # Check if the base class implementation is ready
        base_ready = super().ready

        # If base ready, also check if the warm-up period has elapsed
        if base_ready and not self._ready:
            # Check if warm-up period has elapsed
            if self._warm_up_start_time > 0 and time.time() >= self._warm_up_start_time + CONSTANTS.ORDERBOOK_WARMUP_TIME:
                self.logger().info("QTX Perpetual connector warm-up period completed.")
                self._ready = True

        return base_ready and self._ready

    async def check_network(self) -> NetworkStatus:
        """
        Checks UDP connectivity to the QTX feed and Binance API if trading is required
        """
        try:
            # Check QTX UDP connection
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            sock.sendto(b"", (self._qtx_perpetual_host, self._qtx_perpetual_port))
            sock.close()

            # Check Binance connection if trading is required
            if self._trading_required:
                binance_status = await self._binance_connector.check_network()
                if binance_status != NetworkStatus.CONNECTED:
                    self.logger().warning("Binance API connection check failed")
                    return NetworkStatus.NOT_CONNECTED

            return NetworkStatus.CONNECTED
        except Exception as e:
            self.logger().warning(f"Network check failed: {e}")
            return NetworkStatus.NOT_CONNECTED

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    @property
    def funding_fee_poll_interval(self) -> int:
        return 600  # 10 minutes

    # Market data methods (use QTX implementation)
    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        """
        Create the order book tracker data source using QTX
        """
        return QtxPerpetualAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
            host=self._qtx_perpetual_host,
            port=self._qtx_perpetual_port,
        )

    # User stream methods (use Binance implementation through delegation)
    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        """
        Create the user stream data source
        This uses the Binance stream for order/position updates
        """
        return QtxPerpetualUserStreamDataSource(
            auth=self._auth,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    # All trading methods delegate to Binance connector
    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
        """
        Delegate order cancellation to Binance connector
        """
        if self._trading_required:
            return await self._binance_connector._place_cancel(order_id, tracked_order)
        return False

    async def _place_order(
        self,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        trade_type: TradeType,
        order_type: OrderType,
        price: Decimal,
        position_action: PositionAction = PositionAction.NIL,
        **kwargs,
    ) -> Tuple[str, float]:
        """
        Delegate order placement to Binance connector
        """
        if self._trading_required:
            return await self._binance_connector._place_order(
                order_id=order_id,
                trading_pair=trading_pair,
                amount=amount,
                trade_type=trade_type,
                order_type=order_type,
                price=price,
                position_action=position_action,
                **kwargs
            )
        return "", 0.0

    async def _update_trading_rules(self):
        """
        Delegate trading rules update to Binance connector
        """
        if self._trading_required:
            await self._binance_connector._update_trading_rules()
            self._trading_rules = self._binance_connector._trading_rules
        else:
            self._trading_rules = {}

    async def _update_balances(self):
        """
        Retrieve balances from Binance API
        This method will attempt to get balances regardless of trading_required flag
        """

        try:
            # Ensure Binance connector is initialized and working (if we have credentials)
            if self.binance_api_key and self.binance_api_secret:
                if self._binance_derivative is None:
                    self._init_binance_connector()

                # Log domain and URL information
                self.logger().debug(f"Fetching balance from Binance API using domain: {self._domain}")

                # Directly update balances from Binance API
                account_info = await self._api_get(
                    path_url=BINANCE_CONSTANTS.ACCOUNT_INFO_URL,
                    is_auth_required=True,
                    limit_id=BINANCE_CONSTANTS.ACCOUNT_INFO_URL
                )

                # Log the structure of account_info for debugging
                self.logger().debug(f"Account info keys: {account_info.keys() if isinstance(account_info, dict) else 'Not a dict'}")

                # Process the balances from the account info
                if "assets" in account_info:
                    assets = account_info.get("assets", [])
                    self.logger().debug(f"Received {len(assets)} assets from Binance")

                    # For debugging, log a sample asset if available
                    if len(assets) > 0:
                        self.logger().debug(f"Sample asset structure: {assets[0]}")

                    local_asset_names = set(self._account_balances.keys())
                    remote_asset_names = set()

                    for asset in assets:
                        asset_name = asset.get("asset")

                        # Skip assets with zero balance
                        wallet_balance = Decimal(str(asset.get("walletBalance", "0")))
                        if wallet_balance == Decimal("0"):
                            continue

                        available_balance = Decimal(str(asset.get("availableBalance", "0")))

                        self.logger().debug(f"Processing asset: {asset_name}, wallet: {wallet_balance}, available: {available_balance}")

                        self._account_available_balances[asset_name] = available_balance
                        self._account_balances[asset_name] = wallet_balance
                        remote_asset_names.add(asset_name)

                    # Log updated balances - Keep this as INFO since it's important
                    non_zero_balances = {k: v for k, v in self._account_balances.items() if v > 0}
                    if non_zero_balances:
                        self.logger().info(f"Updated balances: {', '.join([f'{k}: {v}' for k, v in non_zero_balances.items()])}")
                    else:
                        self.logger().debug("No non-zero balances found")

                    # Remove any assets no longer present
                    asset_names_to_remove = local_asset_names.difference(remote_asset_names)
                    for asset_name in asset_names_to_remove:
                        del self._account_available_balances[asset_name]
                        del self._account_balances[asset_name]
                else:
                    self.logger().error(f"Error updating balances: account_info does not contain 'assets' key. Keys: {account_info.keys()}")
            else:
                self.logger().debug("Skipping balance update: Binance API credentials not provided")
                self._account_balances = {}
                self._account_available_balances = {}
        except Exception as e:
            # Log the error but don't crash
            self.logger().error(f"Error updating balances: {str(e)}", exc_info=True)
            # Set empty balances on error
            self._account_balances = {}
            self._account_available_balances = {}

    async def _update_positions(self):
        """
        Delegate position updates to Binance connector
        """
        if self._trading_required:
            await self._binance_connector._update_positions()
            # Copy positions from Binance to our local cache
            for trading_pair, positions in self._binance_connector._perpetual_trading._positions.items():
                for position_side, position in positions.items():
                    self._perpetual_trading.set_position(
                        self._perpetual_trading.position_key(trading_pair, position_side),
                        position
                    )

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        """
        Delegate order status request to Binance connector
        """
        if self._trading_required:
            return await self._binance_connector._request_order_status(tracked_order)
        return OrderUpdate(
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=tracked_order.current_state,
            client_order_id=tracked_order.client_order_id,
        )

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        """
        Delegate trade updates request to Binance connector
        """
        if self._trading_required:
            return await self._binance_connector._all_trade_updates_for_order(order)
        return []

    async def _fetch_last_fee_payment(self, trading_pair: str) -> Tuple[int, Decimal, Decimal]:
        """
        Delegate fee payment request to Binance connector
        """
        if self._trading_required:
            return await self._binance_connector._fetch_last_fee_payment(trading_pair)
        return 0, Decimal("-1"), Decimal("-1")

    # Order type and position mode support
    def supported_order_types(self) -> List[OrderType]:
        """
        Returns order types supported by the connector
        """
        return [OrderType.LIMIT, OrderType.MARKET, OrderType.LIMIT_MAKER]

    def supported_position_modes(self):
        """
        Returns supported position modes
        """
        return [PositionMode.ONEWAY, PositionMode.HEDGE]

    # Set leverage and position mode (delegate to Binance)
    async def _set_trading_pair_leverage(self, trading_pair: str, leverage: int) -> Tuple[bool, str]:
        """
        Delegate leverage setting to Binance connector
        """
        if self._trading_required:
            return await self._binance_connector._set_trading_pair_leverage(trading_pair, leverage)
        return False, "Trading not enabled"

    async def _trading_pair_position_mode_set(self, mode: PositionMode, trading_pair: str) -> Tuple[bool, str]:
        """
        Delegate position mode setting to Binance connector
        """
        if self._trading_required:
            return await self._binance_connector._trading_pair_position_mode_set(mode, trading_pair)
        return False, "Trading not enabled"

    # Fees and trading info
    def get_buy_collateral_token(self, trading_pair: str) -> str:
        """
        Returns the collateral token for buy orders
        """
        if self._trading_required and trading_pair in self._trading_rules:
            return self._trading_rules[trading_pair].buy_order_collateral_token
        base, quote = trading_pair.split("-")
        return quote

    def get_sell_collateral_token(self, trading_pair: str) -> str:
        """
        Returns the collateral token for sell orders
        """
        if self._trading_required and trading_pair in self._trading_rules:
            return self._trading_rules[trading_pair].sell_order_collateral_token
        base, quote = trading_pair.split("-")
        return quote

    def _get_fee(
        self,
        base_currency: str,
        quote_currency: str,
        order_type: OrderType,
        order_side: TradeType,
        position_action: PositionAction,
        amount: Decimal,
        price: Decimal = s_decimal_NaN,
        is_maker: Optional[bool] = None,
    ) -> TradeFeeBase:
        is_maker = is_maker or (order_type is OrderType.LIMIT_MAKER)
        return build_trade_fee(
            self.name,
            is_maker,
            base_currency=base_currency,
            quote_currency=quote_currency,
            order_type=order_type,
            order_side=order_side,
            amount=amount,
            price=price,
        )

    # User stream event processing - delegate to Binance connector
    async def _user_stream_event_listener(self):
        async for event_message in self._iter_user_event_queue():
            try:
                event_type = event_message.get("e")
                if event_type == "ORDER_TRADE_UPDATE":
                    # Process order update
                    await self._process_user_stream_order_update(event_message)
                elif event_type == "ACCOUNT_UPDATE":
                    # Process account update
                    await self._process_user_stream_account_update(event_message)
                elif event_type == "MARGIN_CALL":
                    # Process margin call
                    await self._process_user_stream_margin_call(event_message)
                elif event_type == "ACCOUNT_CONFIG_UPDATE":
                    # Process account config update
                    await self._process_user_stream_config_update(event_message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in user stream listener loop: {e}", exc_info=True)
                await self._sleep(5.0)

    async def _process_user_stream_order_update(self, event_message: Dict[str, Any]):
        order_message = event_message.get("o")
        if order_message:
            client_order_id = order_message.get("c", None)
            tracked_order = self._order_tracker.all_fillable_orders.get(client_order_id)
            if tracked_order is not None:
                # Process trade update if applicable
                trade_id = str(order_message["t"])
                if trade_id != "0":  # Indicates there has been a trade
                    await self._process_trade_update_from_order_message(tracked_order, order_message)

                # Process order status update
                order_update = OrderUpdate(
                    trading_pair=tracked_order.trading_pair,
                    update_timestamp=event_message["T"] * 1e-3,
                    new_state=BINANCE_CONSTANTS.ORDER_STATE[order_message["X"]],
                    client_order_id=client_order_id,
                    exchange_order_id=str(order_message["i"]),
                )
                self._order_tracker.process_order_update(order_update)

    async def _process_trade_update_from_order_message(self, tracked_order: InFlightOrder, order_message: Dict[str, Any]):
        trade_id = str(order_message["t"])
        client_order_id = tracked_order.client_order_id

        fee_asset = order_message.get("N", tracked_order.quote_asset)
        fee_amount = Decimal(order_message.get("n", "0"))
        position_side = order_message.get("ps", "LONG")
        position_action = (PositionAction.OPEN
                           if (tracked_order.trade_type is TradeType.BUY and position_side == "LONG"
                               or tracked_order.trade_type is TradeType.SELL and position_side == "SHORT")
                           else PositionAction.CLOSE)
        flat_fees = [] if fee_amount == Decimal("0") else [TokenAmount(amount=fee_amount, token=fee_asset)]

        fee = TradeFeeBase.new_perpetual_fee(
            fee_schema=self.trade_fee_schema(),
            position_action=position_action,
            percent_token=fee_asset,
            flat_fees=flat_fees,
        )

        trade_update = TradeUpdate(
            trade_id=trade_id,
            client_order_id=client_order_id,
            exchange_order_id=str(order_message["i"]),
            trading_pair=tracked_order.trading_pair,
            fill_timestamp=order_message["T"] * 1e-3,
            fill_price=Decimal(order_message["L"]),
            fill_base_amount=Decimal(order_message["l"]),
            fill_quote_amount=Decimal(order_message["L"]) * Decimal(order_message["l"]),
            fee=fee,
        )
        self._order_tracker.process_trade_update(trade_update)

    async def _process_user_stream_account_update(self, event_message: Dict[str, Any]):
        update_data = event_message.get("a", {})

        # Update balances
        for asset in update_data.get("B", []):
            asset_name = asset["a"]
            self._account_balances[asset_name] = Decimal(asset["wb"])
            self._account_available_balances[asset_name] = Decimal(asset["cw"])

        # Update positions
        for asset in update_data.get("P", []):
            trading_pair = asset["s"]
            try:
                hb_trading_pair = await self.trading_pair_associated_to_exchange_symbol(trading_pair)
            except KeyError:
                # Ignore results for which their symbols is not tracked by the connector
                continue

            side = PositionSide[asset['ps']]
            position = self._perpetual_trading.get_position(hb_trading_pair, side)
            if position is not None:
                amount = Decimal(asset["pa"])
                if amount == Decimal("0"):
                    pos_key = self._perpetual_trading.position_key(hb_trading_pair, side)
                    self._perpetual_trading.remove_position(pos_key)
                else:
                    position.update_position(position_side=PositionSide[asset["ps"]],
                                             unrealized_pnl=Decimal(asset["up"]),
                                             entry_price=Decimal(asset["ep"]),
                                             amount=Decimal(asset["pa"]))
            else:
                await self._update_positions()

    async def _process_user_stream_margin_call(self, event_message: Dict[str, Any]):
        positions = event_message.get("p", [])
        total_maint_margin_required = Decimal(0)
        negative_pnls_msg = ""

        for position in positions:
            trading_pair = position["s"]
            try:
                hb_trading_pair = await self.trading_pair_associated_to_exchange_symbol(trading_pair)
            except KeyError:
                continue

            existing_position = self._perpetual_trading.get_position(hb_trading_pair, PositionSide[position['ps']])
            if existing_position is not None:
                existing_position.update_position(position_side=PositionSide[position["ps"]],
                                                  unrealized_pnl=Decimal(position["up"]),
                                                  amount=Decimal(position["pa"]))

            total_maint_margin_required += Decimal(position.get("mm", "0"))
            if float(position.get("up", 0)) < 1:
                negative_pnls_msg += f"{hb_trading_pair}: {position.get('up')}, "

        self.logger().warning("Margin Call: Your position risk is too high, and you are at risk of "
                              "liquidation. Close your positions or add additional margin to your wallet.")
        self.logger().info(f"Margin Required: {total_maint_margin_required}. "
                           f"Negative PnL assets: {negative_pnls_msg}.")

    async def _process_user_stream_config_update(self, event_message: Dict[str, Any]):
        if "ac" in event_message:
            ac_data = event_message["ac"]
            if "s" in ac_data and "l" in ac_data:
                trading_pair = ac_data["s"]
                leverage = int(ac_data["l"])
                try:
                    hb_trading_pair = await self.trading_pair_associated_to_exchange_symbol(trading_pair)
                    self.logger().info(f"Leverage updated for {hb_trading_pair}: {leverage}x")
                except KeyError:
                    pass

        if "ai" in event_message:
            ai_data = event_message["ai"]
            if "j" in ai_data:
                is_multi_assets_mode = ai_data["j"]
                self.logger().info(f"Multi-assets mode updated: {is_multi_assets_mode}")

    async def _iter_user_event_queue(self) -> AsyncIterable[Dict[str, any]]:
        while True:
            try:
                yield await self._user_stream_tracker.user_stream.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().network(
                    "Unknown error. Retrying after 1 seconds.",
                    exc_info=True,
                    app_warning_msg="Could not fetch user events from Binance. Check API key and network connection.",
                )
                await self._sleep(1.0)

    async def start_network(self):
        """
        Start networking (orderbook, user stream)
        Initializes the warm-up phase for orderbook building
        """
        # Reset the warm-up timer
        self._ready = False
        self._warm_up_start_time = time.time()

        # Log the start of warm-up period
        self.logger().info(f"Starting QTX Perpetual connector warm-up period ({CONSTANTS.ORDERBOOK_WARMUP_TIME} seconds)...")

        # Start networks from parent class
        await super().start_network()

        # If trading is required, update the Binance connector
        if self._trading_required and self._binance_connector is not None:
            await self._binance_connector._update_balances()
            await self._binance_connector._update_positions()
            await self._binance_connector._update_trading_rules()

    async def stop_network(self):
        """
        Stop networking
        """
        # Reset the ready flag
        self._ready = False
        self._warm_up_start_time = 0

        # Stop the network from parent class
        await super().stop_network()

    async def get_last_traded_price(self, trading_pair: str) -> float:
        # Try to get price from QTX market data first
        orderbook = self.get_order_book(trading_pair)
        if orderbook and orderbook.last_trade_price:
            return float(orderbook.last_trade_price)

        # Last resort fallback - midpoint of best bid/ask
        return (orderbook.get_price(True) + orderbook.get_price(False)) / 2

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        """
        Create web assistants factory - delegate to Binance connector when available
        """
        if self._trading_required and hasattr(self._binance_connector, "_create_web_assistants_factory"):
            return self._binance_connector._create_web_assistants_factory()

        # Fallback implementation
        return WebAssistantsFactory(throttler=self._throttler, auth=self._auth)

    def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> Dict[str, TradingRule]:
        """
        Format trading rules - delegate to Binance connector when available
        """
        if self._trading_required and hasattr(self._binance_connector, "_format_trading_rules"):
            return self._binance_connector._format_trading_rules(exchange_info_dict)
        return {}

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        """
        Initialize trading pair symbols - delegate to Binance connector when available
        """
        if self._trading_required and hasattr(self._binance_connector, "_initialize_trading_pair_symbols_from_exchange_info"):
            return self._binance_connector._initialize_trading_pair_symbols_from_exchange_info(exchange_info)

        # Implement basic fallback version
        try:
            # Initialize maps if they don't exist
            if not hasattr(self, "_exchange_symbol_map"):
                self._exchange_symbol_map = {}
            if not hasattr(self, "_symbol_to_exchange_symbol"):
                self._symbol_to_exchange_symbol = {}
            if not hasattr(self, "_exchange_info"):
                self._exchange_info = {}

            symbols_data = exchange_info.get("symbols", [])
            for symbol_data in symbols_data:
                symbol = symbol_data.get("symbol")
                if symbol and symbol_data.get("status") == "TRADING":
                    exchange_symbol = symbol
                    self._exchange_symbol_map[exchange_symbol] = exchange_symbol
                    self._symbol_to_exchange_symbol[exchange_symbol] = exchange_symbol
                    # Store exchange info
                    self._exchange_info[exchange_symbol] = symbol_data
        except Exception as e:
            self.logger().error(f"Error initializing trading pairs from exchange info: {e}", exc_info=True)

    def _is_order_not_found_during_cancelation_error(self, exception) -> bool:
        """
        Check if the exception is due to order not found during cancelation
        Delegate to Binance connector when available
        """
        if self._trading_required and hasattr(self._binance_connector, "_is_order_not_found_during_cancelation_error"):
            return self._binance_connector._is_order_not_found_during_cancelation_error(exception)

        # Fallback implementation
        error_message = str(exception).lower()
        return "order not found" in error_message or "unknown order" in error_message

    def _is_order_not_found_during_status_update_error(self, exception) -> bool:
        """
        Check if the exception is due to order not found during status update
        Delegate to Binance connector when available
        """
        if self._trading_required and hasattr(self._binance_connector, "_is_order_not_found_during_status_update_error"):
            return self._binance_connector._is_order_not_found_during_status_update_error(exception)

        # Fallback implementation
        error_message = str(exception).lower()
        return "order not found" in error_message or "unknown order" in error_message

    def _is_request_exception_related_to_time_synchronizer(self, request_exception) -> bool:
        """
        Check if the exception is related to time synchronization
        Delegate to Binance connector when available
        """
        if self._trading_required and hasattr(self._binance_connector, "_is_request_exception_related_to_time_synchronizer"):
            return self._binance_connector._is_request_exception_related_to_time_synchronizer(request_exception)

        # Fallback implementation
        error_message = str(request_exception).lower()
        return "timestamp" in error_message and "ahead" in error_message

    async def _update_trading_fees(self):
        """
        Update trading fees - delegate to Binance connector when available
        """
        if self._trading_required and hasattr(self._binance_connector, "_update_trading_fees"):
            await self._binance_connector._update_trading_fees()
            if hasattr(self._binance_connector, "_trading_fees"):
                self._trading_fees = self._binance_connector._trading_fees
        # No-op implementation if not trading
