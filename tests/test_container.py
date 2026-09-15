"""Opt-in acceptance tests for the built image and read-only table replacement."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import tomllib
import uuid
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

from gh_aw_router.contracts import API_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKER_TIMEOUT_SECONDS = 120
BUILD_TIMEOUT_SECONDS = 600
HEALTH_TIMEOUT_SECONDS = 90
ALL_PROFILES = [
    {"goal": goal, "mode": mode}
    for goal in ("cost", "cost-speed")
    for mode in ("economy", "balanced", "robust")
]
pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    if shutil.which("docker") is None:
        pytest.fail("--run-docker requires Docker on PATH and a running Linux daemon")
    _docker(["version"])
    name = f"gh-aw-router-contract:{uuid.uuid4().hex}"
    try:
        _docker(
            [
                "build",
                "--build-arg",
                f"PIP_INDEX_URL={_package_index_url()}",
                "--tag",
                name,
                ".",
            ],
            cwd=PROJECT_ROOT,
            timeout=BUILD_TIMEOUT_SECONDS,
        )
        yield name
    finally:
        _docker(["image", "rm", "--force", name], allow_failure=True)


def _package_index_url() -> str:
    """Use an explicit default project index or uv's public PyPI default."""
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    indexes = project.get("tool", {}).get("uv", {}).get("index", [])
    return next(
        (index["url"] for index in indexes if index.get("default")),
        "https://pypi.org/simple",
    )


