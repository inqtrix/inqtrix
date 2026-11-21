import type { ResearchRunEvent } from '@/features/researchRuns/types'

/**
 * Events of a stored run of the retired mission engine, as the server keeps
 * them: plan, plan gate, tasks with their results, a failed task, phases,
 * the memo and the end. Identifiers are replaced and long texts shortened;
 * the shapes are the server's. A desk without the mission UI still has to
 * show every one of these lines.
 */
export const RETIRED_MISSION_RUN_EVENTS: ResearchRunEvent[] = [
  {
    "type": "inqtrix.run.started",
    "run_id": "run-retired-mission",
    "sequence": 3,
    "created_at": 1794500003,
    "data": {
      "status": "running",
      "segment_ordinal": 1,
      "reason": "initial"
    }
  },
  {
    "type": "inqtrix.agent.phase.changed",
    "run_id": "run-retired-mission",
    "sequence": 19,
    "created_at": 1794500019,
    "data": {
      "phase": "intake",
      "previous_phase": "intake"
    }
  },
  {
    "type": "inqtrix.agent.phase.changed",
    "run_id": "run-retired-mission",
    "sequence": 21,
    "created_at": 1794500021,
    "data": {
      "phase": "planning",
      "previous_phase": "intake"
    }
  },
  {
    "type": "inqtrix.agent.plan.proposed",
    "run_id": "run-retired-mission",
    "sequence": 22,
    "created_at": 1794500022,
    "data": {
      "plan_id": "plan-retired-v1",
      "version": 1,
      "task_count": 5
    }
  },
  {
    "type": "inqtrix.agent.narration",
    "run_id": "run-retired-mission",
    "sequence": 23,
    "created_at": 1794500023,
    "data": {
      "narration_id": "n-plan-1",
      "kind": "plan",
      "text": "Mein Plan (5 Aufgaben): Öffentliche Ladepunkte zur Jahresmitte 2026 und historische Vergleichswerte recherchieren, fachlich benannte Ausbaulücken ermitteln und die Ergebnisse kurz mit Quellen zusammenfassen.",
      "phase": "planning",
      "final": true
    }
  },
  {
    "type": "inqtrix.agent.approval.requested",
    "run_id": "run-retired-mission",
    "sequence": 24,
    "created_at": 1794500024,
    "data": {
      "approval_id": "apr-retired-plan-0",
      "kind": "plan",
      "subject_type": "plan",
      "subject_id": "plan-retired"
    }
  },
  {
    "type": "inqtrix.run.waiting",
    "run_id": "run-retired-mission",
    "sequence": 26,
    "created_at": 1794500026,
    "data": {
      "status": "waiting_for_approval"
    }
  },
  {
    "type": "inqtrix.run.resumed",
    "run_id": "run-retired-mission",
    "sequence": 29,
    "created_at": 1794500029,
    "data": {
      "status": "running",
      "segment_ordinal": 2,
      "reason": "approval"
    }
  },
  {
    "type": "inqtrix.agent.approval.decided",
    "run_id": "run-retired-mission",
    "sequence": 30,
    "created_at": 1794500030,
    "data": {
      "approval_id": "apr-retired-plan-0",
      "status": "approved"
    }
  },
  {
    "type": "inqtrix.agent.phase.changed",
    "run_id": "run-retired-mission",
    "sequence": 46,
    "created_at": 1794500046,
    "data": {
      "phase": "execution",
      "previous_phase": "planning"
    }
  },
  {
    "type": "inqtrix.agent.task.started",
    "run_id": "run-retired-mission",
    "sequence": 47,
    "created_at": 1794500047,
    "data": {
      "task_id": "t1",
      "ordinal": 0,
      "tool_kind": "web_instant",
      "title": "Ladepunktzahl Mitte 2026 ermitteln",
      "attempt": 1
    }
  },
  {
    "type": "inqtrix.agent.task.started",
    "run_id": "run-retired-mission",
    "sequence": 48,
    "created_at": 1794500048,
    "data": {
      "task_id": "t2",
      "ordinal": 1,
      "tool_kind": "web_instant",
      "title": "Entwicklung seit Anfang 2024 erfassen",
      "attempt": 1
    }
  },
  {
    "type": "inqtrix.agent.activity",
    "run_id": "run-retired-mission",
    "sequence": 49,
    "created_at": 1794500049,
    "data": {
      "kind": "searching",
      "scope": "task",
      "phase": "execution",
      "operation": "web.search.instant",
      "detail": "Wie viele öffentlich zugängliche Ladepunkte für Elektroautos gab es in Deutschland um den 1. Juli 2026 laut Bundesnetzagentur oder einer anderen nachvollziehbaren Fachquelle, und auf welches Datum bezieht sich die Zahl?",
      "status": "started",
      "current": 1,
      "total": 1,
      "task_id": "t1",
      "query": "Wie viele öffentlich zugängliche Ladepunkte für Elektroautos gab es in Deutschland um den 1. Juli 2026 laut Bundesnetzagentur oder einer anderen nachvollziehbaren Fachquelle, und auf welches Datum bezieht sich die Zahl?",
      "purpose": "Eine belastbare Zahl öffentlich zugänglicher Ladepunkte in Deutschland um Mitte 2026 samt Stichtag und Abgrenzung ermitteln."
    }
  },
  {
    "type": "inqtrix.agent.task.finished",
    "run_id": "run-retired-mission",
    "sequence": 56,
    "created_at": 1794500056,
    "data": {
      "task_id": "t1",
      "ordinal": 0,
      "tool_kind": "web_instant",
      "title": "Ladepunktzahl Mitte 2026 ermitteln",
      "status": "completed",
      "child_run_id": null,
      "result_summary": "Laut Bundesnetzagentur gab es **157.762 Normalladepunkte** und **56.572 Schnellladepunkte** in Deutschland, also zusammen **214.334 öffentlich zugängliche Ladepunkte**. Die Bundesnetzagentur bezieht diese Zahl auf den …"
    }
  },
  {
    "type": "inqtrix.agent.narration",
    "run_id": "run-retired-mission",
    "sequence": 57,
    "created_at": 1794500057,
    "data": {
      "narration_id": "n-task-t1",
      "kind": "task",
      "text": "Ladepunktzahl Mitte 2026 ermitteln: Laut Bundesnetzagentur gab es **157.762 Normalladepunkte** und **56.572 Schnellladepunkte** in Deutschland, also zusammen **214.334 öffentlich zugängliche Ladepunkte**. Die Bundesne …",
      "phase": "execution",
      "final": true
    }
  },
  {
    "type": "inqtrix.agent.task.finished",
    "run_id": "run-retired-mission",
    "sequence": 62,
    "created_at": 1794500062,
    "data": {
      "task_id": "t2",
      "ordinal": 1,
      "tool_kind": "web_instant",
      "title": "Entwicklung seit Anfang 2024 erfassen",
      "status": "completed",
      "child_run_id": null,
      "result_summary": "Laut Bundesnetzagentur gab es in Deutschland: - **am 1. Januar 2024:** **123.449** öffentlich zugängliche Ladepunkte. ([energieagentur.rlp.de](https://www.energieagentur.rlp.de/info/die-energieagentur-informiert/aktue …"
    }
  },
  {
    "type": "inqtrix.agent.narration",
    "run_id": "run-retired-mission",
    "sequence": 63,
    "created_at": 1794500063,
    "data": {
      "narration_id": "n-task-t2",
      "kind": "task",
      "text": "Entwicklung seit Anfang 2024 erfassen: Laut Bundesnetzagentur gab es in Deutschland: - **am 1. Januar 2024:** **123.449** öffentlich zugängliche Ladepunkte.",
      "phase": "execution",
      "final": true
    }
  },
  {
    "type": "inqtrix.agent.phase.changed",
    "run_id": "run-retired-mission",
    "sequence": 68,
    "created_at": 1794500068,
    "data": {
      "phase": "evidence",
      "previous_phase": "execution"
    }
  },
  {
    "type": "inqtrix.agent.task.started",
    "run_id": "run-retired-mission",
    "sequence": 70,
    "created_at": 1794500070,
    "data": {
      "task_id": "s",
      "ordinal": 4,
      "tool_kind": "synthesis",
      "title": "Bericht zur Ladeinfrastruktur verfassen",
      "attempt": 1
    }
  },
  {
    "type": "inqtrix.agent.phase.changed",
    "run_id": "run-retired-mission",
    "sequence": 72,
    "created_at": 1794500072,
    "data": {
      "phase": "synthesis",
      "previous_phase": "evidence"
    }
  },
  {
    "type": "inqtrix.agent.narration",
    "run_id": "run-retired-mission",
    "sequence": 73,
    "created_at": 1794500073,
    "data": {
      "narration_id": "n-synthesis",
      "kind": "synthesis",
      "text": "Ich schreibe jetzt das Memo 'Öffentliche Ladepunkte in Deutschland: Entwicklung und Ausbaulücken' mit 2 Abschnitten.",
      "phase": "synthesis",
      "final": true
    }
  },
  {
    "type": "inqtrix.agent.artifact.created",
    "run_id": "run-retired-mission",
    "sequence": 75,
    "created_at": 1794500075,
    "data": {
      "artifact_id": "art-retired-memo",
      "kind": "memo",
      "revision": 1,
      "title": "Öffentliche Ladepunkte in Deutschland: Entwicklung und Ausbaulücken",
      "status": "writing",
      "updated_by": "agent",
      "from_revision": 0,
      "lines_added": 7,
      "lines_removed": 0
    }
  },
  {
    "type": "inqtrix.agent.task.finished",
    "run_id": "run-retired-mission",
    "sequence": 80,
    "created_at": 1794500080,
    "data": {
      "task_id": "s",
      "ordinal": 4,
      "tool_kind": "synthesis",
      "title": "Bericht zur Ladeinfrastruktur verfassen",
      "status": "completed",
      "child_run_id": null,
      "result_summary": "# Öffentliche Ladepunkte in Deutschland: Entwicklung und Ausbaulücken ## Bestand und Entwicklung seit Anfang 2024 Laut gespeicherten Suchzusammenfassungen gab es in Deutschland am **1. Januar 2024 123.449** öffentlich …"
    }
  },
  {
    "type": "inqtrix.agent.narration",
    "run_id": "run-retired-mission",
    "sequence": 81,
    "created_at": 1794500081,
    "data": {
      "narration_id": "n-task-s",
      "kind": "task",
      "text": "Bericht zur Ladeinfrastruktur verfassen: Öffentliche Ladepunkte in Deutschland: Entwicklung und Ausbaulücken ## Bestand und Entwicklung seit Anfang 2024 Laut gespeicherten Suchzusammenfassungen gab es in Deutschland a …",
      "phase": "execution",
      "final": true
    }
  },
  {
    "type": "inqtrix.agent.phase.changed",
    "run_id": "run-retired-mission",
    "sequence": 83,
    "created_at": 1794500083,
    "data": {
      "phase": "critic",
      "previous_phase": "synthesis"
    }
  },
  {
    "type": "inqtrix.agent.phase.changed",
    "run_id": "run-retired-mission",
    "sequence": 87,
    "created_at": 1794500087,
    "data": {
      "phase": "planning",
      "previous_phase": "critic"
    }
  },
  {
    "type": "inqtrix.agent.plan.revised",
    "run_id": "run-retired-mission",
    "sequence": 88,
    "created_at": 1794500088,
    "data": {
      "plan_id": "plan-retired-v2",
      "version": 2,
      "task_count": 7,
      "reason": "critic_research",
      "auto_approved": false
    }
  },
  {
    "type": "inqtrix.agent.narration",
    "run_id": "run-retired-mission",
    "sequence": 89,
    "created_at": 1794500089,
    "data": {
      "narration_id": "n-plan-2",
      "kind": "plan",
      "text": "Mein Plan (7 Aufgaben): Ergänzend werden ein belastbarer Stichtagswert für Mitte 2026 und die Originalaussagen zu Ausbauücken geprüft.",
      "phase": "planning",
      "final": true
    }
  },
  {
    "type": "inqtrix.agent.approval.requested",
    "run_id": "run-retired-mission",
    "sequence": 90,
    "created_at": 1794500090,
    "data": {
      "approval_id": "apr-retired-plan-1",
      "kind": "replan",
      "subject_type": "plan",
      "subject_id": "plan-retired"
    }
  },
  {
    "type": "inqtrix.agent.approval.decided",
    "run_id": "run-retired-mission",
    "sequence": 96,
    "created_at": 1794500096,
    "data": {
      "approval_id": "apr-retired-plan-1",
      "status": "approved"
    }
  },
  {
    "type": "inqtrix.agent.phase.changed",
    "run_id": "run-retired-mission",
    "sequence": 112,
    "created_at": 1794500112,
    "data": {
      "phase": "execution",
      "previous_phase": "planning"
    }
  },
  {
    "type": "inqtrix.agent.task.started",
    "run_id": "run-retired-mission",
    "sequence": 113,
    "created_at": 1794500113,
    "data": {
      "task_id": "t5",
      "ordinal": 4,
      "tool_kind": "web_research",
      "title": "Ladepunktzahlen und Vergleichbarkeit prüfen",
      "attempt": 1
    }
  },
  {
    "type": "inqtrix.agent.task.finished",
    "run_id": "run-retired-mission",
    "sequence": 234,
    "created_at": 1794500234,
    "data": {
      "task_id": "t5",
      "ordinal": 4,
      "tool_kind": "web_research",
      "title": "Ladepunktzahlen und Vergleichbarkeit prüfen",
      "status": "completed",
      "child_run_id": "run-retired-child",
      "result_summary": "## Kurzfazit **Für Mitte 2026 liegt kein geprüfter Originalwert der Bundesnetzagentur vor; auch die Veränderung seit Anfang 2024 ist daher nicht belastbar bestimmbar.** Als späteren Anhaltspunkt schreibt die Suchauswe …"
    }
  },
  {
    "type": "inqtrix.agent.narration",
    "run_id": "run-retired-mission",
    "sequence": 235,
    "created_at": 1794500235,
    "data": {
      "narration_id": "n-task-t5",
      "kind": "task",
      "text": "Ladepunktzahlen und Vergleichbarkeit prüfen: Kurzfazit **Für Mitte 2026 liegt kein geprüfter Originalwert der Bundesnetzagentur vor; auch die Veränderung seit Anfang 2024 ist daher nicht belastbar bestimmbar.** Als sp …",
      "phase": "execution",
      "final": true
    }
  },
  {
    "type": "inqtrix.agent.task.failed",
    "run_id": "run-retired-mission",
    "sequence": 236,
    "created_at": 1794500236,
    "data": {
      "task_id": "t6",
      "ordinal": 5,
      "tool_kind": "file_analysis",
      "title": "Vorgaben zu IT-Sicherheit und Reisekosten auswerten",
      "status": "failed",
      "child_run_id": null,
      "result_summary": "'Qdrant/bm25'",
      "error": "'Qdrant/bm25'",
      "failure": {
        "code": "task_failed",
        "message": "'Qdrant/bm25'"
      }
    }
  },
  {
    "type": "inqtrix.agent.phase.changed",
    "run_id": "run-retired-mission",
    "sequence": 447,
    "created_at": 1794500447,
    "data": {
      "phase": "done",
      "previous_phase": "critic"
    }
  },
  {
    "type": "inqtrix.agent.outcome",
    "run_id": "run-retired-mission",
    "sequence": 448,
    "created_at": 1794500448,
    "data": {
      "outcome_id": "run-retired-mission:outcome",
      "final": "completed",
      "review": "completed",
      "round": 3,
      "revision": "none",
      "accounting": "complete",
      "basis": "provider_context",
      "applied": 0,
      "verified": 0,
      "unverified_reasons": {},
      "reach": {
        "reread": 8,
        "cited": 8,
        "provider_only": 8
      },
      "codes": [],
      "limits": [
        "replan_rounds",
        "plan_task_limit"
      ],
      "open": {
        "by_code": {
          "replan_rounds": 3,
          "plan_task_limit": 3
        },
        "by_kind": {
          "information": 3,
          "text_defect": 0,
          "revised": 0,
          "finding": 0,
          "marked": 0,
          "not_checked": 3
        }
      },
      "lines": [
        {
          "kind": "not_checked",
          "text": "Ob die Originalquellen W1 „Bundesnetzagentur – E-Mobilität“, W2 „Anzahl der öffentlichen Normalladepunkte in Deutschland steigt auf über …“ und W18 „Elektromobilität entwickelt sich in Deutschland positiv“ die wiederg …",
          "reasons": [
            "not_checked"
          ]
        },
        {
          "kind": "not_checked",
          "text": "Ob W6 „ADAC Test: Kein gutes Zeugnis für das Laden an der Autobahn“, W9 „Studie – now-gmbh.de“, W14 „Ladeinfrastruktur nach 2025/2030: Szenarien für den Markthochlauf“ und W27 „Mittelspannungs-Tankstelle hebt Ladeinfr …",
          "reasons": [
            "not_checked"
          ]
        },
        {
          "kind": "not_checked",
          "text": "Ob W22 „Handbuch Meldeportal Ladesäulenregister – Bundesnetzagentur“ die Angaben zu Meldeverzügen und nachträglichen Registerkorrekturen im Original trägt, ließ sich ohne Originalauszug nicht bestätigen.",
          "reasons": [
            "not_checked"
          ]
        },
        {
          "kind": "information",
          "text": "Kriterium offen: Die Zahl öffentlicher Ladepunkte Mitte 2026 ist mit Bezugsdatum und Quelle angegeben. — Ein belastbarer Stichtagswert für Mitte 2026 fehlt; die Suchzusammenfassungen zu W1 „Bundesnetzagentur – E-Mobil …",
          "reasons": [
            "replan_rounds",
            "plan_task_limit"
          ]
        },
        {
          "kind": "information",
          "text": "Kriterium offen: Ein Wert von Anfang 2024 und die Entwicklung bis Mitte 2026 sind nachvollziehbar dargestellt. — Ohne belastbaren Wert für Mitte 2026 lässt sich die Entwicklung seit Anfang 2024 bis zu diesem Zeitpunkt …",
          "reasons": [
            "replan_rounds",
            "plan_task_limit"
          ]
        },
        {
          "kind": "information",
          "text": "Kriterium offen: Alle zentralen Zahlen und Aussagen sind eindeutig belegt; relevante Vergleichsgrenzen sind offengelegt. — Die zentralen Angaben sind mit Quellenmarken versehen und Vergleichsgrenzen offengelegt, aber …",
          "reasons": [
            "replan_rounds",
            "plan_task_limit"
          ]
        }
      ],
      "claims": {
        "art-retired-memo": {
          "total": 6,
          "verified": 0,
          "source_text": 0,
          "attributed": 6,
          "insufficient": 0
        }
      },
      "summary": "Gegen Anbieterauswertungen geprüft, Originalseiten nicht gelesen · 6 Aussagen: 6 vom Anbieter zugeschrieben",
      "details": "3 Punkte nicht geprüft · 3 Kriterien offen · 8 von 8 zitierten Belegen nachgelesen"
    }
  },
  {
    "type": "inqtrix.run.completed",
    "run_id": "run-retired-mission",
    "sequence": 456,
    "created_at": 1794500456,
    "data": {
      "status": "completed"
    }
  }
]
