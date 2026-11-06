import math

import pytest

from studyspace_node.occupancy import (
    OccupancyDetector,
    OccupancySettings,
    OccupancySource,
    OccupancyState,
)

FREE, OCCUPIED, UNKNOWN = OccupancyState.FREE, OccupancyState.OCCUPIED, OccupancyState.UNKNOWN
NEAR, FAR = 0.5, 2.0


def feed(detector, readings, start=0.0, step=1.0):
    """Apply (motion, distance) readings at regular times; returns the final state."""
    state = detector.state
    for i, (motion, distance) in enumerate(readings):
        state = detector.update(motion, distance, start + i * step)
    return state


def make_free(detector, now=0.0):
    for _ in range(detector.settings.confirm_readings):
        detector.update(False, FAR, now)
    assert detector.state is FREE


def make_occupied(detector, now=0.0, distance=FAR):
    """Occupy via motion. The default FAR keeps the median window from holding presence."""
    for _ in range(detector.settings.confirm_readings):
        detector.update(True, distance, now)
    assert detector.state is OCCUPIED


def test_starts_unknown_and_healthy(detector):
    assert detector.state is UNKNOWN
    assert detector.healthy
    assert detector.consecutive_faults == 0


def test_state_values_are_strings():
    assert {s.value for s in OccupancyState} == {"unknown", "free", "occupied"}
    assert OccupancyState.FREE == "free"


class TestConfirmationDebounce:
    def test_occupied_requires_exact_confirm_count_from_unknown(self, detector):
        assert detector.update(True, None, 0) is UNKNOWN
        assert detector.update(True, None, 1) is UNKNOWN
        assert detector.update(True, None, 2) is OCCUPIED

    def test_occupied_requires_confirm_count_from_free(self, detector):
        make_free(detector)
        assert detector.update(True, None, 10) is FREE
        assert detector.update(True, None, 11) is FREE
        assert detector.update(True, None, 12) is OCCUPIED

    def test_free_from_unknown_requires_confirm_count(self, detector):
        assert detector.update(False, FAR, 0) is UNKNOWN
        assert detector.update(False, FAR, 1) is UNKNOWN
        assert detector.update(False, FAR, 2) is FREE

    def test_single_motion_blip_does_not_occupy(self, detector):
        make_free(detector)
        assert feed(detector, [(True, FAR), (False, FAR), (False, FAR), (False, FAR)]) is FREE

    def test_absent_reading_restarts_the_present_run(self, detector):
        make_free(detector)
        state = feed(detector, [(True, FAR), (True, FAR), (False, FAR), (True, FAR), (True, FAR)])
        assert state is FREE
        assert detector.update(True, FAR, 99) is OCCUPIED

    def test_present_reading_restarts_the_absent_run_from_unknown(self, detector):
        state = feed(detector, [(False, FAR), (False, FAR), (True, FAR), (False, FAR), (False, FAR)])
        assert state is UNKNOWN
        assert detector.update(False, FAR, 99) is FREE

    def test_confirm_of_one_transitions_immediately(self):
        detector = OccupancyDetector(OccupancySettings(confirm_readings=1))
        assert detector.update(True, None, 0) is OCCUPIED


class TestVacancyTimeout:
    def test_boundary(self, detector):
        make_occupied(detector, now=100.0)
        timeout = detector.settings.vacancy_timeout_s
        assert detector.update(False, FAR, 100.0 + timeout - 0.001) is OCCUPIED
        assert detector.update(False, FAR, 100.0 + timeout) is FREE

    def test_presence_during_timeout_resets_it(self, detector):
        make_occupied(detector, now=0.0)
        assert detector.update(False, FAR, 299.0) is OCCUPIED
        assert detector.update(True, FAR, 299.5) is OCCUPIED
        assert detector.update(False, FAR, 300.0) is OCCUPIED
        assert detector.update(False, FAR, 599.4) is OCCUPIED
        assert detector.update(False, FAR, 599.5) is FREE

    def test_sitting_still_is_held_by_distance_alone(self, detector):
        make_occupied(detector, now=0.0, distance=NEAR)
        for t in range(1, 1000, 10):
            assert detector.update(False, NEAR, float(t)) is OCCUPIED

    def test_can_reoccupy_after_vacating_with_fresh_debounce(self, detector):
        make_occupied(detector, now=0.0)
        assert detector.update(False, FAR, 300.0) is FREE
        assert detector.update(True, FAR, 301.0) is FREE
        assert detector.update(True, FAR, 302.0) is FREE
        assert detector.update(True, FAR, 303.0) is OCCUPIED


