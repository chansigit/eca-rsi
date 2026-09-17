"""Check activities wait inside the activity: one activity per wait, not one per poll."""
from ecarsi import work_coordinator as wc


def test_long_poll_returns_on_first_non_waiting_answer(monkeypatch):
    monkeypatch.setattr(wc, 'POLL_STEP_SECONDS', 0)
    answers = iter([{'state': 'waiting'}, {'state': 'waiting'}, {'state': 'ready', 'path': 'p'}, {'state': 'never'}])
    assert wc.long_poll(lambda: next(answers)) == {'state': 'ready', 'path': 'p'}


def test_long_poll_gives_up_at_the_deadline_with_the_last_waiting_answer(monkeypatch):
    monkeypatch.setattr(wc, 'POLL_STEP_SECONDS', 0)
    monkeypatch.setattr(wc, 'POLL_WAIT_SECONDS', 0)
    calls = []
    def check():
        calls.append(1)
        return {'state': 'waiting', 'poll_seconds': 3}
    assert wc.long_poll(check) == {'state': 'waiting', 'poll_seconds': 3}
    assert len(calls) == 1  # deadline already passed after the first check
