# Universe files

`universe_sp500.csv` is the stock universe loaded by `python manage.py load_universe`.
Columns: `ticker,name,sector`. The CSV is the source of truth: tickers removed from it
are set to `is_active=False` (never deleted, so price history is kept).

## Survivorship bias warning

This list is *today's* large-cap names. Backtesting momentum on it has
**survivorship bias**: companies that fell out of the index or went bankrupt are
missing, so results look better than reality.

Fine for testing the plumbing. **Not valid evidence for a strategy.** It is replaced
with point-in-time membership (including delisted tickers) in Phase B, before any
backtest result is allowed to move a strategy up the status ladder.

Tickers with dots (e.g. BRK.B) are left out on purpose: vendors write them in
different formats.
