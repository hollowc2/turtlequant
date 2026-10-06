# Design: on-chain redemption of resolved live positions

Status: **draft for review, not implemented.** This design moves real funds.

## Problem

In live mode, `Trader.settle` marks a resolved position `pending_redemption` and stops there. The winning tokens stay in the wallet as ERC-1155 conditional tokens, and the pUSD they are worth never comes back. `marked_equity` counts the claim at its resolution value, so NAV looks right, but the cash isn't spendable. Nothing ever removes the position. This is review item 11 (docs/REVIEW-2026-09-24.md).

## What gets redeemed

| Position | Action |
|---|---|
| Resolved, payout > 0 for the held token | One redemption transaction |
| Resolved, payout 0 | No transaction. Settle locally at 0 once the CTF payout is reported (below). The worthless tokens stay as dust; burning them costs gas and returns nothing |
| Resolved on Gamma, but the CTF payout not yet reported | Wait (stays `pending_redemption`) |

"Resolved" means both of these:
- Gamma says resolved, which `fetch_resolution` already checks.
- On chain, `CTF.payoutDenominator(conditionId) > 0`, and `payoutNumerators(conditionId, i) / payoutDenominator` matches Gamma's `outcomePrices` for the held outcome. On a mismatch, refuse to redeem and alert. Never trust one source alone.

## Contract calls

Gamma's `negRisk` flag picks the path. As of 2026-10-05, the daily price ladder ("less than" / "between" / "greater than") is `negRisk: true`. "above" and the touch markets ("reach", "hit", "dip") are `negRisk: false`. The bot already parses and can trade "greater than", so both paths are needed.

**Standard market:** ConditionalTokens (CTF, `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`):
```
redeemPositions(collateralToken, parentCollectionId = 0x0, conditionId, indexSets)
```
- `indexSets` is `[1]` for YES and `[2]` for NO. Only the held side is listed, because both are binary partitions.
- The call burns the caller's **entire** balance of those positions and pays `balance × numerator / denominator` in `collateralToken`.

**Neg-risk market:** NegRiskAdapter (`0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`, already in `migrate_pusd_v2.py`):
```
redeemPositions(conditionId, amounts)
```
- `amounts` is `[yes_shares, no_shares]` in 1e6 units, using the wallet's actual balances.
- The adapter pulls the tokens, so the wallet needs `CTF.setApprovalForAll(adapter, true)`.

