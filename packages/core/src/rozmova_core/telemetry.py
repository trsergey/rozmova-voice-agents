"""OpenTelemetry tracing setup.

Export is enabled only by ``OTEL_EXPORTER_OTLP_ENDPOINT``. Without it nothing is installed:
the global tracer stays a no-op, so the demo does not log connection errors for a collector
that is not there.
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from rozmova_core.settings import Settings


def init_telemetry(
    settings: Settings, exporter: SpanExporter | None = None
) -> TracerProvider | None:
    """Install a global tracer provider exporting over OTLP/HTTP.

    Returns the provider so the caller can ``shutdown()`` it (flushing spans) on exit, or
    ``None`` when export is disabled. ``exporter`` replaces the OTLP exporter in tests.
    """
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        return None

    provider = TracerProvider(
        resource=Resource.create(
            {SERVICE_NAME: settings.service_name, "deployment.environment": settings.env}
        )
    )
    if exporter is None:
        exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider
