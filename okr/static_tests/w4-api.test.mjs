// W4 adapter: the new dashboard / report / check-in management query params
// and the new Django fields mapped to the names views.js reads.
import test from 'node:test';
import assert from 'node:assert/strict';
import { keyResult, load, objectiveSummary, owner, page, plain } from './harness.mjs';

const API_FILES = ['domain.js', 'api.js'];

function setup(routes = {}) {
  return load(API_FILES, { routes });
}

const SELECTION = {
  selected_id: 3,
  selected_by: 'default',
  rule: 'Khi chưa chọn chu kỳ: dùng chu kỳ đang hoạt động có ngày hôm nay nằm trong khoảng thời gian.',
  active_cycles: [
    { id: 3, name: 'Q4 2026', start_date: '2026-10-01', end_date: '2026-12-31' },
    { id: 9, name: 'Q1 2027', start_date: '2027-01-01', end_date: '2027-03-31' },
  ],
};

function dashboardBody(overrides = {}) {
  return {
    cycle: { id: 3, name: 'Q4 2026', cycle_type: 'quarter', start_date: '2026-10-01', end_date: '2026-12-31', status: 'active' },
    cycle_selection: SELECTION,
    filters: { team_id: 4, owner_id: 7 },
    progress: { value: '42.50', objective_count: 2, total_weight: '100.00' },
    selection_progress: '61.25',
    expected_progress: '50.00',
    objective_stats: { total: 3, on_track: 1, at_risk: 1, off_track: 0, completed: 1 },
    in_progress_count: 2,
    pending_approval_count: 1,
    key_result_count: 6,
    key_results_completed: 2,
    stale_count: 1,
    attention: [{ key_result_id: 21, key_result_title: 'KR', objective_title: 'O', reason: 'stale', progress: '10.00', expected_progress: '50.00', days_overdue: 3, last_checkin_at: null, owner: owner() }],
    attention_total: 140,
    teams: [],
    featured: [],
    ...overrides,
  };
}

test('dashboard: scope/department/team/owner filters become Django params; invalid values are dropped', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/dashboard/company/': { body: dashboardBody() },
    'GET /api/okr/objectives/': { body: page([]) },
  });
  await JoyOKR.api.request('dashboard', { query: { cycle_id: '3', scope: 'team', department_id: '1', team_id: '4', owner_user_id: '7', health: 'at_risk', status: 'due' } });
  assert.equal(calls[0].path, '/api/okr/dashboard/company/');
  assert.deepEqual(calls[0].query, { cycle: '3', scope: 'team', department: '1', team: '4', owner: '7' }, 'owner is the employee id, never "me"; no health/status here');
  // The drill-down list uses the filters the server applied (team 4, owner 7).
  assert.deepEqual(calls[1].query, { cycle: '3', status: 'approved', page_size: '100', team: '4', owner: '7' });

  const bad = setup({ 'GET /api/okr/dashboard/company/': { body: dashboardBody({ filters: {} }) }, 'GET /api/okr/objectives/': { body: page([]) } });
  await bad.JoyOKR.api.request('dashboard', { query: { cycle_id: '3x', scope: 'galaxy', department_id: '1 OR 1=1', team_id: '-4', owner_user_id: 'me' } });
  assert.deepEqual(bad.calls[0].query, {});
  assert.deepEqual(bad.calls[1].query, { cycle: '3', status: 'approved', page_size: '100' });
});

