"""
orbit_bootstrap.py — Einmaliges Befüllen von ORBIT mit Basis-Policies und Routinen.

Sicher mehrfach ausführbar — erkennt ob bereits durchgeführt.

Ausführen: python3 orbit_bootstrap.py
"""

import os
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

import orbit

BOOTSTRAP_KEY = "bootstrap_v1_done"


def already_done() -> bool:
    return orbit.runtime_get(BOOTSTRAP_KEY) == "1"


def mark_done():
    orbit.runtime_set(BOOTSTRAP_KEY, "1")


POLICIES = [
    {
        "policy_class": "risk_policy",
        "hardness": "hard",
        "scope": ["action", "communication"],
        "reason": (
            "Vor irreversiblen Außenwirkungen (Kalender anlegen/löschen, Mail senden, "
            "externe API schreiben) Tommy informieren und Bestätigung abwarten. "
            "Lesen und interne Verarbeitung sind immer erlaubt."
        ),
    },
    {
        "policy_class": "risk_policy",
        "hardness": "soft",
        "scope": ["action"],
        "reason": (
            "Bei niedriger Confidence (< 0.4) keinen autonomen Aktionismus. "
            "Lieber beobachten, nachfragen oder Task als follow_up anlegen "
            "als mit schwacher Grundlage zu handeln."
        ),
    },
    {
        "policy_class": "communication_policy",
        "hardness": "hard",
        "scope": ["communication"],
        "reason": (
            "Nachts 22:00-08:00 Uhr keine weichen proaktiven Meldungen "
            "(morning_briefing, recommendation, nudge, task_update). "
            "Nur critical_alert darf jederzeit gesendet werden."
        ),
    },
    {
        "policy_class": "communication_policy",
        "hardness": "soft",
        "scope": ["communication"],
        "reason": (
            "Wenn Tommy gerade aktiv im Chat ist (letzter Turn < 5 Minuten), "
            "proaktive Meldungen in die laufende Antwort einbetten statt als "
            "separaten Ping senden. Verhindert Unterbrechungs-Spam."
        ),
    },
    {
        "policy_class": "action_policy",
        "hardness": "hard",
        "scope": ["action"],
        "reason": (
            "Vor jeder Aktion mit Außenwirkung Quality Gate durchlaufen: "
            "Ist das Ziel klar? Ist der Pfad reversibel oder informiert? "
            "Gibt es eine bessere Alternative? Erst wenn alle drei positiv, "
            "wird ausgefuehrt."
        ),
    },
]

ROUTINEN = [
    {
        "routine_class": "check_routine",
        "primary_trigger_type": "scheduled",
        "procedure_body": (
            "07:00-09:00 Uhr: Kalender des heutigen Tages lesen. "
            "Offene heisse Tasks sichten. Anstehende Deadlines erkennen. "
            "Ergebnis fliesst ins morning_briefing. "
            "Steps: tool:calendar_read -> cognition:briefing_prep."
        ),
    },
    {
        "routine_class": "execution_routine",
        "primary_trigger_type": "before_action",
        "procedure_body": (
            "Vor jeder autonomen Ausfuehrung mit Aussenwirkung: "
            "1. Risk-Check (Policy-Scan) "
            "2. Innere Konsultation (Kimi bewertet ob Aktion sinnvoll ist) "
            "3. Erst dann ausfuehren. "
            "Steps: cognition:risk_check -> cognition:inner_consultation -> action:execute."
        ),
    },
    {
        "routine_class": "communication_routine",
        "primary_trigger_type": "before_send",
        "procedure_body": (
            "Vor dem Senden einer proaktiven Meldung: "
            "1. Kanalwahl (WhatsApp oder in Antwort einbetten) "
            "2. Redundanzpruefung (wurde das kuerzlich schon gesendet?) "
            "3. Zeitfenster-Check (Ruhezeit?) "
            "Steps: cognition:channel_select -> cognition:redundancy_check -> cognition:time_check."
        ),
    },
    {
        "routine_class": "review_routine",
        "primary_trigger_type": "after_override",
        "procedure_body": (
            "Nach strong_override oder critical_override: "
            "Warum wurde ORBIT ueberstimmt? War die Policy zu restriktiv? "
            "Soll eine neue Policy vorgeschlagen werden? "
            "Steps: cognition:override_analysis -> cognition:policy_proposal."
        ),
    },
]


def run():
    if already_done():
        print("Bootstrap bereits durchgefuehrt -- ueberspringe.")
        return

    print("Starte ORBIT Bootstrap...")
    print()

    print("-- Policies --")
    for i, p in enumerate(POLICIES, 1):
        try:
            pid = orbit.create_policy(
                policy_class=p["policy_class"],
                primary_origin="bootstrap",
                scope=p.get("scope", []),
                hardness=p.get("hardness", "soft"),
                reason=p["reason"],
            )
            orbit.update_policy(pid, status="active")
            print(f"  [{i}] OK {p['policy_class']} ({p['hardness']})")
        except Exception as e:
            print(f"  [{i}] FEHLER {p['policy_class']}: {e}")

    print()
    print("-- Routinen --")
    for i, r in enumerate(ROUTINEN, 1):
        try:
            rid = orbit.create_routine(
                routine_class=r["routine_class"],
                primary_trigger_type=r["primary_trigger_type"],
                procedure_body=r["procedure_body"],
                primary_origin="bootstrap",
            )
            orbit.update_routine(rid, status="active")
            print(f"  [{i}] OK {r['routine_class']} ({r['primary_trigger_type']})")
        except Exception as e:
            print(f"  [{i}] FEHLER {r['routine_class']}: {e}")

    print()
    mark_done()
    print("Bootstrap abgeschlossen -- 5 Policies, 4 Routinen aktiv.")


if __name__ == "__main__":
    run()
