import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from trading.models import Stock


class Command(BaseCommand):
    help = "Load the stock universe from a CSV (columns: ticker,name,sector)"

    def add_arguments(self, parser):
        parser.add_argument("--file", default="data/universe_sp500.csv")

    def handle(self, *args, **options):
        path = Path(settings.BASE_DIR) / options["file"]
        if not path.exists():
            raise CommandError(f"Universe file not found: {path}")

        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows or "ticker" not in rows[0]:
            raise CommandError("CSV needs a header with at least a 'ticker' column")

        tickers = set()
        created = 0
        for row in rows:
            ticker = row["ticker"].strip().upper()
            if not ticker:
                continue
            tickers.add(ticker)
            _, was_created = Stock.objects.update_or_create(
                ticker=ticker,
                defaults={
                    "name": row.get("name", "").strip(),
                    "sector": row.get("sector", "").strip(),
                    "is_active": True,
                },
            )
            created += was_created

        # Deactivate, never delete: deleting a Stock cascades to its price history
        deactivated = Stock.objects.exclude(ticker__in=tickers).update(is_active=False)

        self.stdout.write(
            self.style.SUCCESS(
                f"{len(tickers)} tickers in file: {created} new, "
                f"{len(tickers) - created} updated, {deactivated} deactivated"
            )
        )
