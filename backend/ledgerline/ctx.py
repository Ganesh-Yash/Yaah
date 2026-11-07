"""Request-scoped context (request id, client ip, user agent).

The audit log and error envelope both need this, but threading it through
every service signature would be noisy, so it lives in a ``ContextVar`` set by
middleware.
"""

from __future__ import annotations

import itertools
import uuid
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    ip: str | None = None
    user_agent: str | None = None


_current: ContextVar[RequestContext | None] = ContextVar("ledgerline_request_context", default=None)

_counter = itertools.count(1)


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:20]}_{next(_counter)}"


def set_context(ctx: RequestContext) -> None:
    _current.set(ctx)


def get_context() -> RequestContext:
    return _current.get() or RequestContext(request_id="req_unknown")


def clear_context() -> None:
    _current.set(None)
