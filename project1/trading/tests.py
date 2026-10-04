import io
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from portfolio.models import Portfolio, Position
from trading.models import MomentumScore, PriceData, RebalanceEvent, Stock, TradingSignal
from trading.services.momentum_calculator import MomentumCalculator
from trading.services.strategy_engine import MomentumTradingStrategy

DAY = date(2026, 10, 3)

def make_stock(ticker='AAA', **kwargs):
    return Stock.objects.create(ticker=ticker, **kwargs)

def make_price(stock, day, close):
    c = Decimal(str(close))
    return PriceData.objects.create(
        stock=stock,
        date=day,
        open_price=c,
        high=c,
        low=c,
        close=c,
        volume=1000,
    )

def make_scores(n, day=DAY):
    """n stocks S000.. with scores 0.000, 0.001, ... (higher index = higher score)."""
    scores = []
    for i in range(n):
        s = make_stock(f"S{i:03d}")
        scores.append(
            MomentumScore.objects.create(
                stock=s,
                calculation_date=day,
                momentum_score=Decimal(i) / 1000,
                period_start=day,
                period_end=day,
            )
        )
    return scores


def make_calculator():
    # Patch the API client so tests never need a key or the network.
    with mock.patch("trading.services.momentum_calculator.get_massive_client"):
        return MomentumCalculator()


class QuintileTests(TestCase):
    def quintile_sizes(self):
        """How many scores sit in quintile 1, 2, 3, 4 and 5."""
        return [
            MomentumScore.objects.filter(calculation_date=DAY, quintile=q).count()
            for q in range(1, 6)
        ]

    def test_bucket_sizes(self):
        expected = {
            5: [1, 1, 1, 1, 1],
            7: [2, 1, 2, 1, 1],
            23: [5, 5, 4, 5, 4],
            30: [6, 6, 6, 6, 6],
            101: [21, 20, 20, 20, 20],
        }
        for n, want in expected.items():
            with self.subTest(n=n):
                Stock.objects.all().delete()  # tickers are unique: start clean each time
                make_scores(n)
                MomentumScore.calculate_quintiles_for_date(DAY)
                self.assertEqual(self.quintile_sizes(), want)

    def test_best_is_rank_1_quintile_1(self):
        make_scores(10)
        MomentumScore.calculate_quintiles_for_date(DAY)
        best = MomentumScore.objects.get(stock__ticker="S009")   # highest score
        worst = MomentumScore.objects.get(stock__ticker="S000")  # lowest score
        self.assertEqual((best.rank, best.quintile), (1, 1))
        self.assertEqual((worst.rank, worst.quintile), (10, 5))

    def test_fewer_than_five_raises(self):
        make_scores(4)
        with self.assertRaisesMessage(ValueError, "at least 5"):
            MomentumScore.calculate_quintiles_for_date(DAY)

    def test_no_scores_returns_none(self):
        self.assertIsNone(MomentumScore.calculate_quintiles_for_date(DAY))

    def test_ties_are_ranked_by_ticker(self):
        for ticker in ["S004", "S003", "S002", "S001", "S000"]:  # created in reverse on purpose
            MomentumScore.objects.create(
                stock=make_stock(ticker),
                calculation_date=DAY,
                momentum_score=Decimal("0.05"),
                period_start=DAY,
                period_end=DAY,
            )
        MomentumScore.calculate_quintiles_for_date(DAY)
        ranks = {s.stock.ticker: s.rank for s in MomentumScore.objects.select_related("stock")}
        self.assertEqual(ranks, {"S000": 1, "S001": 2, "S002": 3, "S003": 4, "S004": 5})

    def test_rerun_is_stable(self):
        make_scores(23)
        columns = ("stock__ticker", "rank", "quintile")
        MomentumScore.calculate_quintiles_for_date(DAY)
        first = list(MomentumScore.objects.order_by("stock__ticker").values_list(*columns))
        MomentumScore.calculate_quintiles_for_date(DAY)
        second = list(MomentumScore.objects.order_by("stock__ticker").values_list(*columns))
        self.assertEqual(first, second)

    def test_is_top_quintile_flag(self):
        make_scores(10)
        MomentumScore.calculate_quintiles_for_date(DAY)
        top = MomentumScore.objects.filter(is_top_quintile=True)
        self.assertEqual(top.count(), 2)
        self.assertEqual(set(top.values_list("quintile", flat=True)), {1})

