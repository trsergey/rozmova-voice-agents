"""The real gateway server with a scripted slow LLM, run as a process by the shutdown test.

``python slow_llm_gateway.py PORT``; settings come from the environment.
"""

import sys
from pathlib import Path

from gateway.__main__ import serve
from gateway.app import create_app
from gateway.bots import StaticBotLoader
from gateway.calls import Calls
from rozmova_contracts.flow import FlowGraph
from rozmova_core.log import configure_logging
from rozmova_core.redis_client import create_redis
from rozmova_core.settings import Settings
from rozmova_engine.providers import TextDelta
from rozmova_engine.providers.fake import Delay, FakeProvider, Step

FLOW_PATH = Path(__file__).resolve().parents[3] / "flows/hvylia-demo.json"
SLOW_REPLY: list[Step] = [
    TextDelta(text="Зараз подивлюсь"),
    Delay(1.5),
    TextDelta(text=" — 120 грн."),
]


def main(port: int) -> None:
    settings = Settings()
    configure_logging(settings)
    flow = FlowGraph.model_validate_json(FLOW_PATH.read_text(encoding="utf-8"))
    loader = StaticBotLoader(flow, FakeProvider([SLOW_REPLY]), "fake")
    calls = Calls()
    app = create_app(loader, create_redis(settings), settings, calls)
    serve(app, calls, host="127.0.0.1", port=port).run()


if __name__ == "__main__":
    main(int(sys.argv[1]))
