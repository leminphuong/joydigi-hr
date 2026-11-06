// W3 admin endpoints in the JoyOKR.api adapter: cycles, teams, departments
// (OKR manager only), OKR roles and settings -> Django /api/okr/, plus the
// configured at-risk threshold driving client-side health.
import test from 'node:test';
import assert from 'node:assert/strict';
import { load, makeConfig, owner, page, plain } from './harness.mjs';

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

function cycle(overrides = {}) {
  return { id: 5, name: 'Q1 2027', cycle_type: 'quarter', start_date: '2027-01-01', end_date: '2027-03-31', status: 'draft', parent_id: 2, ...overrides };
}

function team(overrides = {}) {
  return {
    id: 4, name: 'Team A', description: 'Mô tả', department_id: 1, department_name: 'Kinh doanh', manager: owner(8, 'Trần Quản Lý'),
    status: 'active', member_count: 2, members: [owner(7, 'Nguyễn Văn A'), owner(9, 'Lê Thị B')], ...overrides,
  };
}

const RATING = [
  { min: '0.00', max: '0.30', label: 'Chưa đạt' },
  { min: '0.30', max: '0.70', label: 'Cần cố gắng' },
  { min: '0.70', max: '0.90', label: 'Đạt' },
  { min: '0.90', max: '1.00', label: 'Xuất sắc' },
];

function settingsPayload(overrides = {}) {
  return {
    warning_threshold: '15.00', reminder_frequency: 'weekly', email_reminders: false, rating_ranges: RATING,
    reminder_schedule: { scheduled: false, command: 'okr_checkin_reminders', channels: ['in_app'], email_available: false },
    ...overrides,
  };
}

test('cycles: list keeps objective_count and parent_id; "all" or empty status is omitted', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/cycles/': { body: page([cycle({ objective_count: 4 }), cycle({ id: 6, parent_id: null, objective_count: 0, status: 'closed' })]) },
  });
  const all = await JoyOKR.api.request('cycles', { query: { status: 'all', per_page: 100 } });
  await JoyOKR.api.request('cycles', { query: { status: '', per_page: 100 } });
  await JoyOKR.api.request('cycles', { query: { status: 'closed', cycle_type: 'year', per_page: 100 } });
  await JoyOKR.api.request('cycles', { query: { status: '<x>', cycle_type: 'week' } });
  assert.deepEqual(calls.map((call) => call.query), [
    { page_size: '100' },
    { page_size: '100' },
    { page_size: '100', status: 'closed', cycle_type: 'year' },
    { page_size: '20' },
  ]);
  assert.equal(all.items[0].objective_count, 4);
  assert.equal(all.items[0].parent_id, 2);
  assert.equal(all.items[1].objective_count, 0);
  assert.equal(all.items[1].parent_id, null);
});

test('cycles: create/update send Django names, parent 0 or "" becomes null, with CSRF', async () => {
  const { JoyOKR, calls } = setup({
    'POST /api/okr/cycles/': { status: 201, body: cycle({ parent_id: null }) },
    'GET /api/okr/cycles/5/': { body: cycle() },
    'PUT /api/okr/cycles/5/': { body: cycle() },
  });
  const form = { name: ' Q1 2027 ', cycle_type: 'quarter', start_date: '2027-01-01', end_date: '2027-03-31' };
  const created = await JoyOKR.api.request('cycles', { method: 'POST', body: { ...form, parent_id: '0' } });
  await JoyOKR.api.request('cycles/5', { method: 'PUT', body: { ...form, parent_id: '' } });
  await JoyOKR.api.request('cycles/5', { method: 'PUT', body: { ...form, parent_id: 0 } });
  await JoyOKR.api.request('cycles/5', { method: 'PUT', body: { ...form, parent_id: '2' } });
  const detail = await JoyOKR.api.request('cycles/5');

  const writes = calls.filter((call) => call.method !== 'GET');
  assert.deepEqual(writes.map((call) => `${call.method} ${call.path}`), [
    'POST /api/okr/cycles/', 'PUT /api/okr/cycles/5/', 'PUT /api/okr/cycles/5/', 'PUT /api/okr/cycles/5/',
  ]);
  const expected = { name: 'Q1 2027', cycle_type: 'quarter', start_date: '2027-01-01', end_date: '2027-03-31' };
  assert.deepEqual(writes[0].body, { ...expected, parent_id: null });
  assert.deepEqual(writes[1].body, { ...expected, parent_id: null });
  assert.deepEqual(writes[2].body, { ...expected, parent_id: null });
  assert.deepEqual(writes[3].body, { ...expected, parent_id: 2 });
  writes.forEach((call) => assert.equal(call.headers['X-CSRFToken'], 'csrf-token-123'));
  assert.equal(created.id, 5);
  assert.equal(detail.parent_id, 2);
});

