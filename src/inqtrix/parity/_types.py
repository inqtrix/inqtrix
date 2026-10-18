"""Dataclasses, named constants and shared helpers for parity tooling."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from inqtrix.constants import (
    MAX_TOTAL_SECONDS,
    REASONING_TIMEOUT,
    REQUEST_WAIT_MARGIN_SECONDS,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENDPOINT = "http://127.0.0.1:5100"
DEFAULT_TIMEOUT = MAX_TOTAL_SECONDS + REQUEST_WAIT_MARGIN_SECONDS
TEST_ENDPOINT = "/v1/test/run"
HEALTH_ENDPOINT = "/health"

DEFAULT_QUESTIONS_FILE = PROJECT_ROOT / "tests" / "integration" / "questions.json"
DEFAULT_BASELINES_DIR = PROJECT_ROOT / "tests" / "integration" / "baselines"
DEFAULT_RUNS_DIR = PROJECT_ROOT / "tests" / "integration" / "runs"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "tests" / "integration" / "reports"
DEFAULT_ANALYSIS_TIMEOUT = REASONING_TIMEOUT
CLAIM_REPORT_LIMIT = 5
CLAIM_SNAPSHOT_LIMIT = 3

ALGORITHM_CONTEXT = """
Inqtrix ist ein iterativer Research-Agent mit 5 Hauptschritten:
1. classify: Frage einordnen, Suchmodus festlegen, Aspekte ableiten.
2. plan: Queries fuer die naechste Runde generieren.
3. search: Websuche ausfuehren, Ergebnisse zusammenfassen, Claims extrahieren.
4. evaluate: Evidenzqualitaet, Confidence und Stop-Signale bewerten.
5. answer: finale Antwort mit Quellen synthetisieren.

Bewerte bei Vergleichen nicht nur die Endantwort, sondern auch Query-Strategie,
Confidence-Verlauf, Stop-Logik, Quellen-/Claim-Qualitaet und erkennbare
Algorithmus-Regressionen oder Verbesserungen in den Iterations-Logs.
""".strip()


@dataclass(frozen=True, slots=True)
class QuestionSpec:
    """Canonical parity question specification."""

    id: str
    question: str
    category: str
    expected_keywords: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ParityTolerance:
    """Tolerance bands for semantic parity checks."""

    confidence_delta: int = 1
    exact_round_match: bool = True
    query_delta: int = 3
    citation_fraction: float = 0.10
    min_citation_delta: int = 2
    aspect_coverage_delta: float = 0.15
    source_quality_delta: float = 0.15
    claim_quality_delta: float = 0.15
    evidence_score_delta: int = 2
    count_map_fraction: float = 0.25
    min_count_map_delta: int = 1


@dataclass(frozen=True, slots=True)
class Issue:
    """Single contract or parity issue."""

    level: str
    message: str


@dataclass(frozen=True, slots=True)
class MetricCheck:
    """Single structured metric parity check."""

    key: str
    label: str
    level: str
    status: str
    baseline: Any
    current: Any
    delta: int | float | None = None
    allowed_delta: int | float | None = None
    details: str = ""
