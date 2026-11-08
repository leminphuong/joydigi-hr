// Filter lists (departments, teams, people) and the Teams page read EVERY page
// of the API, and a failed list is reported instead of looking like "no data".
// Before 2026-11-08 they asked one page of at most 100 rows, so a company with
// more departments, teams or people silently lost the rest from its filters,
// and a failed list was indistinguishable from an empty one.
import test from 'node:test';
import assert from 'node:assert/strict';
import { load, makeConfig, makeLocation, objectiveSummary, owner, page } from './harness.mjs';

const VIEW_FILES = ['domain.js', 'api.js', 'store.js', 'views.js'];

/** A DRF-style paged endpoint over `rows`, honouring page and page_size like the server. */
function paged(rows) {
  return (call) => {
    const size = Math.min(Number(call.query.page_size) || 20, 100);
    const number = Number(call.query.page) || 1;
    const slice = rows.slice((number - 1) * size, number * size);
    const more = number * size < rows.length;
    return { body: { count: rows.length, next: more ? `https://x/api/okr/?page=${number + 1}` : null, previous: null, results: slice } };
  };
}

const departments = (n) => Array.from({ length: n }, (_, i) => ({ id: i + 1, name: `Phòng ${String(i + 1).padStart(3, '0')}`, okr_manager: null }));
const teams = (n) => Array.from({ length: n }, (_, i) => ({
  id: i + 1, name: `Team ${i + 1}`, description: '', department_id: null, department_name: null,
  manager: null, status: 'active', member_count: 1, members: [],
}));
const people = (n) => Array.from({ length: n }, (_, i) => ({ id: i + 1, name: `Nhân viên ${i + 1}`, department_id: null, department_name: null }));

async function render(name, routes, search = '') {
  const { JoyOKR, calls } = load(VIEW_FILES, { routes, config: makeConfig(), location: makeLocation(`/okr/${name}`, search) });
  const outlet = { innerHTML: '' };
  await JoyOKR.views.render(name, { params: {}, outlet, isCurrent: () => true });
  return { html: outlet.innerHTML, calls };
}

test('allPages follows `next` and returns every department (225 > 100)', async () => {
  const { JoyOKR, calls } = load(['api.js'], { routes: { 'GET /api/okr/departments/': paged(departments(225)) } });
  const result = await JoyOKR.api.request('departments', { query: {}, allPages: true });
  assert.equal(result.items.length, 225);
  assert.equal(result.total, 225);
  assert.equal(result.complete, true);
  assert.deepEqual(calls.map((call) => [call.query.page, call.query.page_size]), [['1', '100'], ['2', '100'], ['3', '100']]);
});

test('without allPages the old single-page behaviour is kept (admin pages page themselves)', async () => {
  const { JoyOKR, calls } = load(['api.js'], { routes: { 'GET /api/okr/departments/': paged(departments(225)) } });
  const result = await JoyOKR.api.request('departments', { query: { per_page: 100 } });
  assert.equal(result.items.length, 100);
  assert.equal(result.total, 225);
  assert.equal(calls.length, 1);
});

test('company OKR filters list all 130 departments, 120 teams and 150 people', async () => {
  const { html } = await render('company-okrs', {
    'GET /api/okr/objectives/': { body: page([objectiveSummary()]) },
    'GET /api/okr/departments/': paged(departments(130)),
    'GET /api/okr/teams/': paged(teams(120)),
    'GET /api/okr/people/': paged(people(150)),
  });
  assert.ok(html.includes('Phòng 130'), 'last department present');
  assert.ok(html.includes('Team 120'), 'last team present');
  assert.ok(html.includes('Nhân viên 150'), 'last person present');
  assert.ok(!html.includes('data-filter-failure'));
});

test('a failed department list keeps the objective list and says the filter failed', async () => {
  const { html } = await render('company-okrs', {
    'GET /api/okr/objectives/': { body: page([objectiveSummary({ title: 'Mục tiêu vẫn hiện' })]) },
    'GET /api/okr/departments/': { status: 500, body: { code: 'server_error', message: 'Lỗi máy chủ.', fields: {} } },
    'GET /api/okr/teams/': paged(teams(2)),
    'GET /api/okr/people/': paged(people(2)),
  });
  assert.ok(html.includes('Mục tiêu vẫn hiện'), 'objectives still listed');
  assert.ok(html.includes('data-filter-failure'), 'failure is reported');
  assert.ok(html.includes('Không tải được danh sách phòng ban'));
});

test('empty objective list: "no match for filters" only when a filter is set', async () => {
  const routes = {
    'GET /api/okr/objectives/': { body: page([]) },
    'GET /api/okr/departments/': paged(departments(1)),
    'GET /api/okr/teams/': paged([]),
    'GET /api/okr/people/': paged(people(1)),
  };
  const plain = await render('company-okrs', routes);
  assert.ok(plain.html.includes('Chưa có Objective nào bạn được xem'), 'no filter: say why the list is empty');
  assert.ok(!plain.html.includes('phù hợp với bộ lọc'));
  const filtered = await render('company-okrs', routes, '?department_id=1');
  assert.ok(filtered.html.includes('Chưa có Objective phù hợp với bộ lọc.'));
});

test('Teams page lists more than 100 teams', async () => {
  const { html } = await render('teams', {
    'GET /api/okr/teams/': paged(teams(130)),
    'GET /api/okr/dashboard/company/': { body: { cycle: { id: 3, name: 'Q4' }, expected_progress: '0', teams: [] } },
  });
  assert.ok(html.includes('Team 130'));
});

test('an expired session on a filter list still fails the page', async () => {
  await assert.rejects(render('company-okrs', {
    'GET /api/okr/objectives/': { body: page([]) },
    'GET /api/okr/departments/': { status: 401, body: { code: 'not_authenticated', message: 'Vui lòng đăng nhập.', fields: {} } },
    'GET /api/okr/teams/': paged([]),
    'GET /api/okr/people/': paged([]),
  }), (error) => error?.status === 401);
});
