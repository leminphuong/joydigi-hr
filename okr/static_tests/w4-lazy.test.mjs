// W4 performance: a company-wide check-in tree (hundreds of people) starts
// with departments and teams collapsed and builds their children only when
// opened; small scopes keep JOYOKR's fully open tree.
import test from 'node:test';
import assert from 'node:assert/strict';
import { MANAGER_CAPS, load, makeConfig, makeLocation, page } from './harness.mjs';

const CYCLE = { id: 3, name: 'Q4 2026', cycle_type: 'quarter', start_date: '2026-10-01', end_date: '2026-12-31', status: 'active' };

function tree(peopleCount) {
  const people = Array.from({ length: peopleCount }, (_, index) => ({
    employee: { id: 100 + index, name: `Nhân viên ${index}` },
    objectives: [{ id: 500 + index, title: `Mục tiêu ${index}`, scope: 'personal', progress: '10.00', key_results: [] }],
  }));
  return {
    cycle: CYCLE, cycle_selection: { selected_id: 3, selected_by: 'default', rule: '', active_cycles: [{ id: 3, name: 'Q4 2026' }] },
    filters: {}, can_manage: true, scope: 'company', expected_progress: '50.00',
    summary: { employee_count: peopleCount, key_result_count: 0, never_checked_in: 0, checkin_count: 0, overdue_count: 0, due_count: 0 },
    departments: [{ id: 1, name: 'Phòng Kinh doanh', teams: [{ id: 4, name: 'Team Bắc', people }] }],
    unassigned: [],
  };
}

async function render(peopleCount) {
  const { JoyOKR } = load(['domain.js', 'api.js', 'store.js', 'views.js'], {
    routes: {
      'GET /api/okr/checkins/queue/': { body: page([]) },
      'GET /api/okr/checkins/management/': { body: tree(peopleCount) },
      'GET /api/okr/departments/': { body: page([]) },
      'GET /api/okr/teams/': { body: page([]) },
      'GET /api/okr/people/': { body: page([]) },
    },
    location: makeLocation('/okr/check-ins', ''),
    config: makeConfig({ capabilities: MANAGER_CAPS }),
  });
  const outlet = { innerHTML: '' };
  await JoyOKR.views.render('check-ins', { params: {}, outlet, isCurrent: () => true });
  return { html: outlet.innerHTML, views: JoyOKR.views };
}

test('large scope: department and team start collapsed, people are built only on open', async () => {
  const { html, views } = await render(45);
  assert.ok(!html.includes('Nhân viên 0'), 'people are not in the first render');
  const departmentLazy = html.match(/data-checkin-lazy="(department-1)"/);
  assert.ok(departmentLazy, 'department details are lazy');
  assert.match(html, /aria-expanded="false"[^>]*>.{0,80}▸/s);
  const teams = views.renderLazyCheckinChildren('department-1');
  assert.ok(teams.includes('Team Bắc') && !teams.includes('Nhân viên 0'), 'opening the department builds its teams only');
  const lazyTeam = teams.match(/data-checkin-lazy="([^"]+)"/)[1];
  const people = views.renderLazyCheckinChildren(lazyTeam);
  assert.ok(people.includes('Nhân viên 0') && people.includes('Nhân viên 44'), 'opening the team builds its people');
  assert.equal(views.renderLazyCheckinChildren(lazyTeam), null, 'built once');
});

test('small scope keeps the fully open JOYOKR tree', async () => {
  const { html } = await render(5);
  assert.ok(html.includes('Nhân viên 4'));
  assert.ok(!html.includes('data-checkin-lazy'));
});
