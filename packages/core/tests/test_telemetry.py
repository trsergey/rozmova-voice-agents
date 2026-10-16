import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from rozmova_core.settings import Settings
from rozmova_core.telemetry import init_telemetry, shutdown_telemetry


@pytest.fixture(autouse=True)
def _reset_global_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # The OTel API allows setting the global provider once per process; reset it per test.
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)


def test_noop_without_endpoint() -> None:
    provider = init_telemetry(Settings(otel_exporter_otlp_endpoint=None))

    assert provider is None
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
    with trace.get_tracer("test").start_as_current_span("turn") as span:
        assert not span.get_span_context().is_valid


def test_installs_provider_with_endpoint() -> None:
    exporter = InMemorySpanExporter()
    settings = Settings(service_name="gateway", otel_exporter_otlp_endpoint="http://jaeger:4318")

    provider = init_telemetry(settings, exporter=exporter)

    assert provider is not None
    assert trace.get_tracer_provider() is provider
    assert provider.resource.attributes["service.name"] == "gateway"
    with trace.get_tracer("test").start_as_current_span("turn"):
        pass
    provider.shutdown()
    assert [s.name for s in exporter.get_finished_spans()] == ["turn"]


def test_default_exporter_targets_traces_path() -> None:
    provider = init_telemetry(Settings(otel_exporter_otlp_endpoint="http://jaeger:4318/"))

    assert provider is not None
    processor = provider._active_span_processor._span_processors[0]
    assert processor.span_exporter._endpoint == "http://jaeger:4318/v1/traces"  # type: ignore[attr-defined]
    provider.shutdown()


def test_shutdown_flushes_the_installed_provider() -> None:
    exporter = InMemorySpanExporter()
    settings = Settings(service_name="gateway", otel_exporter_otlp_endpoint="http://jaeger:4318")
    init_telemetry(settings, exporter=exporter)
    with trace.get_tracer("test").start_as_current_span("call"):
        pass

    shutdown_telemetry()  # the batch processor exports what it holds

    assert [span.name for span in exporter.get_finished_spans()] == ["call"]


def test_shutdown_without_a_provider_is_a_no_op() -> None:
    shutdown_telemetry()
