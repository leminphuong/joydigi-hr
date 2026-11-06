// Date/time helpers: Django ISO 8601 (with offset and microseconds) must parse;
// JOYOKR's old "+ 'Z'" trick turned those into "—".
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { load, source } from './harness.mjs';

function helpers() {
  return load(['domain.js', 'api.js', 'store.js', 'views.js']).JoyOKR.views.helpers;
}

const FORMAT = new Intl.DateTimeFormat('vi-VN', { dateStyle: 'short', timeStyle: 'short' });

test('parseDateTime reads Django ISO 8601 datetimes with offsets and microseconds', () => {
  const { parseDateTime } = helpers();
  const cases = [
    ['2026-11-05T09:30:12.345678+07:00', Date.UTC(2026, 10, 5, 2, 30, 12, 345)],
    ['2026-11-05T09:30:12+07:00', Date.UTC(2026, 10, 5, 2, 30, 12)],
    ['2026-11-05T09:30:12.1+0700', Date.UTC(2026, 10, 5, 2, 30, 12, 100)],
    ['2026-11-05T09:30+07', Date.UTC(2026, 10, 5, 2, 30, 0)],
    ['2026-11-04T21:00:00-05:30', Date.UTC(2026, 10, 5, 2, 30, 0)],
    ['2026-11-05T02:30:12Z', Date.UTC(2026, 10, 5, 2, 30, 12)],
    ['2026-11-05T02:30:12.999999z', Date.UTC(2026, 10, 5, 2, 30, 12, 999)],
    // JOYOKR / MySQL style without a zone stays UTC, as JOYOKR treated it.
    ['2026-11-05 02:30:12', Date.UTC(2026, 10, 5, 2, 30, 12)],
  ];
  for (const [input, expected] of cases) {
    const parsed = parseDateTime(input);
    assert.ok(parsed, input);
    assert.equal(parsed.getTime(), expected, input);
  }
});

test('parseDateTime: date-only is local midnight; junk is null', () => {
  const { parseDateTime } = helpers();
  assert.equal(parseDateTime('2026-11-05').getTime(), new Date(2026, 10, 5).getTime());
  for (const junk of ['', null, undefined, 'bad-date', '2026-13-45T99:99:99+07:00', '05/11/2026', '<img src=x>']) {
    assert.equal(parseDateTime(junk), null, String(junk));
  }
});

test('dateTime keeps JOYOKR\'s vi-VN short format and no longer shows "—" for ISO input', () => {
  const { dateTime } = helpers();
  const iso = '2026-11-05T09:30:12.345678+07:00';
  assert.equal(dateTime(iso), FORMAT.format(new Date(Date.UTC(2026, 10, 5, 2, 30, 12, 345))));
  assert.notEqual(dateTime(iso), '—');
  assert.equal(dateTime(''), '—');
  assert.equal(dateTime('bad'), '—');

  // The original JOYOKR helper, for the record: ISO + 'Z' is invalid.
  const original = source('views.js').includes("String(value).replace(' ', 'T') + 'Z'");
  assert.equal(original, false, 'the WordPress-only parsing is gone');
  const legacy = vm.runInNewContext("(v) => { const p = new Date(String(v).replace(' ', 'T') + 'Z'); return Number.isNaN(p.valueOf()); }");
  assert.equal(legacy(iso), true, 'JOYOKR v1.6.45 rendered Django datetimes as "—"');
});

test('date keeps calendar dates (YYYY-MM-DD) and the date part of ISO datetimes', () => {
  const { date } = helpers();
  const format = new Intl.DateTimeFormat('vi-VN');
  assert.equal(date('2026-12-31'), format.format(new Date(2026, 11, 31)));
  assert.equal(date('2026-12-31T23:30:00+07:00'), format.format(new Date(2026, 11, 31)));
  assert.equal(date(null), '—');
  assert.equal(date('nope'), '—');
});