class TestMedianFilter:
    def test_single_far_spike_does_not_clear_presence(self, detector):
        for reading in [NEAR, NEAR, NEAR, 3.5, NEAR]:
            state = detector.update(False, reading, 0.0)
        assert state is OCCUPIED

    def test_single_near_spike_does_not_register_presence(self, detector):
        make_free(detector)
        for reading in [FAR, FAR, 0.1, FAR, FAR]:
            assert detector.update(False, reading, 0.0) is FREE

    def test_window_slides_so_old_readings_age_out(self):
        settings = OccupancySettings(distance_window=3, confirm_readings=1)
        detector = OccupancyDetector(settings)
        detector.update(None, FAR, 0)
        detector.update(None, FAR, 1)
        detector.update(None, NEAR, 2)
        assert detector.state is FREE  # median of FAR, FAR, NEAR
        assert detector.update(None, NEAR, 3) is OCCUPIED  # median of FAR, NEAR, NEAR

    def test_threshold_is_strictly_less_than(self):
        detector = OccupancyDetector(OccupancySettings(confirm_readings=1))
        assert detector.update(None, 0.8, 0) is FREE
        fresh = OccupancyDetector(OccupancySettings(confirm_readings=1))
        assert fresh.update(None, 0.79, 0) is OCCUPIED


class TestInvalidDistances:
    @pytest.mark.parametrize("bad", [0.0, 0.019, 4.01, 100.0, -1.0, math.nan, math.inf])
    def test_out_of_range_alone_is_a_fault(self, detector, bad):
        assert detector.update(None, bad, 0) is UNKNOWN
        assert detector.consecutive_faults == 1

    @pytest.mark.parametrize("edge", [0.02, 4.0])
    def test_range_limits_are_inclusive(self, detector, edge):
        detector.update(None, edge, 0)
        assert detector.consecutive_faults == 0

    def test_out_of_range_does_not_enter_the_median_window(self, detector):
        """A 0.01 m glitch is 'near' numerically but must not count as presence."""
        for _ in range(5):
            state = detector.update(False, 0.01, 0.0)
        assert state is FREE

    def test_dropout_does_not_break_a_debounce_run(self, detector):
        assert detector.update(False, NEAR, 1) is UNKNOWN
        assert detector.update(False, 99.0, 2) is UNKNOWN
        # The near window survives the glitch, so that was 3 present rounds in a row.
        assert detector.update(False, NEAR, 3) is OCCUPIED

    def test_dead_distance_sensor_cannot_pin_desk_occupied(self, detector):
        """Stale near readings expire, so motion-only operation can free the desk."""
        make_occupied(detector, now=0.0, distance=NEAR)
        window = detector.settings.distance_window
        for i in range(window):  # the last of these clears the window
            detector.update(False, None, float(i))
        last_presence = float(window - 2)
        timeout = detector.settings.vacancy_timeout_s
        assert detector.update(False, None, last_presence + timeout - 1.0) is OCCUPIED
        assert detector.update(False, None, last_presence + timeout) is FREE


class TestSingleSensorOperation:
    def test_motion_only_occupied_and_free(self, detector):
        assert feed(detector, [(True, None)] * 3) is OCCUPIED
        assert detector.update(False, None, 299.0) is OCCUPIED
        assert detector.update(False, None, 303.0) is FREE
        assert detector.healthy

    def test_motion_only_reaches_free_from_unknown(self, detector):
        assert feed(detector, [(False, None)] * 3) is FREE

    def test_distance_only_occupied_and_free(self, detector):
        assert feed(detector, [(None, NEAR)] * 3) is OCCUPIED
        for _ in range(5):  # flush the median window
            detector.update(None, FAR, 1.0)
        assert detector.update(None, FAR, 400.0) is FREE
        assert detector.consecutive_faults == 0

    def test_motion_overrides_far_distance(self, detector):
        assert feed(detector, [(True, FAR)] * 3) is OCCUPIED