@pytest.mark.parametrize(
    ("configuration", "expected"),
    [
        ("", "https://pypi.org/simple"),
        ('[[tool.uv.index]]\nurl = "https://packages.example/simple"\n', "https://pypi.org/simple"),
        (
            '[[tool.uv.index]]\nurl = "https://packages.example/simple"\ndefault = true\n',
            "https://packages.example/simple",
        ),
    ],
)
def test_package_index_url_uses_default_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configuration: str, expected: str
) -> None:
    (tmp_path / "pyproject.toml").write_text(configuration, encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.PROJECT_ROOT", tmp_path)

    assert _package_index_url() == expected


@pytest.fixture
def network() -> Iterator[str]:
    name = f"gh-aw-router-{uuid.uuid4().hex}"
    try:
        _docker(["network", "create", "--internal", name])
        yield name
    finally:
        _docker(["network", "rm", name], allow_failure=True)


@pytest.mark.parametrize("mounted", [False, True], ids=["bundled", "mounted"])
def test_hardened_container(image: str, network: str, mounted: bool) -> None:
    container = f"gh-aw-router-{uuid.uuid4().hex}"
    arguments = [
        "run",
        "--detach",
        "--name",
        container,
        "--network",
        network,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",  # noqa: S108
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--stop-timeout",
        "35",
    ]
    profiles = ALL_PROFILES
    if mounted:
        profiles = [{"goal": "cost", "mode": "economy"}]
        table = PROJECT_ROOT / "routing" / "cost-economy.json"
        arguments.extend(
            [
                "--mount",
                f"type=bind,src={table},dst=/mnt/replacement.json,readonly",
                "--env",
                "GH_AW_ROUTER_ROUTING_TABLES=/mnt/replacement.json",
            ]
        )
    try:
        _docker([*arguments, image])
        _wait_until_healthy(container)
        _assert_hardening(container)
        _probe_http(image, container, network, profiles)
        _docker(["stop", "--time", "35", container])
        state = json.loads(_docker(["inspect", container]).stdout)[0]["State"]
        assert state["ExitCode"] == 0
    finally:
        _docker(["rm", "--force", container], allow_failure=True)


def _wait_until_healthy(container: str) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = _docker(
            ["inspect", "--format", "{{.State.Status}} {{.State.Health.Status}}", container],
            timeout=10,
        )
        if result.stdout.strip() == "running healthy":
            return
        if result.stdout.startswith("exited "):
            break
        time.sleep(0.5)
    logs = _docker(["logs", container], allow_failure=True)
    pytest.fail(f"container did not become healthy\n{logs.stdout}\n{logs.stderr}")


def _assert_hardening(container: str) -> None:
    details = json.loads(_docker(["inspect", container]).stdout)[0]
    assert details["Config"]["User"] == "10001:10001"
    assert details["Config"]["ExposedPorts"] == {"8737/tcp": {}}
    assert details["HostConfig"]["ReadonlyRootfs"] is True
    assert "ALL" in details["HostConfig"]["CapDrop"]
    assert "no-new-privileges" in details["HostConfig"]["SecurityOpt"]
    assert not details["HostConfig"]["PortBindings"]
    assert not any(details["NetworkSettings"]["Ports"].values())


@pytest.mark.parametrize(
    ("ports", "port_bindings", "valid"),
    [
        ({}, None, True),
        ({"8737/tcp": None}, {}, True),
        ({"8737/tcp": []}, {}, True),
        ({"8737/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8737"}]}, {}, False),
        ({"8737/tcp": None}, {"8737/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8737"}]}, False),
        (
            {"8737/tcp": None, "9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9000"}]},
            {},
            False,
        ),
    ],
    ids=["empty", "null", "empty-list", "published", "configured-binding", "other-port"],
)
def test_hardening_rejects_published_ports(
    monkeypatch: pytest.MonkeyPatch,
    ports: dict[str, list[dict[str, str]] | None],
    port_bindings: dict[str, list[dict[str, str]]] | None,
    valid: bool,
) -> None:
    details = {
        "Config": {"User": "10001:10001", "ExposedPorts": {"8737/tcp": {}}},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "PortBindings": port_bindings,
        },
        "NetworkSettings": {"Ports": ports},
    }

    def inspect(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        assert arguments == ["inspect", "test-container"]
        return subprocess.CompletedProcess(arguments, 0, json.dumps([details]), "")

    monkeypatch.setattr(f"{__name__}._docker", inspect)

    if valid:
        _assert_hardening("test-container")
    else:
        with pytest.raises(AssertionError):
            _assert_hardening("test-container")


def _probe_http(
    image: str,
    container: str,
    network: str,
    profiles: list[dict[str, str]],
) -> None:
    script = f"""
import json
import urllib.error
import urllib.request

base = 'http://{container}:8737'

def get(path):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return response.status, response.read()

def post(path, body):
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={{'content-type': 'application/json'}}, method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)

status, _ = get('/healthz')
assert status == 204
status, data = get('/capabilities')
assert status == 200
capabilities = json.loads(data)
assert capabilities['api_versions'] == [{API_VERSION!r}]
assert capabilities['routing_profiles'] == {profiles!r}
model = capabilities['execution_catalogue']['models'][0]
choice = {{'id': 'offered', 'model': model['model']}}
if model['efforts']:
    choice['effort'] = model['efforts'][0]
request = {{
    'api_version': {API_VERSION!r}, 'repository': 'acme/widgets', 'task_id': 'container-test',
    'conversation': [{{'role': 'user', 'parts': [{{'text': 'Fix this function'}}]}}],
    'models': [choice],
}}
status, classified = post('/classify', request)
assert status == 200 and classified['ranked_choices'] == [choice]
for profile in {profiles!r}:
    status, routed = post('/route', dict(request, objective=profile))
    assert status == 200 and routed['ranked_choices'] == [choice], (profile, routed)
request['objective'] = {profiles[0]!r}
status, no_route = post('/route', dict(request, models=[]))
assert status == 422 and no_route['code'] == 'no_route'
del request['task_id']
status, invalid = post('/route', request)
assert status == 422 and invalid['code'] == 'invalid_json'
"""
    probe = f"gh-aw-router-probe-{uuid.uuid4().hex}"
    try:
        _docker(
            [
                "run",
                "--rm",
                "--name",
                probe,
                "--network",
                network,
                "--entrypoint",
                "python",
                image,
                "-c",
                script,
            ]
        )
    finally:
        _docker(["rm", "--force", probe], allow_failure=True)


def _docker(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = DOCKER_TIMEOUT_SECONDS,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # noqa: S603
            ["docker", *arguments],  # noqa: S607
            cwd=cwd,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        if not allow_failure:
            raise RuntimeError(f"Docker {arguments[0]} failed or exceeded {timeout:g}s") from error
        warnings.warn(f"Docker cleanup failed: {error}", RuntimeWarning, stacklevel=2)
        return subprocess.CompletedProcess(arguments, 1, "", str(error))
    if result.returncode != 0 and not allow_failure:
        raise RuntimeError(f"Docker {arguments[0]} failed\n{result.stdout}\n{result.stderr}")
    return result
