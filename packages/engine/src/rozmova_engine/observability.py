"""The engine's metrics and spans (SDD "Спостережуваність").

Spans go through the global OpenTelemetry tracer: a no-op until ``init_telemetry`` installs
a provider, so without a collector nothing is recorded or exported. Metrics are
Prometheus collectors in the default registry; the gateway serves them on ``/metrics``.

Spans: ``flow.node`` (executor), ``llm.stream`` with ``ttft_ms`` and token counts,
``tool.{name}``, ``rag.search``. The gateway opens the ``call`` and ``turn`` spans above
them.
"""

from opentelemetry import trace
from prometheus_client import Counter, Histogram

tracer = trace.get_tracer("rozmova.engine")

# Seconds; the PRD budget is 1.5 s to the first token of a turn.
LATENCY_BUCKETS = (0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 30.0)

LLM_TTFT = Histogram(
    "rozmova_llm_ttft_seconds",
    "From sending the LLM request to its first stream event.",
    ["provider", "model"],
    buckets=LATENCY_BUCKETS,
)
TOOL_CALLS = Counter(
    "rozmova_tool_calls_total",
    "Tool calls by the LLM; status is ok, an error code from the result, or cancelled.",
    ["tool", "status"],
)
LLM_TOKENS = Counter(
    "rozmova_llm_tokens_total",
    "Tokens of LLM calls; direction is input or output.",
    ["provider", "direction"],
)
