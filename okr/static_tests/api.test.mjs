// JoyOKR.api adapter: JOYOKR endpoint names -> Django /api/okr/ calls, and
// Django responses -> the shapes views.js / forms.js read.
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ADMIN_CAPS, MANAGER_CAPS, cyclesPage, keyResult, load, makeConfig, makeLocation, objectiveDetail, objectiveSummary, owner, page, plain,
} from './harness.mjs';

const API_FILES = ['domain.js', 'api.js'];

function setup(routes = {}, options = {}) {
  return load(API_FILES, { routes, ...options });
}

async function rejection(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  assert.fail('expected the request to fail');
}

test('objective list: JOYOKR filters map to Django query params and DRF pages map back', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/objectives/': { body: page([objectiveSummary({ provisional: true })], { count: 250 }) },
  });
  const result = await JoyOKR.api.request('objectives', {
    query: {
      search: ' Doanh thu ', approval_status: 'approved', cycle_id: '3', owner_user_id: '7', per_page: '500', page: '2',
      scope: 'team', department_id: 'abc', team_id: '4', order_by: 'title', bogus: '<x>',
    },
  });
  assert.equal(calls.length, 1);
  const call = calls[0];
  assert.equal(call.method, 'GET');
  assert.equal(call.path, '/api/okr/objectives/');
  assert.deepEqual(call.query, {
    page: '2', page_size: '100', q: 'Doanh thu', cycle: '3', status: 'approved', scope: 'team', team: '4', owner: 'me', order_by: 'title',
  });
  assert.equal(call.credentials, 'same-origin');
  assert.equal(call.headers['X-CSRFToken'], undefined, 'safe methods carry no CSRF header');
  assert.equal(call.headers.Accept, 'application/json');

  assert.equal(result.total, 250);
  assert.equal(result.page, 2);
  assert.equal(result.per_page, 100);
  assert.equal(result.total_pages, 3);
  const [item] = result.items;
  assert.equal(item.owner_user_id, 7);
  assert.equal(item.owner_name, 'Nguyễn Văn A');
  assert.equal(item.cycle_id, 3);
  assert.equal(item.cycle_name, 'Q4 2026');
  assert.equal(item.end_date, '2026-12-31');
  assert.equal(item.parent_objective_id, 2);
  assert.equal(item.progress_is_provisional, true);
});

test('objective list: another owner id is sent as a number, default page size is 20', async () => {
  const { JoyOKR, calls } = setup({ 'GET /api/okr/objectives/': { body: page([], { count: 41 }) } });
  const result = await JoyOKR.api.request('objectives', { query: { owner_user_id: '15' } });
  assert.deepEqual(calls[0].query, { page_size: '20', owner: '15' });
  assert.equal(result.total_pages, 3);
  assert.equal(result.page, 1);
});

test('objective create/update/delete: body renames, CSRF header and paths', async () => {
  const { JoyOKR, calls } = setup({
    'POST /api/okr/objectives/': { status: 201, body: objectiveDetail() },
    'PUT /api/okr/objectives/12/': { body: objectiveDetail() },
    'DELETE /api/okr/objectives/12/': { body: { message: 'Đã xóa Objective.' } },
  });
  const form = {
    title: 'Mục tiêu', description: 'Mô tả', cycle_id: '3', scope: 'department', department_id: '5', team_id: '',
    owner_user_id: '9', parent_objective_id: '', weight: '100', priority: 'high',
  };
  const created = await JoyOKR.api.request('objectives', { method: 'POST', body: form });
  await JoyOKR.api.request('objectives/12', { method: 'PUT', body: { ...form, parent_objective_id: '2' } });
  const deleted = await JoyOKR.api.request('objectives/12', { method: 'DELETE' });

  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].path, '/api/okr/objectives/');
  assert.equal(calls[0].headers['X-CSRFToken'], 'csrf-token-123');
  assert.equal(calls[0].headers['Content-Type'], 'application/json');
  assert.equal(calls[0].credentials, 'same-origin');
  assert.deepEqual(calls[0].body, {
    title: 'Mục tiêu', description: 'Mô tả', cycle_id: 3, scope: 'department', department_id: 5, team_id: null,
    owner_id: 9, parent_id: null, weight: '100', priority: 'high',
  });
  assert.equal(calls[1].method, 'PUT');
  assert.equal(calls[1].path, '/api/okr/objectives/12/');
  assert.equal(calls[1].body.parent_id, 2);
  assert.equal(calls[2].method, 'DELETE');
  assert.equal(calls[2].path, '/api/okr/objectives/12/');
  assert.equal(calls[2].headers['X-CSRFToken'], 'csrf-token-123');
  assert.equal(calls[2].rawBody, undefined);
  assert.equal(created.owner_name, 'Nguyễn Văn A');
  assert.equal(deleted.message, 'Đã xóa Objective.');
});