**Collateral check, before any transaction.** CLOB token ids *are* CTF position ids. So:
- Compute `CTF.getPositionId(collateral, CTF.getCollectionId(0x0, conditionId, indexSet))` and require it to equal the position's `token_id`.
- `collateral` is pUSD (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`) for standard markets, and the adapter's wrapped collateral (`adapter.wcol()`) for neg-risk ones.
- This one check confirms the collateral, the condition id, the index set and the neg-risk path. If it fails, the redemption is refused. Only the operator can clear it, and the code never falls back to a guess, for example USDC.e from the V1 era.

**Allowlist.** The redeem executor can only build calls to two fixed targets: CTF `redeemPositions`, and adapter `redeemPositions`. It never sends value. The key is never used for an arbitrary call.

## Wallet types

| `SIGNATURE_TYPE` | Tokens held by | Who sends the transaction | Gas paid by |
|---|---|---|---|
| 0 (EOA) | EOA | EOA calls the target directly | EOA, in POL |
| 1 (Polymarket proxy) | Proxy (`POLYMARKET_FUNDER`) | EOA calls the proxy factory, `proxy([ (CALL, target, 0, calldata) ])`. The factory forwards it from the EOA's own proxy, so `msg.sender` at CTF/adapter is the proxy and the payout lands in the proxy | EOA, in POL |
| 2 (Gnosis Safe) | Safe | Not supported. Startup refuses `--auto-redeem` with type 2 | — |

**Factory address and ABI.** The factory address and `ProxyCall` ABI need verifying on Polygonscan before implementation: Polymarket's proxy factory is believed to be `0xaB45c5A4B0c941a2F231C04C3f49182e1A254052`. Before sending, check that the factory's proxy address for the EOA equals `POLYMARKET_FUNDER`.

**Gasless option.** Polymarket's relayer could submit gaslessly instead. It needs separate builder credentials and adds an external party between the bot and the chain, so it's not proposed.

**Existing defect, same root cause.** `scripts/migrate_pusd_v2.py` with `SIGNATURE_TYPE=1` builds transactions `from` the proxy address but signs them with the EOA key. They actually run as the EOA, so the proxy's USDC.e is never wrapped or approved. The "execute as funder" helper built here should replace that path. That script also never sets `CTF.setApprovalForAll` for the exchanges or the adapter, which SELL orders and neg-risk redemption both need. Verify both before live.

## Idempotency: the order-intents ledger

Redemptions go through the same crash-safe journal as orders.

**Schema change.** Allow `side = 'REDEEM'`. The CHECK constraint needs the table rebuild `_migrate` already does. A REDEEM row holds:
- `token_id` and `requested` (shares).
- `order_id`: the latest transaction hash.
- `metadata`:
  - path: `ctf` or `neg_risk`
  - condition id and position id
  - wallet, i.e. the token holder
  - EOA nonce
  - every transaction hash sent for this nonce
  - expected payout: `shares × numerator / denominator`

**Lifecycle:**
1. `pending`: journaled before signing, with the nonce reserved. At most one outstanding REDEEM per market.
2. `submitted`: after `send_raw_transaction` returns a hash.
3. `reconciled`: on a receipt with `status == 1`.
   - **Payout:** measured as the sum of pUSD `Transfer` logs to the wallet in that receipt. This works the same way on both paths, and the event fields don't need to be trusted.
   - **Balance check:** the wallet's balance of the position id is now 0.
   - **Settlement:** then `PositionManager.settle_position(market_id, resolution_price)` runs, followed by a `close` event with `reason: "resolved"`, `tx_hash`, `payout_usd` and `gas_pol`.
   - **Payout mismatch:** if the payout differs from expected by more than $0.01, settle at the **measured** payout, write `redeem_mismatch` and alert. One cause is the wallet holding extra tokens of that position, since `redeemPositions` burns the whole balance. The design assumes one bot per wallet; OPS.md should say so.
4. `failed`: the receipt has `status == 0` (nothing moved), or the reserved nonce was consumed by none of our hashes while the position balance is unchanged. A later pass may retry with a new intent.

**Restart and in-process reconciliation.** `reconcile_in_process` currently calls `reconcile_intent`, which would call CLOB `get_order(tx_hash)`. REDEEM intents must go to a separate `reconcile_redemption`, which checks in this order:
1. Look for a receipt for any recorded hash.
2. Otherwise, check whether the EOA nonce has passed the reserved one.
3. Otherwise, check the position balance.

A `pending` row with no hash after a crash is handled by rule 2 or 3. If the nonce is unused and the tokens are still there, mark it `failed`. If the tokens are gone with no receipt of ours, leave it for the operator.

**Entry gate.** Outstanding REDEEM intents do **not** count toward `unreconciled_orders`. They can't cause a duplicate trade, and every redeem transaction would otherwise halt entries for about a minute. A REDEEM outstanding for more than 30 minutes raises an alert instead.

## Failure handling

| Failure | Handling |
|---|---|
| RPC unreachable | Nothing is sent; retry on the next reprice. The position stays `pending_redemption`, still marked at its resolution value |
| Low POL | Under 2× the estimated cost: skip and alert. No partial attempt |
| `eth_call` simulation of the exact transaction reverts | Not sent. Logged with the revert reason; the operator is alerted after 3 consecutive passes |
| Receipt status 0 | Intent `failed`; exponential backoff per market (5 min → 6 h) |
| No receipt after 10 min | Speed up: same nonce, fees × 1.3. Every hash is recorded on the same intent. Never a second nonce while one is outstanding |
| Gamma and CTF payouts disagree, or the position-id check fails | Refuse and alert. The operator decides |
| Payout differs from expected | Settle at the measured payout, record `redeem_mismatch`, alert |

**Fees and gas.** Gas uses EIP-1559: the estimate × 1.2, with the max fee capped by a `--max-redeem-gas-gwei` setting. After a redemption, call the CLOB `update_balance_allowance` so the new pUSD shows in the CLOB balance.

## Rollout

1. **Operator script.** `scripts/redeem_positions.py`, dry-run by default:
   - For each `pending_redemption` position it shows the on-chain checks, the exact calldata, the simulated result and the expected payout.
   - `--execute --market <id>` sends one redemption through the ledger.
   - First real use: one small winning position of each path, standard and neg-risk.
2. **In-loop, behind `--auto-redeem` (default off).** `Trader.settle`'s live branch calls the same executor at most once per market per reprice pass. Turn it on only after step 1 has redeemed both paths cleanly.

**Tests:**
- A fake chain covering every reconcile branch, including crash points between pending, send and receipt.
- Calldata golden tests against the ABIs.
- The position-id derivation checked against a real token id and condition id from a resolved market.

## Open questions for review

1. Should gas count in P&L, or be tracked as an operating cost? The current plan records `gas_pol` on the close event only.
2. Should losing tokens ever be burned? The current plan says no.
3. Direct proxy-factory calls, or Polymarket's relayer? The current plan is direct.
4. Fix `migrate_pusd_v2.py` for proxy wallets in the same change, or separately first?
