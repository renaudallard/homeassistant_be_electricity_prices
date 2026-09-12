# Tariff card archive

Written daily by `.github/workflows/archive_cards.yml` running
`scripts/archive_cards.py` from the main branch. Not edited by hand.

- `<supplier>/<contract>/<region>/<YYYY-MM>.json`: the card as the
  integration parsed it, filed under the month the card names, or under
  the month it was seen in when it names none.
- `texts/<YYYY-MM>/<sha256>.txt`: every document text a parse read that
  month, stored once and shared between the cards that read it. Each card
  lists its own under `_sources`, and names the PDF it read by SHA-256.
- `pdfs.json`: where each PDF is kept, as `<release tag>/<sha256>.pdf` in
  the releases of the cards repository (`be_price_cards`, shared with
  be_water_prices; this integration's releases are `electricity-<YYYY-MM>`,
  one per month of cards, whatever day the card was captured on).
- `coverage.md`: which months the branch holds for each contract and
  region, whether each was captured live or mirrored from the supplier's
  archive, and links from each month to the PDF it was parsed from, to the
  page text it read and to the JSON above.

To get the original card of a contract and month: open `coverage.md`, find
the row, click `pdf` (or `page`); `json` is what the integration parsed out
of it. Months older than three years are removed.