test('objective update from a non-admin form omits disabled association fields', async () => {
  const { JoyOKR, calls } = setup({ 'PUT /api/okr/objectives/12/': { body: objectiveDetail() } });
  await JoyOKR.api.request('objectives/12', { method: 'PUT', body: { title: 'T', description: '', weight: '50', priority: 'low', parent_objective_id: '2' } });
  assert.deepEqual(calls[0].body, { title: 'T', description: '', parent_id: 2, weight: '50', priority: 'low' });
});

test('workflow: submit, approve, reject(reason) and normalize KR weights', async () => {
  const action = { body: { message: 'OK', notification_count: 1, objective: objectiveSummary() } };
  const { JoyOKR, calls } = setup({
    'POST /api/okr/objectives/12/submit/': action,
    'POST /api/okr/objectives/12/approve/': action,
    'POST /api/okr/objectives/12/reject/': action,
    'POST /api/okr/objectives/12/normalize-kr-weights/': { body: objectiveDetail() },
  });
  await JoyOKR.api.request('objectives/12/submit', { method: 'POST', body: {} });
  await JoyOKR.api.request('objectives/12/approve', { method: 'POST', body: {} });
  await JoyOKR.api.request('objectives/12/reject', { method: 'POST', body: { reason: '  Thiếu KR  ' } });
  const normalized = await JoyOKR.api.request('objectives/12/normalize-kr-weights', { method: 'POST' });
  assert.deepEqual(calls.map((call) => `${call.method} ${call.path}`), [
    'POST /api/okr/objectives/12/submit/',
    'POST /api/okr/objectives/12/approve/',
    'POST /api/okr/objectives/12/reject/',
    'POST /api/okr/objectives/12/normalize-kr-weights/',
  ]);
  assert.equal(calls[0].rawBody, undefined);
  assert.deepEqual(calls[2].body, { reason: 'Thiếu KR' });
  calls.forEach((call) => assert.equal(call.headers['X-CSRFToken'], 'csrf-token-123'));
  assert.equal(normalized.key_results.length, 2);
});

test('objective detail: permissions and can_edit are derived from Django fields', async () => {
  const scenarios = [
    { name: 'owner, draft, active cycle', caps: undefined, detail: {}, cycles: { 3: 'active' }, canEdit: true },
    { name: 'owner, submitted', caps: undefined, detail: { approval_status: 'submitted' }, cycles: { 3: 'active' }, canEdit: false },
    { name: 'owner, closed cycle', caps: undefined, detail: {}, cycles: { 3: 'closed' }, canEdit: false },
    { name: 'admin, approved', caps: ADMIN_CAPS, detail: { approval_status: 'approved', owner: owner(50, 'B') }, cycles: { 3: 'active' }, canEdit: true },
    { name: 'employee, not owner', caps: undefined, detail: { owner: owner(50, 'B') }, cycles: { 3: 'active' }, canEdit: false },
    {
      name: 'manager of another department (KR signal false)', caps: MANAGER_CAPS,
      detail: { owner: owner(50, 'B'), key_results: [keyResult({ owner: owner(50, 'B'), permissions: { can_checkin: false, can_add_evidence: false } })] },
      cycles: { 3: 'active' }, canEdit: false,
    },
    {
      name: 'manager of this department (KR signal true)', caps: MANAGER_CAPS,
      detail: { owner: owner(50, 'B'), key_results: [keyResult({ owner: owner(50, 'B'), permissions: { can_checkin: false, can_add_evidence: true } })] },
      cycles: { 3: 'active' }, canEdit: true,
    },
  ];
  for (const scenario of scenarios) {
    const config = makeConfig(scenario.caps ? { capabilities: scenario.caps } : {});
    const { JoyOKR, calls } = setup({
      'GET /api/okr/objectives/12/': { body: objectiveDetail(scenario.detail) },
      'GET /api/okr/cycles/': { body: cyclesPage(scenario.cycles) },
    }, { config });
    const item = await JoyOKR.api.request('objectives/12');
    assert.equal(item.can_edit, scenario.canEdit, scenario.name);
    assert.equal(item.can_submit, true, scenario.name);
    assert.equal(item.can_comment, true, scenario.name);
    assert.equal(item.can_approve, false, scenario.name);
    assert.equal(item.cycle_status, scenario.cycles[3], scenario.name);
    assert.equal(item.parent_title, 'Objective cha');
    assert.deepEqual(calls.find((call) => call.path === '/api/okr/cycles/').query, { page_size: '100' });
  }
});

