"""Behavioral tests for the schema-maintenance Kubernetes quiesce hook."""

from __future__ import annotations

from typing import Any

import pytest

from inqtrix.deployment.kubernetes_maintenance import (
    KubernetesMaintenanceConfig,
    quiesce_database_clients,
    run_schema_maintenance,
)


class _Api:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, bool]] = []
        self.pod_responses = [
            {"items": [{"metadata": {"name": "rel-inqtrix-api-old"}}]},
            {"items": []},
        ]

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        self.calls.append((path, method, body, missing_ok))
        if "/pods?" in path:
            return self.pod_responses.pop(0)
        return {}


def test_schema_maintenance_removes_hpa_scales_every_client_and_waits(
    monkeypatch,
) -> None:
    api = _Api()
    monkeypatch.setattr(
        "inqtrix.deployment.kubernetes_maintenance.time.sleep", lambda _: None
    )

    quiesce_database_clients(
        api,  # type: ignore[arg-type]
        KubernetesMaintenanceConfig(
            namespace="inqtrix",
            release="rel",
            deployment_names=(
                "rel-inqtrix-api",
                "rel-inqtrix-worker",
                "rel-inqtrix-collaboration",
                "rel-inqtrix-pgbouncer",
            ),
            hpa_names=("rel-inqtrix-api",),
            timeout_seconds=30,
            role_binding_name="rel-inqtrix-schema-maintenance",
        ),
    )

    assert api.calls[0][1:] == ("DELETE", None, True)
    scale_calls = [call for call in api.calls if call[1] == "PATCH"]
    assert [call[0].rsplit("/", 2)[-2] for call in scale_calls] == [
        "rel-inqtrix-api",
        "rel-inqtrix-worker",
        "rel-inqtrix-collaboration",
        "rel-inqtrix-pgbouncer",
    ]
    assert all(call[2] == {"spec": {"replicas": 0}} for call in scale_calls)
    pod_calls = [call for call in api.calls if "/pods?" in call[0]]
    assert len(pod_calls) == 2
    assert (
        "component+in+%28api%2Cworker%2Ccollaboration%2Cpgbouncer%29"
        in pod_calls[0][0]
    )


def test_schema_maintenance_revokes_scale_binding_after_quiesce(
    monkeypatch,
) -> None:
    api = _Api()
    monkeypatch.setattr(
        "inqtrix.deployment.kubernetes_maintenance.time.sleep", lambda _: None
    )
    config = KubernetesMaintenanceConfig(
        namespace="inqtrix",
        release="rel",
        deployment_names=("rel-inqtrix-api",),
        hpa_names=("rel-inqtrix-api",),
        timeout_seconds=30,
        role_binding_name="rel-inqtrix-schema-maintenance",
    )

    run_schema_maintenance(api, config)  # type: ignore[arg-type]

    assert api.calls[-1] == (
        "/apis/rbac.authorization.k8s.io/v1/namespaces/inqtrix/"
        "rolebindings/rel-inqtrix-schema-maintenance",
        "DELETE",
        None,
        True,
    )


def test_schema_maintenance_revokes_scale_binding_when_quiesce_times_out(
    monkeypatch,
) -> None:
    api = _Api()
    api.pod_responses = [
        {"items": [{"metadata": {"name": "rel-inqtrix-api-old"}}]},
    ]
    config = KubernetesMaintenanceConfig(
        namespace="inqtrix",
        release="rel",
        deployment_names=("rel-inqtrix-api",),
        hpa_names=("rel-inqtrix-api",),
        timeout_seconds=-1,
        role_binding_name="rel-inqtrix-schema-maintenance",
    )

    with pytest.raises(RuntimeError, match="did not terminate"):
        run_schema_maintenance(api, config)  # type: ignore[arg-type]

    assert api.calls[-1] == (
        "/apis/rbac.authorization.k8s.io/v1/namespaces/inqtrix/"
        "rolebindings/rel-inqtrix-schema-maintenance",
        "DELETE",
        None,
        True,
    )
