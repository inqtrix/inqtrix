"""Stop criteria strategy — evaluate whether the research loop should stop."""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

from inqtrix.i18n import t
from inqtrix.scoring import SUPPORTED_STATUSES
from inqtrix.settings import AgentSettings
from inqtrix.state import emit_progress
from inqtrix.text import is_none_value

log = logging.getLogger("inqtrix")


class StopCriteriaStrategy(ABC):
    """Contract for the multi-hook stop cascade in the evaluate node.

    The evaluate node calls ten ordered hooks per round. Each hook
    either adjusts the running ``conf`` value (capped or unchanged),
    mutates the agent state ``s`` to record a signal, or returns a
    boolean directly. The final :meth:`should_stop` rolls all signals
    into the loop-control decision.

    State-mutation contract:

    - ``s`` is the live agent state dict — mutate sparingly and only
      via documented keys (``s["competing_events"]``,
      ``s["falsification_triggered"]``, etc.). Every mutation should be
      mirrored in :data:`~inqtrix.state.AgentState`.
    - Progress updates flow through :func:`inqtrix.state.emit_progress`;
      never block the queue.

    Implementations may replace the cascade entirely (build their own
    :meth:`should_stop`) or override individual hooks while inheriting
    from :class:`MultiSignalStopCriteria`.
    """

    @abstractmethod
    def check_contradictions(self, s: dict, eval_text: str, conf: int) -> int:
        """Cap confidence when the evaluator reported source contradictions.

        Args:
            s: Agent state dict. May emit progress messages via
                :func:`inqtrix.state.emit_progress` to surface contradiction hits.
                No persistent fields are mutated by the default
                implementation.
            eval_text: Raw evaluator-model output. Implementations
                parse a ``CONTRADICTIONS:`` block from it.
            conf: Current confidence value (1-10) entering this hook.

        Returns:
            Possibly-capped confidence value in ``[0, conf]``. Hard
            contradictions cap to ``confidence_stop - 2``; light
            contradictions cap to ``confidence_stop - 1``.
        """
        ...

    @abstractmethod
    def extract_competing_events(self, s: dict, eval_text: str, conf: int) -> int:
        """Persist competing-event signals and apply any confidence cap.

        Args:
            s: Agent state dict. Mutates
                ``s["competing_events_history"]`` and
                ``s["competing_events_text"]`` for downstream
                inspection.
            eval_text: Raw evaluator-model output. Implementations
                parse a ``COMPETING_EVENTS:`` block.
            conf: Current confidence value entering this hook.

        Returns:
            Possibly-capped confidence value. Repeated identical
            competing-event text is treated more leniently from
            round 3 onward to avoid thrashing.
        """
        ...

    @abstractmethod
    def extract_evidence_scores(self, s: dict, eval_text: str, conf: int) -> int:
        """Parse evidence sufficiency and consistency ratings.

        Args:
            s: Agent state dict. Mutates ``s["evidence_consistency"]``
                and ``s["evidence_sufficiency"]`` (each in ``[0, 10]``).
                When the evaluator output cannot be parsed, the fields
                stay at their current values and a
                ``_evidence_consistency_parsed`` /
                ``_evidence_sufficiency_parsed`` fallback marker is
                appended to the iteration log so the failure is
                visible.
            eval_text: Raw evaluator-model output. Implementations
                parse ``EVIDENCE_CONSISTENCY:`` and
                ``EVIDENCE_SUFFICIENCY:`` blocks.
            conf: Current confidence value. Returned unchanged unless
                a sufficiency / consistency score below threshold
                triggers a cap.

        Returns:
            Possibly-capped confidence value.
        """
        ...

    @abstractmethod
    def check_falsification(self, s: dict, conf: int, prev_conf: int) -> bool:
        """Return whether the loop should switch into falsification mode.

        Args:
            s: Agent state dict. Mutates
                ``s["falsification_active"]`` to ``True`` when the
                hook decides to enter falsification mode (drives
                planner branch in subsequent rounds).
            conf: Current confidence value after upstream hooks.
            prev_conf: Confidence value from the previous round.

        Returns:
            ``True`` when falsification mode was newly triggered in
            this round (one-shot signal — read by downstream hooks
            to suppress conflicting actions); ``False`` otherwise.
        """
        ...

    @abstractmethod
    def check_stagnation(
        self,
        s: dict,
        conf: int,
        prev_conf: int,
        n_citations: int,
        falsification_just_triggered: bool,
    ) -> tuple[int, bool]:
        """Detect low-confidence stagnation after sufficiently broad search.

        Args:
            s: Agent state dict. May emit progress messages and
                update ``s["stagnation_*"]`` bookkeeping fields.
            conf: Current confidence value.
            prev_conf: Confidence value from the previous round.
            n_citations: Number of distinct citations available so
                far. Used as a "search breadth has plateaued" signal.
            falsification_just_triggered: Whether
                :meth:`check_falsification` triggered in the same
                round (skip stagnation if so to avoid double-stop).

        Returns:
            Tuple ``(adjusted_conf, stagnation_detected)``.
        """
        ...

    @abstractmethod
    def compute_utility(
        self, s: dict, conf: int, prev_conf: int, n_citations: int,
    ) -> tuple[float, bool]:
        """Compute per-round utility and whether it should stop the loop.

        Utility is the normalised marginal benefit of the round
        (citation gain / token cost / confidence delta). Falling
        below the configured threshold for two consecutive rounds is
        the standard low-utility stop signal.

        Args:
            s: Agent state dict. May update ``s["utility_history"]``
                and emit progress.
            conf: Current confidence value.
            prev_conf: Confidence value from the previous round.
            n_citations: Number of distinct citations after this
                round.

        Returns:
            Tuple ``(utility_value, should_stop_on_utility)``.
            ``utility_value`` is in ``[0.0, 1.0]``;
            ``should_stop_on_utility`` is ``True`` only when the
            two-round utility window crossed the stop threshold.
        """
        ...

    @abstractmethod
    def check_plateau(
        self, s: dict, conf: int, prev_conf: int, stagnation_detected: bool,
    ) -> bool:
        """Detect stable-confidence plateaus that justify stopping.

        Args:
            s: Agent state dict. May update plateau-bookkeeping
                fields and emit progress.
            conf: Current confidence value.
            prev_conf: Confidence value from the previous round.
            stagnation_detected: Output of :meth:`check_stagnation`.
                Used to coordinate so the loop does not announce
                plateau and stagnation in the same round.

        Returns:
            ``True`` to stop on plateau; ``False`` otherwise. The
            standard plateau condition is "two consecutive rounds
            with confidence change ``< 1`` and confidence already
            within one point of ``confidence_stop``".
        """
        ...

    @abstractmethod
    def should_stop(self, state: dict) -> tuple[bool, str]:
        """Coarse convenience wrapper combining cascade outputs.

        Args:
            state: Agent state dict. Read-only with respect to this
                hook (the per-cascade hooks above are responsible for
                mutations).

        Returns:
            Tuple ``(stop, reason)`` where ``reason`` is a short
            machine-readable tag (``"confidence"``, ``"max_rounds"``,
            ``"plateau"``, ``"utility"``, ``"stagnation"``,
            ``"timeout"``). Used by the evaluate node when it needs a
            single-call go/no-go decision (e.g. for the
            short-circuit stop after the answer node).
        """
        ...