test('key result create/update/delete bodies and paths', async () => {
  const { JoyOKR, calls } = setup({
    'POST /api/okr/key-results/': { status: 201, body: keyResult() },
    'PUT /api/okr/key-results/21/': { body: keyResult() },
    'DELETE /api/okr/key-results/21/': { body: { message: 'Đã xóa Key Result.' } },
  });
  const form = {
    objective_id: '12', title: 'KR', description: 'D', owner_user_id: '7', start_value: '0', current_value: '0',
    target_value: '10', unit: 'game', weight: '50', priority: 'medium', due_date: '2026-12-01',
  };
  await JoyOKR.api.request('key-results', { method: 'POST', body: form });
  await JoyOKR.api.request('key-results/21', { method: 'PUT', body: form });
  await JoyOKR.api.request('key-results/21', { method: 'DELETE' });
  assert.deepEqual(calls[0].body, {
    objective_id: 12, title: 'KR', description: 'D', unit: 'game', start_value: '0', current_value: '0', target_value: '10',
    weight: '50', priority: 'medium', due_date: '2026-12-01', owner_id: 7,
  });
  assert.equal(calls[1].path, '/api/okr/key-results/21/');
  assert.equal(calls[1].method, 'PUT');
  assert.equal(calls[2].method, 'DELETE');
  calls.forEach((call) => assert.equal(call.headers['X-CSRFToken'], 'csrf-token-123'));
});

test('key result detail: can_checkin / can_add_evidence from permissions, can_edit locked by the objective status', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/key-results/21/': { body: keyResult({ permissions: { can_checkin: true, can_add_evidence: true } }) },
    'GET /api/okr/objectives/12/': { body: objectiveDetail({ approval_status: 'submitted' }) },
  });
  const item = await JoyOKR.api.request('key-results/21');
  assert.deepEqual(calls.map((call) => call.path), ['/api/okr/key-results/21/', '/api/okr/objectives/12/']);
  assert.equal(item.can_checkin, true);
  assert.equal(item.can_add_evidence, true);
  assert.equal(item.objective_approval_status, 'submitted');
  assert.equal(item.can_edit, false, 'a KR of a submitted objective is locked for non-admins');
  assert.equal(item.owner_user_id, 7);
  assert.equal(item.owner_name, 'Nguyễn Văn A');
});

test('check-ins: list maps authors; create posts to /checkins/ with a 32-hex client_request_id', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/checkins/': { body: page([{ id: 4, created_at: '2026-11-05T09:30:12.345678+07:00', author: owner(), confidence: 4, update_text: 'x' }]) },
    'POST /api/okr/checkins/': { status: 201, body: { checkin: { id: 5 }, key_result: keyResult() } },
  });
  const list = await JoyOKR.api.request('key-results/21/checkins');
  assert.deepEqual(calls[0].query, { key_result: '21', page_size: '100' });
  assert.equal(list[0].user_name, 'Nguyễn Văn A');

  await JoyOKR.api.request('key-results/21/checkins', {
    method: 'POST',
    body: { current_value: ' 7.5 ', confidence: '4', update_text: 'Đã làm', blockers: 'Không', next_steps: 'Tiếp', client_request_id: 'a'.repeat(32) },
  });
  assert.equal(calls[1].path, '/api/okr/checkins/');
  assert.equal(calls[1].headers['X-CSRFToken'], 'csrf-token-123');
  assert.deepEqual(calls[1].body, {
    key_result: 21, current_value: '7.5', confidence: 4, update_text: 'Đã làm', client_request_id: 'a'.repeat(32), blockers: 'Không', next_steps: 'Tiếp',
  });

  await JoyOKR.api.request('key-results/21/checkins', { method: 'POST', body: { current_value: '1', confidence: '3', update_text: 'u' } });
  assert.match(calls[2].body.client_request_id, /^[0-9a-f]{32}$/);
  assert.notEqual(JoyOKR.api.newClientRequestId(), JoyOKR.api.newClientRequestId());
});

test('comments and activity use objective= / key_result= instead of object_type/object_id', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/comments/': { body: page([{ id: 1, content: 'Hi', created_at: '2026-11-05T09:30:00+07:00', author: owner(), parent_id: null }]) },
    'POST /api/okr/comments/': { status: 201, body: { id: 2, content: 'Mới' } },
    'GET /api/okr/activity/': { body: page([{ id: 3, action: 'checked_in', created_at: '2026-11-05T09:30:00+07:00', actor: owner(), summary: 'Check-in: 10% → 20%', key_result: null }]) },
  });
  const comments = await JoyOKR.api.request('comments', { query: { object_type: 'objective', object_id: '12' } });
  await JoyOKR.api.request('comments', { method: 'POST', body: { object_type: 'key_result', object_id: '21', content: 'Mới' } });
  const activity = await JoyOKR.api.request('activity', { query: { object_type: 'key_result', object_id: 21 } });
  assert.deepEqual(calls[0].query, { objective: '12', page_size: '100' });
  assert.deepEqual(calls[1].body, { key_result: 21, content: 'Mới' });
  assert.equal(calls[1].headers['X-CSRFToken'], 'csrf-token-123');
  assert.deepEqual(calls[2].query, { key_result: '21', page_size: '100' });
  assert.equal(comments[0].user_name, 'Nguyễn Văn A');
  assert.equal(activity[0].actor_name, 'Nguyễn Văn A');
  assert.equal(activity[0].summary, 'Check-in: 10% → 20%');

  const bad = await rejection(JoyOKR.api.request('comments', { query: { object_type: 'cycle', object_id: '1' } }));
  assert.equal(bad.status, 400);
  assert.equal(calls.length, 3, 'an unknown target type never reaches the server');
});