class TestFaults:
    @pytest.mark.parametrize("make_state, expected", [(make_free, FREE), (make_occupied, OCCUPIED)])
    def test_both_sensors_failing_holds_state(self, detector, make_state, expected):
        make_state(detector)
        for t in range(1, 2000, 100):
            assert detector.update(None, None, float(t)) is expected

    def test_both_failing_holds_unknown(self, detector):
        assert feed(detector, [(None, None)] * 10) is UNKNOWN

    def test_fault_round_does_not_advance_debounce(self, detector):
        make_free(detector)
        detector.update(True, None, 1)
        detector.update(True, None, 2)
        assert detector.update(None, None, 3) is FREE
        assert detector.update(True, None, 4) is OCCUPIED

    def test_invalid_distance_with_failed_motion_is_a_fault_even_with_a_stale_window(self, detector):
        make_occupied(detector)
        detector.update(None, 99.0, 1)
        assert detector.consecutive_faults == 1

    def test_healthy_goes_false_at_threshold_and_recovers(self, detector):
        limit = detector.settings.max_consecutive_faults
        for i in range(limit - 1):
            detector.update(None, None, float(i))
            assert detector.healthy
        detector.update(None, None, 99.0)
        assert not detector.healthy
        assert detector.consecutive_faults == limit
        detector.update(None, None, 100.0)
        assert not detector.healthy
        detector.update(False, None, 101.0)
        assert detector.healthy
        assert detector.consecutive_faults == 0

    def test_distance_alone_also_recovers_health(self, detector):
        for i in range(10):
            detector.update(None, None, float(i))
        detector.update(None, FAR, 11.0)
        assert detector.healthy

    def test_first_absent_reading_after_long_outage_frees_the_desk(self, detector):
        """Documented behaviour: the timeout counts from the last observed presence."""
        make_occupied(detector, now=0.0)
        assert detector.update(None, None, 1000.0) is OCCUPIED
        assert detector.update(False, FAR, 1001.0) is FREE


class TestSettingsValidation:
    def test_defaults(self):
        s = OccupancySettings()
        assert (s.presence_distance_m, s.confirm_readings, s.vacancy_timeout_s) == (0.8, 3, 300)
        assert (s.distance_window, s.min_valid_distance_m) == (5, 0.02)
        assert (s.max_valid_distance_m, s.max_consecutive_faults) == (4.0, 5)
        assert (s.presence_weight_kg, s.weight_window, s.manual_hold_s) == (20.0, 5, 1800)
        assert (s.min_valid_weight_kg, s.max_valid_weight_kg) == (-10.0, 300.0)

    def test_frozen(self):
        with pytest.raises(AttributeError):
            OccupancySettings().confirm_readings = 9

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"presence_distance_m": 0},
            {"presence_distance_m": -1},
            {"presence_distance_m": "near"},
            {"presence_distance_m": math.nan},
            {"presence_distance_m": 0.01},  # below min valid
            {"presence_distance_m": 5.0},  # above max valid
            {"confirm_readings": 0},
            {"confirm_readings": 2.5},
            {"confirm_readings": True},
            {"vacancy_timeout_s": 0},
            {"vacancy_timeout_s": -5},
            {"vacancy_timeout_s": math.inf},
            {"distance_window": 0},
            {"distance_window": "5"},
            {"min_valid_distance_m": -0.1},
            {"max_valid_distance_m": 0},
            {"min_valid_distance_m": 4.0, "max_valid_distance_m": 4.0},
            {"min_valid_distance_m": 3.0, "max_valid_distance_m": 1.0, "presence_distance_m": 2.0},
            {"max_consecutive_faults": 0},
            {"max_consecutive_faults": None},
            {"presence_weight_kg": 0},
            {"presence_weight_kg": 500.0},  # above max valid
            {"weight_window": 0},
            {"min_valid_weight_kg": "low"},
            {"min_valid_weight_kg": 50.0},  # above the presence threshold
            {"max_valid_weight_kg": 0},
            {"manual_hold_s": 0},
            {"manual_hold_s": math.nan},
        ],
    )
    def test_invalid_rejected(self, kwargs):
        with pytest.raises(ValueError):
            OccupancySettings(**kwargs)


SEATED, EMPTY = 70.0, 0.5


def weigh(detector, weight, now=0.0):
    return detector.update(None, None, now, weight_kg=weight)


def seat_someone(detector, now=0.0):
    for _ in range(detector.settings.weight_window):
        weigh(detector, SEATED, now)
    assert detector.state is OCCUPIED


def empty_seat(detector, now=0.0):
    for _ in range(detector.settings.weight_window):
        weigh(detector, EMPTY, now)


class TestWeightOnly:
    def test_occupied_after_confirmation(self, detector):
        assert weigh(detector, SEATED, 0) is UNKNOWN
        assert weigh(detector, SEATED, 1) is UNKNOWN
        assert weigh(detector, SEATED, 2) is OCCUPIED

    def test_free_after_confirmation(self, detector):
        assert weigh(detector, EMPTY, 0) is UNKNOWN
        assert weigh(detector, EMPTY, 1) is UNKNOWN
        assert weigh(detector, EMPTY, 2) is FREE

    def test_threshold_is_inclusive(self):
        settings = OccupancySettings(confirm_readings=1)
        assert OccupancyDetector(settings).update(None, None, 0, weight_kg=20.0) is OCCUPIED
        assert OccupancyDetector(settings).update(None, None, 0, weight_kg=19.99) is FREE

    def test_bag_below_threshold_does_not_occupy(self, detector):
        for t in range(10):
            assert weigh(detector, 8.0, float(t)) in (UNKNOWN, FREE)
        assert detector.state is FREE

    def test_single_spike_is_ignored_by_the_median(self, detector):
        for _ in range(3):
            weigh(detector, EMPTY)
        for reading in [EMPTY, 90.0, EMPTY]:
            assert weigh(detector, reading) is FREE

    def test_standing_up_starts_the_vacancy_timeout(self, detector):
        seat_someone(detector, now=0.0)
        empty_seat(detector, now=10.0)  # median flips partway; last presence is t=10
        assert weigh(detector, EMPTY, 309.0) is OCCUPIED
        assert weigh(detector, EMPTY, 310.0) is FREE

    @pytest.mark.parametrize("bad", [-11.0, 301.0, math.nan, math.inf, None])
    def test_invalid_weight_alone_is_a_fault(self, detector, bad):
        assert weigh(detector, bad) is UNKNOWN
        assert detector.consecutive_faults == 1

    def test_dead_load_cell_cannot_pin_seat_occupied(self, detector):
        seat_someone(detector, now=0.0)
        for t in range(1, 6):
            weigh(detector, None, float(t))
        # Window cleared. It's also a fault, so a second sensor is what lets it free.
        assert detector.update(False, None, 1000.0, weight_kg=None) is FREE

    def test_failed_weight_with_working_motion_is_not_a_fault(self, detector):
        detector.update(False, None, 0, weight_kg=None)
        assert detector.consecutive_faults == 0

    def test_weight_and_motion_combine(self, detector):
        for _ in range(3):
            detector.update(True, None, 0, weight_kg=EMPTY)
        assert detector.state is OCCUPIED


