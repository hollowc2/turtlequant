# Plan: trade live through a Polymarket deposit wallet

Handoff for an implementing agent. Written 2026-11-21 after the first live order
was rejected. Read `docs/OPS.md` ("Live bot VPN" and the live sections) first.

## Where things stand

- **Live is stopped on purpose.** `turtlequant-bot-live` on helios is `Exited`
  (stopped by hand, so it will not restart). `turtlequant-vpn` and
  `turtlequant-grafana-exporter-live` are still up. Shadow is unaffected and keeps
  running. Order intents 1 and 2 are resolved as `failed` (neither reached the book).
- **Wallet:** an EOA, `0xe9B7…3984` (full address in the helios `.env` /
  `POLYMARKET_PRIVATE_KEY`; never print or log the key). It holds about **$101.61 pUSD**
  as CLOB collateral and about 39.9 POL. The three exchange contracts have MAX pUSD
  allowance and CTF `setApprovalForAll` from the EOA.
- **Blocker 1 (fixed):** helios's address (Paris, FR) is geoblocked, so
  `POST /order` returned 403. Live now goes through `turtlequant-vpn` (gluetun v3.41.3,
  PIA, Portugal). See the memory note and `docs/OPS.md`. Do not remove the VPN.
- **Blocker 2 (this plan):** with the VPN the order reaches validation and fails:
  `400 {"error":"maker address not allowed, please use the deposit wallet flow"}`.
  The bot signs as a plain EOA (signature type 0) and the CLOB rejects it.

## What Polymarket requires (from the docs; NOT yet tested here)

Sources: <https://docs.polymarket.com/trading/deposit-wallets>,
<https://docs.polymarket.com/migrate/clob-sdk-to-unified-sdk>, and the
`Polymarket/py-clob-client-v2` issues #51, #53, #56, #64, #70, #75, #77, #83, #85, #90.

- Wallets created on or after 2026-05-04 are **deposit wallets** (smart-contract
  wallets). EOA trading is described as for "allowlisted" EOAs only; the docs do not say
  how to be allowlisted. Assume ours is not.
- A deposit wallet is deterministic per signer (CREATE2) and is a **new address**.
  Deploy it gaslessly through the relayer, which needs a **Builder API key**
  (polymarket.com → Settings → Builders: key, secret, passphrase).
  - `POST https://relayer-v2.polymarket.com/submit` with
    `{"type":"WALLET-CREATE","from":"<signer>","to":"0x00000000000Fb5C9ADea0298D729A0CB3823Cc07","metadata":"Deploy Deposit Wallet"}`;
    headers `POLY_BUILDER_API_KEY`, `POLY_BUILDER_TIMESTAMP`, `POLY_BUILDER_PASSPHRASE`,
    `POLY_BUILDER_SIGNATURE` (urlsafe-base64 of HMAC-SHA256 over
    `timestamp + "POST" + "/submit" + body`, keyed with the base64-decoded secret).
  - Poll `GET /transaction?id=<id>` until `STATE_CONFIRMED`; the result has `proxyAddress`.
