from django.core.management.base import BaseCommand
from trading.services.massive_client import MassiveAPIClient
from datetime import date


class Command(BaseCommand):
    help = "Fetch 12m/1m prices for tickers from Massive"

    def add_arguments(self, parser):
        parser.add_argument("--date", type=date.fromisoformat, default=date.today())
        parser.add_argument("--tickers", nargs="+", default=["AAPL", "NVDA"])

    def handle(self, *args, **options):
        client = MassiveAPIClient()
        result = client.fetch_bulk_momentum_data(options["tickers"], options["date"])
        self.stdout.write(str(result))
