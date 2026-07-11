"""Google Tasks の検索ヘルパーを検証する。"""
from tasks_client import _find_matching_task


class FakeTasks:
    def __init__(self, items):
        self.items = items
        self.list_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return self

    def execute(self):
        return {"items": self.items}


class FakeService:
    def __init__(self, items):
        self.fake_tasks = FakeTasks(items)

    def tasks(self):
        return self.fake_tasks


def test_find_matching_task_uses_titles_match_and_lists_once():
    service = FakeService([{"id": "task-1", "title": "🟡 会議資料作成"}])

    result = _find_matching_task(service, "list-1", "会議資料作成")

    assert result == {"id": "task-1", "title": "🟡 会議資料作成"}
    assert service.fake_tasks.list_calls == [
        {"tasklist": "list-1", "showCompleted": False}
    ]


def test_find_matching_task_returns_none_when_no_title_matches():
    service = FakeService([{"id": "task-1", "title": "別のタスク"}])

    assert _find_matching_task(service, "list-1", "会議資料作成") is None
