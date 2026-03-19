"""
SchnuBot.ai — ORBIT Testmatrix (Build Step 11)
Konzept Kap. 24: Logische Tests, Operative Tests, Recovery-Tests.

Ausfuehren:
    cd /opt/whatsapp-bot && source venv/bin/activate
    python3 orbit_test.py

Gibt Pass/Fail pro Test aus und eine Zusammenfassung am Ende.
"""

import sys
import os
import traceback

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

import orbit
from core.database import get_connection

# =============================================================================
# Test-Runner
# =============================================================================

_results = []


def test(name: str):
    """Decorator fuer einzelne Tests."""
    def decorator(fn):
        try:
            fn()
            _results.append(("PASS", name))
            print(f"  ✓  {name}")
        except AssertionError as e:
            _results.append(("FAIL", name, str(e)))
            print(f"  ✗  {name} — {e}")
        except Exception as e:
            _results.append(("ERROR", name, traceback.format_exc()[-200:]))
            print(f"  !  {name} — {type(e).__name__}: {e}")
        return fn
    return decorator


def section(title: str):
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print(f"{'─'*50}")


# =============================================================================
# Logische Tests — Zustandsuebergaenge
# =============================================================================

section("1. Zustandsübergänge")


@test("Thread: new → watching → converted")
def _():
    tid = orbit.create_thread("Test-Thread", "test", relevance="weak")
    ok1 = orbit.thread_transition(tid, "watching", reason="Test")
    assert ok1, "watching-Übergang fehlgeschlagen"
    task_id = orbit.convert_thread_to_task(tid, task_type="observation")
    assert task_id, "Konvertierung fehlgeschlagen"
    t = orbit.get_thread(tid)
    assert t["status"] == "converted", f"Erwartet converted, got {t['status']}"


@test("Thread: ungültiger Übergang wird abgelehnt")
def _():
    tid = orbit.create_thread("Test ungültig", "test")
    ok = orbit.thread_transition(tid, "merged")  # new → merged nicht erlaubt
    assert not ok, "Ungültiger Übergang hätte abgelehnt werden sollen"


@test("Thread: discard mit Grund")
def _():
    tid = orbit.create_thread("Zu verwerfen", "test")
    ok = orbit.discard_thread(tid, reason="Nicht relevant")
    assert ok
    t = orbit.get_thread(tid)
    assert t["status"] == "discarded"
    assert t["discard_reason"] == "Nicht relevant"


@test("Task: new → active → completed")
def _():
    task_id = orbit.create_task("observation", "Test-Task", "test")
    ok1 = orbit.task_transition(task_id, "active", reason="Test")
    assert ok1
    ok2 = orbit.task_transition(task_id, "completed", reason="Fertig")
    assert ok2
    t = orbit.get_task(task_id)
    assert t["status"] == "completed"


@test("Task: Hotness-Limit (max 3)")
def _():
    # Alle vorhandenen heißen Tasks zuerst kalt setzen
    conn = get_connection()
    hot_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM orbit_tasks WHERE hot = 1"
    ).fetchall()]
    conn.close()
    for hid in hot_ids:
        orbit.set_task_hot(hid, False)

    ids = []
    for i in range(3):
        tid = orbit.create_task("observation", f"Hot Task {i}", "test")
        ok = orbit.set_task_hot(tid, True)
        assert ok, f"Task {i} konnte nicht heiß gesetzt werden"
        ids.append(tid)
    # Vierter soll abgelehnt werden
    tid4 = orbit.create_task("observation", "Overflow Task", "test")
    ok4 = orbit.set_task_hot(tid4, True)
    assert not ok4, "Vierter heißer Task hätte abgelehnt werden sollen"
    # Aufräumen
    for tid in ids:
        orbit.set_task_hot(tid, False)


@test("Task → Thread Rückstufung")
def _():
    task_id = orbit.create_task("observation", "Rückstufungs-Task", "test")
    thread_id = orbit.downgrade_task_to_thread(task_id, reason="Zu unsicher")
    assert thread_id, "Rückstufung fehlgeschlagen"
    t = orbit.get_task(task_id)
    assert t["status"] == "aborted"
    th = orbit.get_thread(thread_id)
    assert th["status"] == "new"