test('evidence: list keeps only http(s) links and authorised download paths', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/evidence/': {
      body: page([
        { id: 1, evidence_type: 'url', label: 'Báo cáo', url: 'https://docs.example.com/a?b=1', created_at: '2026-11-05T09:30:00+07:00', author: owner() },
        { id: 2, evidence_type: 'url', label: 'XSS', url: 'javascript:alert(1)', created_at: '2026-11-05T09:30:00+07:00', author: owner() },
        { id: 3, evidence_type: 'file', label: 'Ảnh', file_name: 'a.png', file_size: 10, download_path: '/okr/evidence/3/download/', created_at: '2026-11-05T09:30:00+07:00', author: owner() },
        { id: 4, evidence_type: 'file', label: 'Lạ', file_name: 'b.png', file_size: 10, download_path: '//evil.example/okr/evidence/4/download/', created_at: '2026-11-05T09:30:00+07:00', author: owner() },
        { id: 5, evidence_type: 'file', label: 'Media', file_name: 'c.png', file_size: 10, download_path: '/media/okr/c.png', created_at: '2026-11-05T09:30:00+07:00', author: owner() },
      ]),
    },
  });
  const items = await JoyOKR.api.request('evidence', { query: { key_result_id: '21' } });
  assert.deepEqual(calls[0].query, { key_result: '21', page_size: '100' });
  assert.deepEqual(plain(items.map((item) => item.url)), [
    'https://docs.example.com/a?b=1',
    '',
    '/api/okr/evidence/3/download/',
    '',
    '',
  ]);
  assert.equal(items[2].evidence_type, 'file');
  assert.equal(items[0].user_name, 'Nguyễn Văn A');
});

test('evidence: a link is JSON, a file is multipart, and a non-http link never leaves the browser', async () => {
  const { JoyOKR, calls } = setup({ 'POST /api/okr/evidence/': { status: 201, body: { id: 9, evidence_type: 'url', label: 'L', url: 'https://x.example/' } } });

  const linkForm = new FormData();
  linkForm.append('key_result_id', '21');
  linkForm.append('label', 'Tài liệu');
  linkForm.append('url', 'https://x.example/doc');
  linkForm.append('file', new File([], ''));
  const created = await JoyOKR.api.request('evidence', { method: 'POST', body: linkForm });
  assert.equal(calls[0].headers['Content-Type'], 'application/json');
  assert.equal(calls[0].headers['X-CSRFToken'], 'csrf-token-123');
  assert.deepEqual(calls[0].body, { key_result: 21, evidence_type: 'url', url: 'https://x.example/doc', label: 'Tài liệu' });
  assert.equal(created.message, 'Đã thêm bằng chứng.');

  const fileForm = new FormData();
  fileForm.append('key_result_id', '21');
  fileForm.append('label', 'Ảnh chụp');
  fileForm.append('url', '');
  fileForm.append('file', new File([new Uint8Array([0x89, 0x50])], 'shot.png', { type: 'image/png' }));
  await JoyOKR.api.request('evidence', { method: 'POST', body: fileForm });
  const multipart = calls[1].rawBody;
  assert.ok(multipart instanceof FormData, 'file evidence is sent as FormData');
  assert.equal(calls[1].headers['Content-Type'], undefined, 'the browser sets the multipart boundary');
  assert.equal(calls[1].headers['X-CSRFToken'], 'csrf-token-123');
  assert.equal(multipart.get('key_result'), '21');
  assert.equal(multipart.get('evidence_type'), 'file');
  assert.equal(multipart.get('label'), 'Ảnh chụp');
  assert.equal(multipart.get('file').name, 'shot.png');

  for (const url of ['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>', 'vbscript:msgbox(1)', '//evil.example/x']) {
    const error = await rejection(JoyOKR.api.request('evidence', { method: 'POST', body: { key_result_id: '21', label: 'x', url } }));
    assert.equal(error.status, 400, url);
    assert.ok(error.fields.url, url);
  }
  assert.equal(calls.length, 2, 'rejected links make no request');
});

