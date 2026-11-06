"""Public Coinbase Advanced Trade futures scanner. No credentials, no trading.
Outputs futures_report.json with OHLCV history and conservative watchlist candidates.
"""
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = 'https://api.coinbase.com/api/v3/brokerage/market'
HEADERS = {'User-Agent': 'CoinbaseFuturesMonitor/2.0', 'Accept': 'application/json'}
MAX_CONTRACTS = int(os.getenv('MAX_CONTRACTS', '500'))
MAX_PAGES = int(os.getenv('MAX_PAGES', '30'))
MAX_AGE_MINUTES = 90


def get_json(path, params=None):
    url = BASE + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=25) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if attempt == 3 or (isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504)):
                raise RuntimeError(f'{path}: {exc}') from exc
            time.sleep(2 ** attempt)


def discover():
    products, seen = [], set()
    for page in range(MAX_PAGES):
        data = get_json('/products', {'limit': 100, 'offset': page * 100, 'product_type': 'FUTURE'})
        batch = data.get('products') or []
        for item in batch:
            pid = item.get('product_id', '')
            if pid and pid not in seen and (pid.endswith('-CDE') or item.get('product_type') == 'FUTURE'):
                seen.add(pid)
                products.append(item)
                if len(products) >= MAX_CONTRACTS:
                    return products, True
        if len(batch) < 100:
            return products, False
    return products, True


def candles(pid, granularity, seconds, count, now):
    # Coinbase market candles endpoint accepts up to 350 candles per request.
    end = now - (now % seconds)
    start = end - count * seconds
    data = get_json('/products/' + urllib.parse.quote(pid, safe='') + '/candles', {
        'start': start, 'end': end, 'granularity': granularity,
    })
    raw = data.get('candles') or []
    parsed = {}
    for c in raw:
        try:
            t = int(c['start'])
            row = {'time': t, 'open': float(c['open']), 'high': float(c['high']),
                   'low': float(c['low']), 'close': float(c['close']), 'volume': float(c['volume'])}
            if all(math.isfinite(v) for k, v in row.items() if k != 'time') and row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high'] and row['volume'] >= 0:
                parsed[t] = row
        except (KeyError, ValueError, TypeError):
            continue
    # Exclude the currently forming candle, if returned.
    return [parsed[t] for t in sorted(parsed) if t + seconds <= now]


def ema(values, period):
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def atr(rows, period=14):
    if len(rows) < period + 1:
        return None
    trs = [max(b['high'] - b['low'], abs(b['high'] - a['close']), abs(b['low'] - a['close']))
           for a, b in zip(rows[:-1], rows[1:])]
    return sum(trs[-period:]) / period


def continuity(rows, seconds, count=20):
    subset = rows[-count:]
    return len(subset) >= count and all(b['time'] - a['time'] == seconds for a, b in zip(subset[:-1], subset[1:]))


