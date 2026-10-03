from django.db import models, transaction
from django.utils import timezone


class Stock(models.Model):
    ticker = models.CharField(max_length=10, unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True)
    sector = models.CharField(max_length=100, blank=True)
    market_cap = models.BigIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class PriceData(models.Model):
    stock = models.ForeignKey(
        Stock, on_delete=models.CASCADE, related_name="price_data"
    )
    date = models.DateField(db_index=True)
    open_price = models.DecimalField(max_digits=12, decimal_places=4)
    high = models.DecimalField(max_digits=12, decimal_places=4)
    low = models.DecimalField(max_digits=12, decimal_places=4)
    close = models.DecimalField(max_digits=12, decimal_places=4)
    volume = models.BigIntegerField()
    adjusted_close = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["stock", "date"], name="uniq_stock_date")
        ]

class MomentumScore(models.Model):
    stock = models.ForeignKey(
        Stock, on_delete=models.CASCADE, related_name="momentum_scores"
    )
    calculation_date = models.DateField(db_index=True)
    momentum_score = models.DecimalField(max_digits=10, decimal_places=6)
    rank = models.IntegerField(null=True, blank=True)
    quintile = models.IntegerField(null=True, blank=True)
    is_top_quintile = models.BooleanField(default=False)
    period_start = models.DateField()
    period_end = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    MIN_SCORES_FOR_QUINTILES = 5

    @classmethod
    def calculate_quintiles_for_date(cls, calculation_date=None):
        if calculation_date is None:
            calculation_date = timezone.now().date()

        # Highest momentum first; ticker breaks ties so reruns are reproducible.
        scores = list(
            cls.objects.filter(calculation_date=calculation_date)
            .select_related("stock")
            .order_by("-momentum_score", "stock__ticker")
        )
        n = len(scores)
        if n == 0:
            return
        if n < cls.MIN_SCORES_FOR_QUINTILES:
            raise ValueError(
                f"Need at least {cls.MIN_SCORES_FOR_QUINTILES} scores to form "
                f"quintiles, got {n}"
            )

        for i, score in enumerate(scores):
            score.rank = i + 1
            # Quintile 1 = HIGHEST momentum, 5 = lowest. Bucket sizes differ by at most 1.
            score.quintile = i * 5 // n + 1
            score.is_top_quintile = score.quintile == 1

        with transaction.atomic():
            cls.objects.bulk_update(scores, ["rank", "quintile", "is_top_quintile"])


class TradingSignal(models.Model):
    SIGNAL_TYPES = [
        ("BUY", "Buy"),
        ("SELL", "Sell"),
        ("HOLD", "Hold"),
    ]

    stock = models.ForeignKey(
        Stock, on_delete=models.CASCADE, related_name="trading_signals"
    )
    signal_date = models.DateField(db_index=True)
    signal_type = models.CharField(max_length=4, choices=SIGNAL_TYPES)
    momentum_score = models.ForeignKey(
        MomentumScore, on_delete=models.CASCADE, null=True
    )
    target_quantity = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    target_value = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    reason = models.TextField(blank=True)
    is_executed = models.BooleanField(default=False)
    executed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "trading_signals"
        ordering = ["-signal_date", "-created_at"]
        indexes = [
            models.Index(fields=["signal_date", "signal_type"]),
            models.Index(fields=["is_executed"]),
        ]


class RebalanceEvent(models.Model):
    date = models.DateField(db_index=True)
    total_stocks_analyzed = models.IntegerField()
    buy_signals_generated = models.IntegerField()
    sell_signals_generated = models.IntegerField()
    total_portfolio_value = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    execution_status = models.CharField(
        max_length=20,
        choices=[
            ("PENDING", "Pending"),
            ("IN_PROGRESS", "In Progress"),
            ("COMPLETED", "Completed"),
            ("DRY_RUN", "Dry run"),
            ("FAILED", "Failed"),
        ],
        default="PENDING",
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    portfolio = models.ForeignKey(
        "portfolio.Portfolio", on_delete=models.CASCADE, related_name="rebalance_events"
    )