test('notifications: list, unread_count, reference routes, read and read-all', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/notifications/': {
      body: page([
        { id: 5, type: 'approval_requested', title: 'Chờ duyệt', message: 'm', is_read: false, created_at: '2026-11-05T09:30:00+07:00', reference: { type: 'objective', id: 12, available: true } },
        { id: 6, type: 'comment', title: 'Bình luận', message: 'm', is_read: true, created_at: '2026-11-05T09:30:00+07:00', reference: { type: 'key_result', id: 21, available: true } },
        { id: 7, type: 'approved', title: 'Đã duyệt', message: '', is_read: false, created_at: '2026-11-05T09:30:00+07:00', reference: { type: 'objective', id: 13, available: false } },
      ], { count: 3, unread_count: 2 }),
    },
    'POST /api/okr/notifications/5/read/': { body: { message: 'Đã đọc thông báo.' } },
    'POST /api/okr/notifications/read-all/': { body: { message: 'Đã đọc tất cả thông báo.', count: 2 } },
  });
  const result = await JoyOKR.api.request('notifications', { query: { per_page: 1 } });
  assert.deepEqual(calls[0].query, { page_size: '1' });
  assert.equal(result.unread, 2);
  assert.equal(result.total, 3);
  assert.deepEqual(plain(result.items.map((item) => item.reference_route)), ['admin/approvals', 'key-results/21', '']);
  assert.deepEqual(plain(result.items.map((item) => item.reference_state)), ['', '', 'missing']);
  assert.deepEqual(plain(result.items.map((item) => item.is_read)), [0, 1, 0]);

  await JoyOKR.api.request('notifications/5/read', { method: 'PUT', body: {} });
  await JoyOKR.api.request('notifications/read-all', { method: 'POST', body: {} });
  assert.equal(calls[1].method, 'POST');
  assert.equal(calls[1].path, '/api/okr/notifications/5/read/');
  assert.equal(calls[2].method, 'POST');
  assert.equal(calls[2].path, '/api/okr/notifications/read-all/');
  assert.equal(calls[1].headers['X-CSRFToken'], 'csrf-token-123');
});

test('approval queue: count -> total, KR count, block reason and title search', async () => {
  const entry = (id, title, canApprove, krs = 2) => ({
    objective: objectiveSummary({ id, title, approval_status: 'submitted' }),
    submitted_at: '2026-11-04T08:00:00+07:00',
    key_results: Array.from({ length: krs }, (_, index) => ({ title: `KR ${index}`, weight: '50.00' })),
    checks: { key_result_count_ok: true, weight_total: '100.00', weight_ok: true, alignment_ok: canApprove },
    can_approve: canApprove,
    block_reason: canApprove ? null : 'Objective cần liên kết với một Objective cấp trên.',
  });
  const { JoyOKR, calls } = setup({ 'GET /api/okr/approvals/': { body: page([entry(12, 'Tăng Doanh Thu', true), entry(13, 'Giảm chi phí', false, 3)]) } });
  const all = await JoyOKR.api.request('approvals');
  assert.deepEqual(calls[0].query, { page_size: '100' });
  assert.equal(all.total, 2);
  assert.equal(all.objective_count, 2);
  assert.equal(all.key_result_count, 5);
  assert.equal(all.items[1].can_approve, false);
  assert.match(all.items[1].block_reason, /liên kết/);
  assert.equal(all.items[0].updated_at, '2026-11-04T08:00:00+07:00');

  const filtered = await JoyOKR.api.request('approvals', { query: { search: 'doanh thu' } });
  assert.deepEqual(plain(filtered.items.map((item) => item.id)), [12]);
  assert.equal(filtered.queue_total, 2, 'the nav badge keeps the whole queue size');
});

test('company dashboard maps to the JOYOKR dashboard shape (with drill-down lists)', async () => {
  const dashboard = {
    cycle: { id: 3, name: 'Q4 2026', cycle_type: 'quarter', start_date: '2026-10-01', end_date: '2026-12-31', status: 'active' },
    progress: { value: '42.50', objective_count: 2, total_weight: '100.00' },
    expected_progress: '50.00',
    objective_stats: { total: 2, on_track: 1, at_risk: 0, off_track: 1, completed: 0 },
    key_result_count: 4,
    stale_count: 1,
    attention: [
      { key_result_id: 21, key_result_title: 'KR cũ', objective_title: 'O', reason: 'stale', progress: '10.00', expected_progress: '50.00', days_overdue: 3, last_checkin_at: null, owner: owner() },
      { key_result_id: 22, key_result_title: 'KR chậm', objective_title: 'O', reason: 'off_track', progress: '5.00', expected_progress: '50.00', days_overdue: 0, last_checkin_at: '2026-11-01T08:00:00+07:00', owner: owner(8, 'B') },
    ],
    teams: [{ id: 4, name: 'Team A', objective_count: 1, progress: '30.00', health: 'at_risk' }],
    featured: [objectiveSummary({ approval_status: 'approved', health: 'on_track' })],
  };
  const { JoyOKR, calls } = setup({
    'GET /api/okr/dashboard/company/': { body: dashboard },
    'GET /api/okr/objectives/': { body: page([objectiveSummary({ id: 1, approval_status: 'approved', health: 'on_track' }), objectiveSummary({ id: 2, approval_status: 'approved', health: 'off_track' })]) },
  });
  const result = await JoyOKR.api.request('dashboard', { query: { cycle_id: '3', unrelated: 'x' } });
  assert.deepEqual(calls[0].query, { cycle: '3' });
  assert.equal(calls[0].path, '/api/okr/dashboard/company/');
  assert.deepEqual(calls[1].query, { cycle: '3', status: 'approved', page_size: '100' });
  assert.equal(result.progress, '42.50');
  assert.match(result.progress_definition.formula, /Công ty đã duyệt/);
  assert.equal(result.stale_checkins, 1);
  assert.equal(result.active_team_count, 1);
  assert.equal(result.objectives[0].owner_name, 'Nguyễn Văn A');
  assert.deepEqual(plain(result.attention_key_results.map((item) => [item.id, item.health, item.checkin_overdue, item.owner_name])), [
    [21, 'off_track', true, 'Nguyễn Văn A'],
    [22, 'off_track', false, 'B'],
  ]);
  assert.deepEqual(plain(result.stale_checkin_items.map((item) => item.id)), [21]);
  assert.deepEqual(plain(result.objective_details.all.map((item) => item.id)), [1, 2]);
  assert.deepEqual(plain(result.objective_details.off_track.map((item) => item.id)), [2]);

  const { JoyOKR: lite, calls: liteCalls } = setup({ 'GET /api/okr/dashboard/company/': { body: dashboard } });
  await lite.api.request('dashboard', { query: {}, summaryOnly: true });
  assert.equal(liteCalls.length, 1, 'the Teams page skips the drill-down request');
});

