# QTX Dynamic Inheritance Refactoring Plan

## Executive Summary

Direct refactoring plan to transform the current `qtx_perpetual_derivative.py` implementation to follow Python metaprogramming best practices. Since this is in development phase, we can make clean, breaking changes without migration concerns.

## Current State vs Target State

### Current Implementation Problems
```python
# CURRENT: Nested class inside __new__
class QtxPerpetualDerivative(PerpetualDerivativePyBase):
    def __new__(cls, ...):
        # Get base class
        base_exchange_class = getattr(module, exchange_info["class"])
        
        # Define class inside __new__ (WRONG!)
        class QtxDynamicConnector(base_exchange_class):
            def __init__(self, ...):
                # 700+ lines of implementation
```

### Target Implementation
```python
# TARGET: Proper factory with types.new_class()
def create_qtx_connector_class(
    exchange_backend: str,
    qtx_config: QtxConnectorConfig
) -> Type[QtxConnectorProtocol]:
    """Factory function using types.new_class()."""
    
    def exec_body(ns):
        """Populate namespace in modular fashion."""
        _add_metadata(ns)
        _add_initialization(ns, qtx_config)
        _add_properties(ns, qtx_config)
        _add_market_data_methods(ns, qtx_config)
        _add_order_methods(ns, qtx_config)
        _add_lifecycle_methods(ns, qtx_config)
    
    return types.new_class(
        f"Qtx{exchange_backend.capitalize()}Connector",
        (base_exchange_class,),
        {},
        exec_body
    )
```

## Key Updates for 6-Exchange Support

### Exchange Configuration
The implementation now supports 6 exchanges with varying parameter names:

```python
EXCHANGE_CONNECTOR_CLASSES = {
    "binance": {
        "module": "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_derivative",
        "class": "BinancePerpetualDerivative",
        "exchange_name_on_qtx": "binance-futures",
        "api_key_param": "binance_perpetual_api_key",
        "api_secret_param": "binance_perpetual_api_secret"
    },
    "okx": {
        "module": "hummingbot.connector.derivative.okx_perpetual.okx_perpetual_derivative",
        "class": "OkxPerpetualDerivative",
        "exchange_name_on_qtx": "okx-futures",
        "api_key_param": "okx_perpetual_api_key",
        "api_secret_param": "okx_perpetual_secret_key"  # Note: different naming pattern
    },
    "bitget": {
        "module": "hummingbot.connector.derivative.bitget_perpetual.bitget_perpetual_derivative",
        "class": "BitgetPerpetualDerivative",
        "exchange_name_on_qtx": "bitget-futures",
        "api_key_param": "bitget_perpetual_api_key",
        "api_secret_param": "bitget_perpetual_secret_key"
    },
    "bybit": {
        "module": "hummingbot.connector.derivative.bybit_perpetual.bybit_perpetual_derivative",
        "class": "BybitPerpetualDerivative",
        "exchange_name_on_qtx": "bybit-futures",
        "api_key_param": "bybit_perpetual_api_key",
        "api_secret_param": "bybit_perpetual_secret_key"
    },
    "kucoin": {
        "module": "hummingbot.connector.derivative.kucoin_perpetual.kucoin_perpetual_derivative",
        "class": "KucoinPerpetualDerivative",
        "exchange_name_on_qtx": "kucoin-futures",
        "api_key_param": "kucoin_perpetual_api_key",
        "api_secret_param": "kucoin_perpetual_secret_key"
    },
    "gate_io": {
        "module": "hummingbot.connector.derivative.gate_io_perpetual.gate_io_perpetual_derivative",
        "class": "GateIoPerpetualDerivative",
        "exchange_name_on_qtx": "gate-io-futures",
        "api_key_param": "gate_io_perpetual_api_key",
        "api_secret_param": "gate_io_perpetual_secret_key"
    }
}
```

## Refactoring Steps

### Step 1: Create Protocol and Type Definitions
**File**: `qtx_perpetual_protocol.py`

```python
from typing import Protocol, runtime_checkable, Optional, Tuple, TYPE_CHECKING, Dict, Any
from decimal import Decimal
import asyncio

if TYPE_CHECKING:
    from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_udp_manager import QtxPerpetualUDPManager
    from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager import QtxPerpetualSharedMemoryManager
    from hummingbot.core.data_type.in_flight_order import InFlightOrder
    from hummingbot.core.data_type.common import OrderType, TradeType, PositionAction

@runtime_checkable
class QtxConnectorProtocol(Protocol):
    """Protocol defining the complete interface for QTX connectors."""
    
    # Required attributes
    _qtx_perpetual_host: str
    _qtx_perpetual_port: int
    _exchange_backend: str
    _exchange_name_on_qtx: str
    _udp_manager: Optional['QtxPerpetualUDPManager']
    _shm_manager: Optional['QtxPerpetualSharedMemoryManager']
    _exchange_api_key: Optional[str]
    _exchange_api_secret: Optional[str]
    
    # Core trading operations
    async def _place_order(
        self,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        trade_type: 'TradeType',
        order_type: 'OrderType',
        price: Decimal,
        position_action: 'PositionAction' = ...,
        **kwargs
    ) -> Tuple[str, float]: ...
    
    async def _place_cancel(
        self, 
        order_id: str, 
        tracked_order: 'InFlightOrder'
    ) -> bool: ...
    
    # Market data operations
    def _create_order_book_data_source(self) -> 'OrderBookTrackerDataSource': ...
    async def _init_udp_manager(self) -> None: ...
    async def _setup_qtx_udp_subscriptions(self) -> None: ...
    async def _consume_diff_messages(self, output: 'asyncio.Queue') -> None: ...
    async def _consume_trade_messages(self, output: 'asyncio.Queue') -> None: ...
    async def _build_qtx_orderbook_snapshot(self, trading_pair: str) -> 'OrderBookMessage': ...
    
    # Properties
    @property
    def udp_manager(self) -> 'QtxPerpetualUDPManager': ...
    
    @property
    def shm_manager(self) -> Optional['QtxPerpetualSharedMemoryManager']: ...
    
    # Lifecycle
    async def start_network(self) -> None: ...
    async def stop_network(self) -> None: ...
```

