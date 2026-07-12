"""Firestore のタスク言及マージを検証する。"""
import pytest

pytest.importorskip("google.cloud.firestore")
memory_store = pytest.importorskip("memory_store")


class FakeDocument:
    def __init__(self, data, doc_id="task-1"):
        self.data = data
        self.id = doc_id
        self.updated = []
        self.reference = self

    def to_dict(self):
        return self.data

    def update(self, data):
        self.updated.append(data)


class FakeTaskMentions:
    def __init__(self, previous):
        self.previous = previous
        self.documents = {doc.id: doc for doc in previous}
        self.added = []
        self.where_calls = []
        self.limit_calls = []

    def where(self, *args):
        self.where_calls.append(args)
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, _limit):
        self.limit_calls.append(_limit)
        return self

    def get(self):
        return self.previous

    def add(self, data):
        self.added.append(data)

    def document(self, doc_id):
        return self.documents[doc_id]


class FakeFirestore:
    def __init__(self, task_mentions):
        self.task_mentions = task_mentions

    def collection(self, name):
        assert name == memory_store.COLLECTION
        return self.task_mentions


def _configure_firestore(monkeypatch, previous):
    task_mentions = FakeTaskMentions(previous)
    monkeypatch.setattr(memory_store, "_client", lambda: FakeFirestore(task_mentions))
    return task_mentions


def test_record_and_resolve_creates_first_mention(monkeypatch):
    task_mentions = _configure_firestore(monkeypatch, [])

    result = memory_store.record_and_resolve("請求書を確認", 3, "月末まで")

    assert result == {
        "title": "請求書を確認",
        "priority": 3,
        "reason": "月末まで",
        "mention_count": 1,
        "promoted": False,
        "previous_priority": 3,
    }
    assert task_mentions.added[0]["mention_count"] == 1
    assert task_mentions.added[0]["priority"] == 3


def test_record_and_resolve_keeps_promoted_priority_for_lower_remention(monkeypatch):
    previous = FakeDocument({"priority": 4, "mention_count": 2})
    _configure_firestore(monkeypatch, [previous])

    result = memory_store.record_and_resolve("請求書を確認", 2, "月末まで")

    assert result["priority"] == 5
    assert result["mention_count"] == 3
    assert result["promoted"] is True
    assert previous.updated[0]["priority"] == 5


def test_record_and_resolve_uses_higher_incoming_priority(monkeypatch):
    previous = FakeDocument({"priority": 2, "mention_count": 4})
    _configure_firestore(monkeypatch, [previous])

    result = memory_store.record_and_resolve("請求書を確認", 5, "今日中")

    assert result["priority"] == 5
    assert result["mention_count"] == 5
    assert result["promoted"] is False
    assert previous.updated[0]["priority"] == 5


def test_exact_title_remention_uses_shared_resolver(monkeypatch):
    previous = FakeDocument({"priority": 2, "mention_count": 1})
    _configure_firestore(monkeypatch, [previous])
    calls = []
    monkeypatch.setattr(
        memory_store,
        "_resolve_remention",
        lambda *args: calls.append(args) or {"title": "Exact task"},
    )

    assert memory_store.record_and_resolve("Exact task", 3, "updated") == {
        "title": "Exact task"
    }
    assert calls[0][0] == previous.data
    assert calls[0][1] is previous.reference


def test_find_pending_questions_filters_and_limits_results(monkeypatch):
    pending = [
        FakeDocument(
            {"title": "Submit report", "pending_question": "Who is the reviewer?"}
        )
    ]
    task_mentions = _configure_firestore(monkeypatch, pending)

    assert memory_store.find_pending_questions(limit=2) == [
        {
            "id": "task-1",
            "title": "Submit report",
            "pending_question": "Who is the reviewer?",
        }
    ]
    assert task_mentions.where_calls == [("status", "==", "needs_input")]
    assert task_mentions.limit_calls == [2]


def test_cosine_similarity_handles_edge_cases():
    from embeddings import cosine_similarity

    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0


def test_exact_title_fast_path_does_not_embed(monkeypatch):
    previous = FakeDocument({"priority": 3, "mention_count": 1})
    _configure_firestore(monkeypatch, [previous])
    monkeypatch.setattr(memory_store, "embed_text", pytest.fail)

    result = memory_store.record_and_resolve("Exact task", 3, "same task")

    assert result["mention_count"] == 2


def test_semantic_match_resolves_existing_task(monkeypatch):
    candidate = FakeDocument(
        {
            "title": "Prepare quarterly budget",
            "priority": 2,
            "mention_count": 3,
            "embedding": [1.0, 0.0],
        },
        doc_id="candidate",
    )
    task_mentions = _configure_firestore(monkeypatch, [])
    task_mentions.documents[candidate.id] = candidate
    infos = []
    monkeypatch.setattr(memory_store, "is_semantic_match_enabled", lambda: True)
    monkeypatch.setattr(
        memory_store,
        "embed_text",
        lambda _title: [0.9, 0.4358898943540673],
    )
    monkeypatch.setattr(
        memory_store.obs,
        "info",
        lambda *args, **kwargs: infos.append((args, kwargs)),
    )
    monkeypatch.setattr(
        memory_store,
        "find_open_tasks",
        lambda: [{"id": "candidate", **candidate.to_dict()}],
    )
    monkeypatch.setenv("YUI_SEMANTIC_THRESHOLD", "0.9")

    result = memory_store.record_and_resolve("Complete budget plan", 2, "rephrased")

    assert result["mention_count"] == 4
    assert candidate.updated[0]["priority"] == 3
    assert infos[0][0] == ("semantic re-mention detected",)
    assert infos[0][1]["matched_title"] == "Prepare quarterly budget"


def test_missing_candidate_embedding_creates_new_task(monkeypatch):
    candidate = FakeDocument({"title": "Old task", "priority": 2}, doc_id="candidate")
    task_mentions = _configure_firestore(monkeypatch, [])
    monkeypatch.setattr(memory_store, "is_semantic_match_enabled", lambda: True)
    monkeypatch.setattr(memory_store, "embed_text", lambda _title: [1.0, 0.0])
    monkeypatch.setattr(
        memory_store,
        "find_open_tasks",
        lambda: [{"id": "candidate", **candidate.to_dict()}],
    )

    memory_store.record_and_resolve("New task", 2, "new")

    assert task_mentions.added[0]["embedding"] == [1.0, 0.0]


def test_embedding_failure_falls_back_to_new_task(monkeypatch):
    task_mentions = _configure_firestore(monkeypatch, [])
    warnings = []
    monkeypatch.setattr(memory_store, "is_semantic_match_enabled", lambda: True)
    monkeypatch.setattr(
        memory_store,
        "embed_text",
        lambda _title: (_ for _ in ()).throw(RuntimeError("embedding unavailable")),
    )
    monkeypatch.setattr(
        memory_store.obs, "warning", lambda *args, **kwargs: warnings.append(kwargs)
    )

    memory_store.record_and_resolve("New task", 2, "new")

    assert "embedding" not in task_mentions.added[0]
    assert warnings == [
        {"api": "embeddings", "detail": "embedding unavailable", "exc_type": "RuntimeError"}
    ]


def test_disabled_semantic_matching_does_not_embed(monkeypatch):
    task_mentions = _configure_firestore(monkeypatch, [])
    monkeypatch.setenv("YUI_SEMANTIC_MATCH", "0")
    monkeypatch.setattr(memory_store, "embed_text", pytest.fail)

    memory_store.record_and_resolve("New task", 2, "new")

    assert "embedding" not in task_mentions.added[0]
