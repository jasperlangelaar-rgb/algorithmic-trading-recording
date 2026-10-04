from decimal import Decimal

from django.test import TestCase

from portfolio.models import Portfolio, Position
from trading.models import Stock


def make_portfolio(cash="1000"):
    return Portfolio.objects.create(
        name="test",
        initial_cash=Decimal(str(cash)),
        current_cash=Decimal(str(cash)),
    )


def make_stock(ticker="AAA"):
    return Stock.objects.create(ticker=ticker)


class PositionTest(TestCase):
    def test_update_current_value_computes_values_and_pnl(self):
        # 2.5 * 110 = 275 value; 2.5 * 100 = 250 cost; 275 - 250 = 25; 25 / 250 = 10%
        pos = Position(
            portfolio=make_portfolio(), stock=make_stock(),
            quantity=Decimal("2.5"), average_cost=Decimal("100"),
            current_price=Decimal("110"),
        )
        pos.update_current_value()
        self.assertEqual(pos.current_value, Decimal("275"))
        self.assertEqual(pos.unrealized_pnl, Decimal("25"))
        self.assertEqual(pos.unrealized_pnl_percent, Decimal("10"))

    def test_loss_gives_negative_pnl(self):
        # 2.5 * 90 = 225 value; cost 250; 225 - 250 = -25; -25 / 250 = -10%
        pos = Position(
            portfolio=make_portfolio(), stock=make_stock(),
            quantity=Decimal("2.5"), average_cost=Decimal("100"),
            current_price=Decimal("90"),
        )
        pos.update_current_value()
        self.assertEqual(pos.current_value, Decimal("225"))
        self.assertEqual(pos.unrealized_pnl, Decimal("-25"))
        self.assertEqual(pos.unrealized_pnl_percent, Decimal("-10"))

    def test_no_price_changes_nothing(self):
        # Start from non-zero values so an overwrite with 0 would be noticed
        pos = Position(
            portfolio=make_portfolio(), stock=make_stock(),
            quantity=Decimal("2"), average_cost=Decimal("100"),
            current_price=None,
            current_value=Decimal("50"),
            unrealized_pnl=Decimal("5"),
            unrealized_pnl_percent=Decimal("1"),
        )
        pos.update_current_value()
        self.assertEqual(pos.current_value, Decimal("50"))
        self.assertEqual(pos.unrealized_pnl, Decimal("5"))
        self.assertEqual(pos.unrealized_pnl_percent, Decimal("1"))

    def test_zero_cost_basis_does_not_divide_by_zero(self):
        pos = Position(
            portfolio=make_portfolio(), stock=make_stock(),
            quantity=Decimal("2"), average_cost=Decimal("0"),
            current_price=Decimal("10"),
        )
        pos.update_current_value()
        self.assertEqual(pos.current_value, Decimal("20"))
        self.assertEqual(pos.unrealized_pnl, Decimal("20"))
        self.assertEqual(pos.unrealized_pnl_percent, Decimal("0"))


class PortfolioTest(TestCase):
    def test_total_value_is_cash_plus_positions(self):
        # current_value is set directly so this test does not depend on
        # Position.update_current_value: 1000 + 220 + 180 = 1400
        p = make_portfolio(cash="1000")
        Position.objects.create(
            portfolio=p, stock=make_stock("AAA"), quantity=Decimal("2"),
            average_cost=Decimal("100"), current_price=Decimal("110"),
            current_value=Decimal("220"),
        )
        Position.objects.create(
            portfolio=p, stock=make_stock("BBB"), quantity=Decimal("3"),
            average_cost=Decimal("50"), current_price=Decimal("60"),
            current_value=Decimal("180"),
        )
        p.calculate_total_value()
        self.assertEqual(p.total_value, Decimal("1400"))

    def test_total_value_with_no_positions(self):
        p = make_portfolio(cash="500")
        p.calculate_total_value()
        self.assertEqual(p.total_value, Decimal("500"))

    def test_get_current_positions_excludes_zero_quantity(self):
        p = make_portfolio(cash="1000")
        Position.objects.create(
            portfolio=p, stock=make_stock("AAA"), quantity=Decimal("2"),
            average_cost=Decimal("100"),
        )
        Position.objects.create(
            portfolio=p, stock=make_stock("BBB"), quantity=Decimal("0"),
            average_cost=Decimal("50"),
        )
        positions = list(p.get_current_positions())
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].stock.ticker, "AAA")