### Step 2: Create Configuration Model with Exchange-Specific Handling
**File**: `qtx_perpetual_config.py`

```python
from typing import Optional, List, Dict, Any, Tuple
from pydantic import BaseModel, validator, IPvAnyAddress

# Import EXCHANGE_CONNECTOR_CLASSES from main module
from .qtx_perpetual_derivative import EXCHANGE_CONNECTOR_CLASSES

class QtxConnectorConfig(BaseModel):
    """Validated configuration for QTX connector."""
    
    # QTX specific
    qtx_perpetual_host: IPvAnyAddress
    qtx_perpetual_port: int
    qtx_place_order_shared_memory_name: Optional[str] = None
    
    # Exchange backend
    exchange_backend: str
    
    # Trading configuration
    trading_pairs: Optional[List[str]] = None
    trading_required: bool = True
    
    # Raw kwargs for flexible parameter handling
    raw_kwargs: Dict[str, Any] = {}
    
    @validator('exchange_backend')
    def validate_exchange_backend(cls, v):
        if v.lower() not in EXCHANGE_CONNECTOR_CLASSES:
            supported = ', '.join(EXCHANGE_CONNECTOR_CLASSES.keys())
            raise ValueError(f"Unsupported exchange: {v}. Supported: {supported}")
        return v.lower()
    
    @validator('qtx_perpetual_port')
    def validate_port(cls, v):
        if not 1 <= v <= 65535:
            raise ValueError(f"Invalid port number: {v}")
        return v
    
    def get_api_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        """Extract API credentials based on exchange-specific parameter names."""
        exchange_info = EXCHANGE_CONNECTOR_CLASSES.get(self.exchange_backend)
        if not exchange_info:
            return None, None
        
        # Get parameter names from exchange config
        api_key_param = exchange_info.get("api_key_param")
        api_secret_param = exchange_info.get("api_secret_param")
        
        if not api_key_param or not api_secret_param:
            # Fallback to pattern-based extraction
            api_key = self.raw_kwargs.get(f"{self.exchange_backend}_perpetual_api_key")
            
            # Handle inconsistent secret parameter naming
            if self.exchange_backend == "binance":
                api_secret = self.raw_kwargs.get(f"{self.exchange_backend}_perpetual_api_secret")
            else:
                api_secret = self.raw_kwargs.get(f"{self.exchange_backend}_perpetual_secret_key")
        else:
            api_key = self.raw_kwargs.get(api_key_param)
            api_secret = self.raw_kwargs.get(api_secret_param)
        
        return api_key, api_secret
    
    def validate_api_credentials(self) -> None:
        """Validate API credentials if trading is required."""
        if self.trading_required:
            api_key, api_secret = self.get_api_credentials()
            if not api_key or not api_secret:
                exchange_info = EXCHANGE_CONNECTOR_CLASSES.get(self.exchange_backend, {})
                key_param = exchange_info.get("api_key_param", f"{self.exchange_backend}_perpetual_api_key")
                secret_param = exchange_info.get("api_secret_param", f"{self.exchange_backend}_perpetual_secret_key")
                raise ValueError(
                    f"API credentials required when trading_required=True. "
                    f"Please provide {key_param} and {secret_param}"
                )
    
    def get_exchange_init_params(self) -> Dict[str, Any]:
        """Get exchange-specific initialization parameters."""
        # Start with all raw kwargs
        params = dict(self.raw_kwargs)
        
        # Remove QTX-specific parameters
        qtx_params = [
            'qtx_perpetual_host', 'qtx_perpetual_port', 
            'qtx_place_order_shared_memory_name', 'exchange_backend'
        ]
        for param in qtx_params:
            params.pop(param, None)
        
        return params
```

### Step 3: Implement Factory Function with Enhanced Parameter Handling
**File**: `qtx_perpetual_factory.py`

