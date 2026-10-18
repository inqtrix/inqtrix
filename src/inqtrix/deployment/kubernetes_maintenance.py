"""Quiesce chart-owned database clients before a schema migration.

The Helm pre-upgrade hook runs this module with a narrowly scoped ServiceAccount.
It scales the release's API, worker, collaboration, and PgBouncer Deployments to zero,
removes the API HPA so it cannot race the scale-down, and waits until every
matching Pod has terminated. Before exiting it revokes its own temporary
RoleBinding. Helm applies the desired Deployments after the migration hook
succeeds; an owner-upgrade render writes a positive API replica count once so a
recreated HPA is reactivated after Kubernetes' implicit maintenance mode.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
_CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


@dataclass(frozen=True)
class KubernetesMaintenanceConfig:
    """Names and timeout bounding one chart-owned maintenance operation."""

    namespace: str
    release: str
    deployment_names: tuple[str, ...]
    hpa_names: tuple[str, ...]
    timeout_seconds: float
    role_binding_name: str


class KubernetesApi:
    """Minimal in-cluster Kubernetes client for the maintenance hook only."""

    def __init__(self, *, host: str, port: str, token: str, ca_path: Path) -> None:
        self._base_url = f"https://{host}:{port}"
        self._token = token
        self._ssl_context = ssl.create_default_context(cafile=str(ca_path))

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        """Call one Kubernetes endpoint and decode its JSON response.

        Args:
            path: Absolute Kubernetes API path.
            method: HTTP method.
            body: Optional JSON merge-patch/request body.
            missing_ok: Return ``None`` for HTTP 404 when a workload is absent.

        Returns:
            The decoded object, or ``None`` for an allowed missing resource.

        Raises:
            RuntimeError: For API failures. The bearer token is never included.
        """
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/merge-patch+json",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=10,
                context=self._ssl_context,
            ) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            raise RuntimeError(
                f"Kubernetes API {method} {path} failed with HTTP {exc.code}"
            ) from None
        except OSError as exc:
            raise RuntimeError(
                f"Kubernetes API {method} {path} failed: {type(exc).__name__}"
            ) from None
        return json.loads(payload) if payload else {}


def quiesce_database_clients(
    api: KubernetesApi,
    config: KubernetesMaintenanceConfig,
) -> None:
    """Scale database clients to zero and wait until their Pods are gone."""
    namespace = urllib.parse.quote(config.namespace, safe="")
    for name in config.hpa_names:
        encoded = urllib.parse.quote(name, safe="")
        api.request(
            f"/apis/autoscaling/v2/namespaces/{namespace}/"
            f"horizontalpodautoscalers/{encoded}",
            method="DELETE",
            missing_ok=True,
        )
    for name in config.deployment_names:
        encoded = urllib.parse.quote(name, safe="")
        api.request(
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{encoded}/scale",
            method="PATCH",
            body={"spec": {"replicas": 0}},
            missing_ok=True,
        )

    selector = (
        f"app.kubernetes.io/instance={config.release},"
        "app.kubernetes.io/component in (api,worker,collaboration,pgbouncer)"
    )
    pods_path = (
        f"/api/v1/namespaces/{namespace}/pods?"
        + urllib.parse.urlencode({"labelSelector": selector})
    )
    deadline = time.monotonic() + config.timeout_seconds
    while True:
        payload = api.request(pods_path)
        items = [] if payload is None else payload.get("items", [])
        if not items:
            return
        if time.monotonic() >= deadline:
            names = sorted(
                str(item.get("metadata", {}).get("name", "unknown"))
                for item in items
            )
            raise RuntimeError(
                "database-client Pods did not terminate before the schema "
                f"migration timeout: {', '.join(names)}"
            )
        time.sleep(2)


def run_schema_maintenance(
    api: KubernetesApi,
    config: KubernetesMaintenanceConfig,
) -> None:
    """Quiesce clients and always revoke the hook's temporary RBAC binding."""
    namespace = urllib.parse.quote(config.namespace, safe="")
    binding = urllib.parse.quote(config.role_binding_name, safe="")
    try:
        quiesce_database_clients(api, config)
    finally:
        api.request(
            f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/"
            f"rolebindings/{binding}",
            method="DELETE",
            missing_ok=True,
        )


def _config_from_environment() -> KubernetesMaintenanceConfig:
    namespace = os.environ.get("INQTRIX_K8S_NAMESPACE", "").strip()
    release = os.environ.get("INQTRIX_K8S_RELEASE", "").strip()
    prefix = os.environ.get("INQTRIX_K8S_WORKLOAD_PREFIX", "").strip()
    if not namespace or not release or not prefix:
        raise RuntimeError(
            "INQTRIX_K8S_NAMESPACE, INQTRIX_K8S_RELEASE, and "
            "INQTRIX_K8S_WORKLOAD_PREFIX are required"
        )
    return KubernetesMaintenanceConfig(
        namespace=namespace,
        release=release,
        deployment_names=tuple(
            f"{prefix}-{component}"
            for component in ("api", "worker", "collaboration", "pgbouncer")
        ),
        hpa_names=(f"{prefix}-api",),
        timeout_seconds=float(
            os.environ.get("INQTRIX_K8S_QUIESCE_TIMEOUT_SECONDS", "300")
        ),
        role_binding_name=os.environ.get(
            "INQTRIX_K8S_MAINTENANCE_ROLE_BINDING", ""
        ).strip()
        or f"{prefix}-schema-maintenance",
    )


def main() -> None:
    """Run the in-cluster schema-maintenance hook."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip()
    if not host or not _TOKEN_PATH.is_file() or not _CA_PATH.is_file():
        raise RuntimeError("Kubernetes in-cluster service-account context is missing")
    api = KubernetesApi(
        host=host,
        port=port,
        token=_TOKEN_PATH.read_text(encoding="utf-8").strip(),
        ca_path=_CA_PATH,
    )
    run_schema_maintenance(api, _config_from_environment())
    print("chart-owned database clients are quiesced for schema migration")


if __name__ == "__main__":
    main()
