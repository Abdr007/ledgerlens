"""Rate limiting (spec §7: 10 req/min/IP).

Two distinct limits, because one number cannot serve both purposes:

* **Upload** — `settings.rate_limit` (10/minute by default). This is the
  expensive path: it spends model tokens and does real work.
* **Read** — a much higher ceiling. The pipeline visual polls
  `/v1/documents/{id}/status` roughly once a second while a document is in
  flight; capping reads at 10/minute would break the product's core interaction
  while protecting nothing, since reads are cheap.

Client identity comes from `trusted_proxy_count`. Behind Cloud Run or a Hugging
Face Space the socket peer is the platform's proxy, so every user would share one
bucket; but blindly trusting `X-Forwarded-For` lets any client forge a fresh
identity per request. Counting back a *known* number of proxies is the only
version of this that is both correct and not spoofable.
"""

from __future__ import annotations

from slowapi import Limiter
from starlette.requests import Request

from app.core.settings import get_settings

_settings = get_settings()

upload_rate_limit: str = _settings.rate_limit
read_rate_limit: str = _settings.read_rate_limit
mutation_rate_limit: str = _settings.mutation_rate_limit


def client_identity(request: Request) -> str:
    """Resolve the caller's IP, honouring exactly `trusted_proxy_count` hops."""
    hops = _settings.trusted_proxy_count
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if len(chain) >= hops:
            # The right-most entries were appended by infrastructure we control;
            # step back exactly that many to reach the first untrusted value.
            return chain[-hops]
    client = request.client
    return client.host if client else "unknown"


limiter = Limiter(
    key_func=client_identity,
    default_limits=[read_rate_limit],
    headers_enabled=True,
)