```python
import types
import importlib
import logging
from typing import Type, TypeVar, Dict, Any, Optional
from weakref import WeakValueDictionary
import threading

from .qtx_perpetual_protocol import QtxConnectorProtocol
from .qtx_perpetual_config import QtxConnectorConfig
from .qtx_perpetual_derivative import EXCHANGE_CONNECTOR_CLASSES

logger = logging.getLogger(__name__)

# Type variable for proper type hints
T = TypeVar('T', bound=QtxConnectorProtocol)

# Thread-safe cache
_CLASS_CACHE: WeakValueDictionary[tuple, Type[QtxConnectorProtocol]] = WeakValueDictionary()
_CACHE_LOCK = threading.RLock()


def create_qtx_connector_class(
    config: QtxConnectorConfig,
    client_config_map: Any
) -> Type[T]:
    """
    Create a dynamic QTX connector class using best practices.
    
    Args:
        config: Validated QTX connector configuration
        client_config_map: Hummingbot client configuration
        
    Returns:
        Dynamically created connector class conforming to QtxConnectorProtocol
        
    Raises:
        ValueError: If exchange backend is not supported
        ImportError: If parent exchange module cannot be loaded
    """
    # Generate cache key
    cache_key = (
        config.exchange_backend,
        str(config.qtx_perpetual_host),
        config.qtx_perpetual_port,
        config.qtx_place_order_shared_memory_name,
        config.trading_required
    )
    
    # Check cache
    with _CACHE_LOCK:
        if cache_key in _CLASS_CACHE:
            logger.debug(f"Returning cached class for {config.exchange_backend}")
            return _CLASS_CACHE[cache_key]
    
    # Get parent exchange class
    exchange_info = EXCHANGE_CONNECTOR_CLASSES[config.exchange_backend]
    module = importlib.import_module(exchange_info["module"])
    base_exchange_class = getattr(module, exchange_info["class"])
    
    # Generate class name
    class_name = f"Qtx{config.exchange_backend.capitalize()}PerpetualConnector"
    
    # Create namespace population function
    def exec_body(ns: Dict[str, Any]) -> None:
        """Populate class namespace following modular design."""
        
        # Add metadata
        _add_class_metadata(ns, config, class_name)
        
        # Add methods in logical groups
        _add_initialization_methods(ns, config, exchange_info)
        _add_property_methods(ns, config, exchange_info)
        _add_market_data_override_methods(ns, config)
        _add_order_management_override_methods(ns, config)
        _add_network_lifecycle_methods(ns, config)
        _add_utility_methods(ns, config)
        _add_debugging_methods(ns, config)
    
    # Create the class
    logger.info(f"Creating dynamic class: {class_name}")
    dynamic_class = types.new_class(
        class_name,
        (base_exchange_class,),
        {},
        exec_body
    )
    
    # Cache the class
    with _CACHE_LOCK:
        _CLASS_CACHE[cache_key] = dynamic_class
    
    return dynamic_class


# Modular namespace population functions

def _add_class_metadata(ns: Dict[str, Any], config: QtxConnectorConfig, class_name: str) -> None:
    """Add class metadata attributes."""
    ns['__module__'] = 'hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_derivative'
    ns['__qualname__'] = class_name
    ns['__doc__'] = f"""
    Dynamic QTX connector for {config.exchange_backend}.
    
    Combines QTX UDP market data feed with {config.exchange_backend} trading API.
    
    Configuration:
        - QTX Host: {config.qtx_perpetual_host}:{config.qtx_perpetual_port}
        - Exchange Backend: {config.exchange_backend}
        - Trading Required: {config.trading_required}
        - SHM Segment: {config.qtx_place_order_shared_memory_name or 'None'}
    """


def _add_initialization_methods(
    ns: Dict[str, Any], 
    config: QtxConnectorConfig,
    exchange_info: Dict[str, str]
) -> None:
    """Add initialization methods."""
    
    def __init__(self, *args, **kwargs):
        """Initialize QTX connector with configuration."""
        # Extract QTX-specific parameters that might be in kwargs
        self._qtx_perpetual_host = kwargs.pop('qtx_perpetual_host', str(config.qtx_perpetual_host))
        self._qtx_perpetual_port = kwargs.pop('qtx_perpetual_port', config.qtx_perpetual_port)
        self._qtx_shared_memory_name = kwargs.pop(
            'qtx_place_order_shared_memory_name', 
            config.qtx_place_order_shared_memory_name
        )
        self._exchange_backend = kwargs.pop('exchange_backend', config.exchange_backend)
        
        # Get API credentials
        api_key, api_secret = config.get_api_credentials()
        self._exchange_api_key = api_key
        self._exchange_api_secret = api_secret
        
        # Initialize managers
        self._udp_manager = None
        self._shm_manager = None
        self._udp_queues = {}
        self._udp_subscriptions = set()
        
        # Exchange info
        self._exchange_name_on_qtx = exchange_info["exchange_name_on_qtx"]
        
        # Call parent constructor with appropriate parameters
        super(self.__class__, self).__init__(*args, **kwargs)
        
        # Override exchange name
        self._name = "qtx_perpetual"
        self._exchange_name = "qtx_perpetual"
    
    # Add name property override
    @property
    def name(self) -> str:
        """Return QTX as the exchange name."""
        return "qtx_perpetual"
    
    ns['__init__'] = __init__
    ns['name'] = name


def _add_property_methods(
    ns: Dict[str, Any], 
    config: QtxConnectorConfig,
    exchange_info: Dict[str, str]
) -> None:
    """Add property accessor methods."""
    
    @property
    def udp_manager(self) -> 'QtxPerpetualUDPManager':
        """Get UDP manager instance."""
        if self._udp_manager is None:
            raise RuntimeError("UDP manager not initialized. Call _init_udp_manager() first.")
        return self._udp_manager
    
    @property
    def shm_manager(self) -> Optional['QtxPerpetualSharedMemoryManager']:
        """Get or create SHM manager instance."""
        if self._shm_manager is None and self._qtx_shared_memory_name:
            from .qtx_perpetual_shm_manager import QtxPerpetualSharedMemoryManager
            
            self._shm_manager = QtxPerpetualSharedMemoryManager(
                api_key=self._exchange_api_key,
                api_secret=self._exchange_api_secret,
                shm_name=self._qtx_shared_memory_name,
                exchange_name_on_qtx=self._exchange_name_on_qtx
            )
            self.logger().debug(
                f"Initialized SHM manager with segment '{self._qtx_shared_memory_name}' "
                f"for exchange '{self._exchange_name_on_qtx}'"
            )
        return self._shm_manager
    
    async def _init_udp_manager(self) -> None:
        """Initialize UDP manager asynchronously."""
        if self._udp_manager is None:
            from .qtx_perpetual_udp_manager import QtxPerpetualUDPManager
            
            self._udp_manager = await QtxPerpetualUDPManager.get_instance()
            self._udp_manager.configure(
                host=self._qtx_perpetual_host,
                port=self._qtx_perpetual_port,
                exchange_name=self._exchange_name_on_qtx
            )
    
    ns['udp_manager'] = udp_manager
    ns['shm_manager'] = shm_manager
    ns['_init_udp_manager'] = _init_udp_manager


def _add_market_data_override_methods(ns: Dict[str, Any], config: QtxConnectorConfig) -> None:
    """Add methods that override parent exchange market data handling."""
    
    # Import all the market data methods from current implementation
    # This is where we extract the 200+ lines of market data logic
    
    def _create_order_book_data_source(self) -> 'OrderBookTrackerDataSource':
        """Override to use QTX UDP feed for market data."""
        # Get parent data source
        parent_data_source = super(self.__class__, self)._create_order_book_data_source()
        
        # Store reference
        self._parent_data_source = parent_data_source
        self._udp_queues = {}
        self._udp_subscriptions = set()
        
        # Replace market data methods with QTX implementations
        async def qtx_listen_for_subscriptions():
            await self._init_udp_manager()
            await self._setup_qtx_udp_subscriptions()
        
        async def qtx_listen_for_order_book_diffs(ev_loop, output):
            await self._consume_diff_messages(output)
        
        async def qtx_listen_for_order_book_snapshots(ev_loop, output):
            await self._handle_orderbook_snapshots(output)
        
        async def qtx_listen_for_trades(ev_loop, output):
            await self._consume_trade_messages(output)
        
        async def qtx_order_book_snapshot(trading_pair):
            return await self._build_qtx_orderbook_snapshot(trading_pair)
        
        # Replace methods
        parent_data_source.listen_for_subscriptions = qtx_listen_for_subscriptions
        parent_data_source.listen_for_order_book_diffs = qtx_listen_for_order_book_diffs
        parent_data_source.listen_for_order_book_snapshots = qtx_listen_for_order_book_snapshots
        parent_data_source.listen_for_trades = qtx_listen_for_trades
        parent_data_source._order_book_snapshot = qtx_order_book_snapshot
        
        return parent_data_source
    
    # Add all the supporting methods
    ns['_create_order_book_data_source'] = _create_order_book_data_source
    
    # Extract these from current implementation
    from ._extracted_market_data_methods import (
        _setup_qtx_udp_subscriptions,
        _consume_diff_messages,
        _consume_trade_messages,
        _consume_messages_from_queue,
        _handle_orderbook_snapshots,
        _build_qtx_orderbook_snapshot
    )
    
    ns['_setup_qtx_udp_subscriptions'] = _setup_qtx_udp_subscriptions
    ns['_consume_diff_messages'] = _consume_diff_messages
    ns['_consume_trade_messages'] = _consume_trade_messages
    ns['_consume_messages_from_queue'] = _consume_messages_from_queue
    ns['_handle_orderbook_snapshots'] = _handle_orderbook_snapshots
    ns['_build_qtx_orderbook_snapshot'] = _build_qtx_orderbook_snapshot


def _add_order_management_override_methods(ns: Dict[str, Any], config: QtxConnectorConfig) -> None:
    """Add order placement and cancellation overrides."""
    
    # Extract these from current implementation
    from ._extracted_order_methods import (
        _place_order,
        _place_cancel,
        _get_current_market_price,
        _determine_smart_position_action
    )
    
    ns['_place_order'] = _place_order
    ns['_place_cancel'] = _place_cancel
    ns['_get_current_market_price'] = _get_current_market_price
    ns['_determine_smart_position_action'] = _determine_smart_position_action


def _add_network_lifecycle_methods(ns: Dict[str, Any], config: QtxConnectorConfig) -> None:
    """Add network lifecycle methods."""
    
    async def start_network(self) -> None:
        """Start network connections including QTX UDP."""
        await super(self.__class__, self).start_network()
        
        # Initialize UDP subscriptions
        if hasattr(self, "_order_book_tracker") and self._order_book_tracker is not None:
            data_source = self._order_book_tracker.data_source
            if hasattr(data_source, "listen_for_subscriptions"):
                await data_source.listen_for_subscriptions()
    
    async def stop_network(self) -> None:
        """Stop network connections and cleanup resources."""
        # Cleanup QTX resources first
        try:
            # Clear queue references
            self._udp_queues.clear()
            
            # Unsubscribe from UDP
            if self._udp_manager is not None and self._udp_manager.is_connected:
                trading_pairs = list(self._udp_manager.subscribed_pairs)
                if trading_pairs:
                    self.logger().info(f"Unsubscribing from {len(trading_pairs)} pairs")
                    await self._udp_manager.unsubscribe_from_trading_pairs(trading_pairs)
            
            # Stop UDP manager
            if self._udp_manager is not None:
                await self._udp_manager.stop()
                self._udp_manager = None
            
            # Disconnect SHM
            if self._shm_manager is not None:
                await self._shm_manager.disconnect()
                self._shm_manager = None
                
        except Exception as e:
            self.logger().error(f"Error during QTX cleanup: {e}", exc_info=True)
        
        # Stop parent network
        await super(self.__class__, self).stop_network()
    
    ns['start_network'] = start_network
    ns['stop_network'] = stop_network


def _add_utility_methods(ns: Dict[str, Any], config: QtxConnectorConfig) -> None:
    """Add utility and helper methods."""
    
    def _setup_qtx_market_data(self, parent_data_source):
        """Initialize QTX market data structures."""
        self._parent_data_source = parent_data_source
        self._udp_queues = {}
        self._udp_subscriptions = set()
    
    ns['_setup_qtx_market_data'] = _setup_qtx_market_data


def _add_debugging_methods(ns: Dict[str, Any], config: QtxConnectorConfig) -> None:
    """Add debugging and introspection methods."""
    
    def __repr__(self):
        """Enhanced representation for debugging."""
        return (
            f"<{self.__class__.__name__}("
            f"backend={self._exchange_backend}, "
            f"qtx={self._qtx_perpetual_host}:{self._qtx_perpetual_port}, "
            f"pairs={len(getattr(self, 'trading_pairs', []))})>"
        )
    
    @classmethod
    def describe_configuration(cls):
        """Describe the connector configuration."""
        return {
            'class_name': cls.__name__,
            'module': cls.__module__,
            'parent_class': cls.__bases__[0].__name__,
            'overridden_methods': cls._get_overridden_methods()
        }
    
    @classmethod
    def _get_overridden_methods(cls):
        """Get list of methods overridden from parent."""
        parent = cls.__bases__[0]
        overridden = []
        
        for name in dir(cls):
            if not name.startswith('__'):
                cls_attr = getattr(cls, name)
                parent_attr = getattr(parent, name, None)
                if callable(cls_attr) and parent_attr and cls_attr != parent_attr:
                    overridden.append(name)
        
        return overridden
    
    ns['__repr__'] = __repr__
    ns['describe_configuration'] = describe_configuration
    ns['_get_overridden_methods'] = _get_overridden_methods
```