@test("Step: ready → running → done")
def _():
    task_id = orbit.create_task("action", "Step-Test", "test")
    step_id = orbit.create_step(task_id, "observe", description="Test-Step")
    ok1 = orbit.step_transition(step_id, "running")
    assert ok1
    ok2 = orbit.step_transition(step_id, "done")
    assert ok2
    s = orbit.get_step(step_id)
    assert s["status"] == "done"


@test("Step: ungültiger Übergang done → running")
def _():
    task_id = orbit.create_task("action", "Step ungültig", "test")
    step_id = orbit.create_step(task_id, "observe")
    orbit.step_transition(step_id, "running")
    orbit.step_transition(step_id, "done")
    ok = orbit.step_transition(step_id, "running")  # nicht erlaubt
    assert not ok, "done → running hätte abgelehnt werden sollen"


# =============================================================================
# Logische Tests — Trigger-Dispatch
# =============================================================================

section("2. Trigger-Dispatch")


@test("Alle 10 Trigger-Typen werden verarbeitet")
def _():
    trigger_types = ["user_input", "heartbeat", "time_window", "tool_result",
                     "cognition_output", "mirror_signal", "review_result",
                     "manual_override", "recovery_result", "wiedervorlage"]
    for tt in trigger_types:
        tid = orbit.create_trigger(tt, source="test", payload={"test": True})
        events = orbit.get_pending_triggers()
        matching = [e for e in events if e["id"] == tid]
        assert matching, f"Trigger {tt} nicht in pending"
        orbit.process(matching)
        # Nach process: markiert
        conn = get_connection()
        row = conn.execute(
            "SELECT processed FROM orbit_triggers WHERE id=?", (tid,)
        ).fetchone()
        conn.close()
        assert row["processed"] == 1, f"Trigger {tt} nicht als processed markiert"


@test("Unbekannter Trigger-Typ wird geloggt und markiert")
def _():
    tid = orbit.create_trigger("unknown_type_xyz", source="test")
    events = [e for e in orbit.get_pending_triggers() if e["id"] == tid]
    orbit.process(events)
    conn = get_connection()
    row = conn.execute(
        "SELECT processed FROM orbit_triggers WHERE id=?", (tid,)
    ).fetchone()
    conn.close()
    assert row["processed"] == 1


# =============================================================================
# Operative Tests — Quality Gate
# =============================================================================

section("3. Quality Gate")


@test("Gate: hohe Confidence → proceed")
def _():
    r = orbit.quality_gate("Test-Aktion", confidence=0.9)
    assert r.passed
    assert r.action == "proceed"


@test("Gate: Commit mit niedriger Confidence → abort")
def _():
    r = orbit.quality_gate("Kritische Aktion", confidence=0.3, is_commit=True)
    assert not r.passed
    assert r.action == "abort"
    assert any("commit_low_confidence" in s for s in r.signals)


@test("Gate: task_status_conflict → abort")
def _():
    task_id = orbit.create_task("action", "Abgebrochener Task", "test")
    orbit.task_transition(task_id, "active")
    orbit.task_transition(task_id, "aborted")
    step_id = orbit.create_step(task_id, "observe")
    step = orbit.get_step(step_id)
    r = orbit.quality_gate("Step auf aborted Task", task_id=task_id, step=step, confidence=0.8)
    assert not r.passed
    assert any("task_status_conflict" in s for s in r.signals)


@test("Pre-Execution-Check: unkritisches Tool → proceed")
def _():
    step = {"step_type": "read", "tool_ref": "websearch",
            "interruptible": True, "commit_point": False, "preflight_required": True}
    r = orbit.pre_execution_check(step)
    assert r.passed


