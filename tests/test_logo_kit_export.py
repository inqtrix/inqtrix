"""The logo kit's export script writes a manifest of the kit's own files."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from PIL import Image

KIT = Path(__file__).parents[1] / "assets" / "inqtrix-png-logo-kit-v6"


def _load_exporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("logo_kit_export_ui", KIT / "export-ui.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load the logo kit exporter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_lists_kit_files_but_not_hidden_system_files(tmp_path, monkeypatch) -> None:
    exporter = _load_exporter()
    monkeypatch.setattr(exporter, "KIT", tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({"name": "kit", "files": []}))
    Image.new("RGBA", (4, 4)).save(tmp_path / "mark.png")
    (tmp_path / "README.md").write_text("kit\n")
    (tmp_path / "previews").mkdir()
    Image.new("RGB", (2, 2)).save(tmp_path / "previews" / "concept.png")
    # Finder writes these next to any folder it shows; they are never kit files.
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (tmp_path / "previews" / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")

    exporter.update_manifest()

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["name"] == "kit"
    assert [row["path"] for row in manifest["files"]] == [
        "README.md",
        "mark.png",
        "previews/concept.png",
    ]