### Step 4: Extract Method Implementations
**File**: `_extracted_market_data_methods.py`

Extract all the market data methods from the current implementation into standalone functions:

```python
"""
Extracted market data methods from the original implementation.
These are used by the dynamic class factory.
"""

async def _setup_qtx_udp_subscriptions(self):
    """Set up UDP subscriptions for trading pairs."""
    # Start UDP listener
    await self.udp_manager.start()
    
    # Subscribe to trading pairs
    from . import qtx_perpetual_trading_pair_utils as trading_pair_utils
    
    for trading_pair in self.trading_pairs:
        try:
            qtx_symbol = trading_pair_utils.convert_to_qtx_trading_pair(
                trading_pair, 
                self._exchange_name_on_qtx
            )
            self._udp_subscriptions.add(qtx_symbol)
            
            # Subscribe and get queues
            queues = await self.udp_manager.subscribe_and_get_queues(trading_pair)
            self._udp_queues[trading_pair] = queues
            
            self.logger().info(
                f"Subscribed to QTX UDP: {qtx_symbol} → {trading_pair}"
            )
        except Exception as e:
            self.logger().error(f"Error subscribing to {trading_pair}: {e}")

# ... Extract all other methods similarly ...
```

### Step 5: Update Main Module with Enhanced Parameter Handling
**File**: `qtx_perpetual_derivative.py`

