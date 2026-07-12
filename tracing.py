"""Optional Cloud Trace integration for the FastAPI application.

Tracing is deliberately opt-in outside Cloud Run.  This keeps local development
and CI independent of Application Default Credentials (ADC).
"""
from __future__ import annotations

import math
import os
from contextlib import contextmanager
from typing import Iterator

from fastapi import FastAPI

import obs


_TRACING_ENABLED = False
_TRACER_NAME = "yui-agent"


def _sample_ratio() -> float:
    """Return a valid trace sample ratio, defaulting invalid input to all traces."""
    try:
        ratio = float(os.environ.get("YUI_TRACE_SAMPLE", "1.0"))
    except ValueError:
        return 1.0
    return ratio if math.isfinite(ratio) and 0.0 <= ratio <= 1.0 else 1.0


def _tracing_requested() -> bool:
    """Enable tracing automatically on Cloud Run or explicitly via YUI_TRACE."""
    return bool(os.environ.get("K_SERVICE")) or os.environ.get("YUI_TRACE") == "1"


def setup_tracing(app: FastAPI) -> bool:
    """Configure Cloud Trace and FastAPI instrumentation when tracing is enabled.

    Configuration failures are fail-open because observability must not prevent
    the application from serving requests.
    """
    global _TRACING_ENABLED

    if not _tracing_requested():
        return False
    if _TRACING_ENABLED or getattr(app.state, "yui_tracing_enabled", False):
        return True

    try:
        # Delay imports until tracing is requested so local and CI test runs do
        # not require either the optional packages or ADC.
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        provider = TracerProvider(
            sampler=TraceIdRatioBased(_sample_ratio()),
            resource=Resource.create(
                {"service.name": os.environ.get("K_SERVICE", _TRACER_NAME)}
            ),
        )
        provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter()))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:
        obs.warning(
            "Cloud Trace setup failed; tracing disabled",
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        return False

    app.state.yui_tracing_enabled = True
    _TRACING_ENABLED = True
    return True


@contextmanager
def span(name: str) -> Iterator[object | None]:
    """Create a child span, or act as a no-op context manager when disabled."""
    if not _TRACING_ENABLED:
        yield None
        return

    from opentelemetry import trace

    with trace.get_tracer(_TRACER_NAME).start_as_current_span(name) as active_span:
        yield active_span