- After deploying: fund it (plain pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`
  ERC-20 transfer from the EOA; the docs imply this, not state it), then set approvals
  through the relayer (`type: "WALLET"` batch signed with EIP-712 domain
  `DepositWallet`/`1`/137), then `GET clob.polymarket.com/balance-allowance/update`
  with `signature_type=3`.
- CLOB API creds are derived from the **signer EOA**; the deposit wallet is the
  **funder**. Client config: `signature_type=3` (`POLY_1271`), `funder=<deposit wallet>`.
- Unified SDK `polymarket-client` (`import polymarket`) is the recommended path and
  includes deployment and approval helpers; the Python v2 client has open issues with
  type-3 orders ("the order signer address has to be the address of the API KEY").

## Unknowns to settle first (do not assume)

1. Does `py-clob-client-v2` (we pin 1.0.1; 1.2.2 exists) place a type-3 order at all, or
   do we need `polymarket-client` (or a Rust helper)? Try a spike before designing.
2. Can pUSD be withdrawn from the deposit wallet back to the EOA (relayer
   `transferErc20`)? The docs do not say transfers to arbitrary addresses are allowed.
3. Is the EOA maybe allowlistable? (Probably not worth chasing; deposit wallet is the default.)

## Phases

Each phase ends with a check and a stop. Use throwaway amounts (**$1**) until phase 4.

1. **Spike (no money).** In a scratch dir, with the Builder key in env, prove:
   derive CLOB creds for the EOA; compute the deposit-wallet address from the factory
   formula and confirm it matches what the relayer returns. Decide the client library.
   *Done when:* a type-3 client object builds and `get_balance_allowance` runs for the
   deposit wallet.
2. **Deploy and fund $1.** `scripts/setup_deposit_wallet.py` (new, idempotent, like
   `scripts/migrate_pusd_v2.py`): deploy via relayer, poll, print the wallet address,
   transfer $1 pUSD from the EOA, run the relayer approval batch (all three exchanges,
   the V2 exchange `0xe3333700cA9d93003F00f0F71f8515005F6c00Aa`, Neg Risk Adapter
   `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`, CTF), then sync the CLOB cache.
   Print only the host of any RPC URL and never print keys (same rule as
   `migrate_pusd_v2.py`). *Done when:* CLOB reports $1 collateral for signature type 3.
3. **Withdraw test.** Send the $1 back to the EOA through the relayer. *Done when:* the
   EOA balance rises by $1. If this is impossible, stop and tell the user before funding
   more.
4. **Wire the bot.** The code already reads `POLYMARKET_SIGNATURE_TYPE` and
   `POLYMARKET_FUNDER` (`src/turtlequant/clob_execution.py:44-71, 435-456`; also
   `scripts/reconcile_nav.py`, `scripts/derive_clob_api_creds.py`).
   - `docker-compose.live.yml` hard-codes `POLYMARKET_SIGNATURE_TYPE: "0"` and
     `POLYMARKET_FUNDER: ""`: change them to `3` and `${POLYMARKET_FUNDER:-}`.
   - Check every place that assumes the wallet is the EOA: balance reads for sizing
     (`wallet_collateral_usd`), `order_reconciliation.py`, `reconcile_nav.py`, and
     `migrate_pusd_v2.py`, which refuses `SIGNATURE_TYPE != 0` on purpose.
   - Add tests: config parsing for type 3 and a funder, and the balance read uses the
     funder.
5. **One real order.** Fund the deposit wallet with the rest (~$100), start the live
   bot (via the VPN) with `MAX_ORDER_USD=6`, and wait for the first fill. Then run
   `scripts/reconcile_nav.py --positions /opt/turtlequant/state/live-state/turtlequant-positions.json`,
   confirm the order event matched and `order_intents` has nothing outstanding.
6. **Permanent deployment.** Back to `docker compose -f docker-compose.live.yml -f releases/<r>/compose.live.yml up -d --no-build`
   with `MAX_ORDER_USD=10`, update `docs/OPS.md`, and update the memory note.

## Rules for whoever implements this

- Secrets: never read or print values from `/opt/turtlequant-app/.env`; refer to key
  names only. Add `POLYMARKET_FUNDER`, `POLY_BUILDER_API_KEY`,
  `POLY_BUILDER_SECRET`, `POLY_BUILDER_PASSPHRASE` to it by name; the user supplies the
  Builder values.
- Do not pass other bots' `PRIVATE_KEY` / `FUNDER_ADDRESS` aliases to live.
- The live bot must only ever run behind `turtlequant-vpn` and only after the geoblock
  endpoint (`https://polymarket.com/api/geoblock`, browser User-Agent) says
  `"blocked":false` from inside its network. Restart the bot whenever the VPN
  container is recreated.
- Do not move more than $1 until phase 3 passes.
- A 400/403 on `POST /order` leaves an intent unresolved and halts entries. Resolve
  it with `scripts/order_intents.py` only after confirming it never reached the book.
- Helios deploys follow the release pattern in `docs/OPS.md`; the user's other
  thread also creates releases, so check `ls -t /opt/turtlequant-app/releases` first.

## What the user must do

1. Create a Builder API key (Settings → Builders) and add the three values to
   `/opt/turtlequant-app/.env` themselves.
2. Decide whether to proceed if withdrawals from the deposit wallet turn out to be
   restricted.