test('reports, tree, check-in queue and management tree endpoints', async () => {
  const report = {
    cycle: { id: 3, name: 'Q4' }, progress: { value: '40.00', objective_count: 1, total_weight: '100.00' }, expected_progress: '50.00',
    objective_stats: {}, key_result_count: 2, stale_count: 1, rows: [objectiveSummary()], by_scope: [{ label: 'company', count: 1, progress: '40.00' }],
    by_owner: [], trend: [{ date: '2026-11-01', progress: '20.00' }],
    checkin_attention_by_owner: [{ owner: owner(), overdue_count: 1, off_track_count: 0, items: [{ key_result_id: 21, key_result_title: 'KR', objective_title: 'O', reason: 'stale', progress: '10.00', expected_progress: '50.00', days_overdue: 4, last_checkin_at: null, owner: owner() }] }],
  };
  const tree = {
    cycle: { id: 3, name: 'Q4' }, expected_progress: '50.00',
    items: [{ ...objectiveSummary({ scope: 'company' }), key_results: [{ id: 21, title: 'KR', weight: '50.00', progress: '10.00', health: null, approval_status: 'draft', owner: owner() }], children: [{ ...objectiveSummary({ id: 13 }), key_results: [], children: [] }] }],
    unlinked_items: [],
  };
  const management = {
    cycle: { id: 3, name: 'Q4' }, can_manage: true, scope: 'managed', summary: { employee_count: 1 }, expected_progress: '50.00',
    departments: [{ id: 1, name: 'Kinh doanh', teams: [{ id: null, name: 'Chưa thuộc team', people: [{ employee: owner(), objectives: [{ id: 12, title: 'O', scope: 'personal', progress: '10.00', key_results: [{ id: 21, title: 'KR', checkins: [{ id: 1, author: owner() }] }] }] }] }] }],
    unassigned: [],
  };
  const { JoyOKR, calls } = setup({
    'GET /api/okr/reports/': { body: report },
    'GET /api/okr/tree/': { body: tree },
    'GET /api/okr/checkins/queue/': { body: page([{ ...keyResult(), latest_checkin: { id: 1, created_at: '2026-11-01T08:00:00+07:00', author: owner() } }]) },
    'GET /api/okr/checkins/management/': { body: management },
  });
  const mappedReport = await JoyOKR.api.request('reports', { query: { cycle_id: '3', scope: 'company', department_id: '1', team_id: '2', owner_user_id: '7' } });
  const mappedTree = await JoyOKR.api.request('tree', { query: { cycle_id: '3' } });
  const queue = await JoyOKR.api.request('checkin-queue', { query: { cycle_id: '3' } });
  const mappedManagement = await JoyOKR.api.request('checkin-management-tree', { query: {} });

  assert.deepEqual(calls[0].query, { cycle: '3', scope: 'company', department: '1', team: '2', owner: '7' });
  assert.equal(mappedReport.summary.progress, '40.00');
  assert.equal(mappedReport.summary.stale_checkins, 1);
  assert.equal(mappedReport.rows[0].owner_name, 'Nguyễn Văn A');
  assert.equal(mappedReport.checkin_attention_by_owner[0].owner_name, 'Nguyễn Văn A');
  assert.equal(mappedReport.checkin_attention_by_owner[0].items[0].overdue_days, 4);
  assert.deepEqual(calls[1].query, { cycle: '3' });
  assert.equal(mappedTree.items[0].children[0].id, 13);
  assert.equal(mappedTree.items[0].key_results[0].owner_name, 'Nguyễn Văn A');
  assert.equal(mappedTree.items[0].key_results[0].expected_progress, '50.00');
  assert.deepEqual(calls[2].query, { cycle: '3', page_size: '100' });
  assert.equal(queue[0].latest_checkin.user_name, 'Nguyễn Văn A');
  assert.deepEqual(calls[3].query, {});
  const person = mappedManagement.departments[0].teams[0].people[0];
  assert.equal(person.display_name, 'Nguyễn Văn A');
  assert.equal(person.id, 7);
  assert.equal(mappedManagement.departments[0].teams[0].id, 0);
});