test('cycles: PUT or POST cycles/{id}/status -> POST status/; clone -> POST clone/', async () => {
  const { JoyOKR, calls } = setup({
    'POST /api/okr/cycles/5/status/': ({ body }) => ({ body: cycle({ status: body.status }) }),
    'POST /api/okr/cycles/3/clone/': { body: { message: 'Đã sao chép OKR sang chu kỳ mới.', objective_count: 7, destination_cycle_id: 5 } },
  });
  const activated = await JoyOKR.api.request('cycles/5/status', { method: 'PUT', body: { status: 'active' } });
  await JoyOKR.api.request('cycles/5/status', { method: 'POST', body: { status: 'closed' } });
  const cloned = await JoyOKR.api.request('cycles/3/clone', { method: 'POST', body: { destination_cycle_id: '5' } });
  assert.deepEqual(calls.map((call) => [call.method, call.path, call.body]), [
    ['POST', '/api/okr/cycles/5/status/', { status: 'active' }],
    ['POST', '/api/okr/cycles/5/status/', { status: 'closed' }],
    ['POST', '/api/okr/cycles/3/clone/', { destination_cycle_id: 5 }],
  ]);
  assert.equal(activated.status, 'active');
  assert.equal(cloned.message, 'Đã sao chép OKR sang chu kỳ mới.');
  assert.equal(cloned.objective_count, 7);
  assert.equal(cloned.destination_cycle_id, 5);
});

test('cycles: 400 cycle_closed and validation fields reach the caller', async () => {
  const closed = 'Chu kỳ đã đóng. Hãy mở lại chu kỳ trước khi sửa.';
  const { JoyOKR } = setup({
    'PUT /api/okr/cycles/5/': { status: 400, body: { code: 'cycle_closed', message: closed, fields: {} } },
    'POST /api/okr/cycles/': { status: 400, body: { code: 'validation_error', message: 'Dữ liệu chưa hợp lệ.', fields: { start_date: 'Chu kỳ cùng loại đang bị trùng thời gian.', parent_id: 'Chu kỳ cha không hợp lệ.' } } },
  });
  const error = await rejection(JoyOKR.api.request('cycles/5', { method: 'PUT', body: cycle() }));
  assert.equal(error.status, 400);
  assert.equal(error.code, 'cycle_closed');
  assert.equal(error.message, closed);
  const invalid = await rejection(JoyOKR.api.request('cycles', { method: 'POST', body: cycle() }));
  assert.deepEqual(plain(invalid.fields), { start_date: 'Chu kỳ cùng loại đang bị trùng thời gian.', parent_id: 'Chu kỳ cha không hợp lệ.' });
});

