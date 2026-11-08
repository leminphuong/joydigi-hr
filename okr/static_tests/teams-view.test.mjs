// "Teams" page: the team list must not depend on an active cycle. Before the
// fix, the page loaded the teams and the cycle dashboard together and showed
// only "Chưa có chu kỳ OKR đang hoạt động." when no cycle was active (e.g. the
// company's only cycle was still a draft), so no team was listed at all.
import test from 'node:test';
import assert from 'node:assert/strict';
import { load, makeConfig, makeLocation, owner, page } from './harness.mjs';

const VIEW_FILES = ['domain.js', 'api.js', 'store.js', 'views.js'];

const TEAMS = page([
  { id: 4, name: 'Team Marketing', description: '', department_id: 1, department_name: 'ADS', manager: owner(8, 'Trần Quản Lý'), status: 'active', member_count: 3, members: [] },
  { id: 5, name: 'Team Dev', description: '', department_id: null, department_name: null, manager: null, status: 'active', member_count: 0, members: [] },
]);

const NO_CYCLE = { status: 404, body: { code: 'no_cycle', message: 'Chưa có chu kỳ OKR đang hoạt động.', fields: {} } };

async function renderTeams(routes) {
  const { JoyOKR, calls } = load(VIEW_FILES, { routes, config: makeConfig(), location: makeLocation('/okr/teams') });
  const outlet = { innerHTML: '' };
  const rendered = await JoyOKR.views.render('teams', { params: {}, outlet, isCurrent: () => true });
  return { rendered, html: outlet.innerHTML, calls };
}

test('teams are listed when no cycle is active, with a notice instead of progress', async () => {
  const { rendered, html } = await renderTeams({
    'GET /api/okr/teams/': { body: TEAMS },
    'GET /api/okr/dashboard/company/': NO_CYCLE,
  });
  assert.equal(rendered, true);
  assert.ok(html.includes('Team Marketing'), 'first team listed');
  assert.ok(html.includes('Team Dev'), 'second team listed');
  assert.ok(html.includes('Trần Quản Lý'), 'manager shown');
  assert.ok(html.includes('data-teams-progress-notice'), 'notice shown');
  assert.ok(html.includes('Chưa có chu kỳ OKR đang hoạt động.'), 'server reason shown in the notice');
  assert.ok(!html.includes('Chưa có team hoạt động.'), 'not the empty state');
});

test('teams still show progress when a cycle is active', async () => {
  const dashboard = {
    cycle: { id: 3, name: 'Q4 2026', start_date: '2026-10-01', end_date: '2026-12-31', status: 'active' },
    expected_progress: '40.00',
    teams: [{ id: 4, name: 'Team Marketing', objective_count: 2, progress: '55.00' }],
  };
  const { html } = await renderTeams({
    'GET /api/okr/teams/': { body: TEAMS },
    'GET /api/okr/dashboard/company/': { body: dashboard },
  });
  assert.ok(html.includes('Team Marketing'));
  assert.ok(!html.includes('data-teams-progress-notice'), 'no notice with an active cycle');
  assert.ok(html.includes('Q4 2026'), 'cycle chooser names the cycle');
});

test('an expired session still fails the page (login redirect is handled by the router)', async () => {
  await assert.rejects(renderTeams({
    'GET /api/okr/teams/': { body: TEAMS },
    'GET /api/okr/dashboard/company/': { status: 401, body: { code: 'not_authenticated', message: 'Vui lòng đăng nhập.', fields: {} } },
  }));
});

test('team names cannot inject markup through the notice path', async () => {
  const evil = page([{ id: 6, name: '<img src=x onerror=alert(1)>', description: '', department_id: null, department_name: '<b>x</b>', manager: null, status: 'active', member_count: 0, members: [] }]);
  const { html } = await renderTeams({
    'GET /api/okr/teams/': { body: evil },
    'GET /api/okr/dashboard/company/': { status: 404, body: { code: 'no_cycle', message: '<script>alert(1)</script>', fields: {} } },
  });
  assert.ok(!html.includes('<img'), 'team name escaped');
  assert.ok(!html.includes('<script>'), 'server message escaped');
  assert.ok(!html.includes('<b>x</b>'), 'department escaped');
});
