# QTX Perpetual Connector Evaluation

This document compares the QTX perpetual connector implementation against the detailed Perpetual Connector Checklist.

## Special Architecture Note

The QTX perpetual connector uses a hybrid approach that diverges from the standard connector architecture:
1. It uses Binance's implementation for order execution, authentication, and REST API communication
2. It uses QTX's UDP feed for market data (orderbook and trades)

This hybrid approach explains why many standard components are delegated to Binance's implementation rather than being explicitly defined in the QTX connector. Additional components specific to the UDP architecture are also present.

## Implementation Evaluation

### CONSTANTS

- [ ] `DEFAULT_DOMAIN` - Check if more than 1 domain is needed, used for generating path URL in web_utils
- [ ] `REST_URLS` - Could be a dictionary like ByBit connector or an f-string like Binance connector, using DEFAULT_DOMAIN
- [ ] `SERVER_TIME_PATH_URL` - Check if exchange needs timestamp sync with server time

Additional Constants Not in Checklist:
- [ ] `DEFAULT_UDP_HOST` - Specific to QTX's architecture
- [ ] `DEFAULT_UDP_PORT` - Specific to QTX's architecture
- [ ] `DEFAULT_UDP_BUFFER_SIZE` - Specific to QTX's architecture
- [ ] `DIFF_MESSAGE_TYPE` - For UDP message handling
- [ ] `TRADE_MESSAGE_TYPE` - For UDP message handling
- [ ] `MAX_ORDER_ID_LEN` - Inherited from Binance
- [ ] `BROKER_ID` - Inherited from Binance
- [ ] `FUNDING_SETTLEMENT_DURATION` - Inherited from Binance
- [ ] `ORDER_STATE` - Inherited from Binance
- [ ] `RATE_LIMITS` - Present, using a similar structure to Binance's rate limits

### Web Utils

- [ ] Copy the Bybit Perpetual connector web_utils file
- [ ] Replace Bybit Perpetual for connector_name with first letter uppercase
- [ ] Replace Bybit Perpetual for connector_name
- [ ] Check for linear perpetual logic or remove
- [ ] Implement functions to get results or success from REST response
- [ ] Implement functions to extract topics and payload from websocket channels
- [ ] Code functions to create REST URL, check if public and private URL needed
- [ ] `get_current_server_time` - Check for server time in response
- [ ] Time synchronization - Check if needed and implement relevant functions
- [ ] `build_api_factory` - Implement with or without time synchronizer

Additional Web Utils:
- [ ] `build_api_url` - Implement or delegate to Binance
- [ ] `build_ws_url` - Implement or delegate to Binance

### Utils

- [ ] Copy the Bybit Perpetual connector utils file
- [ ] Replace Bybit for connector_name with first letter uppercase
- [ ] Replace bybit_perpetual for connector_name
- [ ] Replace `DEFAULT_FEES` with appropriate values
- [ ] Check for connection to other domains or delete
- [ ] Check if linear trading needed or delete
- [ ] `CENTRALIZED` - Set appropriately
- [ ] `EXAMPLE_PAIR` - Set appropriately

Additional Utils:
- [ ] `get_client_order_id` - Implement
- [ ] `get_exchange_order_type` - Implement
- [ ] Position mode conversions - Implement for internal/exchange conversion
- [ ] Order status mapping - Implement for converting statuses

### Order Book Data Source

- [ ] Copy the Bybit Perpetual connector order book data source file
- [ ] Replace Bybit for connector_name with first letter uppercase
- [ ] Replace bybitl for connector_name
- [ ] Replace `HEARTBEAT_TIME_INTERVAL` with appropriate value
- [ ] Implement order book snapshot REST functionality
- [ ] Implement funding info REST functionality
- [ ] Implement WebSocket/UDP subscription management
- [ ] Implement trade message parsing
- [ ] Implement order book diff message parsing
- [ ] Implement order book snapshot message parsing
- [ ] Implement funding info message parsing

Additional Components:
- [ ] `qtx_perpetual_udp_manager.py` - Special component for UDP market data handling

### Auth

- [ ] Copy the Bybit Perpetual connector auth file
- [ ] Replace Bybit for connector_name with first letter uppercase
- [ ] Implement `rest_authenticate` for authentication
- [ ] Implement `add_auth_to_params` 
- [ ] Implement `header_for_authentication`
- [ ] Implement `_generate_signature`
- [ ] Implement `ws_authenticate` for WebSockets