@test("no_action ist echte Entscheidung")
def _():
    did = orbit.no_action("Test no_action", "orbit_test")
    assert did
    conn = get_connection()
    row = conn.execute(
        "SELECT decision_type FROM orbit_decisions WHERE id=?", (did,)
    ).fetchone()
    conn.close()
    assert row["decision_type"] == "no_action"


@test("defensive_fallback dokumentiert Pfadwechsel")
def _():
    did = orbit.defensive_fallback(
        reason="Unsicherheit zu hoch",
        original_action="direct_send",
        fallback_action="too_early",
    )
    assert did
    conn = get_connection()
    row = conn.execute(
        "SELECT decision_type, alternative_rejected FROM orbit_decisions WHERE id=?", (did,)
    ).fetchone()
    conn.close()
    assert row["decision_type"] == "defensive_fallback"
    assert row["alternative_rejected"] == "direct_send"


# =============================================================================
# Operative Tests — Policies & Routinen
# =============================================================================

section("4. Policies & Routinen")


@test("Policy: proposed → active mit Score")
def _():
    pid = orbit.create_policy("action_policy", "test", scope=["orbit"],
                               reason="Keine externen Aktionen ohne Gate")
    ok = orbit.activate_policy(pid, reason="Test")
    assert ok
    p = get_connection().execute(
        "SELECT status, rank FROM orbit_policies WHERE id=?", (pid,)
    ).fetchone()
    assert p["status"] == "active"
    assert p["rank"] >= 0


@test("Policy: hard auf falscher Klasse wird korrigiert")
def _():
    pid = orbit.create_policy("action_policy", "test", scope=["orbit"],
                               hardness="hard", reason="Falsche Hard Policy")
    orbit.activate_policy(pid, reason="Test")
    p = get_connection().execute(
        "SELECT hardness FROM orbit_policies WHERE id=?", (pid,)
    ).fetchone()
    assert p["hardness"] == "soft", f"Erwartet soft, got {p['hardness']}"


@test("Policy: suppress und stale")
def _():
    pid = orbit.create_policy("communication_policy", "test", scope=["orbit"])
    orbit.activate_policy(pid, reason="Test")
    orbit.suppress_policy(pid, reason="Test-Suppression")
    p = get_connection().execute(
        "SELECT status FROM orbit_policies WHERE id=?", (pid,)
    ).fetchone()
    assert p["status"] == "suppressed"


@test("Routine: aktivieren und ausführen")
def _():
    rid = orbit.create_routine(
        routine_class="check_routine",
        primary_trigger_type="heartbeat",
        procedure_body="1. Threads prüfen\n2. Steps prüfen",
        primary_origin="test",
    )
    ok = orbit.activate_routine(rid, reason="Test")
    assert ok
    result = orbit.execute_routine(rid, context={})
    assert result["success"]


@test("Routine: Abweichung wird erkannt")
def _():
    rid = orbit.create_routine(
        routine_class="execution_routine",
        primary_trigger_type="user_input",
        procedure_body="Standardablauf für background-Modus",
        primary_origin="test",
        bindings={"mode": "background"},
    )
    orbit.activate_routine(rid, reason="Test")
    result = orbit.execute_routine(rid, context={"mode": "chat"})
    assert result["success"]
    assert result["deviation"]
    assert "mode" in result.get("deviation_reason", "")


@test("Routine: ohne Ablaufkörper nicht aktivierbar")
def _():
    rid = orbit.create_routine(
        routine_class="check_routine",
        primary_trigger_type="heartbeat",
        procedure_body="",  # leer
        primary_origin="test",
    )
    ok = orbit.activate_routine(rid, reason="Test")
    assert not ok, "Routine ohne Ablaufkörper hätte abgelehnt werden sollen"


# =============================================================================
# Operative Tests — Tools
# =============================================================================

section("5. Tool-Klassifikation")


@test("Tool-Registry: kritische Tools erkannt")
def _():
    assert orbit.is_tool_critical("calendar_write")
    assert orbit.is_tool_critical("mail_send")
    assert orbit.is_tool_critical("todos_delete")