test('teams: list maps members to {id, display_name}; "all" omits status', async () => {
  const { JoyOKR, calls } = setup({ 'GET /api/okr/teams/': { body: page([team(), team({ id: 5, manager: null, members: undefined, member_count: 3, status: 'inactive' })]) } });
  const all = await JoyOKR.api.request('teams', { query: { status: 'all', per_page: 100 } });
  await JoyOKR.api.request('teams', { query: { status: 'inactive', per_page: 100 } });
  await JoyOKR.api.request('teams', { query: { per_page: 100 } });
  assert.deepEqual(calls.map((call) => call.query), [{ page_size: '100' }, { page_size: '100', status: 'inactive' }, { page_size: '100' }]);
  assert.deepEqual(plain(all.items[0].members), [{ id: 7, display_name: 'Nguyễn Văn A' }, { id: 9, display_name: 'Lê Thị B' }]);
  assert.equal(all.items[0].manager_user_id, 8);
  assert.equal(all.items[0].manager_name, 'Trần Quản Lý');
  assert.deepEqual(plain(all.items[1].members), []);
  assert.equal(all.items[1].member_count, 3);
  assert.equal(all.items[1].manager_name, '');
  assert.equal(all.items[1].status, 'inactive');
});

test('teams: create/update rename manager_user_id, hide/restore use status/, members send employee_ids', async () => {
  const { JoyOKR, calls } = setup({
    'POST /api/okr/teams/': { status: 201, body: team() },
    'GET /api/okr/teams/4/': { body: team() },
    'PUT /api/okr/teams/4/': { body: team() },
    'POST /api/okr/teams/4/status/': ({ body }) => ({ body: team({ status: body.status }) }),
    'PUT /api/okr/teams/4/members/': { body: team() },
  });
  await JoyOKR.api.request('teams', { method: 'POST', body: { name: ' Team A ', description: 'Mô tả', department_id: '1', manager_user_id: '8' } });
  await JoyOKR.api.request('teams/4', { method: 'PUT', body: { name: 'Team A', description: '', department_id: '', manager_user_id: '' } });
  const hidden = await JoyOKR.api.request('teams/4', { method: 'DELETE' });
  const restored = await JoyOKR.api.request('teams/4/restore', { method: 'POST' });
  const members = await JoyOKR.api.request('teams/4/members', { method: 'PUT', body: { user_ids: [7, '9', 7, 'x', 0] } });
  const detail = await JoyOKR.api.request('teams/4');

  assert.deepEqual(calls.filter((call) => call.method !== 'GET').map((call) => [call.method, call.path, call.body]), [
    ['POST', '/api/okr/teams/', { name: 'Team A', description: 'Mô tả', department_id: 1, manager_id: 8 }],
    ['PUT', '/api/okr/teams/4/', { name: 'Team A', description: '', department_id: null, manager_id: null }],
    ['POST', '/api/okr/teams/4/status/', { status: 'inactive' }],
    ['POST', '/api/okr/teams/4/status/', { status: 'active' }],
    ['PUT', '/api/okr/teams/4/members/', { employee_ids: [7, 9] }],
  ]);
  assert.equal(hidden.status, 'inactive');
  assert.equal(hidden.message, 'Đã ẩn team.');
  assert.equal(restored.status, 'active');
  assert.equal(restored.message, 'Đã khôi phục team.');
  assert.equal(members.members.length, 2);
  assert.equal(detail.members[1].display_name, 'Lê Thị B');
});

test('teams and departments: Django field errors land on the JOYOKR form fields', async () => {
  const { JoyOKR } = setup({
    'POST /api/okr/teams/': { status: 400, body: { code: 'validation_error', message: 'Dữ liệu chưa hợp lệ.', fields: { name: 'Tên team là bắt buộc.', manager_id: 'Người quản lý không hợp lệ.', department_id: 'Phòng ban không hợp lệ.' } } },
    'PUT /api/okr/teams/4/members/': { status: 400, body: { code: 'validation_error', message: 'Dữ liệu chưa hợp lệ.', fields: { employee_ids: 'Nhân sự không hợp lệ: 99.' } } },
    'PUT /api/okr/departments/1/okr-manager/': { status: 400, body: { code: 'validation_error', message: 'Dữ liệu chưa hợp lệ.', fields: { manager_id: 'Người quản lý không hợp lệ.' } } },
  });
  const teamError = await rejection(JoyOKR.api.request('teams', { method: 'POST', body: { name: '' } }));
  assert.deepEqual(plain(teamError.fields), { name: 'Tên team là bắt buộc.', manager_user_id: 'Người quản lý không hợp lệ.', department_id: 'Phòng ban không hợp lệ.' });
  const memberError = await rejection(JoyOKR.api.request('teams/4/members', { method: 'PUT', body: { user_ids: [99] } }));
  assert.deepEqual(plain(memberError.fields), { user_ids: 'Nhân sự không hợp lệ: 99.' });
  const departmentError = await rejection(JoyOKR.api.request('departments/1', { method: 'PUT', body: { manager_user_id: '99' } }));
  assert.deepEqual(plain(departmentError.fields), { manager_user_id: 'Người quản lý không hợp lệ.' });
});

