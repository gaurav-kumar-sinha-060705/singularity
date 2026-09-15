"""Minimal per-IP token-bucket rate limiter (pure ASGI, no dependencies).

Render terminates TLS on a proxy, so the real client IP arrives in
X-Forwarded-For — we honor that first and fall back to scope["client"].
"""

import time

from fastapi.responses import JSONResponse


class RateLimitMiddleware:
    def __init__(self, app, limit_per_min: int = 60, exempt_paths: tuple[str, ...] = ("/health",)):
        self.app = app
        self.capacity = float(limit_per_min)
        self.refill_rate = self.capacity / 60.0
        self.exempt_paths = exempt_paths
        self._buckets: dict[str, list[float]] = {}  # ip -> [tokens, last_refill_ts]

    @staticmethod
    def _client_ip(scope) -> str:
        for key, value in scope.get("headers", []):
            if key == b"x-forwarded-for":
                return value.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if any(path == p or path.startswith(p + "/") for p in self.exempt_paths):
            await self.app(scope, receive, send)
            return

        ip = self._client_ip(scope)
        now = time.monotonic()
        tokens, last = self._buckets.get(ip, [self.capacity, now])
        tokens = min(self.capacity, tokens + (now - last) * self.refill_rate)

        if tokens < 1.0:
            response = JSONResponse(
                {"detail": "Rate limit exceeded. Retry shortly."}, status_code=429
            )
            await response(scope, receive, send)
            return

        self._buckets[ip] = [tokens - 1.0, now]
        if len(self._buckets) > 10_000:  # crude memory bound for demo scale
            self._buckets.clear()
        await self.app(scope, receive, send)
