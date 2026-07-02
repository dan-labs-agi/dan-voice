"""PIN-based pairing for Voice Dani with rate limiting and audit logging."""

from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict

from .config import config

log = logging.getLogger(__name__)


class PairingManager:
    """PIN → session token mapping with TTL, rate limiting, and audit logging."""

    def __init__(self):
        self._pins: dict[str, float] = {}  # pin → created_at
        self._tokens: dict[str, float] = {}  # session_token → created_at
        self._attempts: dict[str, list[float]] = defaultdict(list)  # ip → [timestamps]
        self._lockouts: dict[str, float] = {}  # ip → lockout_until

    def create_pin(self) -> str:
        """Create a new PIN with configured TTL."""
        pin = f"{secrets.randbelow(10**config.security.pin_length):0{config.security.pin_length}d}"
        self._pins[pin] = time.time()
        log.info(f"PIN created (expires in {config.security.pin_ttl}s)")
        return pin

    def redeem(self, pin: str, client_ip: str = "unknown") -> str | None:
        """Redeem a PIN for a session token with rate limiting."""
        now = time.time()

        # Check lockout
        if client_ip in self._lockouts:
            if now < self._lockouts[client_ip]:
                log.warning(f"PIN attempt blocked (lockout): {client_ip}")
                return None
            else:
                del self._lockouts[client_ip]
                self._attempts[client_ip] = []

        # Check rate limit
        attempts = self._attempts[client_ip]
        recent = [t for t in attempts if now - t < 60]
        if len(recent) >= config.security.rate_limit_per_minute:
            log.warning(f"Rate limit exceeded: {client_ip}")
            return None

        # Record attempt
        self._attempts[client_ip].append(now)

        # Validate PIN
        created = self._pins.pop(pin, None)
        if created is None:
            log.warning(f"Invalid PIN attempt from {client_ip}")
            # Check for lockout
            if len(recent) + 1 >= config.security.max_pin_attempts:
                self._lockouts[client_ip] = now + config.security.pin_lockout_duration
                log.warning(
                    f"IP locked out for {config.security.pin_lockout_duration}s: {client_ip}"
                )
            return None

        # Check TTL
        if now - created > config.security.pin_ttl:
            log.warning(f"Expired PIN from {client_ip}")
            return None

        # Create session token
        token = secrets.token_urlsafe(32)
        self._tokens[token] = now
        log.info(f"PIN redeemed, session created from {client_ip}")
        return token

    def verify(self, token: str) -> bool:
        """Verify a session token."""
        created = self._tokens.get(token)
        if created is None:
            return False
        if time.time() - created > config.security.session_ttl:
            self._tokens.pop(token, None)
            log.info("Session expired")
            return False
        return True

    def reset_rate_limits(self):
        """Clear rate limit and lockout state (for tests)."""
        self._attempts.clear()
        self._lockouts.clear()

    def cleanup_expired(self):
        """Remove expired PINs and tokens."""
        now = time.time()
        # Cleanup PINs
        expired_pins = [p for p, t in self._pins.items() if now - t > config.security.pin_ttl]
        for p in expired_pins:
            del self._pins[p]
        # Cleanup tokens
        expired_tokens = [
            t for t, created in self._tokens.items()
            if now - created > config.security.session_ttl
        ]
        for t in expired_tokens:
            del self._tokens[t]
        # Cleanup lockouts
        expired_lockouts = [ip for ip, until in self._lockouts.items() if now > until]
        for ip in expired_lockouts:
            del self._lockouts[ip]
            if ip in self._attempts:
                del self._attempts[ip]
