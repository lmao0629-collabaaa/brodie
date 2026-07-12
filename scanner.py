import requests
import time
import json
import os
from datetime import datetime, timezone

# ==== CONFIGURATION (set these as Environment Variables on your host) ====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TARGET_CHAIN = os.environ.get("TARGET_CHAIN", "robinhood")      # dexscreener chain id
TARGET_TICKER = os.environ.get("TARGET_TICKER", "")             # optional; leave blank to match ANY ticker
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
MAX_AGE_MINUTES = int(os.environ.get("MAX_AGE_MINUTES", "30"))     # "new pair" / win-watch window
WIN_MULTIPLIER = float(os.environ.get("WIN_MULTIPLIER", "3"))      # price/mcap growth (from first-seen) that counts as a "win"

# Manually specified wallets to watch (comma-separated full addresses). Optional.
WALLET_ADDRESSES = [
    w.strip().lower() for w in os.environ.get("WALLET_ADDRESSES", "").split(",") if w.strip()
]

# "Smart money" auto-discovery settings
SMART_MONEY_MIN_WINS = int(os.environ.get("SMART_MONEY_MIN_WINS", "2"))       # wins needed before we trust a wallet
EARLY_BUY_WINDOW_MINUTES = int(os.environ.get("EARLY_BUY_WINDOW_MINUTES", "10"))  # "early" = bought within this long of launch

TRACKED_FILE = "tracked_pairs.json"
WALLET_TRACKED_FILE = "wallet_tracked.json"
SMART_WALLETS_FILE = "smart_wallets.json"
# ===========================================================================

DEX_TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain}/{token}"
BLOCKSCOUT_BASE = "https://robinhoodchain.blockscout.com/api/v2"

