"""Die Konfliktmeldung darf keine Ursache behaupten, die sie nicht kennt.

Traegt JEDER Kollaborationskonflikt denselben Satz "Der
Kollaborationsstand ist nicht mehr aktuell" — auch dann, wenn der Stand
nachweislich aktuell ist und der Server gar keine Sequenz nennen kann —,
schickt die Meldung den Nutzer in eine Wiederholung, die strukturell nie
gelingen kann, und der echte Grund ist aus ihr nicht herauszulesen.
"""

from __future__ import annotations

import json

from inqtrix.server.routers.editor_collaboration import _public_error
from inqtrix.project.editor_collaboration_ports import CollaborationConflict


def _payload(response) -> dict:
    return json.loads(bytes(response.body).decode("utf-8"))["error"]


def test_ohne_bekannten_stand_wird_keine_veraltung_behauptet() -> None:
    error = _payload(_public_error(CollaborationConflict("instance_fenced")))

    assert error["reason"] == "instance_fenced"
    assert "no longer current" not in error["message"], (
        "ohne current_sequence kann der Server keine Veraltung belegen"
    )
    # Der Grund muss im Text stehen: sonst kann der Nutzer ihn nicht
    # melden und der Betreiber ihn nicht zuordnen.
    assert "instance_fenced" in error["message"]


def test_mit_bekanntem_stand_bleibt_die_veraltungsmeldung() -> None:
    # Gegenprobe: wo der Server einen konkreten Stand nennt, ist die
    # Aussage wahr und muss erhalten bleiben.
    error = _payload(
        _public_error(CollaborationConflict("snapshot_ahead", current_sequence=7))
    )

    assert error["reason"] == "snapshot_ahead"
    assert "no longer current" in error["message"]
    assert error["current_sequence"] == 7


def _antwort(body: dict, status: int = 409):
    """Eine Sidecar-Antwort, wie sie ueber HTTP wirklich ankommt."""
    import httpx

    return httpx.Response(
        status_code=status,
        json=body,
        request=httpx.Request("POST", "http://sidecar/internal"),
    )


def test_der_ersatzname_gewinnt_gegen_den_platzhalter() -> None:
    """Der Sidecar sagt "keine Ahnung, aber hier ist der echte Grund".

    Er bildet die rund vierzig Konfliktgruende der internen API auf seine
    kleine Vokabelliste ab, weil die den Schliesscode steuert. Was er nicht
    abbilden kann, heisst bei ihm ``upstream_conflict`` -- ein Eingestaendnis,
    kein Grund. Wer hier nur ``reason`` liest, wirft den echten Namen
    endgueltig weg: der Nutzer las "upstream_conflict", waehrend im
    Sidecar-Log "patch_not_found" stand, und niemand konnte die beiden
    verbinden.
    """
    from inqtrix.services.collaboration_client import _response_reason

    grund = _response_reason(_antwort({
        "error": {"reason": "upstream_conflict", "upstream_reason": "patch_not_found"}
    }))

    assert grund == "patch_not_found"


def test_ein_eigener_grund_des_knotens_bleibt_massgeblich() -> None:
    """Gegenprobe: der Ersatzname darf NICHT immer gewinnen.

    Bildet der Sidecar auf einen eigenen Grund ab, traegt dieser eine eigene
    Behandlung -- er steuert den Schliesscode und die Wiederholbarkeit. Ihn
    durch einen feineren, aber unbehandelten Namen zu ersetzen, waere
    derselbe Fehler in die andere Richtung.
    """
    from inqtrix.services.collaboration_client import _response_reason

    grund = _response_reason(_antwort({
        "error": {"reason": "sequence_conflict", "upstream_reason": "command_conflict"}
    }))

    assert grund == "sequence_conflict"


def test_ohne_ersatznamen_bleibt_alles_wie_zuvor() -> None:
    from inqtrix.services.collaboration_client import _response_reason

    assert _response_reason(_antwort({"error": {"reason": "invalid_lease"}})) == "invalid_lease"
    assert _response_reason(_antwort({"reason": "generation_mismatch"})) == "generation_mismatch"
    assert _response_reason(_antwort({"unerwartet": True})) == "node_rejected"
