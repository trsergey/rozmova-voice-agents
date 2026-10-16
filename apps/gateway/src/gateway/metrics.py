"""The gateway's Prometheus metrics and its spans (SDD "Спостережуваність").

The engine adds the LLM and tool metrics (``rozmova_engine.observability``); all of them
are in the default registry, which ``/metrics`` serves. The ``call`` span wraps a session
and each turn has a ``turn`` span inside it, so the engine's spans and the call events of
a turn share its trace.
"""

from opentelemetry import trace
from prometheus_client import Counter, Gauge, Histogram

from rozmova_engine.observability import LATENCY_BUCKETS

tracer = trace.get_tracer("rozmova.gateway")

ACTIVE_SESSIONS = Gauge(
    "rozmova_active_sessions", "WebSocket sessions open on this instance.", ["bot_id"]
)
TURN_DURATION = Histogram(
    "rozmova_turn_duration_seconds",
    "A whole turn; outcome is ok, cancelled or the error code the client got.",
    ["bot_id", "outcome"],
    buckets=LATENCY_BUCKETS,
)
TURN_TTFT = Histogram(
    "rozmova_turn_ttft_seconds",
    "End-to-end TTFT (PRD): from accepting the utterance to the first assistant_delta.",
    ["bot_id", "node_type"],
    buckets=LATENCY_BUCKETS,
)
WS_DISCONNECTS = Counter(
    "rozmova_ws_disconnects_total", "Closed WebSocket sessions by why they closed.", ["reason"]
)

# Close codes of a session (session.py) as `reason` labels.
CLOSE_REASONS = {
    None: "client_gone",
    1000: "normal",
    1011: "internal_error",
    1012: "shutdown",
    1013: "backpressure",
    4409: "superseded",
}


def close_reason(code: int | None) -> str:
    return CLOSE_REASONS.get(code, "other")
