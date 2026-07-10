import requests
import time
import json
import os

# ==== CONFIGURATION (set these as Environment Variables on your host) ====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TARGET_CHAIN = os.environ.get("TARGET_CHAIN", "robinhood")      # dexscreener chain id
TARGET_TICKER = os.environ.get("TARGET_TICKER", "")             # optional; leave blank to match ANY ticker
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
MAX_AGE_MINUTES = int(os.environ.get("MAX_AGE_MINUTES", "10"))  # "new pair" / mcap-watch window
MCAP_THRESHOLD = float(os.environ.get("MCAP_THRESHOLD", "50000"))  # market cap alert trigger
TRACKED_FILE = "tracked_pairs.json"
# ===========================================================================

TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain}/{token}"


def load_tracked():
    if os.path.exists(TRACKED_FILE):
        try:
            with open(TRACKED_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_tracked(tracked):
    with open(TRACKED_FILE, "w") as f:
        json.dump(tracked, f)


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


def get_new_tokens_on_chain():
    """Returns a list of tokenAddresses that are newly listed on TARGET_CHAIN."""
    try:
        r = requests.get(TOKEN_PROFILES_URL, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[Dexscreener token-profiles error] {e}")
        return []

    # This endpoint can return either a list or {"profiles": [...]} depending on version — handle both.
    items = data if isinstance(data, list) else data.get("profiles", [])

    tokens = []
    for item in items:
        if (item.get("chainId") or "").lower() == TARGET_CHAIN.lower():
            token_address = item.get("tokenAddress")
            if token_address:
                tokens.append(token_address)
    return tokens


def get_best_pair_for_token(token_address):
    """Fetch trading pair data for a token and return the pair with the most liquidity."""
    url = TOKEN_PAIRS_URL.format(chain=TARGET_CHAIN, token=token_address)
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


def process_pair(pair, tracked, baseline_mode=False):
    pair_address = pair.get("pairAddress")
    symbol = (pair.get("baseToken", {}).get("symbol") or "")

    if not pair_address:
        return False
    if TARGET_TICKER and symbol.lower() != TARGET_TICKER.lower():
        return False

    changed = False
    entry = tracked.get(pair_address)
    if entry is None:
        entry = {"new_alerted": False, "mcap_alerted": False}
        tracked[pair_address] = entry
        changed = True

    now_ms = time.time() * 1000
    name = pair.get("baseToken", {}).get("name", "Unknown")
    price = pair.get("priceUsd", "N/A")
    liquidity = (pair.get("liquidity") or {}).get("usd", "N/A")
    url = pair.get("url", "")
    age_minutes = pair_age_minutes(pair, now_ms)

    # ---- Alert #1: brand new pair ----
    if not entry["new_alerted"]:
        if baseline_mode:
            entry["new_alerted"] = True
            changed = True
        else:
            is_fresh = age_minutes is None or age_minutes <= MAX_AGE_MINUTES
            if is_fresh:
                msg = (
                    f"New pair on Robinhood Chain: ${symbol}\n\n"
                    f"Name: {name}\n"
                    f"Price: ${price}\n"
                    f"Liquidity: ${liquidity}\n"
                    f"Link: {url}"
                )
                print(msg)
                send_telegram(msg)
            entry["new_alerted"] = True
            changed = True

    # ---- Alert #2: hit $50k market cap while still young ----
    if not entry["mcap_alerted"] and not baseline_mode:
        if age_minutes is not None and age_minutes <= MAX_AGE_MINUTES:
            market_cap = pair.get("marketCap") or pair.get("fdv")
            if market_cap and market_cap >= MCAP_THRESHOLD:
                msg = (
                    f"${symbol} hit ${MCAP_THRESHOLD:,.0f} market cap "
                    f"(age: {age_minutes:.1f} min)\n\n"
                    f"Name: {name}\n"
                    f"Market Cap: ${market_cap:,.0f}\n"
                    f"Price: ${price}\n"
                    f"Liquidity: ${liquidity}\n"
                    f"Link: {url}"
                )
                print(msg)
                send_telegram(msg)
                entry["mcap_alerted"] = True
                changed = True
        elif age_minutes is not None and age_minutes > MAX_AGE_MINUTES:
            entry["mcap_alerted"] = True
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
        save_tracked(tracked)


def main():
    print("Starting Dexscreener -> Telegram scanner...")
    ticker_note = f"${TARGET_TICKER}" if TARGET_TICKER else "ANY ticker"
    print(f"Watching {ticker_note} on chain '{TARGET_CHAIN}' | "
          f"new-pair window: {MAX_AGE_MINUTES} min | mcap alert: ${MCAP_THRESHOLD:,.0f}")

    tracked = load_tracked()
    print(f"Loaded {len(tracked)} previously tracked pairs from disk.")

    if not tracked:
        print("No saved history found — running a silent baseline sync first (no alerts for existing pairs)...")
        check_dexscreener(tracked, baseline_mode=True)
        print(f"Baseline complete. {len(tracked)} existing pairs recorded. Now watching for NEW ones and mcap moves.")

    while True:
        check_dexscreener(tracked)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
