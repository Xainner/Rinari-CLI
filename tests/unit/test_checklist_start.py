"""A checklist suggested once per turn when the work is several steps (plan 0.4.0 #48)."""

from __future__ import annotations

from rinari.checklist.reminder import START_AFTER_ROUNDS, ChecklistReminder, multi_step_request


class _Service:
    def __init__(self) -> None:
        self._current_turn = {"s": "t1"}
        self.checklist = None

    def get(self, session_id):
        return self.checklist


def _reminder(service, mode="build", request="Haz 5 imágenes y luego 5 videos"):
    return ChecklistReminder(service, "s", mode=lambda: mode, request=lambda: request)


def test_requests_with_several_things_are_multi_step():
    assert multi_step_request("Haz 5 imágenes, 5 videos y un logo")
    assert multi_step_request("Primero lee el CSV y después arma el gráfico")
    assert multi_step_request("- uno\n- dos\n- tres")
    assert multi_step_request("Corrige main.py y utils.py")
    assert not multi_step_request("¿Qué hora es en Tokio?")
    assert not multi_step_request("Explícame este error")


def test_one_suggestion_after_the_rounds_and_only_once_per_turn():
    service = _Service()
    reminder = _reminder(service)
    notes = [reminder.after_round(["shell.exec"]) for _ in range(START_AFTER_ROUNDS + 3)]
    assert notes[: START_AFTER_ROUNDS - 1] == [None] * (START_AFTER_ROUNDS - 1)
    assert "checklist.update" in (notes[START_AFTER_ROUNDS - 1] or "")
    assert all(note is None for note in notes[START_AFTER_ROUNDS:])
    service._current_turn["s"] = "t2"  # a new turn may get it again
    later = [reminder.after_round(["shell.exec"]) for _ in range(START_AFTER_ROUNDS)]
    assert later[-1] is not None


def test_no_suggestion_for_a_question_review_or_a_list_already_written():
    service = _Service()
    question = _reminder(service, request="¿Qué hace este archivo?")
    assert all(question.after_round(["fs.read"]) is None for _ in range(10))
    service._current_turn["s"] = "t3"
    review = _reminder(service, mode="review")
    assert all(review.after_round(["fs.read"]) is None for _ in range(10))
    written = _reminder(service)
    written.after_round(["checklist.update"])
    assert all(written.after_round(["shell.exec"]) is None for _ in range(10))


def test_writing_several_files_counts_as_several_steps():
    service = _Service()
    reminder = _reminder(service, request="arregla esto")
    notes = [reminder.after_round(["fs.write"]) for _ in range(START_AFTER_ROUNDS)]
    assert notes[-1] is not None
