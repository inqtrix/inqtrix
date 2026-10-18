"""Pins the images' OCI metadata against the repository's own truth.

An image label is a derived artefact, and derived artefacts drift the moment
nothing checks them: the version and licence live in ``pyproject.toml`` and
``legal.py``, are typed again in four Dockerfile stages, and nothing but this
test connects the copies.

Deliberately NOT asserted: ``revision`` and ``created``. They are build-time
facts this repository cannot know, and a label carrying an empty value is
worse than an absent one -- a scanner reads it as "declared, and blank".
"""

from __future__ import annotations

import re
from pathlib import Path

from inqtrix import __version__
from inqtrix.legal import LICENSE_ID

_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILES = (
    "deploy/docker/Dockerfile.api",
    "deploy/docker/Dockerfile.web",
    "deploy/docker/Dockerfile.collaboration",
)
_SOURCE_URL = "https://github.com/inqtrix/inqtrix"


def _label_blocks(text: str) -> list[str]:
    """Every ``LABEL`` block, line continuations folded into one string."""
    folded = text.replace("\\\n", " ")
    return [
        line for line in folded.splitlines() if line.strip().startswith("LABEL ")
    ]


def _shippable_stage_count(text: str) -> int:
    """Stages a `--target` can select, plus the unnamed final one.

    Counting `CMD` would undercount: the nginx stage ships without one because
    it inherits its base image's command.
    """
    named = len(re.findall(r"^FROM .+ AS (\S+)", text, flags=re.MULTILINE))
    unnamed_final = len(re.findall(r"^FROM (?!.* AS ).*$", text, flags=re.MULTILINE))
    return named + unnamed_final


def test_every_shipped_stage_carries_oci_metadata() -> None:
    for name in _DOCKERFILES:
        text = (_ROOT / name).read_text()
        blocks = _label_blocks(text)
        assert blocks, f"{name} ships without OCI metadata"
        for block in blocks:
            for key in (
                "org.opencontainers.image.title",
                "org.opencontainers.image.description",
                "org.opencontainers.image.version",
                "org.opencontainers.image.licenses",
                "org.opencontainers.image.source",
            ):
                assert key in block, f"{name}: {key} missing from a LABEL block"


def test_image_version_and_licence_match_the_repository() -> None:
    # `pyproject` declares the version dynamic and hatchling reads it from
    # `inqtrix.__version__`, so THAT is the single source of truth -- reading
    # the manifest instead would pin a copy against a copy.
    version = __version__
    for name in _DOCKERFILES:
        text = (_ROOT / name).read_text()
        for block in _label_blocks(text):
            assert f'org.opencontainers.image.version="{version}"' in block, (
                f"{name} labels a version other than pyproject's {version}"
            )
            assert f'org.opencontainers.image.licenses="{LICENSE_ID}"' in block, (
                f"{name} labels a licence other than {LICENSE_ID}"
            )
            assert f'org.opencontainers.image.source="{_SOURCE_URL}"' in block


#: Keys a stage may deliberately blank, and only to erase a WRONG value it
#: inherited from its base image. In the nginx target the base image's
#: `revision` and `created` describe nginx, so
#: leaving them makes a scanner attribute someone else's commit to this
#: project. Blank is ambiguous; inherited-and-wrong is false.
_CLEARABLE = ("org.opencontainers.image.revision", "org.opencontainers.image.created")


def test_no_label_is_declared_blank_except_to_erase_an_inherited_one() -> None:
    for name in _DOCKERFILES:
        for block in _label_blocks((_ROOT / name).read_text()):
            assert 'org.opencontainers.image' in block
            for pair in re.findall(r'(org\.opencontainers\.image\.\S+?)=""', block):
                assert pair in _CLEARABLE, (
                    f"{name}: {pair} is declared blank; a scanner reads that as "
                    "'declared, and empty'. Give it a value or leave it out."
                )


def test_the_nginx_stage_erases_the_metadata_it_inherits() -> None:
    # Found by inspecting a built image rather than the Dockerfile text: the
    # nginx base ships its own revision/created/url, and without an override
    # they travel as if they described Inqtrix.
    text = (_ROOT / "deploy/docker/Dockerfile.web").read_text()
    nginx = [b for b in _label_blocks(text) if "nginx" in b]
    assert len(nginx) == 1
    assert 'org.opencontainers.image.url="https://github.com/inqtrix/inqtrix"' in nginx[0]
    for key in _CLEARABLE:
        assert f'{key}=""' in nginx[0], f"{key} would keep the nginx base value"


def test_the_web_image_labels_both_of_its_shippable_stages() -> None:
    # The web Dockerfile ships an nginx variant AND a Python gateway. Labelling
    # only the default target leaves a gap a scanner finds and a person does not.
    text = (_ROOT / "deploy/docker/Dockerfile.web").read_text()
    blocks = _label_blocks(text)
    assert len(blocks) == 2, "both web runtime stages must carry metadata"
    assert any("nginx" in block for block in blocks)


def test_build_only_stages_are_not_labelled() -> None:
    # Metadata belongs on what ships. A label on a build stage is dead weight
    # that never reaches a registry, and it would make the count above lie.
    for name in _DOCKERFILES:
        text = (_ROOT / name).read_text()
        assert len(_label_blocks(text)) < _shippable_stage_count(text)