class PriceLookupTests(TestCase):
    def test_weekend_uses_friday(self):
        calc = make_calculator()
        stock = make_stock()
        make_price(stock, date(2026, 5, 7), 100) #Thursday
        make_price(stock, date(2026, 5, 8), 101) #Friday
        price = calc._get_price_from_db(stock, date(2026, 5, 9)) #Saturday
        self.assertEqual(price, Decimal("101"))
    
    def test_future_price_is_ignored(self):
        calc = make_calculator()
        stock = make_stock()
        make_price(stock, date(2026, 5, 8), 101)    # Friday
        make_price(stock, date(2026, 5, 11), 150)   # Monday, AFTER the Saturday we ask about
        price = calc._get_price_from_db(stock, date(2026, 5, 9))
        self.assertEqual(price, Decimal("101"))


    def test_only_future_price_returns_none(self):
        calc = make_calculator()
        stock = make_stock()
        make_price(stock, date(2026, 5, 11), 150)   # Monday: after the Saturday we ask about
        self.assertIsNone(calc._get_price_from_db(stock, date(2026, 5, 9)))

    def test_latest_price_is_used_not_earliest(self):
        calc = make_calculator()
        stock = make_stock()
        for day, close in [(4, 95), (5, 96), (6, 97), (7, 98), (8, 99)]:   # Mon..Fri
            make_price(stock, date(2026, 5, day), close)
        self.assertEqual(calc._get_price_from_db(stock, date(2026, 5, 8)), Decimal("99"))

    def test_price_exactly_seven_days_back_is_used(self):
        calc = make_calculator()
        stock = make_stock()
        make_price(stock, date(2026, 5, 1), 100)   # exactly 7 days before 05-08
        self.assertEqual(calc._get_price_from_db(stock, date(2026, 5, 8)), Decimal("100"))

    def test_price_eight_days_back_returns_none(self):
        calc = make_calculator()
        stock = make_stock()
        make_price(stock, date(2026, 4, 30), 100)  # 8 days before 05-08: too old
        self.assertIsNone(calc._get_price_from_db(stock, date(2026, 5, 8)))


class MomentumTests(TestCase):
    def test_get_period_default(self):
        calc = make_calculator()
        self.assertEqual(calc.get_period(date(2026, 8, 31)), 
            (date(2025, 8, 31), date(2026, 7, 31)),
        )
    def test_momentum_hand_value(self):
        calc = make_calculator()
        stock = make_stock()
        make_price(stock, date(2025, 10, 3), 100) #Start of hte period for DAY
        make_price(stock, date(2026, 9,3), 130) #End of the period for DAY
        self.assertEqual(calc.calculate_momentum_for_stock(stock, DAY), Decimal("0.3"))  # (130-100)/100 = 0.3


    def test_get_period_month_end_clamps(self):
        calc = make_calculator()
        self.assertEqual(calc.get_period(date(2026, 3, 31), 12, 1)[1], date(2026, 2, 28))

    def test_get_period_six_one(self):
        calc = make_calculator()
        self.assertEqual(
            calc.get_period(date(2026, 8, 31), 6, 1),
            (date(2026, 2, 28), date(2026, 7, 31)),
        )

    def test_get_period_rejects_lookback_not_longer_than_skip(self):
        calc = make_calculator()
        with self.assertRaises(ValueError):
            calc.get_period(DAY, 3, 3)
        with self.assertRaises(ValueError):
            calc.get_period(DAY, 1, 3)

    def test_settings_drive_the_period(self):
        with self.settings(MOMENTUM_LOOKBACK_MONTHS=6):
            calc = make_calculator()   # reads the setting when it is created
        self.assertEqual(calc.get_period(DAY)[0], date(2026, 4, 3))

    def test_momentum_is_negative_when_price_falls(self):
        calc = make_calculator()
        stock = make_stock()
        make_price(stock, date(2025, 10, 3), 100)
        make_price(stock, date(2026, 9, 3), 80)
        self.assertEqual(calc.calculate_momentum_for_stock(stock, DAY), Decimal("-0.2"))

    def test_missing_end_price_gives_none(self):
        calc = make_calculator()
        stock = make_stock()
        make_price(stock, date(2025, 10, 3), 100)   # only the start price exists
        self.assertIsNone(calc.calculate_momentum_for_stock(stock, DAY))

    def test_no_prices_gives_none(self):
        calc = make_calculator()
        self.assertIsNone(calc.calculate_momentum_for_stock(make_stock(), DAY))

    def test_bulk_skips_stocks_without_prices(self):
        calc = make_calculator()
        with_prices = make_stock("AAA")
        make_stock("BBB")                            # no prices at all
        make_price(with_prices, date(2025, 10, 3), 100)
        make_price(with_prices, date(2026, 9, 3), 130)
        result = calc.calculate_momentum_scores_bulk(calculation_date=DAY)
        self.assertEqual([s.stock.ticker for s in result], ["AAA"])
        self.assertEqual(MomentumScore.objects.count(), 1)

    def test_bulk_stores_the_period_actually_used(self):
        calc = make_calculator()
        stock = make_stock()
        make_price(stock, date(2025, 10, 3), 100)
        make_price(stock, date(2026, 9, 3), 130)
        score = calc.calculate_momentum_scores_bulk(calculation_date=DAY)[0]
        self.assertEqual(score.period_start, date(2025, 10, 3))
        self.assertEqual(score.period_end, date(2026, 9, 3))
        self.assertEqual(score.momentum_score, Decimal("0.3"))


