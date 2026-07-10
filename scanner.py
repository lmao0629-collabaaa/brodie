import requests
import time
import json
import os

# ==== CONFIGURATION (set these as Environment Variables on your host — see README) ====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TARGET_TICKER = os.environ.get("TARGET_TICKER", "brodie")     # case-insensitive
TARGET_CHAIN = os.environ.get("TARGET_CHAIN", "robinhood")    # dexscreener chain id
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
SEEN_FILE = "seen_pairs.json"
# =======================================================================================

DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


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


def check_dexscreener(seen):
    try:
        r = requests.get(DEX_SEARCH_URL, params={"q": TARGET_TICKER}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[Dexscreener error] {e}")
        return

    pairs = data.get("pairs") or []
    new_found = False

    for pair in pairs:
        chain_id = (pair.get("chainId") or "").lower()
        symbol = (pair.get("baseToken", {}).get("symbol") or "").lower()
        pair_address = pair.get("pairAddress")

        if chain_id != TARGET_CHAIN.lower():
            continue
        if symbol != TARGET_TICKER.lower():
            continue
        if not pair_address or pair_address in seen:
            continue

        seen.add(pair_address)
        new_found = True

        name = pair.get("baseToken", {}).get("name", "Unknown")
        price = pair.get("priceUsd", "N/A")
        liquidity = pair.get("liquidity", {}).get("usd", "N/A")
        url = pair.get("url", "")

        msg = (
            "New $BRODIE pair on Robinhood Chain\n\n"
            f"Name: {name}\n"
            f"Price: ${price}\n"
            f"Liquidity: ${liquidity}\n"
            f"Link: {url}"
        )
        print(msg)
        send_telegram(msg)

    if new_found:
        save_seen(seen)


def main():
    print("Starting Dexscreener -> Telegram scanner...")
    print(f"Watching for ticker '${TARGET_TICKER}' on chain '{TARGET_CHAIN}'")
    seen = load_seen()
    print(f"Loaded {len(seen)} previously seen pairs from disk.")

    while True:
        check_dexscreener(seen)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
