(function registerDomain(globalScope) {
  'use strict';

  /** Calculates display progress as a percentage between zero and one hundred. */
  function progress(startValue, targetValue, currentValue) {
    const start = Number(startValue);
    const target = Number(targetValue);
    const current = Number(currentValue);

    if (![start, target, current].every(Number.isFinite)) return 0;
    if (start === target) return current === target ? 100 : 0;

    return round(clamp(((current - start) / (target - start)) * 100, 0, 100));
  }

  /** Calculates the weighted average progress of a collection of result items. */
  function weightedProgress(items) {
    if (!Array.isArray(items) || items.length === 0) return 0;

    const totals = items.reduce(
      (summary, item) => {
        const weight = Math.max(0, Number(item.weight) || 0);
        return {
          value: summary.value + clamp(Number(item.progress) || 0, 0, 100) * weight,
          weight: summary.weight + weight,
        };
      },
      { value: 0, weight: 0 },
    );

    return totals.weight === 0 ? 0 : round(totals.value / totals.weight);
  }

  /** Classifies current progress against elapsed-cycle progress. */
  function status(actualProgress, expectedProgress, warningThreshold) {
    const actual = clamp(Number(actualProgress) || 0, 0, 100);
    const expected = clamp(Number(expectedProgress) || 0, 0, 100);
    const threshold = clamp(Number(warningThreshold) || 0, 0, 100);

    if (actual >= 100) return 'completed';
    if (actual >= expected) return 'on_track';
    if (expected - actual > threshold) return 'off_track';
    return 'at_risk';
  }

  /** Finds the configured label for an OKR score between zero and one. */
  function rating(score, ranges) {
    const normalized = clamp(Number(score) || 0, 0, 1);
    const match = ranges.find((range, index) => {
      const isLast = index === ranges.length - 1;
      return normalized >= Number(range.min) && (normalized < Number(range.max) || (isLast && normalized <= Number(range.max)));
    });
    return match ? match.label : '';
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function round(value) {
    return Math.round((value + Number.EPSILON) * 100) / 100;
  }

  globalScope.JoyOKRDomain = Object.freeze({ progress, weightedProgress, status, rating });
})(globalThis);