### User Stream Data Source

- [ ] Copy the Bybit perpetual connector user stream data source file
- [ ] Replace Bybit for connector_name with first letter uppercase
- [ ] Replace bybit for connector_name
- [ ] Replace `HEARTBEAT_TIME_INTERVAL` with appropriate value
- [ ] Check if `LISTEN_KEY_KEEP_ALIVE_INTERVAL` is needed
- [ ] Implement `_get_listen_key` if needed
- [ ] Implement `_ping_listen_key` if needed
- [ ] Implement `_connected_websocket_assistant`
- [ ] Implement `_subscribe_channels`
- [ ] Implement `listen_for_user_stream`

### Exchange

- [ ] Copy the Bybit Perpetual connector derivative file
- [ ] Replace Bybit for connector_name with first letter uppercase
- [ ] Replace bybit for connector_name
- [ ] Implement Generic test class methods
- [ ] Implement ExchangePyBase methods
- [ ] Implement PerpetualDerivativePyBase methods
- [ ] Implement Time synchronizer methods
- [ ] Implement Order fills methods
- [ ] Implement Order status methods
- [ ] Implement User stream event listener methods
- [ ] Implement Position mode methods
- [ ] Implement Funding info methods

### Config File

- [ ] Add connector_name_api_key and connector_name_api_secret to conf_global_TEMPLATE.yml

## Test Methods Evaluation

This section details the test methods required for each component.

### Order Book Data Source Tests

REST Tests:
- [ ] `test_get_new_order_book_successful`
- [ ] `test_get_new_order_book_raises_exception`
- [ ] `test_get_funding_info`

WebSocket Tests:
- [ ] `test_listen_for_subscriptions_subscribes_to_trades_and_order_diffs_and_funding_info`
- [ ] `test_listen_for_subscriptions_raises_cancel_exception`
- [ ] `test_listen_for_subscriptions_logs_exception_details`
- [ ] `test_subscribe_channels_raises_cancel_exception`
- [ ] `test_subscribe_channels_raises_exception_and_logs_error`
- [ ] `test_listen_for_trades_successful`
- [ ] `test_listen_for_trades_cancelled_when_listening`
- [ ] `test_listen_for_trades_logs_exception`
- [ ] `test_listen_for_order_book_diffs_successful`
- [ ] `test_listen_for_order_book_diffs_cancelled`
- [ ] `test_listen_for_order_book_diffs_logs_exception`
- [ ] `test_listen_for_order_book_snapshots_cancelled_when_fetching_snapshot`
- [ ] `test_listen_for_order_book_snapshots_log_exception`
- [ ] `test_listen_for_order_book_snapshots_successful`
- [ ] `test_listen_for_funding_info_cancelled_when_listening`
- [ ] `test_listen_for_funding_info_logs_exception`
- [ ] `test_listen_for_funding_info_successful`

### Auth Tests

REST Tests:
- [ ] `test_rest_authenticate`

### User Stream Data Source Tests

WebSocket Tests:
- [ ] `test_get_listen_key_log_exception`
- [ ] `test_get_listen_key_successful`
- [ ] `test_ping_listen_key_log_exception`
- [ ] `test_ping_listen_key_successful`
- [ ] `test_manage_listen_key_task_loop_keep_alive_failed`
- [ ] `test_manage_listen_key_task_loop_keep_alive_successful`
- [ ] `test_listen_for_user_stream_get_listen_key_successful_with_user_update_event`
- [ ] `test_listen_for_user_stream_does_not_queue_empty_payload`
- [ ] `test_listen_for_user_stream_connection_failed`
- [ ] `test_listen_for_user_stream_iter_message_throws_exception`

### Exchange Tests

