#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["web3>=6.0", "py-clob-client-v2>=1.0.1", "python-dotenv"]
# ///
"""Migrate wallet collateral for CLOB v2: USDC.e → pUSD + exchange approvals.

TurtleQuant uses py-clob-client-v2, which tracks pUSD balance/allowance — not raw
USDC.e on V1 exchange contracts. Run this once before live trading.

Usage:
    cd /opt/polymarket/app/turtlequant
    set -a && source .env && set +a
    uv run scripts/migrate_pusd_v2.py --dry-run
    uv run scripts/migrate_pusd_v2.py
    uv run scripts/migrate_pusd_v2.py --wrap-usd 50   # wrap only $50

See: https://docs.polymarket.com/v2-migration
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"

V2_SPENDERS = [
    ("CTF Exchange", "0xE111180000d2663C0091e4f400237545B87B996B"),
    ("Neg Risk CTF Exchange", "0xe2222d279d744050d28e00520010520000310F59"),
    ("Neg Risk Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
]

ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

ONRAMP_ABI = [
    {
        "name": "wrap",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_asset", "type": "address"},
            {"name": "_to", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [],
    },
]

MAX_UINT256 = 2**256 - 1
APPROVAL_MIN_USD = 1000.0


def _token_balance(w3: Web3, token_addr: str, wallet: str) -> tuple[float, int]:
    token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    raw = token.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
    dec = token.functions.decimals().call()
    return float(raw) / (10**dec), raw


def _allowance_usd(w3: Web3, token_addr: str, owner: str, spender: str) -> float:
    token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    raw = token.functions.allowance(
        Web3.to_checksum_address(owner),
        Web3.to_checksum_address(spender),
    ).call()
    dec = token.functions.decimals().call()
    return float(raw) / (10**dec)


def _clob_collateral(wallet: str, private_key: str, signature_type: int, funder: str) -> dict:
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    kwargs: dict = {
        "host": os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com"),
        "chain_id": 137,
        "key": private_key,
    }
    if signature_type:
        kwargs["signature_type"] = signature_type
    if funder:
        kwargs["funder"] = funder

    client = ClobClient(**kwargs)
    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
    client.update_balance_allowance(params)
    return client.get_balance_allowance(params)


def _print_clob_status(label: str, raw: dict) -> None:
    bal = float(raw.get("balance", 0)) / 1_000_000
    allowances = raw.get("allowances") or {}
    if isinstance(allowances, dict) and allowances:
        min_alw = min(float(v) for v in allowances.values()) / 1_000_000
    else:
        alw_raw = raw.get("allowance", 0)
        min_alw = float(alw_raw) / 1_000_000 if alw_raw else 0.0
    print(f"  {label}: CLOB balance=${bal:.2f}  min_allowance=${min_alw:.2f}")


def _send_tx(w3: Web3, account, tx: dict, dry_run: bool, label: str) -> None:
    if dry_run:
        print(f"  [dry-run] would send: {label}")
        return

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  tx {label}: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"transaction reverted: {label}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Wrap USDC.e to pUSD and set CLOB v2 allowances")
    parser.add_argument("--dry-run", action="store_true", help="Print plan only; send no transactions")
    parser.add_argument(
        "--wrap-usd",
        type=float,
        default=0.0,
        metavar="USD",
        help="USDC.e to wrap (default: all on-chain USDC.e balance)",
    )
    parser.add_argument("--skip-wrap", action="store_true", help="Only approve pUSD for v2 spenders")
    args = parser.parse_args()

    private_key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PRIVATE_KEY", "")
    funder = os.getenv("POLYMARKET_FUNDER") or os.getenv("FUNDER_ADDRESS", "")
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", os.getenv("SIGNATURE_TYPE", "0")))
    rpc = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")

    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY or PRIVATE_KEY required")
        return 1
    if signature_type == 1 and not funder:
        print("ERROR: POLYMARKET_FUNDER / FUNDER_ADDRESS required for SIGNATURE_TYPE=1")
        return 1

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to Polygon RPC: {rpc}")
        return 1

    account = w3.eth.account.from_key(private_key)
    wallet = Web3.to_checksum_address(funder) if signature_type == 1 else account.address

    print(f"Wallet       : {wallet}")
    print(f"Sig type     : {signature_type}")
    print(f"RPC          : {rpc}")
    print(f"Mode         : {'dry-run' if args.dry_run else 'live'}")
    print()

    matic = w3.eth.get_balance(wallet) / 1e18
    print(f"MATIC balance: {matic:.4f}")
    if matic < 0.01:
        print("WARNING: low MATIC — approve/wrap txs need gas")
    print()

    usdce_bal, usdce_raw = _token_balance(w3, USDC_E, wallet)
    pusd_bal, _ = _token_balance(w3, PUSD, wallet)
    print(f"USDC.e balance : ${usdce_bal:.6f}")
    print(f"pUSD balance   : ${pusd_bal:.6f}")
    print()

    try:
        clob_before = _clob_collateral(wallet, private_key, signature_type, funder)
        _print_clob_status("Before", clob_before)
    except Exception as exc:
        print(f"  Before: CLOB check failed ({exc})")
    print()

    wrap_raw = 0
    if not args.skip_wrap:
        wrap_usd = args.wrap_usd if args.wrap_usd > 0 else usdce_bal
        wrap_raw = int(wrap_usd * 1_000_000)
        if wrap_raw <= 0:
            print("Nothing to wrap (USDC.e balance is 0).")
        elif wrap_raw > usdce_raw:
            print(f"ERROR: --wrap-usd {wrap_usd:.2f} exceeds USDC.e balance ${usdce_bal:.6f}")
            return 1
        else:
            onramp_allow = _allowance_usd(w3, USDC_E, wallet, ONRAMP)
            print(f"Onramp USDC.e allowance: ${onramp_allow:,.2f}")
            nonce = w3.eth.get_transaction_count(wallet)
            usdce = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)

            if onramp_allow < wrap_usd:
                print(f"Approving Onramp for ${wrap_usd:.2f} USDC.e ...")
                tx = usdce.functions.approve(
                    Web3.to_checksum_address(ONRAMP), MAX_UINT256
                ).build_transaction(
                    {
                        "from": wallet,
                        "nonce": nonce,
                        "gas": 100_000,
                        "maxFeePerGas": w3.eth.gas_price * 2,
                        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
                        "chainId": 137,
                    }
                )
                _send_tx(w3, account, tx, args.dry_run, "approve USDC.e → Onramp")
                if not args.dry_run:
                    nonce += 1

            print(f"Wrapping ${wrap_raw / 1e6:.6f} USDC.e → pUSD ...")
            onramp = w3.eth.contract(address=Web3.to_checksum_address(ONRAMP), abi=ONRAMP_ABI)
            tx = onramp.functions.wrap(
                Web3.to_checksum_address(USDC_E),
                wallet,
                wrap_raw,
            ).build_transaction(
                {
                    "from": wallet,
                    "nonce": nonce,
                    "gas": 250_000,
                    "maxFeePerGas": w3.eth.gas_price * 2,
                    "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
                    "chainId": 137,
                }
            )
            _send_tx(w3, account, tx, args.dry_run, "wrap USDC.e → pUSD")
            if not args.dry_run:
                nonce += 1

    pusd_bal, _ = _token_balance(w3, PUSD, wallet)
    print(f"\npUSD balance after wrap: ${pusd_bal:.6f}")

    nonce = w3.eth.get_transaction_count(wallet)
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD), abi=ERC20_ABI)
    for name, spender in V2_SPENDERS:
        alw = _allowance_usd(w3, PUSD, wallet, spender)
        print(f"pUSD → {name}: allowance ${alw:,.2f}", end="")
        if alw >= APPROVAL_MIN_USD:
            print("  ✓")
            continue
        print("  → approving MAX ...")
        tx = pusd.functions.approve(Web3.to_checksum_address(spender), MAX_UINT256).build_transaction(
            {
                "from": wallet,
                "nonce": nonce,
                "gas": 100_000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
                "chainId": 137,
            }
        )
        _send_tx(w3, account, tx, args.dry_run, f"approve pUSD → {name}")
        if not args.dry_run:
            nonce += 1

    print()
    try:
        clob_after = _clob_collateral(wallet, private_key, signature_type, funder)
        _print_clob_status("After", clob_after)
    except Exception as exc:
        print(f"  After: CLOB check failed ({exc})")
        return 1

    if args.dry_run:
        print("\nDry-run complete. Re-run without --dry-run to execute.")
    else:
        print("\nDone. CLOB v2 collateral should be ready for TurtleQuant --live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