class TestMarkOccupied:
    def test_holds_empty_seat_for_the_hold_period(self, detector):
        empty_seat(detector)
        detector.mark_occupied(100.0)
        assert detector.state is OCCUPIED
        assert detector.source is OccupancySource.MANUAL
        assert detector.hold_remaining_s(160.0) == 1740.0
        assert weigh(detector, EMPTY, 1899.9) is OCCUPIED

    def test_expires_exactly_at_the_hold_period(self, detector):
        empty_seat(detector)
        detector.mark_occupied(100.0)
        assert weigh(detector, EMPTY, 1900.0) is FREE
        assert detector.source is OccupancySource.SENSOR
        assert detector.hold_remaining_s(1900.0) == 0.0

    def test_expires_even_while_sensors_are_down(self, detector):
        empty_seat(detector)
        detector.mark_occupied(0.0)
        assert weigh(detector, None, 1800.0) is FREE

    def test_sitting_down_ends_the_hold_after_confirmation(self, detector):
        empty_seat(detector)
        detector.mark_occupied(0.0)
        weigh(detector, SEATED, 1)
        weigh(detector, SEATED, 2)
        assert detector.source is OccupancySource.MANUAL
        for t in (3, 4, 5):
            weigh(detector, SEATED, t)
        assert detector.state is OCCUPIED
        assert detector.source is OccupancySource.SENSOR
        assert detector.hold_remaining_s(6) == 0.0

    def test_single_spike_does_not_end_the_hold(self, detector):
        empty_seat(detector)
        detector.mark_occupied(0.0)
        for t, reading in enumerate([90.0, EMPTY, EMPTY, EMPTY]):
            weigh(detector, reading, float(t + 1))
        assert detector.source is OccupancySource.MANUAL

    def test_brief_presence_does_not_end_the_hold(self, detector):
        """A motion blip passes the median filter, so only the debounce protects the hold."""
        empty_seat(detector)
        detector.mark_occupied(0.0)
        detector.update(True, None, 1.0, weight_kg=EMPTY)
        detector.update(True, None, 2.0, weight_kg=EMPTY)
        assert detector.source is OccupancySource.MANUAL
        detector.update(False, None, 3.0, weight_kg=EMPTY)
        detector.update(True, None, 4.0, weight_kg=EMPTY)
        assert detector.source is OccupancySource.MANUAL
        detector.update(True, None, 5.0, weight_kg=EMPTY)
        detector.update(True, None, 6.0, weight_kg=EMPTY)
        assert detector.source is OccupancySource.SENSOR

    def test_pressed_while_seated_survives_until_they_leave_and_return(self, detector):
        seat_someone(detector)
        detector.mark_occupied(10.0)
        for t in range(11, 20):  # still seated while pressing: must not end the hold
            weigh(detector, SEATED, float(t))
        assert detector.source is OccupancySource.MANUAL
        for t in range(20, 26):  # leaves
            weigh(detector, EMPTY, float(t))
        assert detector.source is OccupancySource.MANUAL
        assert weigh(detector, EMPTY, 1000.0) is OCCUPIED  # beyond the 300 s vacancy timeout
        for t in range(1001, 1007):  # comes back
            weigh(detector, SEATED, float(t))
        assert detector.source is OccupancySource.SENSOR
        assert detector.state is OCCUPIED

    def test_pressing_again_extends(self, detector):
        empty_seat(detector)
        detector.mark_occupied(0.0)
        detector.mark_occupied(1000.0)
        assert detector.hold_remaining_s(1000.0) == 1800.0

    def test_works_before_the_first_decision(self, detector):
        detector.mark_occupied(0.0)
        assert detector.state is OCCUPIED


class TestMarkVacant:
    def test_skips_the_vacancy_timeout(self, detector):
        seat_someone(detector)
        empty_seat(detector, now=1.0)
        assert detector.state is OCCUPIED
        assert detector.mark_vacant() is True
        assert detector.state is FREE
        assert weigh(detector, EMPTY, 2.0) is FREE

    def test_refused_while_someone_is_detected(self, detector):
        seat_someone(detector)
        assert detector.mark_vacant() is False
        assert detector.state is OCCUPIED

    def test_refusal_does_not_end_a_hold(self, detector):
        seat_someone(detector)
        detector.mark_occupied(1.0)
        assert detector.mark_vacant() is False
        assert detector.source is OccupancySource.MANUAL

    def test_cancels_a_hold(self, detector):
        empty_seat(detector)
        detector.mark_occupied(0.0)
        assert detector.mark_vacant() is True
        assert detector.state is FREE
        assert detector.source is OccupancySource.SENSOR

    def test_allowed_during_a_sensor_outage(self, detector):
        seat_someone(detector)
        weigh(detector, None, 1.0)
        assert detector.mark_vacant() is True
        assert detector.state is FREE

    def test_reoccupying_needs_a_fresh_debounce(self, detector):
        empty_seat(detector)
        detector.mark_occupied(0.0)
        detector.mark_vacant()
        assert weigh(detector, SEATED, 1) is FREE
        assert weigh(detector, SEATED, 2) is FREE
        assert weigh(detector, SEATED, 3) is OCCUPIED

    def test_resolves_unknown(self, detector):
        assert detector.mark_vacant() is True
        assert detector.state is FREE
