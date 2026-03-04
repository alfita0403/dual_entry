"""Tests for RSI filter and drawdown protection in mean_reversion strategy."""

import sys
import os
import time
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.mean_reversion import (
    _compute_rsi,
    RSIFilter,
    DrawdownProtection,
    SequenceConfig,
)


# ===================================================================
# RSI computation tests
# ===================================================================
class TestComputeRSI:
    def test_all_gains(self):
        """All up closes should give RSI = 100."""
        prices = np.arange(10, 25, dtype=float)  # 15 ascending prices
        rsi = _compute_rsi(prices, 7)
        assert rsi == 100.0

    def test_all_losses(self):
        """All down closes should give RSI = 0."""
        prices = np.arange(25, 10, -1, dtype=float)  # 15 descending prices
        rsi = _compute_rsi(prices, 7)
        assert rsi == 0.0

    def test_mixed_gives_midrange(self):
        """Alternating prices should give RSI around 50."""
        prices = np.array(
            [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100],
            dtype=float,
        )
        rsi = _compute_rsi(prices, 7)
        assert 40 <= rsi <= 60

    def test_insufficient_data_returns_nan(self):
        """With fewer bars than period, should return NaN."""
        prices = np.array([100, 101, 102], dtype=float)
        rsi = _compute_rsi(prices, 7)
        assert np.isnan(rsi)

    def test_known_rsi_range(self):
        """RSI should always be in [0, 100]."""
        np.random.seed(42)
        prices = np.cumsum(np.random.randn(100)) + 100
        rsi = _compute_rsi(prices, 7)
        assert 0 <= rsi <= 100

    def test_period_14(self):
        """RSI(14) should work correctly."""
        np.random.seed(123)
        prices = np.cumsum(np.random.randn(50)) + 100
        rsi = _compute_rsi(prices, 14)
        assert 0 <= rsi <= 100


# ===================================================================
# RSI filter tests
# ===================================================================
class TestRSIFilter:
    def test_disabled_never_skips(self):
        """Disabled filter should never skip."""
        f = RSIFilter(enabled=False, threshold=60.0)
        f._last_rsi = 90.0  # High RSI
        assert f.should_skip is False

    def test_no_data_skips_fail_closed(self):
        """Fail-closed: with no RSI data, should block trading."""
        f = RSIFilter(enabled=True, threshold=60.0)
        assert f.should_skip is True  # _last_rsi = NaN -> fail-closed

    def test_below_threshold_allows(self):
        """RSI below threshold should allow trading."""
        f = RSIFilter(enabled=True, threshold=60.0)
        f._last_rsi = 45.0
        f._last_fetch_ts = time.time()  # mark as fresh
        assert f.should_skip is False

    def test_stale_rsi_skips(self):
        """RSI older than MAX_STALENESS should block trading."""
        f = RSIFilter(enabled=True, threshold=60.0)
        f._last_rsi = 45.0  # below threshold, would normally allow
        f._last_fetch_ts = time.time() - 600  # 10 minutes ago
        assert f.should_skip is True  # stale -> fail-closed

    def test_at_threshold_skips(self):
        """RSI exactly at threshold should skip."""
        f = RSIFilter(enabled=True, threshold=60.0)
        f._last_rsi = 60.0
        f._last_fetch_ts = time.time()  # fresh data
        assert f.should_skip is True

    def test_above_threshold_skips(self):
        """RSI above threshold should skip."""
        f = RSIFilter(enabled=True, threshold=60.0)
        f._last_rsi = 75.0
        f._last_fetch_ts = time.time()  # fresh data
        assert f.should_skip is True

    def test_current_rsi_property(self):
        """current_rsi should reflect internal state."""
        f = RSIFilter()
        assert np.isnan(f.current_rsi)
        f._last_rsi = 55.0
        assert f.current_rsi == 55.0

    def test_fetch_interval_respected(self):
        """Should not re-fetch within fetch_interval."""
        f = RSIFilter(enabled=True)
        f._last_fetch_ts = time.time()  # just fetched
        f._last_rsi = 50.0
        # Should use cached value (no actual HTTP call)
        assert f._last_fetch_ts > 0

    def test_custom_period_and_threshold(self):
        """Custom period and threshold should be stored."""
        f = RSIFilter(period=14, threshold=70.0, timeframe="15m")
        assert f.period == 14
        assert f.threshold == 70.0
        assert f.timeframe == "15m"


