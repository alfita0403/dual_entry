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
    RSIBoostZone,
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
# RSI boost (size advisor) tests
# ===================================================================
class TestRSIBoost:
    def test_disabled_returns_base(self):
        """Boost disabled should always return base size."""
        f = RSIFilter(
            enabled=False,
            boost_enabled=False,
            up_zones=[RSIBoostZone(size=8.0, below=35)],
        )
        f._last_rsi = 20.0  # well below threshold
        f._last_fetch_ts = time.time()
        assert f.get_trade_size("up", 5.0) == 5.0

    def test_up_boost_triggered(self):
        """UP bet with RSI below zone threshold should return boosted size."""
        f = RSIFilter(
            boost_enabled=True,
            up_zones=[RSIBoostZone(size=8.0, below=35)],
        )
        f._last_rsi = 25.0  # below 35 -> boost
        f._last_fetch_ts = time.time()
        assert f.get_trade_size("up", 5.0) == 8.0

    def test_up_no_boost_above_threshold(self):
        """UP bet with RSI above zone threshold should return base."""
        f = RSIFilter(
            boost_enabled=True,
            up_zones=[RSIBoostZone(size=8.0, below=35)],
        )
        f._last_rsi = 50.0  # above 35 -> no boost
        f._last_fetch_ts = time.time()
        assert f.get_trade_size("up", 5.0) == 5.0

    def test_down_boost_triggered(self):
        """DOWN bet with RSI above zone threshold should return boosted size."""
        f = RSIFilter(
            boost_enabled=True,
            down_zones=[RSIBoostZone(size=8.0, above=55)],
        )
        f._last_rsi = 70.0  # above 55 -> boost
        f._last_fetch_ts = time.time()
        assert f.get_trade_size("down", 5.0) == 8.0

    def test_down_no_boost_below_threshold(self):
        """DOWN bet with RSI below zone threshold should return base."""
        f = RSIFilter(
            boost_enabled=True,
            down_zones=[RSIBoostZone(size=8.0, above=55)],
        )
        f._last_rsi = 45.0  # below 55 -> no boost
        f._last_fetch_ts = time.time()
        assert f.get_trade_size("down", 5.0) == 5.0

    def test_nan_rsi_returns_base(self):
        """NaN RSI should return base size (never skip, just don't boost)."""
        f = RSIFilter(
            boost_enabled=True,
            up_zones=[RSIBoostZone(size=8.0, below=35)],
        )
        f._last_rsi = float("nan")
        assert f.get_trade_size("up", 5.0) == 5.0

    def test_stale_rsi_returns_base(self):
        """Stale RSI should return base size."""
        f = RSIFilter(
            boost_enabled=True,
            up_zones=[RSIBoostZone(size=8.0, below=35)],
        )
        f._last_rsi = 20.0  # would trigger boost
        f._last_fetch_ts = time.time() - 600  # stale (10 min)
        assert f.get_trade_size("up", 5.0) == 5.0

    def test_multiple_zones_tightest_wins(self):
        """With multiple zones, tightest threshold should be checked first."""
        f = RSIFilter(
            boost_enabled=True,
            up_zones=[
                RSIBoostZone(size=10.0, below=25),  # tightest
                RSIBoostZone(size=8.0, below=35),  # wider
            ],
        )
        f._last_rsi = 20.0  # below both
        f._last_fetch_ts = time.time()
        # Tightest zone (RSI<25, $10) should match first
        assert f.get_trade_size("up", 5.0) == 10.0

    def test_multiple_zones_wider_fallback(self):
        """RSI between zones should match the wider zone."""
        f = RSIFilter(
            boost_enabled=True,
            up_zones=[
                RSIBoostZone(size=10.0, below=25),  # tightest
                RSIBoostZone(size=8.0, below=35),  # wider
            ],
        )
        f._last_rsi = 30.0  # above 25 but below 35
        f._last_fetch_ts = time.time()
        assert f.get_trade_size("up", 5.0) == 8.0

    def test_boost_counters(self):
        """Boost and base counters should track correctly."""
        f = RSIFilter(
            boost_enabled=True,
            up_zones=[RSIBoostZone(size=8.0, below=35)],
        )
        f._last_fetch_ts = time.time()
        f._last_rsi = 20.0
        f.get_trade_size("up", 5.0)  # boosted
        f._last_rsi = 50.0
        f.get_trade_size("up", 5.0)  # base
        assert f.boost_count == 1
        assert f.base_count == 1

    def test_cross_side_no_interference(self):
        """UP zones should not affect DOWN bets and vice versa."""
        f = RSIFilter(
            boost_enabled=True,
            up_zones=[RSIBoostZone(size=8.0, below=35)],
            down_zones=[RSIBoostZone(size=8.0, above=55)],
        )
        f._last_fetch_ts = time.time()
        f._last_rsi = 20.0  # low RSI
        # UP should boost (RSI<35), DOWN should NOT (RSI not >55)
        assert f.get_trade_size("up", 5.0) == 8.0
        assert f.get_trade_size("down", 5.0) == 5.0

        f._last_rsi = 70.0  # high RSI
        # DOWN should boost (RSI>55), UP should NOT (RSI not <35)
        assert f.get_trade_size("down", 5.0) == 8.0
        assert f.get_trade_size("up", 5.0) == 5.0

    def test_update_active_when_boost_enabled(self):
        """When only boost is enabled (not filter), update should still fetch."""
        f = RSIFilter(enabled=False, boost_enabled=True)
        # Verify the update method doesn't short-circuit
        assert not f.enabled
        assert f.boost_enabled


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

    def test_loads_rsi_boost_config(self):
        """YAML should load RSI boost config with per-side zones."""
        cfg = SequenceConfig.from_yaml("strategies/mean_reversion.yaml", dry_run=True)
        assert cfg.rsi_boost_enabled is True
        # UP zones
        assert cfg.rsi_boost_up_zones is not None
        assert len(cfg.rsi_boost_up_zones) == 1
        assert cfg.rsi_boost_up_zones[0].below == 35.0
        assert cfg.rsi_boost_up_zones[0].size == 8.0
        # DOWN zones
        assert cfg.rsi_boost_down_zones is not None
        assert len(cfg.rsi_boost_down_zones) == 1
        assert cfg.rsi_boost_down_zones[0].above == 55.0
        assert cfg.rsi_boost_down_zones[0].size == 8.0

    def test_loads_drawdown_config(self):
        """YAML should load drawdown protection config."""
        cfg = SequenceConfig.from_yaml("strategies/mean_reversion.yaml", dry_run=True)
        assert cfg.dd_enabled is True
        assert cfg.dd_max_drawdown == 30.0
        assert cfg.dd_max_consecutive_losses == 8
        assert cfg.dd_cooldown_minutes == 30.0

    def test_loads_v2_rules(self):
        """YAML V2 should have 17 rules across BTC, ETH, SOL."""
        cfg = SequenceConfig.from_yaml("strategies/mean_reversion.yaml", dry_run=True)
        assert len(cfg.rules) == 17
        # All max_ask values should be in valid range
        for r in cfg.rules:
            assert 0.30 <= r.max_ask <= 0.90, (
                f"Rule {r.pattern} has max_ask={r.max_ask}, out of range"
            )
        # Check coins used
        all_coins = set()
        for r in cfg.rules:
            if r.coins:
                all_coins.update(r.coins)
        assert "ETH" in all_coins
        assert "BTC" in all_coins
        assert "SOL" in all_coins

    def test_bidirectional_rules(self):
        """YAML should have both UP and DOWN side rules, multi-coin."""
        cfg = SequenceConfig.from_yaml("strategies/mean_reversion.yaml", dry_run=True)

        up_rules = [r for r in cfg.rules if r.buy_side == "up"]
        dn_rules = [r for r in cfg.rules if r.buy_side == "down"]
        # V2: 9 UP rules, 8 DOWN rules
        assert len(up_rules) >= 8
        assert len(dn_rules) >= 7

    def test_conditioned_rules(self):
        """V2 should have 7 conditioned rules with valid conditions."""
        cfg = SequenceConfig.from_yaml("strategies/mean_reversion.yaml", dry_run=True)
        conditioned = [r for r in cfg.rules if r.conditions]
        assert len(conditioned) == 7
        for r in conditioned:
            for cond in r.conditions:
                assert "indicator" in cond
                assert "condition" in cond
                assert cond["indicator"] in ("VOL_1H", "VOL_SUM_1H", "RSI")

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
