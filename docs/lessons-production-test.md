# Production Test Lessons (2026-07-01)

## 1. SG API Field Names (vs US)

| Field | US API | SG API (actual) |
|-------|--------|----------------|
| symbol location | `ticker.symbol` | `symbol` (top-level) |
| quantity type | number | **string** (`"3.0000000000"`) |
| cost price | `costPrice` | `cost_price` |
| market price | `marketPrice` | `last_price` |
| unrealized P&L | `unrealizedProfitLoss` | `unrealized_profit_loss` |

**Fix**: `get_positions()` in executor.py must handle both formats.

## 2. Order Parameters

- `order_type`: use `"MARKET"` / `"LIMIT"` (string), NOT `"MKT"` / `"LMT"`
- `client_order_id`: max **32 characters** (`uuid.uuid4().hex` = 32, OK)
- `quantity`: can be int or string (string works)
- `market` value: `"US"` for US stocks on SG platform

## 3. Buying Power Buffer

Market orders require extra buying power buffer (~5-10% above estimated cost):
```
USD cash:    $384.88
GLD cost:    $373.50  →  MARKET order FAILED (insufficient BP)
IAU cost:    $231.00  →  LIMIT order OK
```

**Fix**: Always use preview before market order, or prefer LIMIT orders.

## 4. Market Hours

Market order submitted at ~17:06 UTC (13:06 ET) on a Wednesday — filled instantly.
- Regular session: Mon-Fri 09:30-16:00 ET
- Core session flag: `support_trading_session: CORE`

## 5. Account Structure (SG)

- Base currency: SGD (`total_asset_currency: "SGD"`)
- USD sub-account holds USD cash and positions
- Buying power per currency is in `account_currency_assets[]`
- Account type: `MRGN` (margin) / cash available

## 6. Test Environments

| Environment | Host | Reachable | Read APIs | Order API |
|-------------|------|-----------|-----------|-----------|
| SG Prod | `api.webull.com.sg` | ✅ | ✅ | ✅ |
| SG UAT | `sg-api.uat.webullbroker.com` | ❌ timeout | - | - |
| US UAT | `us-openapi-alb.uat.webullbroker.com` | ✅ | ✅ | ❌ SDK signing bug |
| HK Sandbox | `api.sandbox.webull.hk` | ✅ | ✅ | untested |

**Shared test credentials** (from docs): US and HK have public test accounts.
**Known bug**: SDK `ClientInitializer.initializer()` calls `/openapi/config/v2/getConfig` which returns 401 on UAT. Must patch/skip it.

## 7. Token Lifecycle

- Token cached at `conf/token.txt` (encrypted)
- Stays NORMAL if called within 15 days
- Weekly keepalive on Wed 12:00 Beijing = 04:00 UTC
- Keepalive calls `get_account_list()` (lightest API)
- Token refresh: SDK auto-refreshes on each API call (resets 15-day counter)

## 8. SDK Quirks

- `is_not_upgrade_api_host()` determines signing algorithm
  - SG prod (`api.webull.com.sg`): NOT in upgrade_hosts → SHA-256
  - US prod (`api.webull.com`): IS in upgrade_hosts → SHA-1
- TradeClient init requires token check endpoint to return 200
- Position API returns different JSON structure per region (v1 vs v2)
- `order_v3.preview_order()` → `estimated_cost` field
- `order_v3.place_order()` returns 200 with `{order_id, client_order_id}`