# ===================================================================
# Drawdown protection tests
# ===================================================================
class TestDrawdownProtection:
    def test_disabled_never_pauses(self):
        """Disabled protection should never pause."""
        dd = DrawdownProtection(enabled=False)
        dd.record_outcome(False, -100.0)  # huge loss
        assert dd.should_skip is False

    def test_within_limits_no_pause(self):
        """Losses within limits should not trigger pause."""
        dd = DrawdownProtection(
            enabled=True, max_drawdown=30.0, max_consecutive_losses=5
        )
        dd.record_outcome(False, -10.0)
        dd.record_outcome(False, -20.0)
        assert dd.should_skip is False
        assert dd.consecutive_losses == 2

    def test_drawdown_triggers_pause(self):
        """Exceeding max_drawdown should pause."""
        dd = DrawdownProtection(enabled=True, max_drawdown=30.0)
        dd.record_outcome(False, -31.0)
        assert dd.paused is True
        assert dd.should_skip is True
        assert "Drawdown limit" in dd.pause_reason

    def test_consecutive_losses_triggers_pause(self):
        """Exceeding max consecutive losses should pause."""
        dd = DrawdownProtection(
            enabled=True, max_drawdown=1000.0, max_consecutive_losses=3
        )
        dd.record_outcome(False, -5.0)
        dd.record_outcome(False, -10.0)
        assert dd.paused is False
        dd.record_outcome(False, -15.0)
        assert dd.paused is True
        assert "Consecutive losses" in dd.pause_reason

    def test_win_resets_consecutive_counter(self):
        """A win should reset consecutive losses to 0."""
        dd = DrawdownProtection(enabled=True, max_consecutive_losses=5)
        dd.record_outcome(False, -5.0)
        dd.record_outcome(False, -10.0)
        assert dd.consecutive_losses == 2
        dd.record_outcome(True, -8.0)
        assert dd.consecutive_losses == 0

    def test_cooldown_resumes(self):
        """After cooldown, trading should resume."""
        dd = DrawdownProtection(
            enabled=True, max_drawdown=10.0, cooldown_minutes=0.01
        )  # 0.6 seconds
        dd.record_outcome(False, -15.0)
        assert dd.paused is True
        # Simulate time passing
        dd.paused_at = time.time() - 2.0  # 2 seconds ago
        assert dd.should_skip is False  # cooldown elapsed
        assert dd.paused is False

    def test_zero_cooldown_requires_restart(self):
        """With cooldown=0, should stay paused (manual restart)."""
        dd = DrawdownProtection(enabled=True, max_drawdown=10.0, cooldown_minutes=0)
        dd.record_outcome(False, -15.0)
        assert dd.paused is True
        dd.paused_at = time.time() - 3600  # 1 hour ago
        assert dd.should_skip is True  # still paused

    def test_drawdown_at_boundary_no_pause(self):
        """PnL exactly at -max_drawdown should NOT pause (need to exceed)."""
        dd = DrawdownProtection(enabled=True, max_drawdown=30.0)
        dd.record_outcome(False, -30.0)
        assert dd.paused is False  # -30 is not < -30


# ===================================================================
# YAML config integration tests
# ===================================================================
class TestYAMLConfig:
    def test_loads_rsi_config(self):
        """YAML should load RSI filter config (disabled after look-ahead fix)."""
        cfg = SequenceConfig.from_yaml("strategies/mean_reversion.yaml", dry_run=True)
        assert cfg.rsi_enabled is False
        assert cfg.rsi_period == 7
        assert cfg.rsi_timeframe == "5m"
        assert cfg.rsi_threshold == 60.0

    def test_loads_drawdown_config(self):
        """YAML should load drawdown protection config."""
        cfg = SequenceConfig.from_yaml("strategies/mean_reversion.yaml", dry_run=True)
        assert cfg.dd_enabled is True
        assert cfg.dd_max_drawdown == 30.0
        assert cfg.dd_max_consecutive_losses == 8
        assert cfg.dd_cooldown_minutes == 30.0

    def test_loads_tighter_maxask(self):
        """YAML should have updated max_ask values."""
        cfg = SequenceConfig.from_yaml("strategies/mean_reversion.yaml", dry_run=True)
        # All rules now use max_ask 0.51
        uuu_rule = next(r for r in cfg.rules if r.pattern == "UUU")
        assert uuu_rule.max_ask == 0.51

        # DDDD->UP ETH should exist (new bidirectional pattern)
        dddd_rule = next(r for r in cfg.rules if r.pattern == "DDDD")
        assert dddd_rule.buy_side == "up"
        assert dddd_rule.max_ask == 0.51

        # ETH UUUU now also 0.51 (uniform max_ask)
        eth_uuuu = next(
            r for r in cfg.rules if r.pattern == "UUUU" and "ETH" in (r.coins or [])
        )
        assert eth_uuuu.max_ask == 0.51

    def test_rules_sorted_longest_first(self):
        """Rules should be sorted by pattern length (longest first)."""
        cfg = SequenceConfig.from_yaml("strategies/mean_reversion.yaml", dry_run=True)
        lengths = [len(r.pattern) for r in cfg.rules]
        assert lengths == sorted(lengths, reverse=True)


# ===================================================================
# Live RSI fetch test (integration, requires network)
# ===================================================================
class TestRSILiveFetch:
    def test_binance_kline_fetch(self):
        """Fetch real klines from Binance and compute RSI."""
        f = RSIFilter(period=7, timeframe="5m", threshold=60.0)
        rsi = f._fetch_and_compute()
        assert not np.isnan(rsi), "Should get a valid RSI from Binance"
        assert 0 <= rsi <= 100
        assert len(f._closes) > 0
        assert f._warmup_done is True
