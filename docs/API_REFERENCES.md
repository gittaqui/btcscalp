# Official API references

Reviewed 2026-09-21. Only official Gemini documentation informed the adapter. Endpoint availability and account permissions must still be verified with the opt-in sandbox suite and deployment smoke checks.

| Contract | Reference |
|---|---|
| Current public WebSocket URL and full initial snapshot option | https://developer.gemini.com/websocket/introduction |
| Depth/trade schemas, nanosecond times, buyer-maker flag, order stream events | https://developer.gemini.com/websocket/streams |
| First differential frame is the snapshot when requested; sequence handling | https://developer.gemini.com/trading/websocket/streams |
| Requests, responses and event envelopes | https://developer.gemini.com/websocket/message-format |
| Public subscribe method | https://developer.gemini.com/websocket/playground |
| WebSocket HMAC upgrade headers | https://developer.gemini.com/websocket/authentication |
| Environment URLs and seeded test balances | https://developer.gemini.com/get-started/sandbox |
| REST maker-or-cancel, IOC and stop-limit order options | https://developer.gemini.com/trading/rest-api/orders/create-new-order |
| Client-order-ID status lookup and individual fill details | https://developer.gemini.com/trading/rest-api/orders/get-order-status |
| Past trades and broken-trade handling | https://developer.gemini.com/trading/rest-api/orders/list-past-trades |
| Account API fees, optional symbol-specific rate request | https://developer.gemini.com/trading/rest-api/orders/get-notional-trading-volume |
| Instrument increments, minimum size and status | https://developer.gemini.com/trading/rest-api/market-data/get-symbol-details |
| Additional fractional precision on fills | https://developer.gemini.com/market-data/symbols-and-minimums |

REST HMAC signs the base64 JSON payload with SHA384. The nonce is persisted and increases across process restarts. WebSocket authentication uses the separately documented base64 epoch-seconds nonce format; it is not the REST signing payload.

Public data uses `wss://ws.gemini.com?snapshot=-1`; sandbox uses `wss://ws.sandbox.gemini.com?snapshot=-1`. The adapter deliberately does not depend on the legacy v1/v2 market-data protocol. Symbol metadata and API fees are fetched at startup rather than assuming publicly advertised increments or fee tiers.

Gemini's status and past-trades APIs are reconciled conservatively. If a history response fills the 500-row page, the implementation stops for offline reconciliation instead of guessing pagination through a potentially missing period. Routine live reconciliation uses overlapping recent history and per-order trade details. Long-outage recovery must be sandbox-certified before operation.
