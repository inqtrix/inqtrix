"""The gateway guides print what was tested.

An operator copies the oauth2-proxy configuration and the resolver examples
out of the docs, so each is compared with the tested file or run as printed.
"""

from __future__ import annotations

import ast
import re
import base64
import hashlib
import hmac
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from tests.test_deployment_stack import _acceptance, _acceptance_proxy
from tests.test_gateway_session import _PEER, _PROOF, World, _guarded

_ROOT = Path(__file__).resolve().parent.parent
_AUTH_MODES = _ROOT / "docs" / "deployment" / "auth-modes.md"
_AUTH_PROVIDER = _ROOT / "docs" / "extending" / "auth-provider.md"
_SIGNING_KEY = "example-signing-key-of-at-least-32-characters"

# The placeholders of the recipes and the values of the tested setup.
_PLACEHOLDERS = {
    "https://<KEYCLOAK-HOST>": "http://keycloak.localhost:8180",
    "<REALM>": "inqtrix-acceptance",
    "<SIGN-IN-CLIENT>": "inqtrix-gateway",
}

# Flags in which a host with a TLS proxy in front differs from the tested
# setup, which has nothing in front of oauth2-proxy and runs on plain http.
_TESTED_ONLY_FLAGS = {
    "--cookie-secure=false",
    "--redirect-url=http://localhost:4180/oauth2/callback",
    "--reverse-proxy=false",
    "--trusted-proxy-ip=127.0.0.1/32",
}
_DOCUMENTED_ONLY_FLAGS = {
    "--redirect-url=https://<HOST>/oauth2/callback",
    "--reverse-proxy=true",
    "--trusted-proxy-ip=<ADDRESS-OF-THE-TLS-PROXY>/32",
}


def _code_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"^```{language}\n(.*?)^```$", text, flags=re.MULTILINE | re.DOTALL)


