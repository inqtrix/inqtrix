"""Regression tests for gateway static responsibility."""

import gzip
from pathlib import Path

from fastapi.testclient import TestClient
from inqtrix_web_gateway import app as gateway_app

def test_static_mount_serves_index_html(fake_dist: Path) -> None:
    """The root path delivers ``index.html`` from the mounted dist/."""
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "fake-react-app" in response.text
    assert response.headers["content-type"].startswith("text/html")

def test_static_mount_serves_assets(fake_dist: Path) -> None:
    """Hashed asset paths are served directly without proxy roundtrip."""
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get("/assets/main.js")
    assert response.status_code == 200
    assert "fake bundle" in response.text

def test_unknown_path_falls_back_to_index_html(fake_dist: Path) -> None:
    """Deep links serve ``index.html`` (nginx ``try_files`` parity).

    Plain ``StaticFiles(html=True)`` answers 404 here; the SPA must load
    on any client-side route, so the gateway has to mirror the nginx
    fallback.
    """
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get("/settings/deep/link")
    assert response.status_code == 200
    assert "fake-react-app" in response.text
    assert response.headers["cache-control"] == "no-cache"

def test_missing_asset_stays_hard_404(fake_dist: Path) -> None:
    """A missing hashed bundle surfaces as 404, never as ``index.html``.

    Falling back under ``/assets/`` would hand HTML to the browser's
    module loader and mask a stale-``index.html`` deployment problem.
    """
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get("/assets/gone-abc123.js")
    assert response.status_code == 404
    assert "fake-react-app" not in response.text

def test_cache_policy_splits_index_and_assets(fake_dist: Path) -> None:
    """``index.html`` revalidates per load, hashed assets cache forever."""
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        index = client.get("/")
        asset = client.get("/assets/main.js")
    assert index.headers["cache-control"] == "no-cache"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"

def test_guest_spa_route_is_never_cached_or_referred(fake_dist: Path) -> None:
    """A bearer-token route must not enter caches or downstream referrers."""
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get("/s/sentinel-guest-token")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"

def test_spa_fallback_survives_present_404_html(fake_dist: Path) -> None:
    """A ``404.html`` in dist/ must not hijack the SPA fallback.

    With ``html=True`` Starlette RETURNS the 404 page for unknown paths
    instead of raising, which would silently bypass the ``index.html``
    fallback; the gateway therefore mounts with ``html=False``.
    """
    (fake_dist / "404.html").write_text("custom not-found page", encoding="utf-8")
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get("/settings/deep/link")
    assert response.status_code == 200
    assert "fake-react-app" in response.text

def test_mjs_assets_served_as_javascript(fake_dist: Path) -> None:
    """ES-module assets get a JavaScript MIME type on every platform.

    Browsers hard-reject module scripts served as octet-stream; the
    gateway registers the ``.mjs`` mapping itself instead of trusting
    the host's mimetypes database (nginx pins a default_type for the
    same reason).
    """
    (fake_dist / "assets" / "worker.mjs").write_text(
        "export {};", encoding="utf-8"
    )
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get("/assets/worker.mjs")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]

def test_precompressed_asset_served_when_accepted(fake_dist: Path) -> None:
    """A build-time ``.br`` sibling is served to clients that accept it.

    Compression happens at build time, never per request: the runtime
    cost of squeezing a 4.9 MB bundle on every cache miss would eat the
    latency the smaller payload is meant to save.
    """
    (fake_dist / "assets" / "main.js.br").write_bytes(b"brotli-payload")
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get(
            "/assets/main.js", headers={"accept-encoding": "br, gzip"}
        )
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "br"
    # The identity MIME type must survive: browsers dispatch on
    # content-type, and a module served as octet-stream is rejected.
    assert "javascript" in response.headers["content-type"]
    assert response.headers["vary"] == "accept-encoding"

def test_gzip_used_when_brotli_not_accepted(fake_dist: Path) -> None:
    """Clients without brotli still get the gzip sibling.

    Real gzip bytes on purpose: the client decodes the body, so this
    also proves the served sibling actually round-trips.
    """
    original = "console.log('fake bundle');"
    (fake_dist / "assets" / "main.js.br").write_bytes(b"brotli-payload")
    (fake_dist / "assets" / "main.js.gz").write_bytes(
        gzip.compress(original.encode("utf-8"))
    )
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get(
            "/assets/main.js", headers={"accept-encoding": "gzip"}
        )
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.text == original

def test_identity_served_when_no_encoding_accepted(fake_dist: Path) -> None:
    """Without a matching accept-encoding the raw file is returned."""
    (fake_dist / "assets" / "main.js.br").write_bytes(b"brotli-payload")
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get(
            "/assets/main.js", headers={"accept-encoding": "identity"}
        )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert "fake bundle" in response.text

def test_missing_sibling_falls_back_to_identity(fake_dist: Path) -> None:
    """An asset without precompressed siblings is served unchanged."""
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get(
            "/assets/main.js", headers={"accept-encoding": "br, gzip"}
        )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert "fake bundle" in response.text

def test_index_html_is_never_precompressed(fake_dist: Path) -> None:
    """Only hashed ``/assets/`` files may be served precompressed.

    ``index.html`` carries ``no-cache`` and is the document that future
    CSRF-bearing surfaces render into. Keeping compression off anything
    outside the immutable asset prefix is what makes the BREACH class
    irrelevant here: those files hold no secret and reflect no
    user-controlled input.
    """
    (fake_dist / "index.html.br").write_bytes(b"brotli-payload")
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get("/", headers={"accept-encoding": "br, gzip"})
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert "fake-react-app" in response.text

def test_precompressed_asset_keeps_immutable_cache_control(
    fake_dist: Path,
) -> None:
    """Encoding negotiation must not weaken the asset cache policy."""
    (fake_dist / "assets" / "main.js.br").write_bytes(b"brotli-payload")
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get(
            "/assets/main.js", headers={"accept-encoding": "br"}
        )
    assert response.headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )

def test_spa_fallback_is_never_precompressed(fake_dist: Path) -> None:
    """Paths that fall back to the SPA never carry an encoding.

    The negotiation appends ``.br``/``.gz`` to a path; it must key off
    the asset the loader actually resolved, not off whatever the client
    asked for. A fallback response is ``index.html`` under a foreign
    name, so it must stay identity even if a same-named sibling exists.
    """
    (fake_dist / "assets").parent.joinpath("deep").mkdir()
    (fake_dist / "deep" / "link.br").write_bytes(b"brotli-payload")
    app = gateway_app.build_app(fake_dist, "http://backend.invalid")
    with TestClient(app) as client:
        response = client.get("/deep/link", headers={"accept-encoding": "br"})
    assert response.status_code == 200
    assert "fake-react-app" in response.text
    assert "content-encoding" not in response.headers