test('departments: PUT departments/{id} and departments/{id}/okr-manager set the OKR manager only', async () => {
  const { JoyOKR, calls } = setup({
    'PUT /api/okr/departments/1/okr-manager/': ({ body }) => ({ body: { id: 1, name: 'Kinh doanh', okr_manager: body.manager_id ? owner(body.manager_id, 'Trần Quản Lý') : null } }),
  });
  const assigned = await JoyOKR.api.request('departments/1', { method: 'PUT', body: { manager_user_id: '8' } });
  const cleared = await JoyOKR.api.request('departments/1/okr-manager', { method: 'PUT', body: { manager_id: '' } });
  assert.deepEqual(calls.map((call) => [call.method, call.path, call.body]), [
    ['PUT', '/api/okr/departments/1/okr-manager/', { manager_id: 8 }],
    ['PUT', '/api/okr/departments/1/okr-manager/', { manager_id: null }],
  ]);
  assert.equal(assigned.manager_user_id, 8);
  assert.equal(assigned.manager_name, 'Trần Quản Lý');
  assert.equal(cleared.manager_name, '');
  assert.equal(cleared.status, 'active');
});

test('roles: list, grant, revoke and role-history map to /roles/ and /roles/history/', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/roles/': { body: page([{ id: 3, employee: owner(8, 'Trần Quản Lý'), role: 'admin', granted_at: '2026-11-05T09:30:00+07:00', granted_by: null }]) },
    'POST /api/okr/roles/': { status: 201, body: { id: 4, employee: owner(9, 'Lê Thị B'), role: 'manager', granted_at: '2026-11-05T10:00:00+07:00', granted_by: owner(8, 'Trần Quản Lý') } },
    'DELETE /api/okr/roles/4/': { body: { message: 'Đã gỡ vai trò.' } },
    'GET /api/okr/roles/history/': {
      body: page([
        { id: 2, employee: owner(9, 'Lê Thị B'), role: 'department_manager', action: 'revoked', department: { id: 1, name: 'Kinh doanh' }, actor: owner(8, 'Trần Quản Lý'), created_at: '2026-11-05T11:00:00+07:00' },
        { id: 1, employee: owner(8, 'Trần Quản Lý'), role: 'admin', action: 'granted', department: null, actor: null, created_at: '2026-11-04T08:00:00+07:00' },
      ], { count: 150 }),
    },
  });
  const roles = await JoyOKR.api.request('roles', { query: { per_page: 100 } });
  const granted = await JoyOKR.api.request('roles', { method: 'POST', body: { employee_id: '9', role: 'manager' } });
  const revoked = await JoyOKR.api.request('roles/4', { method: 'DELETE' });
  const history = await JoyOKR.api.request('role-history', { query: { per_page: 100 } });
  await JoyOKR.api.request('roles/history', { query: { per_page: 100 } });

  assert.deepEqual(calls.map((call) => [call.method, call.path, call.body ?? null]), [
    ['GET', '/api/okr/roles/', null],
    ['POST', '/api/okr/roles/', { employee_id: 9, role: 'manager' }],
    ['DELETE', '/api/okr/roles/4/', null],
    ['GET', '/api/okr/roles/history/', null],
    ['GET', '/api/okr/roles/history/', null],
  ]);
  assert.deepEqual(plain(roles.items[0]), {
    id: 3, employee_id: 8, employee_name: 'Trần Quản Lý', role: 'admin', granted_at: '2026-11-05T09:30:00+07:00', granted_by_id: 0, granted_by_name: '',
  });
  assert.equal(granted.granted_by_name, 'Trần Quản Lý');
  assert.equal(revoked.message, 'Đã gỡ vai trò.');
  assert.equal(history.total, 150);
  assert.deepEqual(plain(history.items[0]), {
    id: 2, employee_id: 9, employee_name: 'Lê Thị B', role: 'department_manager', action: 'revoked', department_id: 1, department_name: 'Kinh doanh',
    actor_id: 8, actor_name: 'Trần Quản Lý', created_at: '2026-11-05T11:00:00+07:00',
  });
  assert.equal(history.items[1].department_name, '');
  assert.equal(history.items[1].actor_name, '');
});