test('dashboard: new KPI fields, cycle_selection, filters and attention_total are mapped', async () => {
  const { JoyOKR } = setup({ 'GET /api/okr/dashboard/company/': { body: dashboardBody() }, 'GET /api/okr/objectives/': { body: page([]) } });
  const result = await JoyOKR.api.request('dashboard', { query: { cycle_id: '3' } });
  assert.equal(result.progress, '42.50', 'the official company figure is kept');
  assert.equal(result.selection_progress, '61.25');
  assert.equal(result.in_progress_count, 2);
  assert.equal(result.pending_approval_count, 1);
  assert.equal(result.key_results_completed, 2);
  assert.equal(result.attention_total, 140);
  assert.deepEqual(plain(result.filters), { team_id: 4, owner_user_id: 7 });
  assert.equal(result.has_filters, true);
  assert.deepEqual(plain(result.cycle_selection), {
    selected_id: 3,
    selected_by: 'default',
    rule: SELECTION.rule,
    active_cycles: SELECTION.active_cycles,
  });

  const { JoyOKR: plainDashboard } = setup({
    'GET /api/okr/dashboard/company/': { body: dashboardBody({ filters: {}, cycle_selection: { selected_id: 'x', selected_by: 'hack', rule: null, active_cycles: 'no' }, attention_total: undefined }) },
    'GET /api/okr/objectives/': { body: page([]) },
  });
  const unfiltered = await plainDashboard.api.request('dashboard', { query: {} });
  assert.equal(unfiltered.has_filters, false);
  assert.deepEqual(plain(unfiltered.cycle_selection), { selected_id: 0, selected_by: 'default', rule: '', active_cycles: [] });
  assert.equal(unfiltered.attention_total, 1, 'falls back to the shown items');
});

test('reports: health passes only when valid; groups, rating bands, overdue KRs and history are mapped', async () => {
  const report = {
    cycle: { id: 3, name: 'Q4 2026' },
    cycle_selection: SELECTION,
    filters: { scope: 'team', department_id: 1, health: 'at_risk', owner_id: null },
    progress: { value: '40.00', objective_count: 1, total_weight: '100.00' },
    selection_progress: '55.00',
    expected_progress: '50.00',
    objective_stats: {},
    key_result_count: 2,
    stale_count: 1,
    rows: [objectiveSummary()],
    by_scope: [],
    by_owner: [],
    by_department: [{ label: 'Kinh doanh', count: 2, progress: '45.00' }, { label: 'Chưa xác định', count: '1', progress: '0.00' }],
    by_team: [{ label: 'Team Bán lẻ', count: 1, progress: '30.00' }],
    by_rating: [{ label: 'Chưa đạt', min: '0', max: '0.3', count: 1 }, { label: 'Đạt', min: '0.7', max: '1', count: 0 }],
    overdue_key_results: [{ id: 21, title: 'Doanh thu 10 tỷ', objective_id: 12, objective_title: 'Tăng doanh thu', owner: owner(8, 'Trần Thị B'), due_date: '2026-10-31', days_late: 5, progress: '40.00' }],
    checkin_count: 75,
    checkin_history: [{
      id: 5, created_at: '2026-11-04T09:00:00+07:00', author: owner(9, 'Lê Văn C'), previous_value: '2', current_value: '4',
      previous_progress: '20.00', current_progress: '40.00', confidence: 4, update_text: 'Ký thêm 2 hợp đồng', blockers: '', next_steps: '',
      key_result: { id: 21, title: 'Doanh thu 10 tỷ', unit: 'tỷ' }, objective_title: 'Tăng doanh thu', owner: owner(8, 'Trần Thị B'),
    }],
    trend: [],
    checkin_attention_by_owner: [],
  };
  const { JoyOKR, calls } = setup({ 'GET /api/okr/reports/': { body: report } });
  const mapped = await JoyOKR.api.request('reports', { query: { cycle_id: '3', scope: 'team', department_id: '1', health: 'at_risk' } });
  await JoyOKR.api.request('reports', { query: { health: 'on_fire', scope: 'x' } });
  assert.deepEqual(calls[0].query, { cycle: '3', scope: 'team', department: '1', health: 'at_risk' });
  assert.deepEqual(calls[1].query, {}, 'unknown health and scope never reach Django');

  assert.equal(mapped.summary.progress, '40.00');
  assert.equal(mapped.summary.selection_progress, '55.00');
  assert.deepEqual(plain(mapped.filters), { scope: 'team', department_id: 1, health: 'at_risk' });
  assert.equal(mapped.has_filters, true);
  assert.equal(mapped.cycle_selection.active_cycles.length, 2);
  assert.deepEqual(plain(mapped.by_department), [
    { label: 'Kinh doanh', count: 2, progress: '45.00' },
    { label: 'Chưa xác định', count: 1, progress: '0.00' },
  ]);
  assert.deepEqual(plain(mapped.by_team), [{ label: 'Team Bán lẻ', count: 1, progress: '30.00' }]);
  assert.deepEqual(plain(mapped.by_rating[0]), { label: 'Chưa đạt', min: '0', max: '0.3', count: 1 });
  assert.deepEqual(plain(mapped.overdue_key_results[0]), {
    id: 21, title: 'Doanh thu 10 tỷ', objective_id: 12, objective_title: 'Tăng doanh thu', owner_user_id: 8, owner_name: 'Trần Thị B',
    due_date: '2026-10-31', days_late: 5, progress: '40.00',
  });
  const entry = mapped.checkin_history[0];
  assert.equal(entry.user_name, 'Lê Văn C', 'author -> user_name');
  assert.equal(entry.owner_name, 'Trần Thị B');
  assert.equal(entry.key_result_id, 21);
  assert.equal(entry.key_result_title, 'Doanh thu 10 tỷ');
  assert.equal(entry.unit, 'tỷ');
  assert.equal(entry.objective_title, 'Tăng doanh thu');
  assert.equal(mapped.checkin_count, 75);
  assert.equal(mapped.summary.checkin_count, 75);
});