Generic Test Class Methods:
- [ ] `all_symbols_request_mock_response`
- [ ] `latest_prices_request_mock_response`
- [ ] `all_symbols_including_invalid_pair_mock_response`
- [ ] `network_status_request_successful_mock_response`
- [ ] `trading_rules_request_mock_response`
- [ ] `trading_rules_request_erroneous_mock_response`
- [ ] `order_creation_request_successful_mock_response`
- [ ] `balance_request_mock_response_for_base_and_quote`
- [ ] `balance_request_mock_response_only_base`
- [ ] `balance_event_websocket_update`
- [ ] `expected_latest_price`
- [ ] `expected_supported_order_types`
- [ ] `expected_trading_rule`
- [ ] `expected_logged_error_for_erroneous_trading_rule`
- [ ] `expected_exchange_order_id`
- [ ] `is_cancel_request_executed_synchronously_by_server`
- [ ] `is_order_fill_http_update_included_in_status_update`
- [ ] `is_order_fill_http_update_executed_during_websocket_order_event_processing`
- [ ] `expected_partial_fill_price`
- [ ] `expected_partial_fill_amount`
- [ ] `expected_fill_fee`
- [ ] `expected_fill_trade_id`
- [ ] `exchange_symbol_for_tokens`
- [ ] `create_exchange_instance`
- [ ] `validate_auth_credentials_present`
- [ ] `validate_order_creation_request`
- [ ] `validate_order_cancelation_request`
- [ ] `validate_order_status_request`
- [ ] `validate_trades_request`
- [ ] `configure_successful_cancelation_response`
- [ ] `configure_erroneous_cancelation_response`
- [ ] `configure_one_successful_one_erroneous_cancel_all_response`
- [ ] `configure_completely_filled_order_status_response`
- [ ] `configure_canceled_order_status_response`
- [ ] `configure_erroneous_http_fill_trade_response`
- [ ] `configure_open_order_status_response`
- [ ] `configure_http_error_order_status_response`
- [ ] `configure_partially_filled_order_status_response`
- [ ] `configure_partial_fill_trade_response`
- [ ] `configure_full_fill_trade_response`
- [ ] `order_event_for_new_order_websocket_update`
- [ ] `order_event_for_canceled_order_websocket_update`
- [ ] `order_event_for_full_fill_websocket_update`
- [ ] `trade_event_for_full_fill_websocket_update`
- [ ] `empty_funding_payment_mock_response`
- [ ] `expected_supported_position_modes`
- [ ] `funding_info_mock_response`
- [ ] `funding_info_url`
- [ ] `funding_payment_url`
- [ ] `funding_payment_mock_response`
- [ ] `configure_failed_set_leverage`
- [ ] `configure_successful_set_leverage`
- [ ] `position_event_for_full_fill_websocket_update`
- [ ] `funding_info_event_for_websocket_update`
- [ ] `configure_successful_set_position_mode`
- [ ] `configure_failed_set_position_mode`

Exchange-Specific Tests:
- [ ] `test_update_time_synchronizer_successfully`
- [ ] `test_update_time_synchronizer_failure_is_logged`
- [ ] `test_update_time_synchronizer_raises_cancelled_error`
- [ ] `test_time_synchronizer_related_request_error_detection`
- [ ] `test_update_order_fills_from_trades_triggers_filled_event`
- [ ] `test_update_order_fills_request_parameters`
- [ ] `test_update_order_fills_from_trades_with_repeated_fill_triggers_only_one_event`
- [ ] `test_update_order_status_when_failed`
- [ ] `test_user_stream_update_for_order_failure`
- [ ] `test_set_position_mode_failure`
- [ ] `test_set_position_mode_success`
- [ ] `test_listen_for_funding_info_update_initializes_funding_info`
- [ ] `test_listen_for_funding_info_update_updates_funding_info`
- [ ] `test_init_funding_info`
- [ ] `test_update_funding_info_polling_loop_success`
- [ ] `test_update_funding_info_polling_loop_raise_exception`

## Conclusion

The QTX perpetual connector follows a non-standard hybrid architecture that delegates many standard components to Binance's implementation while adding specific components for handling QTX's UDP market data feed. This approach is valid but diverges from the standard checklist which assumes a standalone connector implementation.

Key differences from the standard checklist:
1. **Hybrid Architecture**: Uses Binance for order execution and authentication, QTX for market data
2. **Delegated Components**: Instead of implementing all components separately, many are simply delegated to Binance's implementation
3. **Additional UDP Components**: Contains specialized components for handling UDP market data that aren't part of the standard checklist
4. **Trading Pair Utilities**: Contains specialized trading pair utilities to handle conversion between QTX and Binance formats

The implementation status is being tracked through this checklist, with test methods separated into their own section for clarity. 