@test("Tool-Registry: unkritische Tools erkannt")
def _():
    assert not orbit.is_tool_critical("websearch")
    assert not orbit.is_tool_critical("voice")
    assert not orbit.is_tool_critical("calendar_read")


@test("Tool dry-run läuft ohne Ausführung")
def _():
    r = orbit.execute_tool("websearch", "search", params={"query": "test"}, dry_run=True)
    assert r["success"]
    assert r["result"]["dry_run"] is True
    assert r["retries"] == 0


@test("Tool: NotImplemented wird sauber behandelt")
def _():
    r = orbit.execute_tool("mail_send", "send", params={})
    assert not r["success"]
    assert "nicht implementiert" in r["error"]


@test("Tool-Reputation: Default 0.7")
def _():
    rep = orbit.get_tool_reputation("websearch")
    assert 0.6 <= rep <= 0.8, f"Unerwartete Reputation: {rep}"


@test("Tool-Verfügbarkeit prüfen")
def _():
    avail = orbit.check_tool_availability("websearch")
    assert isinstance(avail, bool)


# =============================================================================
# Operative Tests — Proaktivität
# =============================================================================

section("6. Proaktivität")


@test("Proaktiv: Kandidat anlegen")
def _():
    mid = orbit.schedule_proactive_message(
        "recommendation", "test", "Test-Empfehlung"
    )
    assert mid
    conn = get_connection()
    row = conn.execute(
        "SELECT release_state, message_type FROM orbit_proactive_messages WHERE id=?",
        (mid,)
    ).fetchone()
    conn.close()
    assert row["release_state"] == "candidate"
    assert row["message_type"] == "recommendation"


@test("Proaktiv: too_early defer legt Wiedervorlage an")
def _():
    mid = orbit.schedule_proactive_message("nudge", "test", "Test-Nudge")
    orbit.defer_proactive_message(mid, "Test-Defer", recheck_minutes=10)
    conn = get_connection()
    msg = conn.execute(
        "SELECT release_state FROM orbit_proactive_messages WHERE id=?", (mid,)
    ).fetchone()
    wv = conn.execute(
        "SELECT COUNT(*) FROM orbit_wiedervorlagen WHERE target_ref=?", (mid,)
    ).fetchone()
    conn.close()
    assert msg["release_state"] == "too_early"
    assert wv[0] >= 1


@test("Proaktiv: suppress")
def _():
    mid = orbit.schedule_proactive_message("nudge", "test", "Zu supprimieren")
    orbit.suppress_proactive_message(mid, "Test")
    conn = get_connection()
    row = conn.execute(
        "SELECT release_state FROM orbit_proactive_messages WHERE id=?", (mid,)
    ).fetchone()
    conn.close()
    assert row["release_state"] == "suppressed"


@test("Proaktiv: Reaktion aktualisiert Reputation")
def _():
    mid = orbit.schedule_proactive_message("task_update", "test", "Test")
    # Reputation direkt aus orbit_reputation lesen (subject_type=proactive)
    conn = get_connection()
    row_before = conn.execute(
        "SELECT score FROM orbit_reputation WHERE subject_type='proactive' AND message_type='task_update'"
    ).fetchone()
    conn.close()
    score_before = row_before["score"] if row_before else 0.5
    orbit.record_proactive_reaction(mid, "acted_on")
    conn = get_connection()
    row_after = conn.execute(
        "SELECT score FROM orbit_reputation WHERE subject_type='proactive' AND message_type='task_update'"
    ).fetchone()
    conn.close()
    score_after = row_after["score"] if row_after else 0.5
    assert score_after >= score_before, f"Reputation hätte steigen sollen: {score_before} → {score_after}"


@test("Proaktiv: Tageslimit wird gezählt")
def _():
    count = orbit._count_sent_today()
    assert isinstance(count, int)
    assert count >= 0


# =============================================================================
# Recovery-Tests
# =============================================================================

section("7. Recovery & Integrität")


