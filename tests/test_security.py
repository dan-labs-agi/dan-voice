"""Security tests for pairing: rate limiting, IP lockout, PIN validation."""

import time

import pytest

from voice_dani.pairing import PairingManager


@pytest.fixture
def pm():
    return PairingManager()


class TestPinValidation:
    def test_valid_pin(self, pm):
        pin = pm.create_pin()
        token = pm.redeem(pin, "1.2.3.4")
        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 20

    def test_invalid_pin_rejected(self, pm):
        assert pm.redeem("000000", "1.2.3.4") is None

    def test_double_redeem_rejected(self, pm):
        pin = pm.create_pin()
        pm.redeem(pin, "1.2.3.4")
        assert pm.redeem(pin, "1.2.3.4") is None


class TestSessionToken:
    def test_verify_valid_token(self, pm):
        pin = pm.create_pin()
        token = pm.redeem(pin, "1.2.3.4")
        assert pm.verify(token) is True

    def test_verify_invalid_token(self, pm):
        assert pm.verify("bogus-token") is False


class TestRateLimiting:
    def test_rate_limit_per_ip(self, pm):
        for _ in range(10):
            pm.redeem(pm.create_pin(), "1.2.3.4")
        assert pm.redeem(pm.create_pin(), "1.2.3.4") is None

    def test_rate_limit_different_ips(self, pm):
        for _ in range(10):
            pm.redeem(pm.create_pin(), "1.2.3.4")
        token = pm.redeem(pm.create_pin(), "5.6.7.8")
        assert token is not None


class TestLockout:
    def test_lockout_after_max_invalid_attempts(self, pm):
        for _ in range(5):
            pm.redeem("000000", "1.2.3.4")
        assert pm.redeem(pm.create_pin(), "1.2.3.4") is None

    def test_lockout_per_ip(self, pm):
        for _ in range(5):
            pm.redeem("000000", "1.2.3.4")
        token = pm.redeem(pm.create_pin(), "5.6.7.8")
        assert token is not None


class TestCleanupExpired:
    def test_cleanup_removes_old_tokens(self, pm):
        pin = pm.create_pin()
        token = pm.redeem(pin, "1.2.3.4")
        pm._tokens[token] = time.time() - 999999
        pm.cleanup_expired()
        assert pm.verify(token) is False
