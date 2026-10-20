"""Eval jobs over a draft and the publish gate, with a scripted LLM, a fake knowledge base,
a migrated PostgreSQL and a fake Redis."""

import copy
import json
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete

from admin_api.app import create_app
from admin_api.evals import EvalRuntime, read_job
from admin_api.passwords import hash_password
from admin_api.sessions import CSRF_HEADER
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import Bot, User
from rozmova_core.settings import Settings
from rozmova_engine.providers import LLMProvider, TextDelta, ToolCall
from rozmova_engine.providers.fake import Delay, FakeProvider
from rozmova_engine.providers.keys import Providers
from rozmova_engine.rag.search import SearchResult

ROOT = Path(__file__).resolve().parents[3]
DEMO_FLOW: dict[str, Any] = json.loads((ROOT / "flows/hvylia-demo.json").read_text("utf-8"))
ADMIN = {"email": "admin@example.com", "password": "admin-password"}
VIEWER = {"email": "viewer@example.com", "password": "viewer-password"}
# One balance case: the run passes iff the model calls get_balance.
CASE = """
- id: balance_basic
  description: Plain balance question.
  tags: [balance]
  turns:
    - user: скільки в мене грошей на рахунку?
      expected_tools:
        - name: get_balance
      reference: Називає суму на рахунку.
"""


class ScriptedProviders(Providers):
    """Every bot gets the same scripted fake, as if it had a key."""

    def __init__(self) -> None:
        super().__init__({})
        self.fake = FakeProvider()

    def has_key(self, name: str, ciphertext: str | None = None) -> bool:
        return True

    def get(self, name: str, ciphertext: str | None = None) -> LLMProvider:
        return self.fake

    def answers_balance(self, *, delay: float = 0) -> None:
        """Script one passing run: a get_balance call, then the reply."""
        self.fake.add_turn(Delay(delay), ToolCall(id="c1", name="get_balance", arguments={}))
        self.fake.add_turn(TextDelta(text="На рахунку 120.50 грн."))

    def answers_without_tools(self) -> None:
        """Script one failing run: a reply without the expected tool call."""
        self.fake.add_turn(TextDelta(text="Не знаю."))


class EmptyKnowledgeBase:
    async def search(self, query: str, *, top_k: int, min_similarity: float) -> SearchResult:
        return SearchResult(query=query, hits=[], best_similarity=None)


@pytest.fixture
def evals_path(tmp_path: Path) -> Path:
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases/balance.yaml").write_text(CASE, encoding="utf-8")
    shutil.copy(ROOT / "evals/thresholds.yaml", tmp_path / "thresholds.yaml")
    return tmp_path


@pytest.fixture
async def settings(database_url: SecretStr, evals_path: Path) -> Settings:
    settings = Settings(
        env="test",
        database_url=database_url,
        demo_seed=False,
        admin_email=ADMIN["email"],
        admin_password=SecretStr(ADMIN["password"]),
        evals_path=evals_path,
    )
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(delete(Bot))
        await db.execute(delete(User))
        viewer_hash = hash_password(VIEWER["password"])
        db.add(User(email=VIEWER["email"], password_hash=viewer_hash, role="viewer"))
    await engine.dispose()
    return settings


class Setup:
    """An admin and a viewer, each on its own app (one event loop per app), sharing the
    database and Redis; the admin's app runs the eval jobs."""

    def __init__(self, settings: Settings) -> None:
        self.server = FakeServer()
        self.llm = ScriptedProviders()
        self.admin = self._client(settings, self.llm)
        self.viewer = self._client(settings, ScriptedProviders())

    def _client(self, settings: Settings, llm: ScriptedProviders) -> TestClient:
        app = create_app(
            settings,
            create_engine(settings),
            FakeAsyncRedis(server=self.server, decode_responses=True),
            eval_runtime=EvalRuntime(llm, EmptyKnowledgeBase()),
        )
        return TestClient(app)

    def login(self, http: TestClient, credentials: dict[str, str]) -> None:
        body = http.post("/auth/login", json=credentials).json()
        http.headers[CSRF_HEADER] = body["csrf_token"]

    def new_bot(self, draft: dict[str, Any] = DEMO_FLOW) -> str:
        bot = self.admin.post(
            "/bots", json={"name": "Support", "provider": "openai", "model": "gpt-4.1-mini"}
        ).json()
        assert self.admin.put(f"/bots/{bot['id']}/draft", json=draft).status_code == 200
        bot_id: str = bot["id"]
        return bot_id

    def start_eval(self, bot_id: str) -> str:
        response = self.admin.post(f"/bots/{bot_id}/evals")
        assert response.status_code == 202, response.text
        job_id: str = response.json()["job_id"]
        return job_id

    def wait(self, job_id: str, timeout: float = 10) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job: dict[str, Any] = self.viewer.get(f"/evals/{job_id}").json()
            if job["state"] != "running":
                return job
            time.sleep(0.05)
        raise AssertionError(f"eval job {job_id} still running")

    def publish(self, bot_id: str) -> Any:
        return self.admin.post(f"/bots/{bot_id}/publish", json={})


@pytest.fixture
def setup(settings: Settings) -> Iterator[Setup]:
    setup = Setup(settings)
    with setup.admin, setup.viewer:
        setup.login(setup.admin, ADMIN)
        setup.login(setup.viewer, VIEWER)
        yield setup


def gate_reason(response: Any) -> str:
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "eval_required"
    reason: str = error["details"]["reason"]
    return reason