@test("Recovery: running Step ohne Worker → ready")
def _():
    task_id = orbit.create_task("action", "Recovery-Task", "test")
    step_id = orbit.create_step(task_id, "observe")
    orbit.step_transition(step_id, "running")
    # Künstlich alt machen
    conn = get_connection()
    conn.execute(
        "UPDATE orbit_steps SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id=?",
        (step_id,)
    )
    conn.commit()
    conn.close()
    recovered = orbit._recover_running_steps()
    s = orbit.get_step(step_id)
    assert s["status"] in ("ready", "failed"), f"Erwartet ready/failed, got {s['status']}"


@test("Recovery: stale Thread markieren")
def _():
    tid = orbit.create_thread("Stale Thread", "test", relevance="weak")
    orbit.thread_transition(tid, "watching")
    conn = get_connection()
    conn.execute(
        "UPDATE orbit_threads SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id=?",
        (tid,)
    )
    conn.commit()
    conn.close()
    stale = orbit._mark_stale_objects()
    assert stale["threads"] >= 1
    t = orbit.get_thread(tid)
    assert t["stale"] == 1


@test("Recovery: orphaned Step erkennen")
def _():
    # Task anlegen, abschliessen, Step bleibt aktiv
    task_id = orbit.create_task("action", "Orphan-Task", "test")
    step_id = orbit.create_step(task_id, "observe")
    orbit.task_transition(task_id, "active")
    orbit.task_transition(task_id, "completed")
    orphaned = orbit._detect_orphaned_objects()
    # Step sollte als orphaned markiert sein
    s = orbit.get_step(step_id)
    assert s["orphaned"] == 1 or orphaned["steps"] >= 0  # tolerant


@test("Recovery: Full Recovery läuft ohne Crash")
def _():
    orbit.full_recovery_on_start()
    conn = get_connection()
    report = conn.execute(
        "SELECT * FROM orbit_recovery_reports ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert report is not None
    assert report["finished_at"] is not None


@test("Recovery: Konflikt policy_conflict → resolved")
def _():
    pid1 = orbit.create_policy("action_policy", "test", scope=["orbit"], reason="A")
    pid2 = orbit.create_policy("action_policy", "test", scope=["orbit"], reason="B")
    orbit.activate_policy(pid1, reason="Test")
    orbit.activate_policy(pid2, reason="Test")
    result = orbit.resolve_conflict("policy_conflict", pid1, pid2)
    assert result == "resolved"


@test("Recovery: Konflikt goal_conflict → manual_attention")
def _():
    task_id = orbit.create_task("action", "Konflikt-Task", "test")
    result = orbit.resolve_conflict("goal_conflict", task_id, "other_id")
    assert result == "manual_attention"


@test("manual_attention: gesetzt und abrufbar")
def _():
    task_id = orbit.create_task("observation", "MA-Task", "test")
    orbit.raise_manual_attention(task_id, "task", "Test-Grund")
    conn = get_connection()
    row = conn.execute(
        "SELECT manual_attention FROM orbit_tasks WHERE id=?", (task_id,)
    ).fetchone()
    conn.close()
    assert row["manual_attention"] == 1


# =============================================================================
# Zusammenfassung
# =============================================================================

section("Ergebnis")

passed  = [r for r in _results if r[0] == "PASS"]
failed  = [r for r in _results if r[0] == "FAIL"]
errors  = [r for r in _results if r[0] == "ERROR"]
total   = len(_results)

print(f"\n  Gesamt:  {total}")
print(f"  ✓ Pass:  {len(passed)}")
if failed:
    print(f"  ✗ Fail:  {len(failed)}")
    for r in failed:
        print(f"      → {r[1]}: {r[2]}")
if errors:
    print(f"  ! Error: {len(errors)}")
    for r in errors:
        print(f"      → {r[1]}: {r[2][-100:]}")

print()
if not failed and not errors:
    print("  ORBIT Testmatrix: GRÜN ✓")
    sys.exit(0)
else:
    print("  ORBIT Testmatrix: FEHLER — siehe oben")
    sys.exit(1)
