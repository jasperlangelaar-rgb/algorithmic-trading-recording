from django.conf import settings
from massive import RESTClient
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
import logging
import time
from urllib3.exceptions import MaxRetryError
from massive.exceptions import BadResponse

logger = logging.getLogger(__name__)


class MassiveAPIClient:
    def __init__(self, api_key: str = None, requests_per_minute: int = 5):
        self.api_key = api_key or settings.MASSIVE_API_KEY
        if not self.api_key:
            raise ValueError("Massive API key is required")
        self.client = RESTClient(self.api_key)
        self.requests_per_minute = requests_per_minute
        self._cache = {}  # Simple in-memory cache
        self.request_times = []

    def _get_cache_key(self, ticker: str, start_date: str, end_date: str) -> str:
        """Generate cache key for API requests"""
        return f"{ticker}:{start_date}:{end_date}"

    def _rate_limit(self):
        """Implement rate limiting to avoid hitting API limits"""
        current_time = time.time()
        # Remove requests older than 1 minute
        self.request_times = [t for t in self.request_times if current_time - t < 60]

        if len(self.request_times) >= self.requests_per_minute:
            sleep_time = 60 - (current_time - self.request_times[0]) + 1
            if sleep_time > 0:
                logger.info(f"Rate limiting: sleeping for {sleep_time:.2f} seconds")
                time.sleep(sleep_time)

        self.request_times.append(current_time)

    def fetch_stock_data(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        multiplier: int = 1,
        timespan: str = "day",
        adjusted: bool = True,
        limit: int = 50000,
        use_cache: bool = True,
        max_retries: int = 3,
        retry_delay: int = 60,
    ) -> List[Dict]:
        cache_key = self._get_cache_key(ticker, start_date, end_date)

        # Check cache first
        if use_cache and cache_key in self._cache:
            logger.info(f"Using cached data for {ticker}")
            return self._cache[cache_key]

        # Apply rate limiting
        self._rate_limit()

        for attempt in range(max_retries):
            try:
                aggs = []
                for agg in self.client.list_aggs(
                    ticker,
                    multiplier,
                    timespan,
                    start_date,
                    end_date,
                    adjusted=adjusted,
                    limit=limit,
                ):
                    aggs.append(
                        {
                            "date": datetime.fromtimestamp(
                                agg.timestamp / 1000, tz=timezone.utc
                            ).date(),
                            "open": agg.open,
                            "high": agg.high,
                            "low": agg.low,
                            "close": agg.close,
                            "volume": agg.volume,
                            "vwap": getattr(agg, "vwap", None),
                            "transactions": getattr(agg, "transactions", None),
                        }
                    )

                # Cache the result
                if use_cache:
                    self._cache[cache_key] = aggs

                logger.info(f"Fetched {len(aggs)} data points for {ticker}")
                return aggs

            except MaxRetryError as e:
                if "429" in str(e) and attempt < max_retries - 1:
                    logger.warning(
                        f"Rate limited on attempt {attempt + 1} for {ticker}, retrying in {retry_delay} seconds"
                    )
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"Max retries exceeded for {ticker}: {str(e)}")
                    raise
            except (ValueError, TypeError, BadResponse) as e:
                logger.error(f"Error fetching data for {ticker}: {str(e)}")
                raise

    def fetch_multiple_stocks(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
        adjusted: bool = True,
        batch_size: int = 10,
        delay_between_batches: int = 12,
    ) -> Dict[str, List[Dict]]:
        """
        Fetch data for multiple stocks with intelligent batching and rate limiting.

        Args:
            tickers: List of stock tickers
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            adjusted: Whether to use adjusted prices
            batch_size: Number of stocks to process in each batch
            delay_between_batches: Seconds to wait between batches
        """
        results = {}
        total_tickers = len(tickers)

        logger.info(
            f"Fetching data for {total_tickers} stocks in batches of {batch_size}"
        )

        # Process tickers in batches
        for i in range(0, total_tickers, batch_size):
            batch_tickers = tickers[i : i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (total_tickers + batch_size - 1) // batch_size

            logger.info(
                f"Processing batch {batch_num}/{total_batches} ({len(batch_tickers)} stocks)"
            )

            for ticker in batch_tickers:
                try:
                    results[ticker] = self.fetch_stock_data(
                        ticker=ticker,
                        start_date=start_date,
                        end_date=end_date,
                        adjusted=adjusted,
                    )
                    logger.info(f"Successfully fetched data for {ticker}")
                except (MaxRetryError, ValueError, TypeError, BadResponse) as e:
                    logger.error(f"Failed to fetch data for {ticker}: {str(e)}")
                    results[ticker] = []

            # Delay between batches to avoid rate limits
            if i + batch_size < total_tickers:
                logger.info(
                    f"Waiting {delay_between_batches} seconds before next batch..."
                )
                time.sleep(delay_between_batches)

        return results

    def get_price_on_date(
        self, ticker: str, target_date: datetime, tolerance_days: int = 7
    ) -> Optional[float]:
        start_date = target_date - timedelta(days=tolerance_days)
        end_date = target_date + timedelta(days=tolerance_days)

        try:
            data = self.fetch_stock_data(
                ticker=ticker,
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
            )

            if not data:
                return None

            # Find the closest date
            target_date_obj = (
                target_date if hasattr(target_date, "date") else target_date
            )
            if hasattr(target_date_obj, "date"):
                target_date_obj = target_date_obj.date()

            closest_data = min(
                data, key=lambda x: abs((x["date"] - target_date_obj).days)
            )

            return float(closest_data["close"])

        except (ValueError, TypeError) as e:
            logger.error(f"Error getting price for {ticker} on {target_date}: {str(e)}")
            return None


def get_massive_client() -> MassiveAPIClient:
    return MassiveAPIClient()
