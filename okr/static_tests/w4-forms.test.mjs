// W4: a new Objective goes into the same default cycle the server shows
// (okr.services.cycles.active_cycle), not simply the newest active one.
import test from 'node:test';
import assert from 'node:assert/strict';
import { load, makeDocument } from './harness.mjs';

function forms() {
  const { JoyOKR } = load(['domain.js', 'api.js', 'store.js', 'views.js', 'forms.js'], {
    document: makeDocument(),
    extra: { HTMLElement: class HTMLElement {} },
  });
  return JoyOKR.forms;
}

const cycle = (id, start, end, status = 'active') => ({ id, name: `C${id}`, start_date: start, end_date: end, status });

test('defaultCycleId: the active cycle covering today beats a newer active one', () => {
  const { defaultCycleId } = forms();
  const today = new Date(2026, 10, 5);
  const cycles = [cycle(3, '2027-01-05', '2027-03-31'), cycle(1, '2026-10-06', '2027-01-04'), cycle(2, '2026-07-01', '2026-09-30', 'closed')];
  assert.equal(defaultCycleId(cycles, today), 1);
});

test('defaultCycleId: with no current active cycle, the newest active one; none active gives undefined', () => {
  const { defaultCycleId } = forms();
  const today = new Date(2026, 10, 5);
  assert.equal(defaultCycleId([cycle(5, '2027-04-01', '2027-06-30'), cycle(3, '2027-01-05', '2027-03-31')], today), 5);
  assert.equal(defaultCycleId([cycle(2, '2026-07-01', '2026-09-30', 'closed')], today), undefined);
});
