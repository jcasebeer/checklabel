"""Spend-cap accounting: bucketing, rolling window, cap enforcement."""
from app import config
from app.spend import SpendLedger, bucket_for_ip, estimate_usd, usage_usd


class TestBucketing:
    def test_ipv4_buckets_are_full_addresses(self):
        assert bucket_for_ip("203.0.113.7") == "203.0.113.7/32"
        assert bucket_for_ip("203.0.113.7") != bucket_for_ip("203.0.113.8")

    def test_ipv6_buckets_by_top_32_bits(self):
        # Rotating within a /64 (or anywhere below the provider /32) must not
        # reset the meter.
        a = bucket_for_ip("2001:db8:aaaa:1::1")
        b = bucket_for_ip("2001:db8:ffff:2:abcd::9")
        assert a == b == "2001:db8::/32"
        assert bucket_for_ip("2607:f8b0::1") != a

    def test_non_ip_strings_are_their_own_bucket(self):
        assert bucket_for_ip("testclient") == "testclient"


class TestLedger:
    def test_charges_accumulate_and_expire(self):
        led = SpendLedger(window_seconds=100)
        led.charge("b", 0.5, now=1000)
        led.charge("b", 0.25, now=1050)
        assert led.spent("b", now=1060) == 0.75
        # First charge ages out of the window.
        assert led.spent("b", now=1101) == 0.25

    def test_buckets_are_independent(self):
        led = SpendLedger()
        led.charge("a", 1.0, now=0)
        assert led.spent("b", now=1) == 0.0

    def test_would_exceed(self, monkeypatch):
        monkeypatch.setattr(config, "SPEND_CAP_PER_IP_USD", 1.0)
        led = SpendLedger()
        led.charge("b", 0.9)
        assert not led.would_exceed("b", 0.05)
        assert led.would_exceed("b", 0.2)

    def test_cap_zero_disables(self, monkeypatch):
        monkeypatch.setattr(config, "SPEND_CAP_PER_IP_USD", 0.0)
        led = SpendLedger()
        led.charge("b", 1e9)
        assert not led.would_exceed("b", 1e9)


class TestCostModel:
    def test_usage_usd_uses_configured_rates(self, monkeypatch):
        monkeypatch.setattr(config, "USD_PER_MTOK_IN", 3.0)
        monkeypatch.setattr(config, "USD_PER_MTOK_OUT", 15.0)
        assert usage_usd({"input_tokens": 1_000_000, "output_tokens": 0}) == 3.0
        assert usage_usd({"input_tokens": 0, "output_tokens": 100_000}) == 1.5
        assert usage_usd(None) == 0.0

    def test_estimate_scales_with_panels_and_halves_for_batch(self):
        one = estimate_usd(1)
        two = estimate_usd(2)
        assert two > one > 0
        assert estimate_usd(2, batch_pricing=True) == two / 2