test('tree: cycle_selection is mapped', async () => {
  const { JoyOKR } = setup({ 'GET /api/okr/tree/': { body: { cycle: { id: 3, name: 'Q4' }, cycle_selection: SELECTION, expected_progress: '50.00', items: [], unlinked_items: [] } } });
  const tree = await JoyOKR.api.request('tree', { query: {} });
  assert.equal(tree.cycle_selection.selected_by, 'default');
  assert.deepEqual(plain(tree.cycle_selection.active_cycles.map((cycle) => cycle.id)), [3, 9]);
});

test('check-in management: department/team/owner/status filters; KR status, due date and totals are mapped', async () => {
  const management = {
    cycle: { id: 3, name: 'Q4' },
    cycle_selection: SELECTION,
    filters: { department_id: 1, status: 'overdue' },
    can_manage: true,
    scope: 'company',
    summary: { employee_count: 1, key_result_count: 2, never_checked_in: 1, checkin_count: 26, overdue_count: '1', due_count: 0 },
    expected_progress: '50.00',
    departments: [{ id: 1, name: 'Kinh doanh', teams: [{ id: 4, name: 'Team A', people: [{ employee: owner(8, 'Trần Thị B'), objectives: [{ id: 12, title: 'O', scope: 'personal', progress: '10.00', key_results: [
      { ...keyResult({ id: 21 }), due_date: '2026-12-31', checkin_status: 'overdue', checkin_total: 26, checkins: [{ id: 1, author: owner(8, 'Trần Thị B') }] },
      { ...keyResult({ id: 22 }), due_date: null, checkin_status: 'weird', checkin_total: null, checkins: [] },
    ] }] }] }] }],
    unassigned: [],
  };
  const { JoyOKR, calls } = setup({ 'GET /api/okr/checkins/management/': { body: management } });
  const mapped = await JoyOKR.api.request('checkin-management-tree', { query: { cycle_id: '3', department_id: '1', team_id: '4', owner_user_id: '8', status: 'overdue' } });
  await JoyOKR.api.request('checkin-management-tree', { query: { status: 'late', owner_user_id: 'me', team_id: '0' } });
  assert.equal(calls[0].path, '/api/okr/checkins/management/');
  assert.deepEqual(calls[0].query, { cycle: '3', department: '1', team: '4', owner: '8', status: 'overdue' });
  assert.deepEqual(calls[1].query, {});

  assert.deepEqual(plain(mapped.filters), { department_id: 1, status: 'overdue' });
  assert.equal(mapped.has_filters, true);
  assert.equal(mapped.summary.overdue_count, 1);
  assert.equal(mapped.summary.due_count, 0);
  assert.equal(mapped.summary.checkin_count, 26);
  assert.equal(mapped.cycle_selection.selected_id, 3);
  const [first, second] = mapped.departments[0].teams[0].people[0].objectives[0].key_results;
  assert.deepEqual(plain([first.due_date, first.checkin_status, first.checkin_total, first.checkins[0].user_name]), ['2026-12-31', 'overdue', 26, 'Trần Thị B']);
  assert.deepEqual(plain([second.due_date, second.checkin_status, second.checkin_total]), ['', '', 0]);
});
