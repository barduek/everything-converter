"""Pruebas de src/converter/tasks/progress.py: Porcentaje_de_Avance acotado, que ignora NaN, no
retrocede y admite franjas para Tareas de varios pasos (Req 14.5, 14.12)."""

from __future__ import annotations

import math

import pytest

from converter.tasks.progress import ProgressBand, ProgressReporter, ProgressSink


def _reported(*fractions: float) -> ProgressReporter:
    reporter = ProgressReporter()
    for fraction in fractions:
        reporter.report(fraction)
    return reporter


@pytest.mark.req("14.5")
def test_starts_at_zero_percent() -> None:
    assert ProgressReporter().percent == 0


@pytest.mark.req("14.5", "14.12")
@pytest.mark.parametrize(
    ("fraction", "percent"),
    [(0.0, 0), (0.004, 0), (0.5, 50), (0.759, 75), (0.999, 99), (1.0, 100)],
)
def test_percent_is_the_floor_of_one_hundred_times_the_fraction(
    fraction: float, percent: int
) -> None:
    assert _reported(fraction).percent == percent


@pytest.mark.req("14.12")
@pytest.mark.parametrize(
    ("fraction", "percent"),
    [(-0.5, 0), (-math.inf, 0), (1.5, 100), (math.inf, 100)],
)
def test_fractions_outside_zero_to_one_are_clamped(fraction: float, percent: int) -> None:
    assert _reported(fraction).percent == percent


@pytest.mark.req("14.12")
def test_nan_is_ignored() -> None:
    assert _reported(math.nan).percent == 0
    assert _reported(0.4, math.nan).percent == 40


@pytest.mark.req("14.12")
def test_lower_fraction_does_not_move_the_percent_back() -> None:
    reporter = _reported(0.6)
    reporter.report(0.3)
    reporter.report(-1.0)
    assert reporter.percent == 60
    reporter.report(0.61)
    assert reporter.percent == 61


@pytest.mark.req("14.5")
def test_band_maps_step_fractions_to_its_part_of_the_total() -> None:
    reporter = ProgressReporter()
    transcription = reporter.band(0.1, 1.0)
    transcription.report(0.0)
    assert reporter.percent == 10
    transcription.report(0.5)
    assert reporter.percent == 55


@pytest.mark.req("14.12")
def test_band_clamps_and_ignores_nan_like_the_reporter() -> None:
    reporter = ProgressReporter()
    step = reporter.band(0.2, 0.5)
    step.report(-3.0)
    assert reporter.percent == 20
    step.report(math.nan)
    assert reporter.percent == 20
    step.report(3.0)
    assert reporter.percent == 50


@pytest.mark.req("14.5", "14.12")
def test_two_step_task_advances_through_both_bands_up_to_one_hundred() -> None:
    reporter = ProgressReporter()
    extraction = reporter.band(0.0, 0.1)
    transcription = reporter.band(0.1, 1.0)
    seen = [reporter.percent]
    for fraction in (0.25, 0.5, 1.0):
        extraction.report(fraction)
        seen.append(reporter.percent)
    for fraction in (0.0, 0.3, 0.9, 1.0):
        transcription.report(fraction)
        seen.append(reporter.percent)
    assert seen == [0, 2, 5, 10, 10, 37, 91, 100]


@pytest.mark.req("14.12")
def test_band_below_the_current_maximum_does_not_move_the_percent_back() -> None:
    reporter = ProgressReporter()
    reporter.band(0.0, 0.5).report(1.0)
    reporter.band(0.2, 0.6).report(0.0)
    assert reporter.percent == 50


# En estas franjas `start + (end - start) × 1` queda un ulp por debajo de `end`
@pytest.mark.parametrize(
    ("start", "end", "percent"), [(0.05, 0.21, 21), (0.1, 0.42, 42), (0.6, 1.0, 100)]
)
def test_band_end_is_reached_exactly(start: float, end: float, percent: int) -> None:
    reporter = ProgressReporter()
    reporter.band(start, end).report(1.0)
    assert reporter.percent == percent


def test_nested_band_uses_fractions_of_its_parent_step() -> None:
    reporter = ProgressReporter()
    step = reporter.band(0.1, 1.0)
    first_half = step.band(0.0, 0.5)
    assert (first_half.start, first_half.end) == (0.1, 0.55)
    first_half.report(1.0)
    assert reporter.percent == 55
    step.band(0.5, 1.0).report(1.0)
    assert reporter.percent == 100


def test_zero_width_band_reports_its_single_point() -> None:
    reporter = ProgressReporter()
    reporter.band(0.4, 0.4).report(0.7)
    assert reporter.percent == 40


_INVALID_BOUNDS = [(-0.1, 0.5), (0.5, 0.4), (0.2, 1.1), (math.nan, 0.5), (0.0, math.nan)]


@pytest.mark.parametrize(("start", "end"), _INVALID_BOUNDS)
def test_band_rejects_invalid_bounds(start: float, end: float) -> None:
    reporter = ProgressReporter()
    with pytest.raises(ValueError, match="franja"):
        reporter.band(start, end)
    with pytest.raises(ValueError, match="franja"):
        reporter.band(0.0, 1.0).band(start, end)
    with pytest.raises(ValueError, match="franja"):
        ProgressBand(reporter, start, end)


def test_reporter_and_band_are_progress_sinks() -> None:
    reporter = ProgressReporter()
    sinks: list[ProgressSink] = [reporter, reporter.band(0.5, 1.0)]
    for sink in sinks:
        sink.report(0.2)
    assert reporter.percent == 60
