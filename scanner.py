
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Free market-data source. No wallet connection or trading API.
DEX = "https://api.dexscreener.com"
SEEN_FILE = "seen.json"

MIN_LIQUIDITY_USD = 50_000
MIN_M5_VOLUME_USD = 5_000
MIN_M5_TXNS = 30
MIN_BUYS = 5
MIN_SELLS = 5
MIN_POOL_AGE_MIN = 10
MAX_POOL_AGE_HOURS = 24
MIN_M5_CHANGE_PCT = 1
MAX_M5_CHANGE_PCT = 20
MIN_SCORE = 70
MAX_ALERTS_PER_HOUR = 5


def get_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SolanaTradingSidekick/1.0"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def money(value):
    return f"${value:,.0f}"


def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def save_seen(seen):
    # Keep the state file small.
    trimmed = dict(
        sorted(seen.items(), key=lambda item: item[1], reverse=True)[-5000:]
    )
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, indent=2)


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub secret."
        )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": "true"
    }).encode("utf-8")

    request = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message.")


def get_solana_profiles():
    feeds = [
        "/token-profiles/latest/v1",
        "/token-boosts/latest/v1",
        "/token-boosts/top/v1",
        "/community-takeovers/latest/v1",
    ]
    profiles = []
    for endpoint in feeds:
        try:
            data = get_json(f"{DEX}{endpoint}")
            if not isinstance(data, list):
                continue
            for item in data:
                if item.get("chainId") != "solana":
                    continue
                address = item.get("tokenAddress", "")
                if address:
                    profiles.append(address)
        except Exception as exc:
            print(f"Discovery feed failed ({endpoint}): {type(exc).__name__}")
    unique_profiles = list(dict.fromkeys(profiles))
    print(f"Discovery feeds returned {len(unique_profiles)} unique Solana tokens.")
    return unique_profiles[:80]


def get_pairs(mint):
    url = f"{DEX}/token-pairs/v1/solana/{urllib.parse.quote(mint)}"
    data = get_json(url)
    return data if isinstance(data, list) else []


def score_pair(pair):
    liquidity = (pair.get("liquidity") or {}).get("usd") or 0
    volume = (pair.get("volume") or {}).get("m5") or 0
    txns = (pair.get("txns") or {}).get("m5") or {}
    buys = txns.get("buys") or 0
    sells = txns.get("sells") or 0
    change = (pair.get("priceChange") or {}).get("m5")

    try:
        change = float(change)
    except (TypeError, ValueError):
        return None

    created = pair.get("pairCreatedAt")
    if not created:
        return None

    age_minutes = (time.time() * 1000 - created) / 60000

    # Hard rejection rules. Missing required data fails closed.
    if liquidity < MIN_LIQUIDITY_USD:
        return None
    if volume < MIN_M5_VOLUME_USD:
        return None
    if buys + sells < MIN_M5_TXNS:
        return None
    if buys < MIN_BUYS or sells < MIN_SELLS:
        return None
    if not MIN_POOL_AGE_MIN <= age_minutes <= MAX_POOL_AGE_HOURS * 60:
        return None
    if not MIN_M5_CHANGE_PCT <= change <= MAX_M5_CHANGE_PCT:
        return None

    # Market-activity score only; NOT a safety score or win probability.
    liquidity_points = min(25, liquidity / 4000)
    volume_points = min(25, volume / 1000)
    txn_points = min(20, (buys + sells) / 3)
    momentum_points = 20 if 2 <= change <= 10 else 12
    age_points = 10 if 15 <= age_minutes <= 360 else 6

    score = round(
        liquidity_points + volume_points + txn_points
        + momentum_points + age_points
    )

    return {
        "score": min(100, score),
        "liquidity": liquidity,
        "volume": volume,
        "buys": buys,
        "sells": sells,
        "change": change,
        "age": age_minutes,
        "pair": pair
    }


def build_alert(mint, result):
    pair = result["pair"]
    base = pair.get("baseToken") or {}
    name = base.get("name") or "Unknown token"
    symbol = base.get("symbol") or "?"
    pair_address = pair.get("pairAddress") or ""
    dex_url = pair.get("url") or (
        "https://dexscreener.com/solana/" + pair_address
    )

    rug_url = "https://rugcheck.xyz/tokens/" + mint
    axiom_url = "https://axiom.trade/"

    return (
        f"{'EARLY WATCH' if result['score'] < 80 else 'SOLANA CANDIDATE'} — {result['score']}/100 MARKET SCORE\n\n"
        f"Token: {name} ({symbol})\n"
        f"Mint: {mint}\n"
        f"Pool age: {result['age']:.0f} minutes\n"
        f"Liquidity: {money(result['liquidity'])}\n"
        f"5-min volume: {money(result['volume'])}\n"
        f"5-min buys/sells: {result['buys']}/{result['sells']}\n"
        f"5-min price change: {result['change']:.1f}%\n\n"
        "MARKET FILTERS: PASSED\n"
        "TOKEN SAFETY: NOT VERIFIED\n"
        "MANUAL REVIEW REQUIRED before any trade.\n\n"
        f"DEX Screener: {dex_url}\n"
        f"RugCheck: {rug_url}\n"
        f"Axiom: {axiom_url}\n\n"
        "A score is not a prediction of profit. Verify the mint, "
        "holders, authorities, liquidity and sellability yourself."
    )


def main():
    seen = load_seen()
    now = time.time()
    hour_ago = now - 3600

    # Retain timestamps for hourly alert limiting.
    seen = {
        mint: timestamp for mint, timestamp in seen.items()
        if isinstance(timestamp, (int, float))
        and timestamp > now - 7 * 86400
    }

    recent_alerts = sum(
        1 for timestamp in seen.values() if timestamp >= hour_ago
    )
    if recent_alerts >= MAX_ALERTS_PER_HOUR:
        print("Hourly alert cap reached; no more alerts this run.")
        save_seen(seen)
        return

    profiles = get_solana_profiles()
    print(f"Received {len(profiles)} Solana token profiles.")

    candidates = []
    for mint in profiles:
        if mint in seen:
            continue

        try:
            pairs = get_pairs(mint)
            # Use the most liquid qualifying Solana pair for each token.
            for pair in pairs:
                if pair.get("chainId") != "solana":
                    continue
                result = score_pair(pair)
                if result:
                    candidates.append((mint, result))
        except Exception as exc:
            print(f"Could not check a token pair: {type(exc).__name__}")

        # Stay comfortably below published public API request limits.
        time.sleep(0.2)

    candidates.sort(key=lambda item: item[1]["score"], reverse=True)
    sent = 0

    for mint, result in candidates:
        if result["score"] < MIN_SCORE:
            continue
        if mint in seen:
            continue
        if sent >= MAX_ALERTS_PER_HOUR - recent_alerts:
            break

        try:
            send_telegram(build_alert(mint, result))
            seen[mint] = time.time()
            sent += 1
            print(f"Alert sent: {mint}, score {result['score']}")
        except Exception as exc:
            print(f"Alert failed: {type(exc).__name__}: {exc}")

    save_seen(seen)
    print(f"Finished. New alerts sent: {sent}")


if __name__ == "__main__":
    main()
