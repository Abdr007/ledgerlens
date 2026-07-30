"""Rate limiting (spec §7: 10 req/min/IP).

Two distinct limits, because one number cannot serve both purposes:

* **Upload** — `settings.rate_limit` (10/minute by default). This is the
  expensive path: it spends model tokens and does real work.
* **Read** — a much higher ceiling. The pipeline visual polls
  `/v1/documents/{id}/status` roughly once a second while a document is in
  flight; capping reads at 10/minute would break the product's core interaction
  while protecting nothing, since reads are cheap.

Client identity comes from `trusted_proxy_count`. Behind a Hugging Face Space the
socket peer is the platform's proxy, so every user would share one bucket; but
blindly trusting `X-Forwarded-For` lets any client forge a fresh identity per
request. Counting back a *known* number of proxies is the only version of this
that is both correct and not spoofable.

**"Known" is the load-bearing word, and it was never checked.** `X-Forwarded-For`
is built left to right — each proxy appends the peer it received from — so with
`hops` proxies in front, the caller is `chain[-hops]`. Set `hops` too low and that
index lands on a *proxy's* address instead of the caller's, and since a platform
answers from a fleet of them, callers get spread across however many edge nodes
happen to serve them. The limit still reports itself in the response headers and
still counts; it just counts the wrong thing, and never binds.

That is what production was doing. `TRUSTED_PROXY_COUNT=1` was set from this
module's own comment rather than from a measurement, the host actually presented
two hops, and **14 uploads in a few seconds all returned 202** where the 11th
should have been 429. The limiter was working perfectly; the number it was given
was wrong, and nothing anywhere would have said so.

So the count is no longer trusted silently. `client_identity` compares the chain
it observes against the chain it was configured for and warns, once, when they
disagree — naming both numbers, because the fix is to set the setting to the
number in the log line. `make verify-hosted` additionally asserts that the limit
*binds* against the deployment, which is the check that would have caught this
without anyone reading a log at all.
"""

from __future__ import annotations

from slowapi import Limiter
from starlette.requests import Request

from app.core.logging import get_logger
from app.core.settings import get_settings

logger = get_logger(__name__)

_settings = get_settings()

upload_rate_limit: str = _settings.rate_limit
read_rate_limit: str = _settings.read_rate_limit
mutation_rate_limit: str = _settings.mutation_rate_limit

#: Emitted at most once per process. A mismatch is a deployment-configuration
#: fault, not a per-request event, so logging it on every request would bury it.
#: A set rather than a bool so the flag can be flipped without `global`.
_depth_warned: set[bool] = set()


def _warn_once_on_depth_mismatch(observed: int, configured: int) -> None:
    if _depth_warned or observed == configured:
        return
    _depth_warned.add(True)
    logger.warning(
        "proxy_depth_mismatch",
        extra={
            "observed_hops": observed,
            "trusted_proxy_count": configured,
            "effect": (
                "rate limiting is keyed on a proxy address, not the caller, and will not "
                "bind as configured"
                if observed > configured
                else "X-Forwarded-For is being trusted further left than there are proxies, "
                "so a caller can forge a fresh identity per request"
            ),
            "fix": f"set TRUSTED_PROXY_COUNT={observed}",
        },
    )


def client_identity(request: Request) -> str:
    """Resolve the caller's IP, honouring exactly `trusted_proxy_count` hops."""
    hops = _settings.trusted_proxy_count
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        _warn_once_on_depth_mismatch(len(chain), hops)
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
