// Check-in form idempotency: one client_request_id per opened form, reused
// when the user retries the same form after a failure; a new form gets a new id.
import test from 'node:test';
import assert from 'node:assert/strict';
import { keyResult, load } from './harness.mjs';

/** FormData stand-in that reads a fake form's entries (forms.js formObject uses new FormData(form)). */
class FakeFormData {
  constructor(form) { this.items = form ? [...form.entries] : []; }
  entries() { return this.items[Symbol.iterator](); }
  get(name) { return this.items.find(([key]) => key === name)?.[1] ?? null; }
  getAll(name) { return this.items.filter(([key]) => key === name).map(([, value]) => value); }
  append(name, value) { this.items.push([name, value]); }
}

function fakeForm(values) {
  return { entries: Object.entries(values), querySelectorAll: () => [] };
}

test('retrying the same check-in form reuses its client_request_id; a new form gets a new one', async () => {
  let attempt = 0;
  const { JoyOKR, calls } = load(['domain.js', 'api.js', 'store.js', 'forms.js'], {
    routes: {
      'GET /api/okr/key-results/21/': { body: keyResult() },
      'GET /api/okr/objectives/12/': { body: { id: 12, approval_status: 'approved', scope: 'personal' } },
      'POST /api/okr/checkins/': () => {
        attempt += 1;
        if (attempt === 1) return 'network-error';
        if (attempt === 2) return { status: 504, raw: '' };
        return { status: attempt === 3 ? 201 : 200, body: { checkin: { id: 9 }, key_result: keyResult() } };
      },
    },
    extra: { FormData: FakeFormData },
  });
  const definition = await JoyOKR.forms.helpers.buildDefinition('checkin', { id: 21 });
  assert.match(definition.clientRequestId, /^[0-9a-f]{32}$/);
  const form = fakeForm({ current_value: '6', confidence: '4', update_text: 'Đã ký 2 hợp đồng', blockers: 'Không', next_steps: 'Tiếp tục' });

  await assert.rejects(definition.submit(form), (error) => error.data.code === 'network_error');
  await assert.rejects(definition.submit(form), (error) => error.status === 504);
  const saved = await definition.submit(form);
  assert.equal(saved.message, 'Đã lưu check-in.');

  const posts = calls.filter((call) => call.method === 'POST');
  assert.equal(posts.length, 3);
  posts.forEach((call) => {
    assert.equal(call.path, '/api/okr/checkins/');
    assert.equal(call.headers['X-CSRFToken'], 'csrf-token-123');
    assert.equal(call.credentials, 'same-origin');
    assert.equal(call.body.client_request_id, definition.clientRequestId);
    assert.equal(call.body.key_result, 21);
    assert.equal(call.body.confidence, 4);
    assert.equal(call.body.current_value, '6');
  });

  const another = await JoyOKR.forms.helpers.buildDefinition('checkin', { id: 21 });
  assert.notEqual(another.clientRequestId, definition.clientRequestId);
  await another.submit(form);
  assert.equal(calls.filter((call) => call.method === 'POST').at(-1).body.client_request_id, another.clientRequestId);
});
