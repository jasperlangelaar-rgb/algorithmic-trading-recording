from django.conf import settings
from django.utils import timezone
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Dict, Optional, Tuple
import pandas as pd
import numpy as np
import logging

from trading.models import Stock, PriceData, MomentumScore
from trading.services.massive_client import get_massive_client
from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)


class MomentumCalculator:
    def __init__(self):
        self.massive_client = get_massive_client()
        self.lookback_months = getattr(settings, "MOMENTUM_LOOKBACK_MONTHS", 12)
        self.skip_months = getattr(settings, "MOMENTUM_SKIP_MONTHS", 1)

    def get_period(self, calculation_date, lookback_months=None, skip_months=None):
        """Return (period_start, period_end): the two price dates momentum compares.

        Momentum = return from (date - lookback months) to (date - skip months),
        e.g. 12 and 1 give the classic "12-1" momentum. Calendar months; the
        as-of price lookup steps back over weekends and holidays.
        """
        lookback = self.lookback_months if lookback_months is None else lookback_months
        skip = self.skip_months if skip_months is None else skip_months
        if lookback <= skip:
            raise ValueError("lookback_months must be greater than skip_months")
        return (
            calculation_date - relativedelta(months=lookback),
            calculation_date - relativedelta(months=skip),
        )

    def calculate_momentum_for_stock(
        self, stock: Stock, calculation_date: datetime = None
    ) -> Optional[Decimal]:
        if calculation_date is None:
            calculation_date = timezone.now().date()

        period_start, period_end = self.get_period(calculation_date)

        # Prices come from the database only (see pull_prices); missing data is
        # reported, never silently filled from the API.
        price_start = self._get_price_from_db(stock, period_start, tolerance_days=7)
        price_end = self._get_price_from_db(stock, period_end, tolerance_days=7)

        if price_start and price_end and price_start > 0:
            return (price_end - price_start) / price_start

        logger.warning(
            f"Could not calculate momentum for {stock.ticker}: "
            f"price_start={price_start}, price_end={price_end}"
        )
        return None

    def _get_price_from_db(
        self, stock: Stock, target_date: datetime, tolerance_days: int = 7
    ) -> Optional[Decimal]:
        # "As of" lookup: last close on or before target_date. Never looks past it
        # (no look-ahead) and steps back over weekends/holidays.
        price_data = (
            stock.price_data.filter(
                date__lte=target_date,
                date__gte=target_date - timedelta(days=tolerance_days),
            )
            .order_by("-date")
            .first()
        )

        return price_data.close if price_data else None

    def calculate_momentum_scores_bulk(
        self, stock_list: List[Stock] = None, calculation_date: datetime = None
    ) -> List[MomentumScore]:
        """
        Score every stock from prices already stored in PriceData (no API calls).
        """
        if calculation_date is None:
            calculation_date = timezone.now().date()

        if stock_list is None:
            stock_list = Stock.objects.filter(is_active=True)

        momentum_scores = []
        period_start, period_end = self.get_period(calculation_date)

        for stock in stock_list:
            momentum = self.calculate_momentum_for_stock(stock, calculation_date)
            if momentum is None:
                continue

            momentum_score, created = MomentumScore.objects.update_or_create(
                stock=stock,
                calculation_date=calculation_date,
                defaults={
                    "momentum_score": momentum,
                    "period_start": period_start,
                    "period_end": period_end,
                },
            )
            momentum_scores.append(momentum_score)
            logger.info(
                f"{'Created' if created else 'Updated'} momentum score for "
                f"{stock.ticker}: {momentum:.6f}"
            )

        logger.info(
            f"Scored {len(momentum_scores)} of {len(stock_list)} stocks for {calculation_date}"
        )
        return momentum_scores

    def rank_stocks_by_momentum(
        self, calculation_date: datetime = None
    ) -> List[MomentumScore]:
        if calculation_date is None:
            calculation_date = timezone.now().date()

        # Calculate quintiles for the date
        MomentumScore.calculate_quintiles_for_date(calculation_date)

        # Return ranked scores (rank 1 = highest momentum)
        return MomentumScore.objects.filter(calculation_date=calculation_date).order_by(
            "rank"
        )

    def get_top_quintile_stocks(self, calculation_date: datetime = None) -> List[Stock]:
        if calculation_date is None:
            calculation_date = timezone.now().date()

        momentum_scores = (
            MomentumScore.objects.filter(
                calculation_date=calculation_date, is_top_quintile=True
            )
            .select_related("stock")
            .order_by("-momentum_score")
        )

        return [score.stock for score in momentum_scores]

    def get_bottom_quintile_stocks(
        self, calculation_date: datetime = None
    ) -> List[Stock]:
        if calculation_date is None:
            calculation_date = timezone.now().date()

        momentum_scores = (
            MomentumScore.objects.filter(calculation_date=calculation_date, quintile=5)
            .select_related("stock")
            .order_by("momentum_score")
        )

        return [score.stock for score in momentum_scores]

    def update_stock_universe(self, tickers: List[str] = None) -> List[Stock]:
        if tickers is None:
            stocks = list(Stock.objects.filter(is_active=True))
            if not stocks:
                raise ValueError(
                    "Stock universe is empty; run: python manage.py load_universe"
                )
            return stocks

        stocks = []
        for ticker in tickers:
            stock, created = Stock.objects.get_or_create(
                ticker=ticker, defaults={"name": ticker, "is_active": True}
            )
            stocks.append(stock)

            if created:
                logger.info(f"Added new stock: {ticker}")

        return stocks

    def backfill_price_data(self, stock: Stock, days_back: int = 420) -> int:
        end_date = timezone.now().date()
        start_date = end_date - timedelta(days=days_back)

        # Always re-pull the whole window and upsert: the API is one call per ticker
        # either way, and overwriting keeps history consistent after splits.
        api_data = self.massive_client.fetch_stock_data(
            ticker=stock.ticker,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
        )

        # adjusted_close stays NULL: Massive's adjusted=True is split-adjusted only
        # (no dividends), so it is not a true adjusted close.
        rows = [
            PriceData(
                stock=stock,
                date=r["date"],
                open_price=Decimal(str(r["open"])),
                high=Decimal(str(r["high"])),
                low=Decimal(str(r["low"])),
                close=Decimal(str(r["close"])),
                volume=int(r["volume"]),
            )
            for r in api_data
        ]
        PriceData.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=["stock", "date"],
            update_fields=["open_price", "high", "low", "close", "volume"],
        )

        logger.info(f"Stored {len(rows)} price rows for {stock.ticker}")
        return len(rows)

    def get_momentum_statistics(self, calculation_date: datetime = None) -> Dict:
        if calculation_date is None:
            calculation_date = timezone.now().date()

        scores = MomentumScore.objects.filter(calculation_date=calculation_date)

        if not scores.exists():
            return {}

        momentum_values = [float(score.momentum_score) for score in scores]

        return {
            "total_stocks": len(momentum_values),
            "mean_momentum": np.mean(momentum_values),
            "median_momentum": np.median(momentum_values),
            "std_momentum": np.std(momentum_values),
            "min_momentum": min(momentum_values),
            "max_momentum": max(momentum_values),
            "top_quintile_threshold": np.percentile(momentum_values, 80),
            "bottom_quintile_threshold": np.percentile(momentum_values, 20),
        }

    def validate_momentum_calculation(
        self, stock: Stock, calculation_date: datetime = None
    ) -> Dict:
        if calculation_date is None:
            calculation_date = timezone.now().date()

        period_start, period_end = self.get_period(calculation_date)

        validation_result = {
            "stock": stock.ticker,
            "calculation_date": calculation_date,
            "has_sufficient_data": False,
            "period_start": period_start,
            "period_end": period_end,
            "momentum_score": None,
            "data_points_available": 0,
        }

        # Check data availability
        data_count = stock.price_data.filter(
            date__gte=period_start, date__lte=calculation_date
        ).count()

        validation_result["data_points_available"] = data_count
        # ~250 trading days in 365 calendar days; allow a few missing days
        validation_result["has_sufficient_data"] = data_count >= 240

        # Get prices and calculate momentum
        try:
            validation_result["momentum_score"] = self.calculate_momentum_for_stock(
                stock, calculation_date
            )
        except (ValueError, TypeError) as e:
            validation_result["error"] = str(e)

        return validation_result


def get_momentum_calculator() -> MomentumCalculator:
    return MomentumCalculator()
