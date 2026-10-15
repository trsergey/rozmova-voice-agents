"""Deploy one commit's images to a Railway environment, smoke-test it, roll back on failure.

    uv run python infra/deploy.py --environment dev --sha <commit> \
        --gateway-url https://gateway-dev-9b22.up.railway.app \
        --admin-url https://admin-api-dev-0e9c.up.railway.app

Used by the CI jobs `deploy-dev` (push to main) and `release` (tag v*). The Railway CLI
authenticates with `RAILWAY_TOKEN` (a project token scoped to the environment) in CI, or
with `railway login` + `railway link` locally.

1. Remember the images running now: the rollback point.
2. Switch gateway and admin-api to `ghcr.io/trsergey/rozmova-<service>:<sha>` in one
   environment commit. admin-api's pre-deploy command migrates the database first.
3. Wait until both new deployments are SUCCESS. Railway keeps the old deployment serving
   until the new one passes its /healthz check, so a broken image never takes traffic.
4. Smoke test from outside, through Railway's proxy: both /healthz return 200 and the
   gateway WebSocket refuses a call without a ticket with 4401 (TLS, upgrade, routing and
   the app itself are up). A full call needs a ticket from the admin API (ROZ-61).
5. On any failure switch back to the previous images, wait for them, and exit 1.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime

import websockets

SERVICES = ("gateway", "admin-api")
IMAGE = "ghcr.io/trsergey/rozmova-{service}:{sha}"
TERMINAL = {"SUCCESS", "FAILED", "CRASHED", "REMOVED", "SKIPPED"}
DEPLOY_TIMEOUT = 600.0
START_TIMEOUT = 120.0
POLL_SECONDS = 10.0


class DeployError(Exception):
    pass


def log(message: str) -> None:
    print(f"[deploy] {message}", flush=True)


def railway(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 - the railway CLI on PATH, arguments from here
        ["railway", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DeployError(f"railway {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout


def environment_id(name: str) -> str:
    listing = json.loads(railway("environment", "list", "--json"))
    for environment in listing["environments"]:
        if environment["name"] == name:
            return str(environment["id"])
    raise DeployError(f"no Railway environment {name!r}")


def current_images(environment: str) -> dict[str, tuple[str, str]]:
    """Service name → (service id, image it runs now) for the app services."""
    config = json.loads(railway("environment", "config", "--environment", environment, "--json"))
    sources = {
        service_id: (service.get("source") or {}).get("image", "")
        for service_id, service in config.get("services", {}).items()
    }
    found = {}
    for name in SERVICES:
        prefix = IMAGE.format(service=name, sha="")
        matches = [(sid, image) for sid, image in sources.items() if image.startswith(prefix)]
        if len(matches) != 1:
            raise DeployError(f"expected one {name} service in {environment}, found {matches}")
        found[name] = matches[0]
    return found


def switch(environment: str, images: Mapping[str, tuple[str, str]], message: str) -> datetime:
    """Set each service's image in this environment only, as one commit; returns its time.

    An environment patch, not ``railway service source connect``: that changes the image
    in every environment, and a deploy to dev must not touch demo. (``railway environment
    edit --service-config <service> source.image`` reports "No changes to apply" and does
    nothing, as of CLI 5.68.)
    """
    started = datetime.now(UTC)
    patch = {"services": {sid: {"source": {"image": image}} for sid, image in images.values()}}
    railway(
        "api",
        "mutation($env: String!, $patch: EnvironmentConfig!, $msg: String) {"
        " environmentPatchCommit(environmentId: $env, patch: $patch, commitMessage: $msg) }",
        "--raw-var",
        f"env={environment_id(environment)}",
        "--raw-var",
        f"msg={message}",
        "--var",
        f"patch={json.dumps(patch)}",
    )
    return started


def wait_for(environment: str, images: Mapping[str, tuple[str, str]], since: datetime) -> None:
    """Until each service's deployment of its image, created after ``since``, is final.

    Only deployments newer than the switch count: a rollback goes back to an image that
    already has old, finished deployments.
    """
    deadline = time.monotonic() + DEPLOY_TIMEOUT
    start_deadline = time.monotonic() + START_TIMEOUT
    pending = {name: image for name, (_, image) in images.items()}
    while pending:
        statuses = []
        for name, image in list(pending.items()):
            deployments = json.loads(
                railway(
                    "deployment", "list", "--service", name, "--environment", environment, "--json"
                )
            )
            mine = [
                d
                for d in deployments
                if (d.get("meta") or {}).get("image") == image
                and datetime.fromisoformat(d["createdAt"]) >= since
            ]
            status = mine[0]["status"] if mine else "NOT_STARTED"
            statuses.append(f"{name}={status}")
            if status in TERMINAL:
                if status != "SUCCESS":
                    raise DeployError(f"{name}: deployment {status}")
                del pending[name]
            elif status == "NOT_STARTED" and time.monotonic() > start_deadline:
                raise DeployError(f"{name}: Railway started no deployment of {image}")
        log(", ".join(statuses))
        if pending:
            if time.monotonic() > deadline:
                raise DeployError(f"timed out waiting for {', '.join(pending)}")
            time.sleep(POLL_SECONDS)


def healthz(base_url: str) -> None:
    with urllib.request.urlopen(f"{base_url}/healthz", timeout=10) as response:  # noqa: S310
        if response.status != 200:
            raise DeployError(f"{base_url}/healthz: HTTP {response.status}")


async def websocket_refuses_without_ticket(gateway_url: str) -> None:
    ws_url = gateway_url.replace("https://", "wss://", 1)
    url = f"{ws_url}/ws/calls/00000000-0000-0000-0000-000000000000"
    try:
        async with asyncio.timeout(15), websockets.connect(url) as ws:
            await ws.recv()
    except websockets.ConnectionClosed as closed:
        code = closed.rcvd.code if closed.rcvd else None
        if code == 4401:
            return
        raise DeployError(f"WebSocket closed with {code}, expected 4401") from closed
    raise DeployError("WebSocket without a ticket was not refused")


def smoke(gateway_url: str, admin_url: str) -> None:
    for attempt in range(1, 7):
        try:
            healthz(gateway_url)
            healthz(admin_url)
            asyncio.run(websocket_refuses_without_ticket(gateway_url))
        except (OSError, DeployError, TimeoutError) as exc:
            # Right after SUCCESS the proxy can still route to the old instance or not yet
            # to the new one; a few retries cover the switch-over.
            if attempt == 6:
                raise DeployError(f"smoke test failed: {exc}") from exc
            log(f"smoke attempt {attempt}: {exc}; retrying")
            time.sleep(POLL_SECONDS)
        else:
            log("smoke test passed: /healthz 200, WebSocket without a ticket → 4401")
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--environment", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--admin-url", required=True)
    args = parser.parse_args()

    previous = current_images(args.environment)
    target = {
        name: (service_id, IMAGE.format(service=name, sha=args.sha))
        for name, (service_id, _) in previous.items()
    }
    log(
        f"{args.environment}: "
        + ", ".join(
            f"{name} {image.rsplit(':', 1)[1][:7]}" for name, (_, image) in previous.items()
        )
        + f" → {args.sha[:7]}"
    )
    # Railway redeploys only a service whose image changes; the others are left alone.
    changed = [name for name in target if target[name] != previous[name]]
    try:
        if changed:
            since = switch(
                args.environment, {n: target[n] for n in changed}, f"deploy {args.sha[:7]}"
            )
            wait_for(args.environment, {n: target[n] for n in changed}, since)
        else:
            log("already running this commit; smoke test only")
        smoke(args.gateway_url, args.admin_url)
    except DeployError as exc:
        log(f"FAILED: {exc}")
        if changed:
            log(f"rolling back {', '.join(changed)} to the previous images")
            rollback = {n: previous[n] for n in changed}
            since = switch(args.environment, rollback, f"rollback from {args.sha[:7]}")
            wait_for(args.environment, rollback, since)
            log("rolled back")
        return 1
    log("deployed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