Replace the entire current implementation with:

```python
"""
QTX Perpetual Connector: Dynamic Runtime Inheritance Architecture

This module provides the main entry point for creating QTX connectors
that combine QTX market data with parent exchange trading APIs.
"""

import logging
from typing import TYPE_CHECKING, List, Optional, Type

from .qtx_perpetual_factory import create_qtx_connector_class
from .qtx_perpetual_config import QtxConnectorConfig
from .qtx_perpetual_protocol import QtxConnectorProtocol

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter

logger = logging.getLogger(__name__)

# Exchange connector mapping for supported exchanges
# This stays here as it's core configuration, not just constants
EXCHANGE_CONNECTOR_CLASSES = {
    "binance": {
        "module": "hummingbot.connector.derivative.binance_perpetual.binance_perpetual_derivative",
        "class": "BinancePerpetualDerivative",
        "exchange_name_on_qtx": "binance-futures",
        "api_key_param": "binance_perpetual_api_key",
        "api_secret_param": "binance_perpetual_api_secret"
    },
    "okx": {
        "module": "hummingbot.connector.derivative.okx_perpetual.okx_perpetual_derivative",
        "class": "OkxPerpetualDerivative",
        "exchange_name_on_qtx": "okx-futures",
        "api_key_param": "okx_perpetual_api_key",
        "api_secret_param": "okx_perpetual_secret_key"
    },
    "bitget": {
        "module": "hummingbot.connector.derivative.bitget_perpetual.bitget_perpetual_derivative",
        "class": "BitgetPerpetualDerivative",
        "exchange_name_on_qtx": "bitget-futures",
        "api_key_param": "bitget_perpetual_api_key",
        "api_secret_param": "bitget_perpetual_secret_key"
    },
    "bybit": {
        "module": "hummingbot.connector.derivative.bybit_perpetual.bybit_perpetual_derivative",
        "class": "BybitPerpetualDerivative",
        "exchange_name_on_qtx": "bybit-futures",
        "api_key_param": "bybit_perpetual_api_key",
        "api_secret_param": "bybit_perpetual_secret_key"
    },
    "kucoin": {
        "module": "hummingbot.connector.derivative.kucoin_perpetual.kucoin_perpetual_derivative",
        "class": "KucoinPerpetualDerivative",
        "exchange_name_on_qtx": "kucoin-futures",
        "api_key_param": "kucoin_perpetual_api_key",
        "api_secret_param": "kucoin_perpetual_secret_key"
    },
    "gate_io": {
        "module": "hummingbot.connector.derivative.gate_io_perpetual.gate_io_perpetual_derivative",
        "class": "GateIoPerpetualDerivative",
        "exchange_name_on_qtx": "gate-io-futures",
        "api_key_param": "gate_io_perpetual_api_key",
        "api_secret_param": "gate_io_perpetual_secret_key"
    }
}


def QtxPerpetualDerivative(
    client_config_map: "ClientConfigAdapter",
    qtx_perpetual_host: str,
    qtx_perpetual_port: int,
    exchange_backend: str,
    qtx_place_order_shared_memory_name: str = None,
    trading_pairs: Optional[List[str]] = None,
    trading_required: bool = True,
    **kwargs  # Accept all exchange-specific parameters
) -> QtxConnectorProtocol:
    """
    Create a QTX perpetual connector instance.
    
    This function creates a dynamic connector class that inherits from the
    specified exchange backend and overrides specific methods to use QTX's
    market data feed and order routing.
    
    Args:
        client_config_map: Hummingbot client configuration
        qtx_perpetual_host: QTX UDP server host
        qtx_perpetual_port: QTX UDP server port
        exchange_backend: Parent exchange to inherit from (binance, okx, bitget, bybit, kucoin, gate_io)
        qtx_place_order_shared_memory_name: Optional shared memory segment name
        trading_pairs: List of trading pairs to trade
        trading_required: Whether trading is required (affects API key validation)
        **kwargs: Exchange-specific parameters (API keys, etc.)
        
    Returns:
        Instance of dynamically created QTX connector
        
    Raises:
        ValueError: If configuration is invalid
        ImportError: If parent exchange module cannot be loaded
    """
    # Create and validate configuration
    config = QtxConnectorConfig(
        qtx_perpetual_host=qtx_perpetual_host,
        qtx_perpetual_port=qtx_perpetual_port,
        qtx_place_order_shared_memory_name=qtx_place_order_shared_memory_name,
        exchange_backend=exchange_backend,
        trading_pairs=trading_pairs,
        trading_required=trading_required,
        raw_kwargs=kwargs  # Store all kwargs for flexible parameter extraction
    )
    
    # Validate API credentials if trading is required
    config.validate_api_credentials()
    
    # Create dynamic class
    connector_class = create_qtx_connector_class(config, client_config_map)
    
    # Prepare initialization parameters
    init_params = {
        "client_config_map": client_config_map,
        "trading_pairs": trading_pairs,
        "trading_required": trading_required,
        # Include QTX parameters for potential override in __init__
        "qtx_perpetual_host": qtx_perpetual_host,
        "qtx_perpetual_port": qtx_perpetual_port,
        "qtx_place_order_shared_memory_name": qtx_place_order_shared_memory_name,
        "exchange_backend": exchange_backend,
    }
    
    # Add all exchange-specific parameters from kwargs
    init_params.update(config.get_exchange_init_params())
    
    # Create and return instance
    logger.info(
        f"Creating {connector_class.__name__} instance for "
        f"{len(trading_pairs or [])} trading pairs"
    )
    
    return connector_class(**init_params)
```