def _secret_paths_named_alike(node: object) -> object:
    # The docs name secret files by their purpose, the tested setup by its own
    # prefix; both stay under /run/secrets.
    if isinstance(node, dict):
        return {key: _secret_paths_named_alike(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_secret_paths_named_alike(value) for value in node]
    if isinstance(node, str) and node.startswith("/run/secrets/"):
        return "/run/secrets/<file>"
    return node


def test_the_documented_oauth2_proxy_configurations_are_the_tested_files() -> None:
    documented: dict[str, object] = {}
    for block in _code_blocks(_AUTH_MODES.read_text(encoding="utf-8"), "yaml"):
        if "providers:" not in block or "injectRequestHeaders:" not in block:
            continue
        for placeholder, value in _PLACEHOLDERS.items():
            block = block.replace(placeholder, value)
        assert not re.search(r"<[A-Z][A-Z-]*>", block), block
        config = yaml.safe_load(block)
        names = {header["name"] for header in config["injectRequestHeaders"]}
        assertion = "jwt" if "X-Inqtrix-Assertion" in names else "header"
        assert assertion not in documented, f"two documented configurations for {assertion}"
        documented[assertion] = _secret_paths_named_alike(config)

    assert set(documented) == {"jwt", "header"}
    for assertion, config in documented.items():
        assert config == _secret_paths_named_alike(_acceptance_proxy(assertion)), assertion


def test_the_documented_oauth2_proxy_flags_are_the_tested_ones() -> None:
    blocks = [
        block
        for block in _code_blocks(_AUTH_MODES.read_text(encoding="utf-8"), "text")
        if block.startswith("--alpha-config=")
    ]
    tested = [str(flag) for flag in _acceptance()["services"]["oauth2-proxy"]["command"]]

    assert len(blocks) == 2 and len(set(blocks)) == 1, "each recipe prints the same complete flags"
    documented = blocks[0].split()
    assert set(tested) & _TESTED_ONLY_FLAGS == _TESTED_ONLY_FLAGS
    assert set(documented) & _DOCUMENTED_ONLY_FLAGS == _DOCUMENTED_ONLY_FLAGS
    assert [flag for flag in documented if flag not in _DOCUMENTED_ONLY_FLAGS] == [
        flag for flag in tested if flag not in _TESTED_ONLY_FLAGS
    ]


def _documented_module(name: str, folder: Path) -> Path:
    """Cut the example module out of the guide, byte for byte, into *folder*."""
    blocks = [
        block
        for block in _code_blocks(_AUTH_PROVIDER.read_text(encoding="utf-8"), "python")
        if block.startswith(f'"""{name}:')
    ]
    assert len(blocks) == 1, name
    path = folder / f"{name}.py"
    path.write_text(blocks[0], encoding="utf-8")
    return path


def _load(name: str, folder: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(folder))
    monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _signed(claims_text: str, key: str = _SIGNING_KEY) -> str:
    payload = base64.urlsafe_b64encode(claims_text.encode()).decode().rstrip("=")
    return f"{payload}.{hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()}"


@pytest.fixture
def example_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("EXAMPLE_IDENTITY_SIGNING_SECRET", _SIGNING_KEY)
    _documented_module("example_identity", tmp_path)
    return _load("example_identity", tmp_path, monkeypatch)


def _session(module: ModuleType, value: str):
    world = World(
        assertion="resolver",
        resolver=_guarded(module.ExampleIdentityResolver()),
        issuer_key="gateway:sso",
        peer=_PEER,
    )
    response = world.client.get("/api/auth/session", headers={"X-Example-Identity": value, **_PROOF})
    return world, response


def test_the_documented_resolver_signs_a_person_in_through_the_core(example_identity: ModuleType) -> None:
    claims = json.dumps({"uid": "u-1027", "mail": "ann@example.org", "name": "Ann Example", "groups": ["staff"]})

    world, response = _session(example_identity, _signed(claims))

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert [user.display_name for user in world.all_users()] == ["Ann Example"]


@pytest.mark.parametrize(
    ("value", "status", "outcome"),
    [
        (_signed(json.dumps({"uid": "u-1027"}), key="another-key-of-at-least-32-characters!"), 401, "signature_invalid"),
        (_signed("[" * 100_000 + "]" * 100_000), 400, "malformed"),
        (_signed(json.dumps({"uid": "u-1027", "exp": time.time() - 60})), 401, "expired"),
        (_signed(json.dumps({"uid": "u-1027", "groups": "staff"})), 401, "claims_invalid"),
        ("no-dot-in-this-value", 400, "malformed"),
    ],
    ids=["foreign-key", "deep-json", "expired", "groups-not-a-list", "no-signature"],
)
def test_the_documented_resolver_answers_each_bad_value_with_its_class(
    example_identity: ModuleType, value: str, status: int, outcome: str
) -> None:
    world, response = _session(example_identity, value)

    assert response.status_code == status
    assert response.json()["error"]["type"] == outcome
    assert not world.all_users()


def test_the_documented_resolver_stops_the_start_naming_its_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("EXAMPLE_IDENTITY_SIGNING_SECRET", raising=False)
    _documented_module("example_identity", tmp_path)
    module = _load("example_identity", tmp_path, monkeypatch)

    with caplog.at_level(logging.ERROR), pytest.raises(module.ExampleIdentityConfigError):
        module.ExampleIdentityResolver()

    assert "EXAMPLE_IDENTITY_SIGNING_SECRET" in caplog.text


def test_the_documented_library_app_starts_its_server_with_the_resolver_passed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXAMPLE_IDENTITY_SIGNING_SECRET", _SIGNING_KEY)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    _documented_module("example_identity", tmp_path)
    path = _documented_module("library_app", tmp_path)
    module = _load("library_app", tmp_path, monkeypatch)
    calls: dict[str, object] = {}

    def build_gateway_provider(settings: object, *, resolver: object) -> object:
        calls["resolver"] = resolver
        return "provider"

    def create_app(*, settings: object, auth_provider: object) -> object:
        calls["auth_provider"] = auth_provider
        return "app"

    def run(app: object, **options: object) -> None:
        calls["app"] = app
        calls["options"] = options

    monkeypatch.setattr(module, "configure_logging", lambda **_: None)
    monkeypatch.setattr(module, "build_gateway_provider", build_gateway_provider)
    monkeypatch.setattr(module, "create_app", create_app)
    monkeypatch.setattr(module.uvicorn, "run", run)

    module.main()

    assert type(calls["resolver"]).__name__ == "ExampleIdentityResolver"
    assert calls["auth_provider"] == "provider" and calls["app"] == "app"
    # The same server options as `python -m inqtrix`, which long-lived event
    # streams and the collaboration socket depend on; only the address and
    # the worker count are the example's own.
    import inqtrix.__main__ as server_main

    started: dict[str, object] = {}
    monkeypatch.setattr(server_main, "configure_logging", lambda **_: None)
    monkeypatch.setattr(server_main, "log_status_line", lambda *_, **__: None)
    monkeypatch.setattr(server_main, "create_app", lambda **_: "server")
    monkeypatch.setattr(server_main, "freeze_startup_heap", lambda: None)
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **options: started.update(options))
    server_main.main()
    expected = {key: value for key, value in started.items() if key not in {"host", "port", "workers"}}
    assert {key: calls["options"].get(key) for key in expected} == expected
    # Started as a script, the module runs main(); a module that only defines
    # it ends at once and silently.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guard = [node for node in tree.body if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)]
    assert guard and "main()" in ast.unparse(guard[0])
