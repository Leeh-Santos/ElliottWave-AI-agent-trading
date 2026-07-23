#!/usr/bin/env python3
"""
Interactive market LONG with leverage on Hyperliquid perps.

Asks for everything at runtime (private key is typed hidden, never stored),
then places a market buy via the official hyperliquid-python-sdk.

Usage:
    .venv/bin/python long_trade.py
"""
import getpass
import sys

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL


def ask(prompt, default=None, cast=str, validate=None):
    """Prompt until the input casts and validates; empty input takes the default."""
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if raw == "" and default is not None:
            raw = str(default)
        try:
            value = cast(raw)
        except ValueError:
            print("  Invalid value, try again.")
            continue
        if validate is not None:
            error = validate(value)
            if error:
                print(f"  {error}")
                continue
        return value


def main():
    print("=" * 60)
    print("  Hyperliquid — market LONG with leverage")
    print("=" * 60)

    # --- Network ---
    net = ask("Network: (m)ainnet or (t)estnet", default="m",
              cast=lambda s: s.lower()[0],
              validate=lambda v: None if v in ("m", "t") else "Enter m or t.")
    base_url = MAINNET_API_URL if net == "m" else TESTNET_API_URL
    print(f"Using {'MAINNET' if net == 'm' else 'TESTNET'} ({base_url})")

    # --- Private key (hidden input, kept only in memory) ---
    pk = getpass.getpass("Private key (input hidden): ").strip()
    if pk.startswith("0x"):
        pk = pk[2:]
    try:
        wallet = Account.from_key(bytes.fromhex(pk))
    except Exception:
        sys.exit("Invalid private key.")
    # If you trade through an API wallet, the funded account is a different address.
    account_address = input(
        f"Main account address (Enter if it's the key's own address {wallet.address}): "
    ).strip() or wallet.address
    print(f"Trading for account: {account_address}")

    # --- Connect and load market data ---
    info = Info(base_url, skip_ws=True)
    exchange = Exchange(wallet, base_url, account_address=account_address)

    meta = info.meta()
    mids = info.all_mids()
    assets = {a["name"]: a for a in meta["universe"] if not a.get("isDelisted")}

    state = info.user_state(account_address)
    withdrawable = float(state["withdrawable"])
    account_value = float(state["marginSummary"]["accountValue"])
    print(f"\nAccount value: ${account_value:,.2f}   Free margin: ${withdrawable:,.2f}")

    # --- Pair ---
    def check_pair(name):
        if name not in assets:
            matches = [n for n in assets if name in n]
            hint = f" Did you mean: {', '.join(matches[:8])}?" if matches else ""
            return f"Unknown pair.{hint}"
        return None

    coin = ask("Pair (e.g. BTC, ETH, SOL)", cast=lambda s: s.upper(), validate=check_pair)
    asset = assets[coin]
    max_lev = asset["maxLeverage"]
    sz_decimals = asset["szDecimals"]
    price = float(mids[coin])
    print(f"{coin}-PERP  mid price ${price:,.6g}   max leverage {max_lev}x")

    # --- Leverage & margin mode ---
    leverage = ask(f"Leverage (1-{max_lev})", default=min(5, max_lev), cast=int,
                   validate=lambda v: None if 1 <= v <= max_lev else f"Must be 1-{max_lev}.")
    mode = ask("Margin mode: (c)ross or (i)solated", default="c",
               cast=lambda s: s.lower()[0],
               validate=lambda v: None if v in ("c", "i") else "Enter c or i.")
    is_cross = mode == "c"

    # --- Size ---
    unit = ask(f"Size in (u)sd notional or ({coin.lower()[0]}) {coin} units", default="u",
               cast=lambda s: s.lower()[0],
               validate=lambda v: None if v in ("u", coin.lower()[0]) else "Pick one.")
    if unit == "u":
        usd = ask("Position size in USD (notional, after leverage)", cast=float,
                  validate=lambda v: None if v >= 10 else "Hyperliquid minimum order is $10.")
        sz = round(usd / price, sz_decimals)
    else:
        sz = ask(f"Size in {coin}", cast=float,
                 validate=lambda v: None if v * price >= 10 else
                 f"Order value ${v * price:.2f} is below the $10 minimum.")
        sz = round(sz, sz_decimals)
    if sz <= 0:
        sys.exit(f"Size rounds to 0 at {sz_decimals} decimals — too small for {coin}.")
    notional = sz * price
    margin_needed = notional / leverage

    # --- Slippage ---
    slippage = ask("Max slippage % for the market order", default=1.0, cast=float,
                   validate=lambda v: None if 0 < v <= 10 else "Use 0-10.") / 100

    # --- Confirm ---
    print("\n" + "-" * 60)
    print(f"  LONG {sz:g} {coin}  (~${notional:,.2f} notional)")
    print(f"  Leverage: {leverage}x {'cross' if is_cross else 'isolated'}"
          f"   margin used ~${margin_needed:,.2f}")
    print(f"  Market order, max slippage {slippage:.2%}")
    print(f"  Network: {'MAINNET' if net == 'm' else 'TESTNET'}")
    print("-" * 60)
    if margin_needed > withdrawable:
        print(f"  WARNING: margin needed exceeds free margin (${withdrawable:,.2f}).")
    if input("Execute? (yes/no): ").strip().lower() not in ("y", "yes"):
        sys.exit("Aborted, nothing sent.")

    # --- Execute ---
    lev_result = exchange.update_leverage(leverage, coin, is_cross)
    if lev_result.get("status") != "ok":
        sys.exit(f"Failed to set leverage: {lev_result}")
    print(f"Leverage set to {leverage}x {'cross' if is_cross else 'isolated'}.")

    result = exchange.market_open(coin, True, sz, None, slippage)
    if result.get("status") != "ok":
        sys.exit(f"Order failed: {result}")

    for status in result["response"]["data"]["statuses"]:
        if "filled" in status:
            f = status["filled"]
            print(f"\nFILLED: {f['totalSz']} {coin} @ avg ${float(f['avgPx']):,.6g} "
                  f"(oid {f['oid']})")
        elif "error" in status:
            sys.exit(f"\nOrder error: {status['error']}")
        else:
            print(f"\nOrder status: {status}")

    # --- Show resulting position ---
    state = info.user_state(account_address)
    for p in state["assetPositions"]:
        pos = p["position"]
        if pos["coin"] == coin:
            print(f"Position: {pos['szi']} {coin}  entry ${float(pos['entryPx']):,.6g}  "
                  f"liq px {pos['liquidationPx']}  margin ${float(pos['marginUsed']):,.2f}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