@pytest.mark.integration
def test_green_eval_lets_the_draft_go_live(setup: Setup) -> None:
    bot_id = setup.new_bot()
    setup.llm.answers_balance()

    job = setup.wait(setup.start_eval(bot_id))

    assert (job["state"], job["passed"], job["cases_done"], job["cases_total"]) == (
        "done",
        True,
        1,
        1,
    )
    last_eval = setup.viewer.get(f"/bots/{bot_id}").json()["last_eval"]
    assert (last_eval["passed"], last_eval["current"]) == (True, True)
    assert setup.publish(bot_id).status_code == 201
    [version] = setup.viewer.get(f"/bots/{bot_id}/versions").json()
    assert version["eval"]["passed"] is True
    assert version["eval"]["cases"] == 1


@pytest.mark.integration
def test_publish_refuses_without_a_report_with_a_stale_one_and_a_failed_one(
    setup: Setup,
) -> None:
    bot_id = setup.new_bot()
    assert gate_reason(setup.publish(bot_id)) == "missing"

    setup.llm.answers_without_tools()
    failed = setup.wait(setup.start_eval(bot_id))
    assert (failed["state"], failed["passed"]) == ("done", False)
    assert gate_reason(setup.publish(bot_id)) == "failed"

    setup.llm.answers_balance()
    setup.wait(setup.start_eval(bot_id))
    edited = copy.deepcopy(DEMO_FLOW)
    edited["nodes"][0]["data"]["greeting"] = "Привіт!"
    setup.admin.put(f"/bots/{bot_id}/draft", json=edited)
    assert gate_reason(setup.publish(bot_id)) == "stale"

    # Moving nodes on the canvas keeps the report valid.
    setup.admin.put(
        f"/bots/{bot_id}/draft",
        json={**DEMO_FLOW, "nodes": [{**n, "ui": {"x": 9, "y": 9}} for n in DEMO_FLOW["nodes"]]},
    )
    assert setup.publish(bot_id).status_code == 201


@pytest.mark.integration
def test_rollback_copies_the_report_and_skips_the_gate(setup: Setup) -> None:
    bot_id = setup.new_bot()
    setup.llm.answers_balance()
    setup.wait(setup.start_eval(bot_id))
    setup.publish(bot_id)
    edited = copy.deepcopy(DEMO_FLOW)
    edited["nodes"][0]["data"]["greeting"] = "Привіт!"
    setup.admin.put(f"/bots/{bot_id}/draft", json=edited)
    setup.llm.answers_balance()
    setup.wait(setup.start_eval(bot_id))
    setup.publish(bot_id)

    rolled = setup.admin.post(f"/bots/{bot_id}/rollback/1")

    assert rolled.status_code == 201, rolled.text
    versions = setup.viewer.get(f"/bots/{bot_id}/versions").json()
    assert [(v["version"], v["eval"]["passed"]) for v in versions] == [
        (3, True),
        (2, True),
        (1, True),
    ]


@pytest.mark.integration
def test_one_running_job_per_bot(setup: Setup) -> None:
    bot_id = setup.new_bot()
    setup.llm.answers_balance(delay=0.5)

    first = setup.start_eval(bot_id)
    second = setup.start_eval(bot_id)

    assert second == first
    assert setup.wait(first)["state"] == "done"
    setup.llm.answers_balance()
    assert setup.start_eval(bot_id) != first


@pytest.mark.integration
def test_a_broken_run_fails_the_job_and_frees_the_bot(setup: Setup) -> None:
    bot_id = setup.new_bot()
    # Nothing scripted: the first model call fails, the runner records it, the case fails.
    job = setup.wait(setup.start_eval(bot_id))

    assert (job["state"], job["passed"]) == ("done", False)
    setup.llm.answers_balance()
    assert setup.wait(setup.start_eval(bot_id))["passed"] is True


@pytest.mark.integration
async def test_shutdown_cancels_a_running_job(settings: Settings) -> None:
    setup = Setup(settings)
    with setup.admin:
        setup.login(setup.admin, ADMIN)
        bot_id = setup.new_bot()
        setup.llm.answers_balance(delay=30)
        job_id = setup.start_eval(bot_id)
    # The app is shut down: read what the job left in Redis.
    redis = FakeAsyncRedis(server=setup.server, decode_responses=True)

    job = await read_job(redis, uuid.UUID(job_id))

    assert job is not None
    assert (job.state, job.error) == ("failed", "cancelled")
    assert await redis.get(f"eval-job:bot:{bot_id}") is None


@pytest.mark.integration
def test_refusals(setup: Setup) -> None:
    bot_id = setup.new_bot()
    broken = {**DEMO_FLOW, "edges": [e for e in DEMO_FLOW["edges"] if not e.get("when")]}
    broken_id = setup.new_bot(broken)

    as_viewer = setup.viewer.post(f"/bots/{bot_id}/evals")
    invalid = setup.admin.post(f"/bots/{broken_id}/evals")
    no_bot = setup.admin.post("/bots/00000000-0000-0000-0000-000000000000/evals")
    no_job = setup.viewer.get("/evals/00000000-0000-0000-0000-000000000000")

    assert as_viewer.status_code == 403
    assert (invalid.status_code, invalid.json()["error"]["code"]) == (422, "draft_invalid")
    assert no_bot.status_code == 404
    assert (no_job.status_code, no_job.json()["error"]["code"]) == (404, "eval_job_not_found")


@pytest.mark.integration
def test_bypass_publishes_without_an_eval(settings: Settings) -> None:
    setup = Setup(settings.model_copy(update={"eval_gate_bypass": True}))
    with setup.admin:
        setup.login(setup.admin, ADMIN)
        bot_id = setup.new_bot()

        response = setup.publish(bot_id)

    assert response.status_code == 201, response.text
    assert response.json()["version"] == 1
