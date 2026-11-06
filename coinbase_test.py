
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

product = "BIT-27NOV26-CDE"
end = datetime.now(timezone.utc)
start = end - timedelta(hours=3)

params = urllib.parse.urlencode({
    "start": int(start.timestamp()),
    "end": int(end.timestamp()),
    "granularity": "FIFTEEN_MINUTE"
})

url = (
    "https://api.coinbase.com/api/v3/brokerage/market/"
    f"products/{product}/candles?{params}"
)

request = urllib.request.Request(
    url, headers={"User-Agent": "CoinbaseFuturesMonitor/1.0"}
)

try:
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)

    candles = data.get("candles", [])
    print("Product:", product)
    print("Candles received:", len(candles))

    if not candles:
        raise RuntimeError("No candles returned")

    latest = max(candles, key=lambda c: int(c["start"]))
    print("Latest candle:", json.dumps(latest, indent=2))
    print("SUCCESS: Coinbase futures API accessible")

except Exception as error:
    print("FAILED:", repr(error))
    raise
  
