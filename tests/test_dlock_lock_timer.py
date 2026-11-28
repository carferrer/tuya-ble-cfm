"""DLOCK-only estimated closure with a deterministic event-loop clock."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.tuya_ble.dlock_attempts import notify_local_open
from test_dlock_lock import MODULE, entity_for, report


class TimerClock:
    def __init__(self):
        self.elapsed = 0.0
        self.handles = []

    @property
    def timestamp(self):
        return 1800000000.0 + self.elapsed

    def call_later(self, delay, callback, *args, **kwargs):
        handle = SimpleNamespace(
            deadline=self.elapsed + delay, callback=callback, args=args,
            cancelled=False, fired=False,
        )
        handle.cancel = lambda: setattr(handle, "cancelled", True)
        self.handles.append(handle)
        return handle

    def advance(self, seconds):
        end = self.elapsed + seconds
        for handle in sorted(self.handles, key=lambda item: item.deadline):
            if not handle.cancelled and not handle.fired and handle.deadline <= end:
                self.elapsed = handle.deadline
                handle.fired = True
                handle.callback(*handle.args)
        self.elapsed = end

    def patches(self):
        return (
            patch.object(asyncio.get_running_loop(), "call_later", self.call_later),
            patch(f"{MODULE}.datetime"),
        )


def run_with_clock(test):
    async def run():
        clock = TimerClock()
        loop_patch, datetime_patch = clock.patches()
        with loop_patch, datetime_patch as mocked_datetime:
            mocked_datetime.now.side_effect = lambda _: datetime.fromtimestamp(clock.timestamp, UTC)
            await test(clock)
    asyncio.run(run())


@pytest.mark.parametrize("method", [12, 13, 14, 15, 19, 55, 62, 63, "dp71", "motor"])
def test_opening_estimates_closed_after_seven_seconds_only(method):
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        cache = {47: True}
        entity._device.datapoints = cache
        if method == "dp71":
            entity._local_open()
        elif method == "motor":
            entity._handle_updates([report(47, True, clock.timestamp)])
        else:
            entity._handle_updates([report(method, 100, clock.timestamp)])
        assert entity.is_locked is False
        assert clock.handles[-1].deadline == 7
        clock.advance(6.99)
        assert entity.is_locked is False
        clock.advance(0.01)
        assert entity.is_locked is True
        assert entity.extra_state_attributes["state_source"] == "auto_lock_timer"
        assert entity.extra_state_attributes["state_estimated"] is True
        assert entity._last_evidence == 1800000000.0
        assert cache == {47: True}  # Timer never changes motor cache or sends writes.
    run_with_clock(run)


@pytest.mark.parametrize("passage", [True, None])
def test_passage_or_unknown_mode_never_starts_timer(passage):
    async def run(clock):
        entity = entity_for()
        entity._passage = passage
        entity._local_open()
        entity._handle_updates([report(12, 100, clock.timestamp)])
        clock.advance(60)
        assert not clock.handles
        assert entity.is_locked is False
    run_with_clock(run)


def test_new_opening_restarts_timer_and_old_callback_cannot_close():
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        entity._local_open()
        first = clock.handles[-1]
        clock.advance(4)
        entity._handle_updates([report(12, 100, clock.timestamp)])
        assert first.cancelled
        first.callback()  # A cancelled callback already queued is harmless.
        assert entity.is_locked is False
        clock.advance(3)
        assert entity.is_locked is False
        clock.advance(4)
        assert entity.is_locked is True
    run_with_clock(run)


def test_repeated_or_recovered_events_do_not_extend_or_start_timer():
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        original = report(12, 100, clock.timestamp)
        entity._handle_updates([original])
        clock.advance(1)
        entity._handle_updates([
            original,
            report(13, 100, clock.timestamp - 3600),
            report(15, 100, clock.timestamp + 100),
        ])
        assert len(clock.handles) == 1
        clock.advance(6)
        assert entity.is_locked is True
        entity._handle_updates([original])
        assert len(clock.handles) == 1
        assert entity.is_locked is True
    run_with_clock(run)


def test_enabling_passage_cancels_and_disabling_alone_does_not_start_timer():
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        entity._local_open()
        first = clock.handles[-1]
        clock.advance(2)
        entity._handle_updates([report(33, False, clock.timestamp)])
        assert first.cancelled
        clock.advance(10)
        assert entity.is_locked is False
        entity._handle_updates([report(33, True, clock.timestamp)])
        clock.advance(20)
        assert entity.is_locked is False
        assert len(clock.handles) == 1
        first.callback()
        assert entity.is_locked is False
        entity._local_open()
        clock.advance(7)
        assert entity.is_locked is True
    run_with_clock(run)


def test_confirmed_close_cancels_and_has_priority_over_estimate():
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        entity._local_open()
        first = clock.handles[-1]
        clock.advance(4)
        entity._handle_updates([report(47, False, clock.timestamp)])
        assert first.cancelled
        assert entity.is_locked is True
        clock.advance(10)
        first.callback()
        assert entity.extra_state_attributes["state_source"] == "motor"
        assert entity.extra_state_attributes["state_estimated"] is False
    run_with_clock(run)


def test_new_motor_open_cancels_previous_deadline_and_starts_new_window():
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        entity._local_open()
        first = clock.handles[-1]
        clock.advance(4)
        entity._handle_updates([report(47, True, clock.timestamp)])
        assert first.cancelled
        assert entity.extra_state_attributes["state_source"] == "motor"
        assert entity.extra_state_attributes["state_estimated"] is False
        clock.advance(3)
        assert entity.is_locked is False
        clock.advance(4)
        assert entity.is_locked is True
        assert entity.extra_state_attributes["state_estimated"] is True
    run_with_clock(run)


def test_real_close_after_timer_removes_estimated_flag():
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        entity._local_open()
        clock.advance(7)
        entity._handle_updates([report(47, False, clock.timestamp)])
        assert entity.is_locked is True
        assert entity.extra_state_attributes["state_source"] == "motor"
        assert entity.extra_state_attributes["state_estimated"] is False
    run_with_clock(run)


def test_timer_is_not_new_evidence_that_hides_a_slightly_delayed_opening():
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        entity._local_open()
        clock.advance(7.5)
        entity._handle_updates([report(12, 100, clock.timestamp - 1)])
        assert entity.is_locked is False
        assert entity.extra_state_attributes["state_source"] == "access_event"
        clock.advance(7)
        assert entity.is_locked is True
    run_with_clock(run)


def test_repeated_auto_lock_report_does_not_cancel_a_pending_estimate():
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        entity._local_open()
        clock.advance(2)
        entity._handle_updates([report(33, True, clock.timestamp)])
        clock.advance(5)
        assert entity.is_locked is True
        assert entity.extra_state_attributes["state_source"] == "auto_lock_timer"
    run_with_clock(run)


def test_three_units_and_removal_have_independent_timers():
    async def run(clock):
        units = [entity_for(n) for n in range(3)]
        for entity in units:
            entity._device._cfm_lock_state = SimpleNamespace(get=lambda dp: True)
            with patch(f"{MODULE}.TuyaBLEEntity.async_added_to_hass", new_callable=AsyncMock):
                await entity.async_added_to_hass()
        assert not clock.handles  # Startup estimate does not schedule a timer.
        notify_local_open(units[0]._device)
        clock.advance(2)
        notify_local_open(units[1]._device)
        clock.advance(1)
        units[1]._handle_updates([report(33, False, clock.timestamp)])
        clock.advance(4)
        assert [e.is_locked for e in units] == [True, False, True]
        notify_local_open(units[0]._device)
        handle = clock.handles[-1]
        for call in units[0].async_on_remove.call_args_list:
            call.args[0]()
        assert handle.cancelled
        assert not units[0]._device._dlock_open_listeners
        handle.callback()
        clock.advance(20)
        assert units[0].is_locked is False
        assert units[1].is_locked is False
    run_with_clock(run)


def test_new_passage_report_invalidates_timer_estimate_without_opening():
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        entity._local_open()
        clock.advance(7)
        assert entity.is_locked is True
        entity._handle_updates([report(33, False, clock.timestamp)])
        assert entity.is_locked is None
        assert entity.extra_state_attributes["state_source"] == "unknown"
        assert len(clock.handles) == 1
    run_with_clock(run)


def test_old_motor_report_does_not_cancel_the_new_opening_timer():
    async def run(clock):
        entity = entity_for()
        entity._passage = False
        entity._local_open()
        clock.advance(1)
        old_timestamp = clock.timestamp
        clock.advance(0.5)
        entity._local_open()
        current = clock.handles[-1]
        entity._handle_updates([report(47, False, old_timestamp)])
        assert not current.cancelled
        assert entity.is_locked is False
        clock.advance(7)
        assert entity.extra_state_attributes["state_source"] == "auto_lock_timer"
    run_with_clock(run)
