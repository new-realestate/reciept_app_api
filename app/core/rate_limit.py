"""Simple in-memory fixed-window rate limiter, keyed by client IP + route.

Good enough for a single-process MVP deployment. Swap for a Redis-backed
implementation (same interface) once running multiple workers/instances.
"""

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int, *, per_path: bool = True):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # per_path=False counts all requests from one client together, so token-guessing
        # across many different URLs can't dodge the limit.
        self.per_path = per_path
        self._hits: dict[str, list[float]] = defaultdict(list)

    async def __call__(self, request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        key = f"{request.url.path}:{client}" if self.per_path else client
        now = time.monotonic()
        window_start = now - self.window_seconds

        hits = [t for t in self._hits[key] if t > window_start]
        if len(hits) >= self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests, please try again later",
            )

        hits.append(now)
        self._hits[key] = hits


auth_rate_limiter = RateLimiter(max_requests=10, window_seconds=60)
receipt_create_rate_limiter = RateLimiter(max_requests=60, window_seconds=60)
# Public invoice/quotation pages: unauthenticated, so limited per client IP across all tokens.
public_doc_rate_limiter = RateLimiter(max_requests=60, window_seconds=60, per_path=False)
# Quotation accept/reject submissions are far rarer than views.
public_response_rate_limiter = RateLimiter(max_requests=10, window_seconds=60, per_path=False)