### Step 6: Create Comprehensive Tests
**File**: `test_qtx_perpetual_factory.py`

```python
import unittest
import asyncio
from unittest.mock import Mock, patch, AsyncMock
from decimal import Decimal

from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_factory import (
    create_qtx_connector_class, 
    _CLASS_CACHE
)
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_config import QtxConnectorConfig
from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_protocol import QtxConnectorProtocol


class TestQtxDynamicFactory(unittest.TestCase):
    """Test suite for QTX dynamic class factory."""
    
    def setUp(self):
        """Set up test configuration."""
        self.config = QtxConnectorConfig(
            exchange_backend="binance",
            qtx_perpetual_host="127.0.0.1",
            qtx_perpetual_port=8080,
            qtx_place_order_shared_memory_name="/test_shm",
            trading_required=True,
            trading_pairs=["BTC-USDT"],
            raw_kwargs={
                "binance_perpetual_api_key": "test_key",
                "binance_perpetual_api_secret": "test_secret"
            }
        )
        self.client_config = Mock()
        
        # Clear cache before each test
        _CLASS_CACHE.clear()
    
    def test_class_creation_all_exchanges(self):
        """Test class creation for all supported exchanges."""
        test_cases = [
            ("binance", "QtxBinancePerpetualConnector", 
             {"binance_perpetual_api_key": "key", "binance_perpetual_api_secret": "secret"}),
            ("okx", "QtxOkxPerpetualConnector",
             {"okx_perpetual_api_key": "key", "okx_perpetual_secret_key": "secret"}),
            ("bitget", "QtxBitgetPerpetualConnector",
             {"bitget_perpetual_api_key": "key", "bitget_perpetual_secret_key": "secret"}),
            ("bybit", "QtxBybitPerpetualConnector",
             {"bybit_perpetual_api_key": "key", "bybit_perpetual_secret_key": "secret"}),
            ("kucoin", "QtxKucoinPerpetualConnector",
             {"kucoin_perpetual_api_key": "key", "kucoin_perpetual_secret_key": "secret"}),
            ("gate_io", "QtxGate_ioPerpetualConnector",
             {"gate_io_perpetual_api_key": "key", "gate_io_perpetual_secret_key": "secret"}),
        ]
        
        for exchange, expected_class_name, api_params in test_cases:
            with self.subTest(exchange=exchange):
                config = QtxConnectorConfig(
                    exchange_backend=exchange,
                    qtx_perpetual_host="127.0.0.1",
                    qtx_perpetual_port=8080,
                    trading_required=False,  # Don't require API keys for test
                    raw_kwargs=api_params
                )
                
                with patch('importlib.import_module') as mock_import:
                    # Mock the parent module and class
                    mock_module = Mock()
                    mock_parent_class = type(f'{exchange.capitalize()}PerpetualDerivative', (), {})
                    setattr(mock_module, f'{exchange.capitalize()}PerpetualDerivative', mock_parent_class)
                    mock_import.return_value = mock_module
                    
                    connector_class = create_qtx_connector_class(config, self.client_config)
                    
                    # Verify class name
                    self.assertEqual(connector_class.__name__, expected_class_name)
    
    def test_api_credential_extraction(self):
        """Test correct extraction of API credentials for each exchange."""
        test_cases = [
            ("binance", "binance_perpetual_api_key", "binance_perpetual_api_secret"),
            ("okx", "okx_perpetual_api_key", "okx_perpetual_secret_key"),
            ("bitget", "bitget_perpetual_api_key", "bitget_perpetual_secret_key"),
            ("bybit", "bybit_perpetual_api_key", "bybit_perpetual_secret_key"),
            ("kucoin", "kucoin_perpetual_api_key", "kucoin_perpetual_secret_key"),
            ("gate_io", "gate_io_perpetual_api_key", "gate_io_perpetual_secret_key"),
        ]
        
        for exchange, key_param, secret_param in test_cases:
            with self.subTest(exchange=exchange):
                config = QtxConnectorConfig(
                    exchange_backend=exchange,
                    qtx_perpetual_host="127.0.0.1",
                    qtx_perpetual_port=8080,
                    trading_required=False,
                    raw_kwargs={
                        key_param: f"{exchange}_test_key",
                        secret_param: f"{exchange}_test_secret"
                    }
                )
                
                api_key, api_secret = config.get_api_credentials()
                self.assertEqual(api_key, f"{exchange}_test_key")
                self.assertEqual(api_secret, f"{exchange}_test_secret")
    
    def test_parameter_passthrough(self):
        """Test that all kwargs are properly passed through."""
        extra_params = {
            "custom_param_1": "value1",
            "custom_param_2": 123,
            "custom_param_3": True
        }
        
        config = QtxConnectorConfig(
            exchange_backend="binance",
            qtx_perpetual_host="127.0.0.1",
            qtx_perpetual_port=8080,
            trading_required=False,
            raw_kwargs={
                "binance_perpetual_api_key": "key",
                "binance_perpetual_api_secret": "secret",
                **extra_params
            }
        )
        
        exchange_params = config.get_exchange_init_params()
        
        # Verify all custom params are included
        for param, value in extra_params.items():
            self.assertIn(param, exchange_params)
            self.assertEqual(exchange_params[param], value)
        
        # Verify QTX params are excluded
        qtx_params = ['qtx_perpetual_host', 'qtx_perpetual_port', 
                      'qtx_place_order_shared_memory_name', 'exchange_backend']
        for param in qtx_params:
            self.assertNotIn(param, exchange_params)
    
    def test_protocol_conformance(self):
        """Test that generated class conforms to protocol."""
        connector_class = create_qtx_connector_class(self.config, self.client_config)
        
        # Check protocol conformance
        self.assertTrue(issubclass(connector_class, QtxConnectorProtocol))
        
        # Verify required methods exist
        required_methods = [
            '_place_order', '_place_cancel', 
            '_create_order_book_data_source',
            'start_network', 'stop_network'
        ]
        
        for method in required_methods:
            self.assertTrue(hasattr(connector_class, method))
    
    def test_caching_behavior(self):
        """Test that identical configs return cached classes."""
        # First call
        class1 = create_qtx_connector_class(self.config, self.client_config)
        
        # Second call with same config
        class2 = create_qtx_connector_class(self.config, self.client_config)
        
        # Should be the same object
        self.assertIs(class1, class2)
        
        # Different config should create new class
        different_config = QtxConnectorConfig(
            exchange_backend="okx",
            qtx_perpetual_host="127.0.0.1",
            qtx_perpetual_port=8080,
            trading_required=False,
            raw_kwargs={}
        )
        
        with patch('importlib.import_module') as mock_import:
            mock_module = Mock()
            mock_module.OkxPerpetualDerivative = type('OkxPerpetualDerivative', (), {})
            mock_import.return_value = mock_module
            
            class3 = create_qtx_connector_class(different_config, self.client_config)
            
            self.assertIsNot(class1, class3)
            self.assertEqual(class3.__name__, "QtxOkxPerpetualConnector")
    
    def test_thread_safety(self):
        """Test thread-safe caching."""
        import concurrent.futures
        
        results = []
        
        def create_class():
            cls = create_qtx_connector_class(self.config, self.client_config)
            results.append(cls)
        
        # Create classes from multiple threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(create_class) for _ in range(10)]
            concurrent.futures.wait(futures)
        
        # All should be the same instance
        self.assertEqual(len(results), 10)
        self.assertTrue(all(cls is results[0] for cls in results))
    
    def test_configuration_validation(self):
        """Test configuration validation."""
        # Invalid exchange
        with self.assertRaises(ValueError) as cm:
            QtxConnectorConfig(
                exchange_backend="invalid_exchange",
                qtx_perpetual_host="127.0.0.1",
                qtx_perpetual_port=8080,
                raw_kwargs={}
            )
        self.assertIn("Unsupported exchange", str(cm.exception))
        
        # Invalid port
        with self.assertRaises(ValueError) as cm:
            QtxConnectorConfig(
                exchange_backend="binance",
                qtx_perpetual_host="127.0.0.1",
                qtx_perpetual_port=99999,
                raw_kwargs={}
            )
        self.assertIn("Invalid port", str(cm.exception))
        
        # Missing API key when trading required
        config = QtxConnectorConfig(
            exchange_backend="binance",
            qtx_perpetual_host="127.0.0.1",
            qtx_perpetual_port=8080,
            trading_required=True,
            raw_kwargs={}  # No API credentials
        )
        
        with self.assertRaises(ValueError) as cm:
            config.validate_api_credentials()
        self.assertIn("API credentials required", str(cm.exception))
        self.assertIn("binance_perpetual_api_key", str(cm.exception))


if __name__ == '__main__':
    unittest.main()
```