def assess(fifteen, hourly, now):
    result = {'decision': 'PASS', 'reason': '', 'direction': None, 'trigger': None,
              'stop_reference': None, 'target_reference': None, 'rr_before_fees': None,
              'score': 0, 'requires_contract_and_fee_verification': True}
    if len(fifteen) < 50 or len(hourly) < 55:
        result['reason'] = 'Insufficient completed candles (need 50 x 15m and 55 x 1h)'
        return result
    if now - fifteen[-1]['time'] > MAX_AGE_MINUTES * 60 or now - hourly[-1]['time'] > 150 * 60:
        result['reason'] = 'Stale candle data'
        return result
    if not continuity(fifteen, 900) or not continuity(hourly, 3600):
        result['reason'] = 'Recent candle gaps'
        return result
    closes_h = [r['close'] for r in hourly]
    e20, e50 = ema(closes_h, 20), ema(closes_h, 50)
    a = atr(fifteen)
    if not a or a <= 0:
        result['reason'] = 'Invalid volatility'
        return result
    direction = 'LONG' if e20 > e50 and closes_h[-1] > e20 else 'SHORT' if e20 < e50 and closes_h[-1] < e20 else None
    if not direction:
        result['reason'] = 'No aligned 1h trend'
        return result
    recent = fifteen[-21:-1]  # 20 fully closed bars prior to most recent closed bar
    latest = fifteen[-1]
    hi = max(x['high'] for x in recent)
    lo = min(x['low'] for x in recent)
    # Trigger is conditional, not a market entry. Use structural stop reference.
    if direction == 'LONG':
        trigger = hi
        stop = min(x['low'] for x in fifteen[-11:])
        target = trigger + 2 * (trigger - stop)
        triggered = latest['close'] > hi
        distance = trigger - stop
    else:
        trigger = lo
        stop = max(x['high'] for x in fifteen[-11:])
        target = trigger - 2 * (stop - trigger)
        triggered = latest['close'] < lo
        distance = stop - trigger
    if distance <= 0 or distance < 0.5 * a or distance > 5 * a:
        result['reason'] = 'Structural stop distance outside volatility guardrails'
        return result
    if direction == 'SHORT' and target <= 0:
        result['reason'] = 'Invalid target reference'
        return result
    result.update({'decision': 'CONDITIONAL', 'reason': 'Trend aligned; breakout level is a watch trigger, not an executable order',
                   'direction': direction, 'trigger': round(trigger, 8), 'stop_reference': round(stop, 8),
                   'target_reference': round(target, 8), 'rr_before_fees': 2.0,
                   'score': round(abs(e20 - e50) / a, 3), 'last_closed_candle_broke_level': triggered})
    return result


def main():
    now = int(time.time())
    report = {'generated_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
              'scanner_version': '2.0', 'analysis_only': True,
              'notes': ['All levels are references, not orders or trade recommendations.',
                        'No bid/ask spread, fee, tick value, margin or account-specific risk checks.',
                        'Candle volume may not be comparable between contracts.'],
              'contracts_discovered': 0, 'discovery_truncated': False, 'contracts_processed': 0,
              'errors': [], 'results': [], 'ranked_watchlist': []}
    try:
        products, truncated = discover()
        report['contracts_discovered'] = len(products)
        report['discovery_truncated'] = truncated
        print(f'Discovered {len(products)} futures contracts; truncated={truncated}', flush=True)
        for i, product in enumerate(products, 1):
            pid = product['product_id']
            entry = {'product_id': pid, 'display_name': product.get('display_name'),
                     'status': product.get('status'), 'price': product.get('price'),
                     'candles_15m': [], 'candles_1h': [], 'analysis': {'decision': 'PASS', 'reason': 'Not processed'}}
            try:
                entry['candles_15m'] = candles(pid, 'FIFTEEN_MINUTE', 900, 100, now)
                entry['candles_1h'] = candles(pid, 'ONE_HOUR', 3600, 100, now)
                entry['analysis'] = assess(entry['candles_15m'], entry['candles_1h'], now)
            except Exception as exc:
                entry['analysis'] = {'decision': 'PASS', 'reason': 'API/data error'}
                report['errors'].append({'product_id': pid, 'error': str(exc)[:300]})
            report['results'].append(entry)
            report['contracts_processed'] += 1
            if i % 20 == 0:
                print(f'Processed {i}/{len(products)}', flush=True)
    except Exception as exc:
        report['errors'].append({'stage': 'discovery', 'error': str(exc)[:300]})
    report['ranked_watchlist'] = sorted(({'product_id': r['product_id'], 'display_name': r['display_name'], **r['analysis']}
                                          for r in report['results'] if r['analysis']['decision'] == 'CONDITIONAL'),
                                         key=lambda x: x['score'], reverse=True)[:20]
    Path('futures_report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f"Done: {report['contracts_processed']} scanned, {len(report['ranked_watchlist'])} conditional, "
          f"{len(report['errors'])} errors. Report: futures_report.json", flush=True)


if __name__ == '__main__':
    main()
        
