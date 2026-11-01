"""
Live market order books from BitJita, and what selling into them earns.

Two ways to sell are modelled:
  instant  — fill existing buy orders, best price first. Guaranteed gold, but a
             thin book leaves units unsold (they earn nothing here).
  listed   — fill buy orders paying at least the lowest ask, list the rest at
             the lowest ask. Assumes the listing sells, so it is an upper bound
             on oversupplied markets.
"""

from concurrent.futures import ThreadPoolExecutor

from _lib import api_get


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
    ids = sorted({str(i) for i in item_ids})
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(ids, pool.map(order_book, ids)))


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