test('roles: 409 last_admin and role_exists keep the server message', async () => {
  const lastAdmin = 'Không thể gỡ quản trị OKR cuối cùng của công ty.';
  const { JoyOKR } = setup({
    'DELETE /api/okr/roles/3/': { status: 409, body: { code: 'last_admin', message: lastAdmin, fields: {} } },
    'POST /api/okr/roles/': { status: 409, body: { code: 'role_exists', message: 'Nhân sự đã có vai trò này.', fields: {} } },
  });
  const error = await rejection(JoyOKR.api.request('roles/3', { method: 'DELETE' }));
  assert.equal(error.status, 409);
  assert.equal(error.code, 'last_admin');
  assert.equal(error.message, lastAdmin);
  const duplicate = await rejection(JoyOKR.api.request('roles', { method: 'POST', body: { employee_id: 8, role: 'admin' } }));
  assert.equal(duplicate.code, 'role_exists');
  assert.equal(duplicate.message, 'Nhân sự đã có vai trò này.');
});

test('settings: GET maps Django settings; PUT converts form values and updates the threshold in memory', async () => {
  const config = makeConfig({ warningThreshold: 15 });
  const { JoyOKR, calls, sandbox } = setup({
    'GET /api/okr/settings/': { body: settingsPayload() },
    'PUT /api/okr/settings/': ({ body }) => ({ body: settingsPayload({ ...body, warning_threshold: '30.00', rating_ranges: RATING }) }),
  }, { config });
  const settings = await JoyOKR.api.request('settings');
  assert.equal(settings.warning_threshold, 15);
  assert.equal(settings.email_reminders, false);
  assert.deepEqual(plain(settings.rating_ranges[0]), { min: '0.00', max: '0.30', label: 'Chưa đạt' });
  assert.deepEqual(plain(settings.reminder_schedule), { scheduled: false, command: 'okr_checkin_reminders', channels: ['in_app'], email_available: false });

  const saved = await JoyOKR.api.request('settings', {
    method: 'PUT',
    body: {
      warning_threshold: '30', reminder_frequency: 'biweekly', email_reminders: 'on',
      rating_ranges: RATING.map((range, index) => ({ ...range, label: index === 0 ? '  Yếu  ' : range.label })),
    },
  });
  const put = calls.find((call) => call.method === 'PUT');
  assert.equal(put.path, '/api/okr/settings/');
  assert.equal(put.headers['X-CSRFToken'], 'csrf-token-123');
  assert.deepEqual(put.body, {
    warning_threshold: 30,
    reminder_frequency: 'biweekly',
    email_reminders: true,
    rating_ranges: [
      { min: 0, max: 0.3, label: 'Yếu' }, { min: 0.3, max: 0.7, label: 'Cần cố gắng' }, { min: 0.7, max: 0.9, label: 'Đạt' }, { min: 0.9, max: 1, label: 'Xuất sắc' },
    ],
  });
  assert.equal(saved.message, 'Đã lưu cài đặt.');
  assert.equal(sandbox.JoyOKRConfig.warningThreshold, 30);
  assert.equal(JoyOKR.api.warningThreshold(), 30);
});

