"""
OpenTelemetry instrumentation setup (Step 13).

Configures a TracerProvider once at startup. app/routers/chat.py uses the
resulting tracer to create the request-lifecycle spans this step asks for:
request_receipt (root), auth, routing, rate_limit_check,
provider_selection, provider_call, response_processing, and
response_delivery.

Exporter: ConsoleSpanExporter. There's no collector or Grafana stack
running yet (that's later - "no Grafana dashboard JSON yet" in this
step's own scope), so spans print as structured output to stdout, which is
enough to see real traces locally without standing up Jaeger/Tempo/an
OTel Collector. Swapping the exporter for an OTLP one pointed at a
collector is a small, isolated change here once that infrastructure
exists - nothing in app/routers/chat.py would need to change.

Explicitly NOT in scope here: metrics (this is tracing only), Grafana
dashboards, alerting on trace data.
"""
from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_SERVICE_NAME = "llm-gateway"

_provider: TracerProvider | None = None


def configure_tracing() -> TracerProvider:
    global _provider
    _provider = TracerProvider(resource=Resource.create({"service.name": _SERVICE_NAME}))
    _provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(_provider)
    return _provider


def shutdown_tracing() -> None:
    if _provider is not None:
        _provider.shutdown()


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(_SERVICE_NAME)