test('pickers: cycles, teams, departments and people map to JOYOKR names', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/cycles/': { body: cyclesPage({ 3: 'active', 4: 'draft' }) },
    'GET /api/okr/teams/': { body: page([{ id: 4, name: 'Team A', description: '', department_id: 1, department_name: 'KD', manager: owner(8, 'Quản lý'), status: 'active', member_count: 5 }]) },
    'GET /api/okr/departments/': { body: page([{ id: 1, name: 'Kinh doanh', okr_manager: null }]) },
    'GET /api/okr/people/': { body: page([{ id: 7, name: 'Nguyễn Văn A', department_id: 1, department_name: 'KD', manager_id: null }]) },
  });
  const cycles = await JoyOKR.api.request('cycles', { query: { per_page: 100, page: 1 } });
  const teams = await JoyOKR.api.request('teams', { query: { status: 'active', per_page: 100 } });
  const departments = await JoyOKR.api.request('departments', { query: { status: 'active', per_page: 100 } });
  const people = await JoyOKR.api.request('people', { query: { search: 'An', per_page: 100 } });
  assert.deepEqual(calls.map((call) => call.query), [
    { page: '1', page_size: '100' },
    { page_size: '100', status: 'active' },
    { page_size: '100' },
    { page_size: '100', q: 'An' },
  ]);
  assert.equal(cycles.items.length, 2);
  assert.equal(teams.items[0].manager_name, 'Quản lý');
  assert.equal(teams.items[0].member_count, 5);
  assert.equal(departments.items[0].manager_name, '');
  assert.equal(people.items[0].display_name, 'Nguyễn Văn A');
});

test('errors: Django {code, message, fields} becomes ApiError with JOYOKR field names', async () => {
  const { JoyOKR } = setup({
    'POST /api/okr/objectives/': { status: 400, body: { code: 'validation_error', message: 'Dữ liệu chưa hợp lệ.', fields: { owner_id: 'Owner không hợp lệ.', parent_id: 'Sai cấp.', title: 'Tên Objective là bắt buộc.' } } },
    'POST /api/okr/objectives/12/submit/': { status: 400, body: { code: 'invalid_weight', message: 'Tổng trọng số Key Result phải bằng 100%.', fields: {} } },
    'DELETE /api/okr/objectives/12/': { status: 409, body: { code: 'objective_locked', message: 'Objective đang chờ duyệt.', fields: {} } },
    'GET /api/okr/objectives/99/': { status: 404, body: { code: 'deleted', message: 'Objective đã bị xóa.', fields: {} } },
  });
  const validation = await rejection(JoyOKR.api.request('objectives', { method: 'POST', body: { title: '' } }));
  assert.equal(validation.name, 'ApiError');
  assert.ok(validation instanceof JoyOKR.api.ApiError);
  assert.equal(validation.status, 400);
  assert.equal(validation.message, 'Dữ liệu chưa hợp lệ.');
  assert.equal(validation.code, 'validation_error');
  assert.deepEqual(plain(validation.fields), { owner_user_id: 'Owner không hợp lệ.', parent_objective_id: 'Sai cấp.', title: 'Tên Objective là bắt buộc.' });
  assert.deepEqual(plain(validation.data), { code: 'validation_error', status: 400, fields: plain(validation.fields) });

  const rule = await rejection(JoyOKR.api.request('objectives/12/submit', { method: 'POST' }));
  assert.equal(rule.data.code, 'invalid_weight');
  const conflict = await rejection(JoyOKR.api.request('objectives/12', { method: 'DELETE' }));
  assert.equal(conflict.status, 409);
  const deleted = await rejection(JoyOKR.api.request('objectives/99'));
  assert.equal(deleted.status, 404);
  assert.equal(deleted.data.code, 'deleted');
});

test('401 sends the browser to the HR login with next=<current path>, once', async () => {
  const location = makeLocation('/okr/objectives/12', '?page=2');
  const { JoyOKR, calls } = setup({ '*': { status: 401, body: { code: 'not_authenticated', message: 'Vui lòng đăng nhập.', fields: {} } } }, { location });
  const first = await rejection(JoyOKR.api.request('objectives', { query: {} }));
  const second = await rejection(JoyOKR.api.request('notifications', { query: {} }));
  assert.equal(first.status, 401);
  assert.equal(second.status, 401);
  assert.equal(first.data.code, 'not_authenticated');
  assert.deepEqual(location.assigned, ['/login/?next=%2Fokr%2Fobjectives%2F12%3Fpage%3D2']);
  assert.equal(calls.length, 2);
});

