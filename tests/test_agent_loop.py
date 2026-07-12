"""State-transition coverage for the autonomous task loop."""

import pytest

import agent_loop


class FakeReference:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.updates = []

    def get(self):
        return self._snapshot

    def update(self, payload):
        self.updates.append(payload)


class FakeSnapshot:
    def __init__(self, data, *, exists=True, doc_id="task-1"):
        self._data = data
        self.exists = exists
        self.id = doc_id
        self.reference = FakeReference(self)

    def to_dict(self):
        return self._data


class FakeCollection:
    def __init__(self, docs, document_ref=None):
        self._docs = docs
        self._document_ref = document_ref

    def where(self, *_args):
        return self

    def limit(self, _value):
        return self

    def get(self):
        return self._docs

    def document(self, _doc_id):
        return self._document_ref


class FakeDatabase:
    def __init__(self, collection):
        self._collection = collection

    def collection(self, name):
        assert name == agent_loop.COLLECTION
        return self._collection


def test_answer_question_reopens_task_clears_question_and_syncs_reason(monkeypatch):
    snapshot = FakeSnapshot(
        {
            "title": "Budget report",
            "priority": 4,
            "reason": "Choose a reviewer.",
            "pending_question": "Who will review it?",
            "status": "needs_input",
        }
    )
    calls = []
    monkeypatch.setattr(
        agent_loop,
        "_db",
        lambda: FakeDatabase(FakeCollection([], snapshot.reference)),
    )
    monkeypatch.setattr(
        agent_loop,
        "upsert_task",
        lambda *args: calls.append(args),
    )

    result = agent_loop.answer_question("task-1", "Maya will review it.")

    assert snapshot.reference.updates == [
        {
            "status": "open",
            "pending_question": None,
            "reason": result["reason"],
        }
    ]
    assert result["title"] == "Budget report"
    assert "Choose a reviewer." in result["reason"]
    assert "Who will review it?" in result["reason"]
    assert result["reason"].endswith("Maya will review it.")
    assert calls == [("Budget report", 4, result["reason"])]


def test_answer_question_does_not_sync_when_task_is_missing(monkeypatch):
    missing = FakeSnapshot({}, exists=False)
    calls = []
    monkeypatch.setattr(
        agent_loop,
        "_db",
        lambda: FakeDatabase(FakeCollection([], missing.reference)),
    )
    monkeypatch.setattr(
        agent_loop,
        "upsert_task",
        lambda *args: calls.append(args),
    )

    assert agent_loop.answer_question("missing", "answer") == {
        "error": "task not found"
    }
    assert missing.reference.updates == []
    assert calls == []


@pytest.mark.parametrize(
    "action, worker",
    [("research", "_research"), ("draft", "_draft")],
)
def test_run_agent_loop_verifies_and_persists_agent_notes(
    monkeypatch, action, worker
):
    snapshot = FakeSnapshot(
        {
            "title": "Research vendor",
            "reason": "Need current pricing.",
            "priority": 3,
            "asked_questions": [],
        }
    )
    synced = []
    verified = []
    monkeypatch.setattr(
        agent_loop,
        "_db",
        lambda: FakeDatabase(FakeCollection([snapshot])),
    )
    monkeypatch.setattr(
        agent_loop,
        "_diagnose",
        lambda *_args: agent_loop.Diagnosis(action=action),
    )
    monkeypatch.setattr(
        agent_loop,
        worker,
        lambda title, reason: f"{action} note for {title}: {reason}",
    )

    def verify(title, reason, received_action, note):
        verified.append((title, reason, received_action, note))
        return agent_loop.Verification(sufficient=True)

    monkeypatch.setattr(agent_loop, "_verify", verify)
    monkeypatch.setattr(agent_loop, "upsert_task", lambda *args: synced.append(args))

    result = agent_loop.run_agent_loop()

    note = f"{action} note for Research vendor: Need current pricing."
    assert snapshot.reference.updates == [
        {
            "status": "in_progress",
            "agent_notes": note,
            "pending_question": None,
        }
    ]
    assert result == {
        "progressed": [
            {"title": "Research vendor", "action": action, "note": note}
        ],
        "asked": [],
    }
    assert verified == [("Research vendor", "Need current pricing.", action, note)]
    assert len(synced) == 1
    assert synced[0][:2] == ("Research vendor", 3)
    assert "Need current pricing." in synced[0][2]
    assert note in synced[0][2]


def test_run_agent_loop_skips_duplicate_question_without_firestore_update(monkeypatch):
    question = "Which account should be used?"
    snapshot = FakeSnapshot(
        {
            "title": "Create account",
            "reason": "Missing account choice.",
            "priority": 2,
            "asked_questions": [question],
        }
    )
    synced = []
    monkeypatch.setattr(
        agent_loop,
        "_db",
        lambda: FakeDatabase(FakeCollection([snapshot])),
    )
    monkeypatch.setattr(
        agent_loop,
        "_diagnose",
        lambda *_args: agent_loop.Diagnosis(action="ask", question=question),
    )
    monkeypatch.setattr(agent_loop, "is_duplicate", lambda *_args: True)
    monkeypatch.setattr(agent_loop, "upsert_task", lambda *args: synced.append(args))

    result = agent_loop.run_agent_loop()

    # A duplicate must not move the task back to needs_input every loop cycle.
    assert result == {"progressed": [], "asked": []}
    assert snapshot.reference.updates == []
    assert synced == []


def test_run_agent_loop_ignores_monitor_and_unknown_actions(monkeypatch):
    snapshots = [
        FakeSnapshot(
            {"title": "Monitor", "reason": "Wait.", "asked_questions": []},
            doc_id="monitor",
        ),
        FakeSnapshot(
            {"title": "Unknown", "reason": "Wait.", "asked_questions": []},
            doc_id="unknown",
        ),
    ]
    actions = iter(["monitor", "unexpected"])
    monkeypatch.setattr(
        agent_loop,
        "_db",
        lambda: FakeDatabase(FakeCollection(snapshots)),
    )
    monkeypatch.setattr(
        agent_loop,
        "_diagnose",
        lambda *_args: agent_loop.Diagnosis(action=next(actions)),
    )
    monkeypatch.setattr(
        agent_loop,
        "upsert_task",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not sync")),
    )

    assert agent_loop.run_agent_loop() == {"progressed": [], "asked": []}
    assert [snapshot.reference.updates for snapshot in snapshots] == [[], []]
