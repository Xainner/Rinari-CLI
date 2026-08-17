import pytest

from rinari.memory.service import MemoryService, find_sensitive_match
from rinari.shared.errors import InvalidUsageError


@pytest.fixture
def mem(app_ctx) -> MemoryService:
    return MemoryService(app_ctx)


def test_remember_and_recall_user(mem):
    result = mem.remember_user(
        "Prefers Python 3.12 and uv",
        kind="preference",
        topic="python",
        provenance="user-stated",
    )
    assert result["store"] == "user"
    assert result["action"] == "created"

    rows = mem.search_user("uv")
    assert len(rows) == 1
    assert rows[0]["kind"] == "preference"
    assert rows[0]["provenance"] == "user-stated"

    by_kind = mem.search_user("", kind="rule")
    assert by_kind == []


def test_remember_user_idempotent_and_supersede(mem):
    first = mem.remember_user("Uses ruff for lint", topic="linter")
    refreshed = mem.remember_user("Uses ruff for lint", topic="LINTER")
    assert refreshed["action"] == "refreshed"
    assert refreshed["id"] == first["id"]
    assert len(mem.list_user()) == 1

    replaced = mem.remember_user("Uses ruff and mypy", topic="linter")
    assert replaced["action"] == "superseded"
    live = mem.list_user()
    assert len(live) == 1
    assert live[0]["id"] == replaced["id"]
    # the old record is history only: never returned by reads
    assert mem.search_user("ruff") == [live[0]]


def test_update_and_forget_user(mem):
    stored = mem.remember_user("Answer in Spanish", topic="language")
    updated = mem.update_user(stored["id"], confidence=0.8)
    assert updated["confidence"] == 0.8
    assert updated["text"] == "Answer in Spanish"

    assert mem.forget_user(stored["id"]) is True
    assert mem.forget_user(stored["id"]) is False
    assert mem.list_user() == []


def test_sensitive_filter_rejects_secrets(mem):
    for sample in (
        "key is sk-" + "a" * 32,
        "token: ghp_" + "b" * 30,
        "Authorization: Bearer " + "c" * 24,
        "password = " + "d" * 20,
        "-----BEGIN RSA PRIVATE KEY-----",
        "jwt eyJ" + "e" * 19 + "." + "f" * 20 + "." + "g" * 10,
    ):
        with pytest.raises(InvalidUsageError):
            mem.remember_user(sample, topic="creds")

    # git SHAs are legitimate memory content and must pass
    sha = "a" * 40
    stored = mem.remember_user(f"last good commit {sha}", topic="git")
    assert stored["action"] == "created"

    assert find_sensitive_match("sk-abcdefghijklmnop") is not None
    assert find_sensitive_match("plain text about testing") is None


def test_project_memory_scoped_and_forget(mem):
    root = "/repo"
    stored = mem.remember_project(root, "Deploy via CI pipeline", kind="fact", topic="deploy")
    assert mem.search_project(root, "deploy")[0]["id"] == stored["id"]
    # a different project sees nothing
    assert mem.search_project("/other", "deploy") == []
    live = mem.list_project(root)
    assert len(live) == 1

    assert mem.forget_project(root, stored["id"]) is True
    assert mem.list_project(root) == []
    # forgetting from the wrong project is a no-op
    other = mem.remember_project("/other", "x", topic="x")
    assert mem.forget_project(root, other["id"]) is False


def test_project_memory_conflict_supersedes(mem):
    root = "/repo"
    mem.remember_project(root, "Tests run with pytest", topic="tests")
    second = mem.remember_project(root, "Tests run with uv run pytest", topic="tests")
    assert second["action"] == "superseded"
    live = mem.list_project(root)
    assert len(live) == 1
    assert live[0]["id"] == second["id"]


def test_episodic_record_and_search(mem):
    record = mem.record_episodic(
        "ses_1", "/repo", "Fixed the checkpoint restore bug", outcome="done"
    )
    assert record["session_ref"] == "ses_1"
    rows = mem.search_episodic("/repo", "checkpoint")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "done"
    # truncated to 400 chars
    long = mem.record_episodic("ses_1", "/repo", "x" * 900)
    assert long["summary_chars"] == 400
    with pytest.raises(InvalidUsageError):
        mem.record_episodic("ses_1", "/repo", "", outcome="done")


def test_pattern_records_and_dedup(mem):
    created = mem.remember_pattern("undo", "Always run the checkpoint tests after edits")
    assert created["action"] == "created"
    again = mem.remember_pattern("undo ", "Always run the checkpoint tests after edits")
    assert again["action"] == "exists"
    assert again["id"] == created["id"]

    hits = mem.search_pattern("checkpoint")
    assert [h["id"] for h in hits] == [created["id"]]
    assert mem.list_pattern(scope="global")[0]["id"] == created["id"]
    assert mem.list_pattern(scope="user") == []
    assert mem.forget_pattern(created["id"]) is True
    assert mem.list_pattern() == []


def test_prompt_segment_rendering(mem):
    assert mem.prompt_segment(None) is None
    mem.remember_user("Prefers terse answers", topic="style")
    segment = mem.prompt_segment("/repo")
    assert segment is not None
    assert "Prefers terse answers" in segment
    assert "possibly stale" in segment

    mem.remember_project("/repo", "Main branch is protected", topic="git")
    segment = mem.prompt_segment("/repo")
    assert "[project:fact] Main branch is protected" in segment
    # project memory is namespaced: other projects do not leak into the block
    assert "Main branch" not in (mem.prompt_segment("/other") or "")


def test_kind_validation(mem):
    with pytest.raises(InvalidUsageError):
        mem.remember_user("x", kind="bogus", topic="t")
    with pytest.raises(InvalidUsageError):
        mem.remember_project("/repo", "x", kind="bogus", topic="t")
    with pytest.raises(InvalidUsageError):
        mem.remember_user("x", topic="")
    with pytest.raises(InvalidUsageError):
        mem.remember_user("y" * 5000, topic="big")