test('403 csrf_failed surfaces the server message and is not retried or redirected', async () => {
  const location = makeLocation('/okr/my-okrs');
  const message = 'Phiên làm việc thiếu mã bảo vệ hoặc đã hết hạn. Vui lòng tải lại trang.';
  const { JoyOKR, calls } = setup({ 'POST /api/okr/comments/': { status: 403, body: { code: 'csrf_failed', message, fields: {} } } }, { location });
  const error = await rejection(JoyOKR.api.request('comments', { method: 'POST', body: { object_type: 'objective', object_id: 12, content: 'x' } }));
  assert.equal(error.status, 403);
  assert.equal(error.data.code, 'csrf_failed');
  assert.equal(error.message, message);
  assert.equal(calls.length, 1, 'no retry loop');
  assert.deepEqual(location.assigned, [], 'no redirect or reload');
});

test('timeouts (15 s by default) abort the request and raise request_timeout', async () => {
  const { JoyOKR, calls } = setup({ 'GET /api/okr/objectives/': 'hang' }, { config: makeConfig({ apiTimeoutMs: 25 }) });
  const started = Date.now();
  const error = await rejection(JoyOKR.api.request('objectives'));
  assert.equal(error.status, 408);
  assert.equal(error.data.code, 'request_timeout');
  assert.ok(calls[0].signal.aborted, 'the fetch is aborted');
  assert.ok(Date.now() - started < 2000);

  // Without apiTimeoutMs the timer is JOYOKR's 15 s (fired early here).
  const delays = [];
  const fastTimeout = (callback, ms) => { delays.push(ms); return setTimeout(callback, 0); };
  const { JoyOKR: defaults } = load(API_FILES, { routes: { 'GET /api/okr/objectives/': 'hang' }, extra: { setTimeout: fastTimeout } });
  const defaultError = await rejection(defaults.api.request('objectives'));
  assert.equal(defaultError.status, 408);
  assert.deepEqual(delays, [15000]);
});

test('network failures and non-JSON error pages become ApiError', async () => {
  const { JoyOKR } = setup({
    'GET /api/okr/objectives/': 'network-error',
    'GET /api/okr/tree/': { status: 502, raw: '<html>Bad gateway</html>' },
  });
  const network = await rejection(JoyOKR.api.request('objectives'));
  assert.equal(network.status, 0);
  assert.equal(network.data.code, 'network_error');
  const gateway = await rejection(JoyOKR.api.request('tree'));
  assert.equal(gateway.status, 502);
  assert.equal(gateway.data.code, 'invalid_response');
});

test('endpoints Django does not have are rejected without a network call', async () => {
  const { JoyOKR, calls } = setup({ '*': { body: {} } });
  const unsupported = [
    ['users', 'GET'], ['users/3', 'PUT'], ['users/3/activate', 'POST'], ['me', 'GET'], ['profile', 'PUT'], ['password', 'PUT'],
    ['profile/avatar', 'POST'], ['seed-demo', 'POST'], ['system-status', 'GET'], ['notifications/sync', 'POST'],
    ['security/transfer-admin', 'POST'], ['key-results/3/submit', 'POST'], ['key-results/3/approve', 'POST'],
    ['key-results', 'GET'], ['chat/messages', 'GET'], ['objectives/1/../../users', 'GET'], ['objectives/abc', 'GET'],
    // HR owns departments: no create, hide or restore. Cycles are never deleted.
    ['departments', 'POST'], ['departments/3', 'DELETE'], ['departments/3/restore', 'POST'], ['cycles/3', 'DELETE'],
    ['roles/3', 'PUT'], ['roles/history', 'POST'], ['settings', 'POST'], ['teams/4/members', 'POST'],
  ];
  for (const [endpoint, method] of unsupported) {
    const error = await rejection(JoyOKR.api.request(endpoint, { method, body: {} }));
    assert.equal(error.status, 501, `${method} ${endpoint}`);
    assert.equal(error.data.code, 'unsupported_endpoint', `${method} ${endpoint}`);
  }
  assert.equal(calls.length, 0);

  const traversal = await rejection(JoyOKR.api.send('GET', '../../admin/'));
  assert.equal(traversal.data.code, 'invalid_path');
  assert.equal(calls.length, 0);
});

test('no credentials or tokens are stored in web storage', () => {
  const touched = [];
  const storage = new Proxy({}, { get(_target, key) { touched.push(String(key)); return () => null; } });
  const { JoyOKR } = load(API_FILES, { extra: { localStorage: storage, sessionStorage: storage } });
  assert.ok(JoyOKR.api);
  assert.deepEqual(touched, []);
});
