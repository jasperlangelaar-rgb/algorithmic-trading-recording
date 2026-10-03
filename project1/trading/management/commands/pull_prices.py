from django.core.management.base import BaseCommand, CommandError

from trading.models import Stock
from trading.services.momentum_calculator import MomentumCalculator


class Command(BaseCommand):
    help = "Download daily prices from Massive into PriceData"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=420)
        parser.add_argument("--tickers", nargs="+")
        parser.add_argument("--rpm", type=int, default=5, help="API requests per minute")

    def handle(self, *args, **options):
        stocks = Stock.objects.filter(is_active=True)
        if options["tickers"]:
            stocks = stocks.filter(ticker__in=[t.upper() for t in options["tickers"]])
        stocks = list(stocks.order_by("ticker"))
        if not stocks:
            raise CommandError("No active stocks; run load_universe first")

        calc = MomentumCalculator()
        calc.massive_client.requests_per_minute = options["rpm"]

        failed = []
        for i, stock in enumerate(stocks, 1):
            try:
                n = calc.backfill_price_data(stock, days_back=options["days"])
                self.stdout.write(f"[{i}/{len(stocks)}] {stock.ticker}: {n} rows")
            except Exception as e:  # one bad ticker must not stop the whole batch
                failed.append(stock.ticker)
                self.stderr.write(f"[{i}/{len(stocks)}] {stock.ticker}: FAILED ({e})")

        if failed:
            raise CommandError(f"{len(failed)} tickers failed: {', '.join(failed)}")
        self.stdout.write(self.style.SUCCESS(f"Done: {len(stocks)} tickers"))