class MultiSignalStopCriteria(StopCriteriaStrategy):
    """Three-phase stop heuristic for the iterative research loop.

    The evaluate node first threads confidence through LLM-parsed
    signals (contradictions, competing events, evidence scores), then
    applies deterministic guardrail caps (falsification, stagnation),
    and finally evaluates broader stop conditions (utility, plateau).

    The default implementation tracks state in the following keys
    (all under ``s``):

    - ``competing_events_history`` / ``competing_events_text``
    - ``falsification_active``
    - ``stagnation_history`` / ``stagnation_active``
    - ``utility_history``
    - ``plateau_history``

    All keys are appended to / overwritten in place; readers must
    therefore be tolerant of missing keys on the first round.
    """

    def __init__(self, settings: AgentSettings) -> None:
        """Bind the active :class:`AgentSettings` to the strategy.

        Args:
            settings: Resolved agent settings. The strategy reads
                ``confidence_stop`` and ``max_rounds``; it does not
                mutate the settings.
        """
        self._settings = settings

    @property
    def _confidence_stop(self) -> int:
        """Return the ``confidence_stop`` of the settings the strategy was built with."""
        return self._settings.confidence_stop

    def _stop_for(self, s: dict) -> int:
        """The RUN's stop threshold: the evaluate node writes the profile-
        adjusted value into ``s["_confidence_stop"]``; the shared strategy
        instance is built once per process with the base settings, so its
        own value is only the fallback."""
        try:
            value = int(s.get("_confidence_stop") or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else self._confidence_stop

    @property
    def _max_rounds(self) -> int:
        """Return the live ``max_rounds`` from settings (proxy)."""
        return self._settings.max_rounds

    # ------------------------------------------------------------------ #
    # check_contradictions
    # ------------------------------------------------------------------ #
    def check_contradictions(self, s: dict, eval_text: str, conf: int) -> int:
        """Cap confidence on contradiction signals from the evaluator.

        Implements :meth:`StopCriteriaStrategy.check_contradictions`.
        Parses ``CONTRADICTIONS:`` from ``eval_text``; only acts when
        the line opens with an affirmative token (``yes`` / ``ja``).
        Severe contradictions (``fundamental``, ``contradict``,
        ``incompatible``, ``grundlegend``, ``widerspricht``, …) cap
        confidence to ``confidence_stop - 2``; lighter contradictions cap
        to ``confidence_stop - 1``.

        Args:
            s: Agent state. Used only for progress emission.
            eval_text: Raw evaluator-model output.
            conf: Current confidence value.

        Returns:
            ``min(conf, cap)`` based on the contradiction severity, or
            ``conf`` unchanged when the line is not affirmative.
        """
        m = re.search(r"CONTRADICTIONS:\s*(.+?)(?:\n|$)", eval_text)
        if not m:
            return conf
        contradiction_text = m.group(1).lower()
        lead = re.split(r"[\s.,:;!()\-]+", contradiction_text.strip(), maxsplit=1)[0]
        if lead not in {"yes", "ja"}:
            return conf
        severe_keywords = (
            "fundamental", "contradict", "incompatible", "irreconcilable",
            "opposite", "complete", "entirely", "false", "incorrect",
            "grundlegend", "gegenteil", "widerspricht", "unvereinbar",
            "komplett", "voellig", "falsch", "inkorrekt", "gegensaetzlich",
        )
        if any(kw in contradiction_text for kw in severe_keywords):
            emit_progress(s, t(s, "contradictions_severe"))
            return min(conf, self._stop_for(s) - 2)
        emit_progress(s, t(s, "contradictions_light"))
        return min(conf, self._stop_for(s) - 1)

    # ------------------------------------------------------------------ #
    # extract_competing_events
    # ------------------------------------------------------------------ #
    def extract_competing_events(self, s: dict, eval_text: str, conf: int) -> int:
        """Record competing explanations and cap confidence for new ambiguity.

        Only in event mode (``s["event_mode"]``: news questions). Outside it
        the evaluator is not asked for the line, and any text it volunteers
        is ignored: a comparison or market question has no "competing
        event", and honouring text volunteered for such questions would cap
        the confidence and force extra rounds.

        Repeated competing-event text is treated more leniently from round 3
        onward so the agent does not thrash forever on the same ambiguity.
        """
        if not s.get("event_mode"):
            s["competing_events"] = ""
            return conf
        m = re.search(r"COMPETING_EVENTS:\s*(.+?)(?:\n|$)", eval_text)
        if not m:
            # No line this round: the ambiguity of the previous round must
            # not survive into the plan block, the answer prompt and the
            # metric as if it had been confirmed again.
            s["competing_events"] = ""
            return conf
        comp_text = m.group(1).strip()
        if is_none_value(comp_text):
            s["competing_events"] = ""
            log.info("TRACE evaluate: competing_events_present=false")
            return conf

        s["competing_events"] = comp_text
        log.info(
            "TRACE evaluate: competing_events_present=true chars=%d",
            len(comp_text),
        )
        emit_progress(s, t(s, "multiple_explanations"))

        _prev_comp = s.get("prev_competing_events", "")
        _comp_is_new = (not _prev_comp) or (comp_text != _prev_comp)
        stop = self._stop_for(s)
        if conf >= stop and (_comp_is_new or s["round"] < 3):
            conf = stop - 1
            log.info(
                "TRACE evaluate: competing_events cap applied (is_new=%s, round=%d)",
                _comp_is_new, s["round"],
            )
        elif conf >= stop:
            log.info(
                "TRACE evaluate: competing_events cap SKIPPED "
                "(same text for 2+ rounds, round=%d)",
                s["round"],
            )
        return conf

    # ------------------------------------------------------------------ #
    # extract_evidence_scores
    # ------------------------------------------------------------------ #
    def extract_evidence_scores(self, s: dict, eval_text: str, conf: int) -> int:
        """Parse evidence consistency / sufficiency scores from evaluator output.

        Implements :meth:`StopCriteriaStrategy.extract_evidence_scores`.
        Looks for ``EVIDENCE_CONSISTENCY: N`` and
        ``EVIDENCE_SUFFICIENCY: N`` (each ``N`` in ``[0, 10]``).
        When either is missing, defaults to ``5`` and sets the
        corresponding ``_*_parsed`` flag to ``False`` so the iteration
        log shows the fallback.

        Sanity check: when both scores are exactly ``0`` AND
        confidence is at or above ``confidence_stop``, caps confidence
        to ``confidence_stop - 1`` (defensive — the model claimed high
        confidence with zero evidence support, which is almost always
        a parse glitch or model halluciation).

        Args:
            s: Agent state. Mutates ``s["evidence_consistency"]``,
                ``s["evidence_sufficiency"]``,
                ``s["_evidence_consistency_parsed"]``,
                ``s["_evidence_sufficiency_parsed"]``.
            eval_text: Raw evaluator-model output.
            conf: Current confidence value.

        Returns:
            Possibly-capped confidence value.
        """
        m_consistency = re.search(r"EVIDENCE_CONSISTENCY:\s*(\d+)", eval_text)
        if m_consistency:
            s["evidence_consistency"] = int(m_consistency.group(1))
            s["_evidence_consistency_parsed"] = True
        else:
            s["evidence_consistency"] = 5
            s["_evidence_consistency_parsed"] = False
            log.warning(
                "TRACE evaluate: EVIDENCE_CONSISTENCY field missing in LLM response -> default 5",
            )

        m_sufficiency = re.search(r"EVIDENCE_SUFFICIENCY:\s*(\d+)", eval_text)
        if m_sufficiency:
            s["evidence_sufficiency"] = int(m_sufficiency.group(1))
            s["_evidence_sufficiency_parsed"] = True
        else:
            s["evidence_sufficiency"] = 5
            s["_evidence_sufficiency_parsed"] = False
            log.warning(
                "TRACE evaluate: EVIDENCE_SUFFICIENCY field missing in LLM response -> default 5",
            )

        if (
            not s.get("_evidence_consistency_parsed", True)
            or not s.get("_evidence_sufficiency_parsed", True)
        ):
            emit_progress(
                s,
                t(s, "evaluate_evidence_scores_missing"),
                severity="warning",
            )

        if (
            s["evidence_consistency"] == 0
            and s["evidence_sufficiency"] == 0
            and conf >= self._stop_for(s)
        ):
            log.warning(
                "TRACE evaluate: evidence sanity check failed "
                "(consistency=0, sufficiency=0, conf=%d -> %d)",
                conf, self._stop_for(s) - 1,
            )
            conf = self._stop_for(s) - 1

        log.info(
            "TRACE evaluate: evidence_consistency=%d evidence_sufficiency=%d",
            s["evidence_consistency"], s["evidence_sufficiency"],
        )
        return conf

    # ------------------------------------------------------------------ #
    # check_falsification
    # ------------------------------------------------------------------ #
    def check_falsification(self, s: dict, conf: int, prev_conf: int) -> bool:
        """Toggle falsification mode based on consistently low confidence.

        Implements :meth:`StopCriteriaStrategy.check_falsification`.
        Two transitions:

        - **Release** (sticky-off): if the previous round had marked
          ``falsification_triggered`` and confidence has recovered to
          at least ``confidence_stop - 2``, clears the flag so the
          plan node stops injecting falsification prompt blocks.
        - **Trigger**: when round ``>= 2`` AND both ``prev_conf`` and
          ``conf`` are ``<= 4``, sets ``falsification_triggered`` and
          returns ``True`` so the plan node switches to
          falsification-style queries in the next round.

        Args:
            s: Agent state. Mutates ``s["falsification_triggered"]``
                in either direction. Emits progress on both transitions.
            conf: Current confidence value.
            prev_conf: Confidence value from the previous round.

        Returns:
            ``True`` only when falsification mode was newly triggered
            in this round (one-shot signal). ``False`` for the
            release case and for steady-state continuation.
        """
        # Reset the sticky falsification mode once confidence recovers
        # meaningfully — otherwise the plan node keeps injecting
        # "debunked / hoax / refuted" prompt blocks long after the
        # evidence picture has changed.
        if (
            s.get("falsification_triggered", False)
            and conf >= self._confidence_stop - 2
        ):
            s["falsification_triggered"] = False
            log.info(
                "TRACE evaluate: falsification mode RELEASED "
                "(conf=%d >= confidence_stop-2=%d, round=%d)",
                conf, self._confidence_stop - 2, s["round"],
            )
            emit_progress(s, t(s, "falsification_released"))

        if (
            s["round"] >= 2
            and prev_conf > 0
            and prev_conf <= 4
            and conf <= 4
            and not s.get("falsification_triggered", False)
        ):
            s["falsification_triggered"] = True
            log.info(
                "TRACE evaluate: falsification triggered (prev=%d, curr=%d, round=%d)",
                prev_conf, conf, s["round"],
            )
            emit_progress(s, t(s, "falsification_started"))
            return True
        return False

    # ------------------------------------------------------------------ #
    # check_stagnation
    # ------------------------------------------------------------------ #
    def check_stagnation(
        self,
        s: dict,
        conf: int,
        prev_conf: int,
        n_citations: int,
        falsification_just_triggered: bool,
    ) -> tuple[int, bool]:
        """Force a stop when broad search repeatedly fails to raise confidence.

        This is the negative-evidence branch described in the architecture:
        after multiple low-confidence rounds and enough citation breadth, the
        loop treats the lack of supporting evidence as informative.
        """
        if (
            s["round"] >= 2
            and prev_conf > 0
            and prev_conf <= 4
            and conf <= 4
            and abs(conf - prev_conf) <= 1
            and not falsification_just_triggered
            and (n_citations >= 30 or s.get("falsification_triggered", False))
        ):
            log.info(
                "TRACE evaluate: stagnation detected "
                "(prev=%d, curr=%d, citations=%d, falsified=%s) -> forcing stop",
                prev_conf, conf, n_citations, s.get("falsification_triggered", False),
            )
            s["done"] = True
            s["_stop_reason"] = "stagnation_low_evidence"
            emit_progress(s, t(s, "stagnation_low_evidence_stop"))
            return conf, True
        return conf, False

    # ------------------------------------------------------------------ #
    # compute_utility
    # ------------------------------------------------------------------ #
    def compute_utility(
        self,
        s: dict,
        conf: int,
        prev_conf: int,
        n_citations: int,
    ) -> tuple[float, bool]:
        """Compute marginal research utility from confidence, citations, and sufficiency.

        Returns:
            Tuple of ``(utility, should_stop)``. The stop signal is emitted
            when two consecutive low-utility rounds occur.
        """
        _delta_conf = (conf - prev_conf) / 10.0 if prev_conf > 0 else 0.5
        _new_cit = n_citations - s.get("prev_citation_count", 0)
        _delta_cit_norm = min(1.0, _new_cit / 10.0)
        _sufficiency_norm = s.get("evidence_sufficiency", 5) / 10.0
        _claim_counts = s.get("claim_status_counts", {}) or {}
        _supported_claims = sum(
            int(_claim_counts.get(status, 0) or 0) for status in SUPPORTED_STATUSES
        )
        _new_supported_claims = _supported_claims - int(
            s.get("prev_supported_claim_count", 0) or 0
        )
        # Evidence gain is measured purely by newly supported consolidated
        # claims, verified or attributed alike (a provider that returns no
        # source text gains evidence like any other) -- there is no separate
        # report-evidence-bundle list.
        _evidence_gain_norm = min(
            1.0,
            max(0, _new_supported_claims) / 3.0,
        )
        utility = round(
            0.3 * _delta_conf
            + 0.2 * _delta_cit_norm
            + 0.2 * _sufficiency_norm
            + 0.3 * _evidence_gain_norm,
            4,
        )
        s["utility_scores"].append(utility)
        s["prev_citation_count"] = n_citations
        s["prev_supported_claim_count"] = _supported_claims

        log.info(
            "TRACE evaluate: utility=%.4f (delta_conf=%.2f delta_cit=%.2f suff=%.2f evidence_gain=%.2f) scores=%s",
            utility, _delta_conf, _delta_cit_norm, _sufficiency_norm, _evidence_gain_norm,
            [round(u, 3) for u in s["utility_scores"]],
        )

        utility_stop = False
        if len(s["utility_scores"]) >= 2 and not s["done"]:
            _last = s["utility_scores"][-1]
            _prev_u = s["utility_scores"][-2]
            if _last < 0.15 and _prev_u < 0.15:
                utility_stop = True
                s["done"] = True
                log.info(
                    "TRACE evaluate: utility stop triggered (scores=%s)",
                    [round(u, 3) for u in s["utility_scores"][-2:]],
                )
                emit_progress(s, t(s, "utility_stop"))
        return utility, utility_stop

    # ------------------------------------------------------------------ #
    # check_plateau
    # ------------------------------------------------------------------ #
    def check_plateau(
        self,
        s: dict,
        conf: int,
        prev_conf: int,
        stagnation_detected: bool,
    ) -> bool:
        """Stop when confidence stays stable at a sufficiently high level.

        Plateau stopping is suppressed while competing events are still moving,
        unless that ambiguity has effectively stabilized across repeated rounds.
        """
        _prev_comp_for_plateau = s.get("prev_competing_events", "")
        _current_competing = s.get("competing_events", "")
        _competing_active_and_changing = (
            bool(_current_competing)
            and _current_competing != _prev_comp_for_plateau
        )

        _conf_stable_rounds = s.get("_conf_stable_rounds", 0)
        if prev_conf > 0 and conf == prev_conf:
            _conf_stable_rounds += 1
        else:
            _conf_stable_rounds = 0
        s["_conf_stable_rounds"] = _conf_stable_rounds

        if _competing_active_and_changing and _conf_stable_rounds >= 2:
            log.info(
                "TRACE evaluate: competing events override \u2014 conf stable for %d rounds, "
                "treating as non-changing for plateau check",
                _conf_stable_rounds,
            )
            _competing_active_and_changing = False

        plateau_stop = False
        if (
            s["round"] >= 2
            and prev_conf > 0
            and conf == prev_conf
            and conf >= 6
            and not stagnation_detected
            and not _competing_active_and_changing
            and not s["done"]
        ):
            if (
                bool(s.get("_evidence_depth_gap_active", False))
                and int(s.get("round", 0) or 0) < self._max_rounds
            ):
                log.info(
                    "TRACE evaluate: plateau stop SUPPRESSED "
                    "(evidence-depth gap active, conf=%d, round=%d/%d)",
                    conf, s["round"], self._max_rounds,
                )
            else:
                plateau_stop = True
                s["done"] = True
                log.info(
                    "TRACE evaluate: plateau stop triggered "
                    "(conf=%d stable for 2 rounds, round=%d)",
                    conf, s["round"],
                )
                emit_progress(s, t(s, "plateau_stop", conf=conf))
        elif _competing_active_and_changing and not s["done"]:
            log.info(
                "TRACE evaluate: plateau stop SUPPRESSED "
                "(competing events changed, round=%d)",
                s["round"],
            )

        s["prev_competing_events"] = _current_competing
        return plateau_stop

    # ------------------------------------------------------------------ #
    # should_stop  (combined convenience method)
    # ------------------------------------------------------------------ #
    def should_stop(self, state: dict) -> tuple[bool, str]:
        """High-level stop check combining all signals.

        Returns ``(should_stop, reason)`` where *reason* is a short tag
        describing which signal triggered the stop (empty when not stopping).

        This method is intentionally a thin wrapper.  The evaluate node
        typically calls the individual ``check_*`` / ``compute_*`` methods
        directly so it can thread confidence values between them; this
        combined entry-point is provided for simpler call-sites that only
        need a boolean answer.
        """
        if state.get("done"):
            return True, "already_done"

        conf = int(state.get("final_confidence", 0))
        if conf >= self._confidence_stop:
            return True, "confidence"

        if int(state.get("round", 0)) >= self._max_rounds:
            return True, "max_rounds"

        return False, ""