FAKE_ROWS = [
    {"date": date(2026, 1, 5), "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 5.0},
    {"date": date(2026, 1, 6), "open": 10.5, "high": 12.0, "low": 10.0, "close": 11.5, "volume": 7.0},
]

class BackfillTests(TestCase):
    def test_rerun_does_not_duplicate(self):
        calc = make_calculator()
        stock = make_stock()
        calc.massive_client.fetch_stock_data.return_value = FAKE_ROWS
        calc.backfill_price_data(stock)
        calc.backfill_price_data(stock)
        self.assertEqual(PriceData.objects.count(), 2)


    def test_returns_row_count_and_stores_rows(self):
        calc = make_calculator()
        stock = make_stock()
        calc.massive_client.fetch_stock_data.return_value = FAKE_ROWS
        self.assertEqual(calc.backfill_price_data(stock), 2)
        self.assertEqual(PriceData.objects.filter(stock=stock).count(), 2)

    def test_asks_the_api_for_the_right_ticker(self):
        calc = make_calculator()
        stock = make_stock("XYZ")
        calc.massive_client.fetch_stock_data.return_value = []
        calc.backfill_price_data(stock)
        self.assertEqual(calc.massive_client.fetch_stock_data.call_args.kwargs["ticker"], "XYZ")

    def test_revised_data_is_updated_not_duplicated(self):
        calc = make_calculator()
        stock = make_stock()
        calc.massive_client.fetch_stock_data.return_value = FAKE_ROWS
        calc.backfill_price_data(stock)
        revised = [dict(FAKE_ROWS[0], close=5.25), FAKE_ROWS[1]]   # e.g. after a split
        calc.massive_client.fetch_stock_data.return_value = revised
        calc.backfill_price_data(stock)
        self.assertEqual(PriceData.objects.count(), 2)
        row = PriceData.objects.get(stock=stock, date=date(2026, 1, 5))
        self.assertEqual(row.close, Decimal("5.25"))

    def test_adjusted_close_is_null_and_volume_is_whole(self):
        calc = make_calculator()
        stock = make_stock()
        calc.massive_client.fetch_stock_data.return_value = FAKE_ROWS
        calc.backfill_price_data(stock)
        row = PriceData.objects.order_by("date").first()
        self.assertIsNone(row.adjusted_close)
        self.assertEqual(row.volume, 5)
        self.assertIsInstance(row.volume, int)

    def test_api_error_propagates_and_stores_nothing(self):
        calc = make_calculator()
        stock = make_stock()
        calc.massive_client.fetch_stock_data.side_effect = ValueError("boom")
        with self.assertRaises(ValueError):
            calc.backfill_price_data(stock)
        self.assertEqual(PriceData.objects.count(), 0)

    def test_empty_api_answer_stores_nothing(self):
        calc = make_calculator()
        stock = make_stock()
        calc.massive_client.fetch_stock_data.return_value = []
        self.assertEqual(calc.backfill_price_data(stock), 0)
        self.assertEqual(PriceData.objects.count(), 0)

class PriceDataConstraintTests(TestCase):
    def test_duplicate_stock_and_date_is_rejected(self):
        stock = make_stock()
        make_price(stock, DAY, 100)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_price(stock, DAY, 101)

    def test_different_date_is_allowed(self):
        stock = make_stock()
        make_price(stock, DAY, 100)
        make_price(stock, DAY - timedelta(days=1), 101)
        self.assertEqual(PriceData.objects.count(), 2)

class LoadUniverseTests(TestCase):
    CSV = "ticker,name,sector\nAAA,Alpha,Tech\nBBB,Beta,Energy\n"

    def write_csv(self, text):
        folder = self.enterContext(tempfile.TemporaryDirectory())  # removed after the test
        path = Path(folder) / "u.csv"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def load(self, path):
        out = io.StringIO()
        call_command("load_universe", file=path, stdout=out)
        return out.getvalue()

    def test_second_load_is_idempotent(self):
        path = self.write_csv(self.CSV)
        self.load(path)
        output = self.load(path)
        self.assertEqual(Stock.objects.count(), 2)
        self.assertIn("0 new", output)


    def test_first_load_creates_stocks_with_name_and_sector(self):
        output = self.load(self.write_csv(self.CSV))
        self.assertIn("2 new", output)
        alpha = Stock.objects.get(ticker="AAA")
        self.assertEqual((alpha.name, alpha.sector, alpha.is_active), ("Alpha", "Tech", True))

    def test_removed_ticker_is_deactivated_not_deleted(self):
        self.load(self.write_csv(self.CSV))
        old_id = Stock.objects.get(ticker="BBB").id
        self.load(self.write_csv("ticker,name,sector\nAAA,Alpha,Tech\n"))
        bbb = Stock.objects.get(ticker="BBB")
        self.assertFalse(bbb.is_active)
        self.assertEqual(bbb.id, old_id)
        self.assertTrue(Stock.objects.get(ticker="AAA").is_active)

    def test_readded_ticker_is_reactivated_with_same_id(self):
        self.load(self.write_csv(self.CSV))
        old_id = Stock.objects.get(ticker="BBB").id
        self.load(self.write_csv("ticker,name,sector\nAAA,Alpha,Tech\n"))
        self.load(self.write_csv(self.CSV))
        bbb = Stock.objects.get(ticker="BBB")
        self.assertTrue(bbb.is_active)
        self.assertEqual(bbb.id, old_id)

    def test_messy_rows_are_normalised(self):
        self.load(self.write_csv("ticker,name,sector\n  aaa ,Alpha,Tech\n,,\n"))
        self.assertEqual(list(Stock.objects.values_list("ticker", flat=True)), ["AAA"])

    def test_changed_name_is_updated_on_reload(self):
        self.load(self.write_csv(self.CSV))
        self.load(self.write_csv("ticker,name,sector\nAAA,Alpha Renamed,Tech\nBBB,Beta,Energy\n"))
        self.assertEqual(Stock.objects.get(ticker="AAA").name, "Alpha Renamed")

    def test_missing_file_raises(self):
        with self.assertRaises(CommandError):
            self.load("/no/such/file.csv")

    def test_csv_without_ticker_column_raises(self):
        with self.assertRaises(CommandError):
            self.load(self.write_csv("symbol,name\nAAA,Alpha\n"))

    def test_header_only_csv_raises(self):
        with self.assertRaises(CommandError):
            self.load(self.write_csv("ticker,name,sector\n"))

    def test_shipped_universe_file_loads_30_stocks(self):
        call_command("load_universe", stdout=io.StringIO())
        self.assertEqual(Stock.objects.filter(is_active=True).count(), 30)
        self.assertEqual(Stock.objects.filter(sector="").count(), 0)

class PullPricesTests(TestCase):
    def test_one_failure_does_not_stop_the_batch(self):
        for ticker in ["AAA", "BBB", "CCC"]:
            make_stock(ticker)

        def fake_backfill(stock, days_back):
            if stock.ticker == "BBB":
                raise RuntimeError("API down")
            return 289

        with mock.patch("trading.management.commands.pull_prices.MomentumCalculator") as Calc:
            Calc.return_value.backfill_price_data.side_effect = fake_backfill
            with self.assertRaises(CommandError) as ctx:
                call_command("pull_prices", stdout=io.StringIO(), stderr=io.StringIO())

        self.assertEqual(Calc.return_value.backfill_price_data.call_count, 3)
        self.assertIn("BBB", str(ctx.exception))


    def run_pull(self, **options):
        """Run pull_prices with a fake calculator; returns the fake so tests can inspect it."""
        with mock.patch("trading.management.commands.pull_prices.MomentumCalculator") as Calc:
            Calc.return_value.backfill_price_data.return_value = 289
            call_command("pull_prices", stdout=io.StringIO(), stderr=io.StringIO(), **options)
        return Calc

    def test_tickers_option_limits_the_run(self):
        make_stock("AAA")
        make_stock("BBB")
        Calc = self.run_pull(tickers=["aaa"])   # lower case on purpose
        backfill = Calc.return_value.backfill_price_data
        self.assertEqual(backfill.call_count, 1)
        self.assertEqual(backfill.call_args.args[0].ticker, "AAA")

    def test_inactive_stocks_are_skipped(self):
        make_stock("AAA")
        make_stock("BBB")
        make_stock("CCC", is_active=False)
        Calc = self.run_pull()
        self.assertEqual(Calc.return_value.backfill_price_data.call_count, 2)

    def test_days_option_is_passed_on(self):
        make_stock("AAA")
        Calc = self.run_pull(days=100)
        self.assertEqual(Calc.return_value.backfill_price_data.call_args.kwargs["days_back"], 100)

    def test_rpm_option_sets_the_rate_limit(self):
        make_stock("AAA")
        Calc = self.run_pull(rpm=12)
        self.assertEqual(Calc.return_value.massive_client.requests_per_minute, 12)

    def test_no_active_stocks_raises(self):
        make_stock("AAA", is_active=False)
        with self.assertRaises(CommandError):
            self.run_pull()

class StrategyTests(TestCase):
    def make_strategy(self, portfolio):
        with mock.patch("trading.services.momentum_calculator.get_massive_client"):
            return MomentumTradingStrategy(portfolio)

    def setUp(self):
        self.portfolio = Portfolio.objects.create(
            name="p", initial_cash=Decimal("10000"), current_cash=Decimal("10000"))
        self.stocks = []
        for i in range(25):
            s = make_stock(f"T{i:02d}")
            self.stocks.append(s)
            MomentumScore.objects.create(
                stock=s, calculation_date=DAY, momentum_score=Decimal(i) / 100,
                period_start=DAY, period_end=DAY)
        MomentumScore.calculate_quintiles_for_date(DAY)
        Position.objects.create(
            portfolio=self.portfolio, stock=self.stocks[0], quantity=Decimal("10"),
            average_cost=Decimal("5"), current_price=Decimal("5"), current_value=Decimal("50"))
        self.strategy = self.make_strategy(self.portfolio)

    def test_signals_buys_and_sells(self):
        buys, sells = self.strategy.generate_trading_signals(DAY)
        self.assertEqual(sorted(b.stock.ticker for b in buys), ["T20", "T21", "T22", "T23", "T24"])
        self.assertEqual([s.stock.ticker for s in sells], ["T00"])
        for b in buys:
            self.assertEqual(b.target_value, Decimal("1900"))   # 10000 * 0.95 / 5


    def make_event(self, portfolio, status, days_ago):
        return RebalanceEvent.objects.create(
            portfolio=portfolio,
            date=timezone.now().date() - timedelta(days=days_ago),
            total_stocks_analyzed=0,
            buy_signals_generated=0,
            sell_signals_generated=0,
            execution_status=status,
        )

    def stub_slow_steps(self):
        """Replace the universe + API scoring steps; ranking and signals stay real."""
        calc = self.strategy.momentum_calculator
        calc.update_stock_universe = mock.Mock(return_value=self.stocks)
        calc.calculate_momentum_scores_bulk = mock.Mock(
            return_value=list(MomentumScore.objects.all())
        )
        return calc

    def test_rerun_replaces_signals_instead_of_duplicating(self):
        self.strategy.generate_trading_signals(DAY)
        self.strategy.generate_trading_signals(DAY)
        self.assertEqual(TradingSignal.objects.filter(signal_date=DAY).count(), 6)

    def test_held_top_stock_gets_no_buy_signal(self):
        Position.objects.create(
            portfolio=self.portfolio, stock=self.stocks[24], quantity=Decimal("1"),
            average_cost=Decimal("5"), current_price=Decimal("5"), current_value=Decimal("5"))
        buys, sells = self.strategy.generate_trading_signals(DAY)
        self.assertEqual(sorted(b.stock.ticker for b in buys), ["T20", "T21", "T22", "T23"])
        for b in buys:
            self.assertEqual(b.target_value, Decimal("2375"))   # 10000 * 0.95 / 4
        self.assertEqual([s.stock.ticker for s in sells], ["T00"])

    def test_no_cash_means_no_buy_signals(self):
        self.portfolio.current_cash = Decimal("0")
        buys, sells = self.strategy.generate_trading_signals(DAY)
        self.assertEqual(buys, [])
        self.assertEqual(len(sells), 1)

    def test_live_mode_is_refused_and_creates_no_event(self):
        with self.assertRaises(NotImplementedError):
            self.strategy.execute_rebalance(DAY, dry_run=False)
        self.assertEqual(RebalanceEvent.objects.count(), 0)

    def test_execute_trading_signals_is_not_implemented_yet(self):
        with self.assertRaises(NotImplementedError):
            self.strategy.execute_trading_signals([], [], None)

    def test_dry_run_records_a_dry_run_event(self):
        self.stub_slow_steps()
        event = self.strategy.execute_rebalance(DAY)
        self.assertEqual(event.execution_status, "DRY_RUN")
        self.assertEqual(event.portfolio, self.portfolio)
        self.assertEqual(event.total_stocks_analyzed, 25)
        self.assertEqual(event.buy_signals_generated, 5)
        self.assertEqual(event.sell_signals_generated, 1)
        self.assertEqual(event.total_portfolio_value, Decimal("10050"))   # 10000 cash + 50 position
        self.assertIsNotNone(event.completed_at)

    def test_failure_is_recorded_and_reraised(self):
        calc = self.stub_slow_steps()
        calc.update_stock_universe = mock.Mock(side_effect=RuntimeError("universe exploded"))
        with self.assertRaises(RuntimeError):
            self.strategy.execute_rebalance(DAY)
        event = RebalanceEvent.objects.get()
        self.assertEqual(event.execution_status, "FAILED")
        self.assertEqual(event.error_message, "universe exploded")

    def test_should_rebalance_with_no_history(self):
        self.assertTrue(self.strategy.should_rebalance())

    def test_dry_run_does_not_block_a_real_rebalance(self):
        self.make_event(self.portfolio, "DRY_RUN", 0)
        self.assertTrue(self.strategy.should_rebalance())

    def test_failed_event_does_not_block(self):
        self.make_event(self.portfolio, "FAILED", 1)
        self.assertTrue(self.strategy.should_rebalance())

    def test_recent_completed_rebalance_blocks(self):
        self.make_event(self.portfolio, "COMPLETED", 3)
        self.assertFalse(self.strategy.should_rebalance())

    def test_six_days_after_completed_rebalance_still_blocks(self):
        self.make_event(self.portfolio, "COMPLETED", 6)
        self.assertFalse(self.strategy.should_rebalance())

    def test_seven_days_after_completed_rebalance_allows(self):
        self.make_event(self.portfolio, "COMPLETED", 7)
        self.assertTrue(self.strategy.should_rebalance())

    def test_old_completed_rebalance_does_not_block(self):
        self.make_event(self.portfolio, "COMPLETED", 10)
        self.assertTrue(self.strategy.should_rebalance())

    def test_other_portfolios_rebalance_does_not_block(self):
        other = Portfolio.objects.create(
            name="other", initial_cash=Decimal("1"), current_cash=Decimal("1"))
        self.make_event(other, "COMPLETED", 3)
        self.assertTrue(self.strategy.should_rebalance())

    def test_monthly_frequency(self):
        with self.settings(REBALANCE_FREQUENCY="monthly"):
            strategy = self.make_strategy(self.portfolio)   # reads the setting when created
        self.make_event(self.portfolio, "COMPLETED", 10)
        self.assertFalse(strategy.should_rebalance())
        RebalanceEvent.objects.all().delete()
        self.make_event(self.portfolio, "COMPLETED", 30)
        self.assertTrue(strategy.should_rebalance())

    def test_validate_reports_missing_api_key(self):
        with self.settings(MASSIVE_API_KEY=""):
            result = self.strategy.validate_strategy_setup()
        self.assertFalse(result["is_valid"])
        self.assertIn("Massive API key not configured", result["issues"])

    def test_validate_ok_with_key_but_warns_about_broker(self):
        with self.settings(MASSIVE_API_KEY="test-key"):
            result = self.strategy.validate_strategy_setup()
        self.assertTrue(result["is_valid"])
        self.assertIn("Portfolio has no broker set", result["warnings"])

