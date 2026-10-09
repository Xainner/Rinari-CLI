"""The branch warning measures from the last work in the worktree.

Case ses_01M2V3KQEPZ8NW9JH3BMZAQR9Y: a conversation begun on
feature/style-lora-ratatatat74 kept warning "-> main" on every open, weeks
after the work in that checkout had moved on.
"""

from __future__ import annotations

import contextlib
import os

from rinari.projects.branch_tracking import BranchTracker, observe_checkout
from tests.unit import test_resume_reconciliation as base
from tests.unit.test_resume_reconciliation import _finding, _git

env = base.env
repo = base.repo


def _tracker(s):
    return BranchTracker(s.ctx.db, s.ctx.clock)


def _work(s, root, session_id="ses_a", turn="turn_1"):
    _tracker(s).record_work(root, session_id=session_id, turn_id=turn, source="turn.end")


def test_old_start_branch_is_not_reported_on_first_sight(env, repo) -> None:
    s = env[2]
    _git(repo, "checkout", "-qb", "feature/style")
    record = s.sessions.start(repo).session
    record.git_branch = "feature/style"
    s.ctx.session_repo.update(record)
    _git(repo, "checkout", "-q", "main")

    first = s.sessions.resume(record.id)

    assert _finding(first, "git-branch").state == "ok"
    assert not any("git-branch" in w for w in first.warnings)
    # The conversation still remembers where it started.
    assert s.ctx.session_repo.get(record.id).git_branch == "feature/style"


def test_work_that_moved_branches_is_not_reported_again(env, repo) -> None:
    s = env[2]
    record = s.sessions.start(repo).session
    _work(s, repo, record.id, "turn_1")  # work on main
    _git(repo, "checkout", "-qb", "feature")
    _work(s, repo, record.id, "turn_2")  # the work itself moved to feature

    assert _finding(s.sessions.resume(record.id), "git-branch").state == "ok"


def test_external_change_is_reported_once_from_the_last_work(env, repo) -> None:
    s = env[2]
    record = s.sessions.start(repo).session
    _git(repo, "checkout", "-qb", "b")
    _work(s, repo, record.id)
    _git(repo, "checkout", "-qb", "c")

    first = s.sessions.resume(record.id)
    again = s.sessions.resume(record.id)

    change = _finding(first, "git-branch")
    assert change.state == "changed"
    assert change.data["from"] == "b" and change.data["to"] == "c"
    assert change.data["reference"] == "last_work"
    assert change.data["session_id"] == record.id
    assert _finding(again, "git-branch").state == "ok"

    # Later work on c, then d: measured from c, never from b.
    _work(s, repo, record.id, "turn_3")
    _git(repo, "checkout", "-qb", "d")
    later = _finding(s.sessions.resume(record.id), "git-branch")
    assert (later.data["from"], later.data["to"]) == ("c", "d")


def test_another_conversation_in_the_same_checkout_moves_the_reference(env, repo) -> None:
    s = env[2]
    old = s.sessions.start(repo).session
    _work(s, repo, old.id)
    _git(repo, "checkout", "-qb", "next")
    _work(s, repo, "ses_other")

    assert _finding(s.sessions.resume(old.id), "git-branch").state == "ok"


def test_worktrees_keep_separate_references(env, repo, tmp_path) -> None:
    s = env[2]
    other = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(other))
    _work(s, repo)
    _work(s, other)

    assert _tracker(s).last(observe_checkout(repo).worktree)["branch"] == "main"
    assert _tracker(s).last(observe_checkout(other).worktree)["branch"] == "side"


def test_detached_head_and_missing_git_keep_the_last_valid_observation(env, repo) -> None:
    s = env[2]
    _work(s, repo)
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "--detach")
    change = _tracker(s).transition(repo)
    assert change["to"] == f"detached HEAD at {head[:7]}"

    worktree = observe_checkout(repo).worktree
    plain = repo.parent / "plain"
    plain.mkdir()
    assert _tracker(s).record_work(plain, session_id="x", turn_id="t", source="turn.end") is None
    assert _tracker(s).last(worktree)["branch"] == "main"


def test_turns_record_the_branch_even_when_they_fail(env, repo, monkeypatch) -> None:
    from rinari.cli import agent_runtime

    s = env[2]
    record = s.sessions.start(repo).session
    session = agent_runtime.build_agent_session(s, record, interactive=False)

    def switch_then_fail(*args, **kwargs):
        _git(repo, "checkout", "-qb", "during-turn")
        raise RuntimeError("model failed")

    monkeypatch.setattr(agent_runtime, "_run_turn_unlocked", switch_then_fail)
    with contextlib.suppress(RuntimeError):
        agent_runtime.run_turn(session, "hola", turn_id="turn_x")

    last = _tracker(s).last(os.path.normcase(str(repo.resolve())))
    assert last["branch"] == "during-turn"
    assert last["source"] == "turn.end" and last["turn_id"] == "turn_x"
