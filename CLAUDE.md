# Bitcraft Effort — Claude Notes

## Fetching from the BitJita API

The bitjita.com site returns HTML for browser URLs. Always use the `/api/items/<id>` endpoint and include these headers:

```python
headers = {'User-Agent': 'BitJita (Billard)', 'Accept': 'application/json'}
```

- Item page URL: `https://bitjita.com/items/<id>` → returns HTML (useless)
- API endpoint: `https://bitjita.com/api/items/<id>` → returns JSON with `item`, `equipmentStats`, `craftingRecipes`, etc.
- Market search: `https://bitjita.com/api/market?q=<name>&limit=100` → returns `data.items[]` with id, name, rarityStr, etc.

When a user provides a bitjita.com URL, extract the item ID and hit `/api/items/<id>` with the headers above.
