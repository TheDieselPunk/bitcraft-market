"""
Live market order books from BitJita, and what selling into them earns.

Three ways to sell are modelled:
  instant  — fill existing buy orders, best price first. Guaranteed gold, but a
             thin book leaves units unsold (they earn nothing here).
  listed   — fill buy orders paying at least the lowest ask, list the rest at
             the lowest ask. Assumes the listing sells, so it is an upper bound
             on oversupplied markets.
  capped   — like listed, but listings only sell as fast as buyers have recently
             taken listings of the item (BitJita trade history); the rest go
             into lower buy orders or stay unsold. Assumes you get all of those
             sales, so it is still an upper bound, but not for dead markets.
"""

from concurrent.futures import ThreadPoolExecutor

from _lib import api_get

SALES_WINDOWS = (1, 7, 30)  # days of trade history the capped estimate can use


def _fetch_all(fn, item_ids):
    """Run fn over several item ids in parallel: {item_id: fn(item_id)}."""
    ids = sorted({str(i) for i in item_ids})
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(ids, pool.map(fn, ids)))


def order_book(item_id):
    """{'ask', 'bid', 'bids': [(price_per_unit, units)] best first, 'name'}; prices None if unavailable."""
    try:
        d = api_get(f'/api/market/item/{item_id}')
    except Exception:
        return {'ask': None, 'bid': None, 'bids': [], 'name': None}
    stats = d.get('stats') or {}
    bids = [(float(o['priceThreshold']), int(o['quantity'])) for o in d.get('buyOrders') or []]
    # Package buy orders (e.g. 1000 oil per package) count per unit
    ratio = (d.get('packageInfo') or {}).get('ratio') or 0
    if ratio:
        bids += [(float(o['priceThreshold']) / ratio, int(o['quantity']) * ratio)
                 for o in d.get('packageBuyOrders') or []]
    return {
        'ask':  stats.get('lowestSell'),
        'bid':  stats.get('highestBuy'),
        'bids': sorted(bids, reverse=True),
        'name': (d.get('item') or {}).get('name'),
    }


def order_books(item_ids):
    """Fetch several order books in parallel: {item_id: order_book}."""
    return _fetch_all(order_book, item_ids)


def recent_sales(item_id):
    """
    Units buyers took from listings over each of SALES_WINDOWS days, and the
    30-day average trade price: {'units': {days: units}, 'avg_30d'}. None if
    the trade history can't be fetched.
    """
    try:
        st = api_get(f'/api/market/item/{item_id}/price-history').get('priceStats') or {}
    except Exception:
        return None
    # soldVolume* counts listings bought; boughtVolume* (sales into buy orders) is
    # already covered by today's buy orders
    return {
        'units':   {1: st.get('soldVolume24h') or 0, 7: st.get('soldVolume7d') or 0,
                    30: st.get('soldVolume30d') or 0},
        'avg_30d': st.get('avg30d'),
    }


def all_recent_sales(item_ids):
    """Fetch several items' recent sales in parallel: {item_id: recent_sales}."""
    return _fetch_all(recent_sales, item_ids)


def sell_instant(book, qty):
    """(gold, units sold) from filling buy orders, best price first."""
    gold = sold = 0.0
    for price, units in book['bids']:
        take = min(units, qty - sold)
        gold += take * price
        sold += take
        if sold >= qty:
            break
    return gold, sold


def sell_listed(book, qty):
    """Gold from filling buy orders at or above the lowest ask, then listing the rest at the ask."""
    ask = book['ask'] or 0
    gold = sold = 0.0
    for price, units in book['bids']:
        if price < ask or sold >= qty:
            break
        take = min(units, qty - sold)
        gold += take * price
        sold += take
    return gold + (qty - sold) * ask


def sell_capped(book, qty, listing_units, list_price):
    """
    (gold, units sold) if listings only sell as fast as they recently have: fill
    buy orders paying at least list_price, list up to listing_units at
    list_price, then sell the rest into the lower buy orders.
    """
    list_price = list_price or 0
    gold = sold = 0.0
    lower = []
    for price, units in book['bids']:
        if price < list_price:
            lower.append((price, units))
            continue
        take = min(units, qty - sold)
        gold += take * price
        sold += take
    listed = min(listing_units, qty - sold) if list_price else 0
    gold += listed * list_price
    sold += listed
    for price, units in lower:
        if sold >= qty:
            break
        take = min(units, qty - sold)
        gold += take * price
        sold += take
    return gold, sold