test('settings: PUT is partial, flat rating_<i>_* inputs are accepted, a blank threshold is not sent as 0', async () => {
  const { JoyOKR, calls, sandbox } = setup({
    'PUT /api/okr/settings/': ({ body }) => (body.warning_threshold === ''
      ? { status: 400, body: { code: 'validation_error', message: 'Dữ liệu chưa hợp lệ.', fields: { warning_threshold: 'Ngưỡng phải từ 0 đến 100.' } } }
      : { body: settingsPayload() }),
  }, { config: makeConfig({ warningThreshold: 22 }) });
  await JoyOKR.api.request('settings', { method: 'PUT', body: { email_reminders: false } });
  await JoyOKR.api.request('settings', {
    method: 'PUT',
    body: { rating_0_min: '0', rating_0_max: '0.5', rating_0_label: 'A', rating_1_min: '0.5', rating_1_max: '1', rating_1_label: 'B' },
  });
  const error = await rejection(JoyOKR.api.request('settings', { method: 'PUT', body: { warning_threshold: '  ' } }));
  assert.deepEqual(calls.map((call) => call.body), [
    { email_reminders: false },
    { rating_ranges: [{ min: 0, max: 0.5, label: 'A' }, { min: 0.5, max: 1, label: 'B' }] },
    { warning_threshold: '' },
  ]);
  assert.deepEqual(plain(error.fields), { warning_threshold: 'Ngưỡng phải từ 0 đến 100.' });
  assert.equal(sandbox.JoyOKRConfig.warningThreshold, 15, 'the saved settings (15.00) replace the old threshold');
});

test('warningThreshold(): configured value, else JOYOKR 15 when missing or invalid', () => {
  const cases = [[30, 30], ['12.5', 12.5], [0, 0], [100, 100], [undefined, 15], [null, 15], ['', 15], ['abc', 15], [-1, 15], [150, 15], [Number.NaN, 15]];
  for (const [value, expected] of cases) {
    const { JoyOKR } = load(API_FILES, { config: makeConfig({ warningThreshold: value }) });
    assert.equal(JoyOKR.api.warningThreshold(), expected, String(value));
  }
});

test('the configured threshold drives client-side health: with 30 a gap of 20 is at risk, not off track', async () => {
  const dashboard = {
    cycle: { id: 3, name: 'Q4', start_date: '2026-10-01', end_date: '2026-12-31', status: 'active' },
    progress: { value: '30.00', objective_count: 1, total_weight: '100.00' }, expected_progress: '50.00', objective_stats: {},
    key_result_count: 1, stale_count: 1, teams: [], featured: [],
    attention: [{ key_result_id: 21, key_result_title: 'KR', objective_title: 'O', reason: 'stale', progress: '30.00', expected_progress: '50.00', days_overdue: 3, last_checkin_at: null, owner: owner() }],
  };
  const routes = {
    'GET /api/okr/dashboard/company/': { body: dashboard },
    'PUT /api/okr/settings/': { body: settingsPayload({ warning_threshold: '30.00' }) },
  };
  const health = async (JoyOKR) => (await JoyOKR.api.request('dashboard', { query: {}, summaryOnly: true })).attention_key_results[0].health;

  const relaxed = setup(routes, { config: makeConfig({ warningThreshold: 30 }) });
  assert.equal(await health(relaxed.JoyOKR), 'at_risk');
  const fallback = setup(routes, { config: makeConfig() });
  assert.equal(await health(fallback.JoyOKR), 'off_track', 'JOYOKR default 15 without a configured threshold');

  // Saving a new threshold takes effect without a page reload.
  await fallback.JoyOKR.api.request('settings', { method: 'PUT', body: { warning_threshold: 30 } });
  assert.equal(await health(fallback.JoyOKR), 'at_risk');
});
