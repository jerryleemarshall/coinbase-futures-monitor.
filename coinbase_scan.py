
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = "https://api.coinbase.com/api/v3/brokerage/market"
HEADERS = {"User-Agent": "CoinbaseFuturesMonitor/1.0"}

def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)

def discover_futures():
    products = []
    offset = 0
    limit = 100

    for page in range(30):
        data = get_json(
            BASE + "/products",
            {
                "product_type": "FUTURE",
                "limit": limit,
                "offset": offset
            }
        )
        batch = data.get("products", [])
        products.extend(
            p for p in batch
            if p.get("product_type") == "FUTURE"
        )

        if len(batch) < limit:
            break
        offset += len(batch)
    else:
        print("WARNING: Product pagination limit reached")

    return {
        p["product_id"]: p
        for p in products
        if p.get("product_id")
    }

def get_candles(product_id, granularity, hours):
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)

    params = {
        "start": int(start.timestamp()),
        "end": int(end.timestamp()),
        "granularity": granularity
    }

    path = "/products/" + urllib.parse.quote(
        product_id, safe=""
    ) + "/candles"

    data = get_json(BASE + path, params)
    candles = data.get("candles", [])

    return sorted(
        candles,
        key=lambda c: int(c["start"])
    )

def summarize(candles):
    if not candles:
        return None

    latest = candles[-1]
    closes = [float(c["close"]) for c in candles]

    return {
        "timestamp": latest["start"],
        "close": float(latest["close"]),
        "high": float(latest["high"]),
        "low": float(latest["low"]),
        "volume": float(latest["volume"]),
        "candles": len(candles),
        "change_pct": round(
            (closes[-1] / closes[0] - 1) * 100, 3
        ) if closes[0] else None
    }

def main():
    products = discover_futures()
    print("Futures contracts discovered:", len(products))

    if not products:
        raise RuntimeError(
            "No futures discovered. Check API response."
        )

    results = []
    errors = []

    for index, (product_id, product) in enumerate(
        products.items(), 1
    ):
        print(f"[{index}/{len(products)}] {product_id}")

        item = {
            "product_id": product_id,
            "status": product.get("status"),
            "display_name": product.get("display_name"),
            "price": product.get("price")
        }

        try:
            candles_15m = get_candles(
                product_id, "FIFTEEN_MINUTE", 24
            )
            candles_1h = get_candles(
                product_id, "ONE_HOUR", 168
            )

            item["15m"] = summarize(candles_15m)
            item["1h"] = summarize(candles_1h)

            if not candles_15m or not candles_1h:
                item["data_status"] = "INCOMPLETE"
            else:
                item["data_status"] = "RECEIVED"

        except Exception as exc:
            item["data_status"] = "ERROR"
            errors.append({
                "product_id": product_id,
                "error": str(exc)
            })
            print("ERROR:", str(exc))

        results.append(item)
        time.sleep(0.25)

    report = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "contracts_discovered": len(products),
        "contracts_processed": len(results),
        "errors": errors,
        "results": results
    }

    Path("futures_report.json").write_text(
        json.dumps(report, indent=2)
    )

    print("Scan complete")
    print("Contracts processed:", len(results))
    print("Errors:", len(errors))
    print("Report saved: futures_report.json")

if __name__ == "__main__":
    main()