# In-memory state for wallets discovered via the smart-money logic
SMART_WALLETS = {}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def load_json_file(path):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_json_file(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Missing bot token or chat id — skipping send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print(f"[Telegram error] {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[Telegram exception] {e}")


def pair_age_minutes(pair, now_ms):
    created_at_ms = pair.get("pairCreatedAt")
    if not created_at_ms:
        return None
    return (now_ms - created_at_ms) / 60000


# ---------------------------------------------------------------------------
# Coin scanner (Dexscreener)
# ---------------------------------------------------------------------------

def get_new_tokens_on_chain():
    """Returns tokenAddresses that are newly listed on TARGET_CHAIN."""
    try:
        r = requests.get(DEX_TOKEN_PROFILES_URL, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[Dexscreener token-profiles error] {e}")
        return []

    items = data if isinstance(data, list) else data.get("profiles", [])
    tokens = []
    for item in items:
        if (item.get("chainId") or "").lower() == TARGET_CHAIN.lower():
            token_address = item.get("tokenAddress")
            if token_address:
                tokens.append(token_address)
    return tokens


def get_best_pair_for_token(token_address):
    """Fetch trading pair data for a token; return the pair with the most liquidity."""
    url = DEX_TOKEN_PAIRS_URL.format(chain=TARGET_CHAIN, token=token_address)
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        pairs = r.json()
    except Exception as e:
        print(f"[Dexscreener token-pairs error] {e}")
        return None
    if not pairs:
        return None
    pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd", 0), reverse=True)
    return pairs[0]


# ---------------------------------------------------------------------------
# Blockscout helpers (on-chain data for Robinhood Chain)
# ---------------------------------------------------------------------------

def parse_blockscout_timestamp(ts_str):
    if not ts_str:
        return None
    try:
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp() * 1000
    except Exception:
        return None


def get_early_buyers(token_address, pair_created_at_ms):
    """Return wallet addresses that received this token within EARLY_BUY_WINDOW_MINUTES of launch."""
    url = f"{BLOCKSCOUT_BASE}/tokens/{token_address}/transfers"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[Blockscout early-buyers error] {e}")
        return []

    items = data.get("items", [])
    window_ms = EARLY_BUY_WINDOW_MINUTES * 60000
    buyers = set()

    for tx in items:
        ts_ms = parse_blockscout_timestamp(tx.get("timestamp"))
        if ts_ms is None or pair_created_at_ms is None:
            continue
        if ts_ms - pair_created_at_ms > window_ms:
            continue
        to_addr = ((tx.get("to") or {}).get("hash") or "").lower()
        if to_addr:
            buyers.add(to_addr)

    return list(buyers)


def get_incoming_token_transfers(wallet_address):
    """Fetch recent incoming ERC-20 token transfers for a wallet."""
    url = f"{BLOCKSCOUT_BASE}/addresses/{wallet_address}/token-transfers"
    try:
        r = requests.get(url, params={"type": "ERC-20", "filter": "to"}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[Blockscout error for {wallet_address}] {e}")
        return []
    return data.get("items", [])


# ---------------------------------------------------------------------------
# Smart-money discovery
# ---------------------------------------------------------------------------

def record_winner_and_scan_early_buyers(pair):
    """Called when a pair crosses the mcap threshold. Credits early buyers as 'wins'."""
    token_address = pair.get("baseToken", {}).get("address")
    created_at_ms = pair.get("pairCreatedAt")
    if not token_address or not created_at_ms:
        return

    buyers = get_early_buyers(token_address, created_at_ms)
    for wallet in buyers:
        entry = SMART_WALLETS.get(wallet, {"win_count": 0, "promoted": False})
        entry["win_count"] += 1
        SMART_WALLETS[wallet] = entry

        if entry["win_count"] >= SMART_MONEY_MIN_WINS and not entry["promoted"]:
            entry["promoted"] = True
            print(
                f"[Smart money promoted] {wallet} — {entry['win_count']} early wins. "
                f"Now watching its buys."
            )

    save_json_file(SMART_WALLETS_FILE, SMART_WALLETS)


def all_watched_wallets():
    promoted = [w for w, e in SMART_WALLETS.items() if e.get("promoted")]
    return list(set(WALLET_ADDRESSES) | set(promoted))


# ---------------------------------------------------------------------------
# Coin scanner processing
# ---------------------------------------------------------------------------

def process_pair(pair, tracked, baseline_mode=False):
    pair_address = pair.get("pairAddress")
    symbol = pair.get("baseToken", {}).get("symbol") or ""

    if not pair_address:
        return False
    if TARGET_TICKER and symbol.lower() != TARGET_TICKER.lower():
        return False

    changed = False
    entry = tracked.get(pair_address)
    if entry is None:
        entry = {
            "new_alerted": False,
            "win_alerted": False,
            "initial_mcap": pair.get("marketCap") or pair.get("fdv"),
        }
        tracked[pair_address] = entry
        changed = True

    now_ms = time.time() * 1000
    name = pair.get("baseToken", {}).get("name", "Unknown")
    price = pair.get("priceUsd", "N/A")
    liquidity = (pair.get("liquidity") or {}).get("usd", "N/A")
    url = pair.get("url", "")
    age_minutes = pair_age_minutes(pair, now_ms)

    # ---- New pair detected (logged only — no longer sent to Telegram) ----
    if not entry["new_alerted"]:
        if baseline_mode:
            entry["new_alerted"] = True
            changed = True
        else:
            is_fresh = age_minutes is None or age_minutes <= MAX_AGE_MINUTES
            if is_fresh:
                print(f"[New pair] ${symbol} ({name}) — {url}")
            entry["new_alerted"] = True
            changed = True

    # ---- Win detection: price/mcap grew WIN_MULTIPLIER-x from when we first saw it (logged only, feeds smart-money scoring) ----
    if not entry["win_alerted"] and not baseline_mode:
        if age_minutes is not None and age_minutes <= MAX_AGE_MINUTES:
            current_mcap = pair.get("marketCap") or pair.get("fdv")
            initial_mcap = entry.get("initial_mcap")
            if current_mcap and initial_mcap and current_mcap >= initial_mcap * WIN_MULTIPLIER:
                growth = current_mcap / initial_mcap
                print(f"[Win] ${symbol} grew {growth:.1f}x (mcap ${initial_mcap:,.0f} -> ${current_mcap:,.0f}) at {age_minutes:.1f} min old — scanning early buyers")
                entry["win_alerted"] = True
                changed = True
                record_winner_and_scan_early_buyers(pair)
        elif age_minutes is not None and age_minutes > MAX_AGE_MINUTES:
            entry["win_alerted"] = True
            changed = True

    return changed


def check_dexscreener(tracked, baseline_mode=False):
    token_addresses = get_new_tokens_on_chain()
    changed = False

    for token_address in token_addresses:
        pair = get_best_pair_for_token(token_address)
        if pair is None:
            continue
        if process_pair(pair, tracked, baseline_mode=baseline_mode):
            changed = True

    if changed:
        save_json_file(TRACKED_FILE, tracked)


# ---------------------------------------------------------------------------
# Wallet-buy tracker (manual + auto-discovered smart wallets)
# ---------------------------------------------------------------------------

def check_wallets(wallet_tracked):
    changed = False

    for wallet in all_watched_wallets():
        first_time = wallet not in wallet_tracked
        transfers = get_incoming_token_transfers(wallet)
        seen_hashes = set(wallet_tracked.get(wallet, []))
        new_hashes = []

        for tx in transfers:
            tx_hash = tx.get("transaction_hash") or tx.get("tx_hash")
            if not tx_hash or tx_hash in seen_hashes:
                continue
            new_hashes.append(tx_hash)

            if first_time:
                continue  # silent baseline for this wallet's first check

            token = tx.get("token", {}) or {}
            token_symbol = token.get("symbol", "Unknown")
            token_name = token.get("name", "Unknown token")
            token_address = token.get("address", "")
            total = (tx.get("total") or {}).get("value", "N/A")

            msg = (
                "Smart money just bought a coin\n\n"
                f"Wallet: {wallet}\n"
                f"Token: {token_name} (${token_symbol})\n"
                f"Amount (raw units): {total}\n"
                f"Token address: {token_address}\n"
                f"Tx: https://robinhoodchain.blockscout.com/tx/{tx_hash}"
            )
            print(msg)
            send_telegram(msg)

        if new_hashes:
            wallet_tracked[wallet] = list(seen_hashes.union(new_hashes))
            changed = True

    if changed:
        save_json_file(WALLET_TRACKED_FILE, wallet_tracked)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global SMART_WALLETS

    print("Starting Dexscreener -> Telegram scanner...")
    ticker_note = f"${TARGET_TICKER}" if TARGET_TICKER else "ANY ticker"
    print(f"Watching {ticker_note} on chain '{TARGET_CHAIN}' | "
          f"win window: {MAX_AGE_MINUTES} min | win condition: {WIN_MULTIPLIER}x growth from first-seen mcap")
    print(f"Smart-money discovery: {SMART_MONEY_MIN_WINS} wins required, "
          f"early-buy window: {EARLY_BUY_WINDOW_MINUTES} min")

    tracked = load_json_file(TRACKED_FILE)
    print(f"Loaded {len(tracked)} previously tracked pairs from disk.")

    SMART_WALLETS = load_json_file(SMART_WALLETS_FILE)
    promoted_count = sum(1 for e in SMART_WALLETS.values() if e.get("promoted"))
    print(f"Loaded {len(SMART_WALLETS)} candidate wallets ({promoted_count} promoted to smart money).")

    wallet_tracked = load_json_file(WALLET_TRACKED_FILE)
    print(f"Manually watching {len(WALLET_ADDRESSES)} wallets, plus {promoted_count} auto-discovered.")

    if not tracked:
        print("No saved history found — running a silent baseline sync first (no alerts for existing pairs)...")
        check_dexscreener(tracked, baseline_mode=True)
        print(f"Baseline complete. {len(tracked)} existing pairs recorded. Now watching for NEW ones and mcap moves.")

    while True:
        check_dexscreener(tracked)
        if all_watched_wallets():
            check_wallets(wallet_tracked)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