## Implementation Order

### Phase 1: Foundation (Day 1-2)
1. Create `qtx_perpetual_protocol.py` with complete protocol definition
2. Create `qtx_perpetual_config.py` with Pydantic validation and enhanced parameter handling
3. Set up basic test structure

### Phase 2: Factory Implementation (Day 3-4)
1. Implement `qtx_perpetual_factory.py` with caching
2. Extract method groups into separate modules:
   - `_extracted_market_data_methods.py`
   - `_extracted_order_methods.py`
   - `_extracted_lifecycle_methods.py`
3. Implement all namespace population functions with proper parameter handling

### Phase 3: Integration (Day 5)
1. Replace `qtx_perpetual_derivative.py` with new implementation
2. Keep `EXCHANGE_CONNECTOR_CLASSES` in the main module
3. Update imports in all files that use the connector
4. Run existing tests to ensure compatibility

### Phase 4: Testing & Optimization (Day 6-7)
1. Run comprehensive test suite including all 6 exchanges
2. Performance testing and optimization
3. Add additional debugging utilities
4. Documentation updates

## Key Benefits of This Refactoring

1. **Proper Architecture**: Uses `types.new_class()` following Python best practices
2. **Type Safety**: Full protocol definition with type hints
3. **Performance**: Class caching eliminates redundant creation
4. **Maintainability**: Modular design with extracted methods
5. **Debugging**: Enhanced introspection and meaningful class names
6. **Thread Safety**: Proper synchronization for concurrent use
7. **Validation**: Pydantic-based configuration validation
8. **Testing**: Comprehensive test coverage from day one
9. **Flexibility**: Handles all 6 exchanges with different parameter naming
10. **Parameter Passthrough**: Properly handles all exchange-specific parameters

## Files to Create/Modify

### New Files:
- `qtx_perpetual_protocol.py` - Protocol definition
- `qtx_perpetual_config.py` - Configuration model with exchange-specific handling
- `qtx_perpetual_factory.py` - Factory implementation
- `_extracted_market_data_methods.py` - Extracted market data logic
- `_extracted_order_methods.py` - Extracted order management logic
- `_extracted_lifecycle_methods.py` - Extracted lifecycle methods
- `test_qtx_perpetual_factory.py` - Comprehensive tests

### Modified Files:
- `qtx_perpetual_derivative.py` - Complete replacement with clean implementation (keeps EXCHANGE_CONNECTOR_CLASSES)

This refactoring plan provides a clean, direct transformation from the current problematic implementation to a best-practices architecture without any migration concerns, while properly handling all 6 supported exchanges and their varying parameter requirements.