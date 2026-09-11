# Tariff card archive

Written daily by `.github/workflows/archive_cards.yml` running
`scripts/archive_cards.py` from the main branch. Not edited by hand.

- `<supplier>/<contract>/<region>/<YYYY-MM>.json`: the card as the
  integration parsed it, filed under the month the card names, or under
  the month it was seen in when it names none.
- `texts/<YYYY-MM>/<sha256>.txt`: every document text a parse read that
  month, stored once and shared between the cards that read it. Each card
  lists its own under `_sources`.

Months older than three years are removed.
