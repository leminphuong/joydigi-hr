// XSS hardening: the escapers in views.js / forms.js, stripTags without
// HTML parsing, and every wired screen rendered from Django-shaped data that
// is full of injection payloads.
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ADMIN_CAPS, XSS, keyResult, load, makeConfig, makeLocation, objectiveDetail, objectiveSummary, page,
} from './harness.mjs';

const P = `${XSS.tag}${XSS.attr}`;
const VIEW_FILES = ['domain.js', 'api.js', 'store.js', 'views.js'];

function escaperCases(name, escape) {
  assert.equal(escape(XSS.tag), '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;', name);
  assert.equal(escape("' onmouseover='"), '&#39; onmouseover=&#39;', name);
  assert.equal(escape('`x` & y'), '&#96;x&#96; &amp; y', name);
  assert.equal(escape(null), '', name);
  assert.equal(escape(undefined), '', name);
  assert.equal(escape(0), '0', name);
  for (const character of ['<', '>', '"', "'", '`']) assert.ok(!escape(P).includes(character), `${name} leaves ${character}`);
}

/** Every real tag: no inline handler attributes, no unquoted / single-quoted values. */
function assertSafeMarkup(html, label) {
  assert.ok(!html.includes('<img'), `${label}: raw <img>`);
  assert.ok(!html.includes('"><img'), `${label}: attribute break-out`);
  assert.ok(!html.includes("' onmouseover="), `${label}: single-quote break-out`);
  assert.ok(!/<script/i.test(html), `${label}: <script>`);
  const tags = html.match(/<[a-zA-Z][^>]*>/g) || [];
  for (const tag of tags) {
    const bare = tag.replace(/"[^"]*"/g, '""');
    assert.ok(!/\son[a-z]+\s*=/i.test(bare), `${label}: inline handler in ${tag}`);
    assert.ok(!bare.includes("'"), `${label}: stray quote in ${tag}`);
    assert.ok(!/javascript:/i.test(tag), `${label}: javascript: URL in ${tag}`);
  }
  for (const [, kind, id] of html.matchAll(/data-route="(objectives|key-results)\/([^"]*)"/g)) {
    assert.match(id, /^\d+$/, `${label}: ${kind} route id "${id}"`);
  }
  for (const [, href] of html.matchAll(/href="([^"]*)"/g)) {
    assert.ok(/^https?:\/\//.test(href) || /^\/(?!\/)/.test(href), `${label}: unsafe href ${href}`);
  }
}

test('views.js and forms.js escapers escape & < > " \' and backtick without the DOM', () => {
  const { JoyOKR } = load([...VIEW_FILES, 'forms.js']);
  escaperCases('views.escapeHtml', JoyOKR.views.helpers.escapeHtml);
  escaperCases('views.escapeAttr', JoyOKR.views.helpers.escapeAttr);
  escaperCases('forms.escapeHtml', JoyOKR.forms.helpers.escapeHtml);
  escaperCases('forms.escapeAttr', JoyOKR.forms.helpers.escapeAttr);
});

test('stripTags removes tags as text and never parses HTML (document.createElement throws here)', () => {
  const { JoyOKR } = load(VIEW_FILES);
  const { stripTags } = JoyOKR.views.helpers;
  assert.equal(stripTags('<img src=x onerror=alert(1)>Tiêu đề <b>đậm</b><!-- c -->'), 'Tiêu đề đậm');
  assert.equal(stripTags('Giảm lỗi <5% và 2 < 3'), 'Giảm lỗi <5% và 2 < 3');
  assert.equal(stripTags(null), '');
});

test('page(): the subtitle is text unless wrapped in raw(); the title is escaped', () => {
  const { JoyOKR } = load(VIEW_FILES);
  const { page, raw } = JoyOKR.views.helpers;
  const html = page(P, P, '', '');
  assertSafeMarkup(html, 'page');
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(page('T', raw('<b>ok</b>'), '', '').includes('<p><b>ok</b></p>'));
});

test('forms field/textarea/select/hidden escape values and labels', () => {
  const { JoyOKR } = load([...VIEW_FILES, 'forms.js']);
  const { field, textarea, select, selectValues, hidden } = JoyOKR.forms.helpers;
  const html = [
    field('title', P, P, { help: P }),
    textarea('description', P, P),
    select('owner_user_id', P, [{ id: P, display_name: P }], P, 'id', 'display_name'),
    selectValues('scope', P, [P], P),
    hidden('object_id', P),
  ].join('');
  assertSafeMarkup(html, 'form helpers');
  assert.ok(html.includes(`value="&quot;&gt;&lt;img src=x onerror=alert(1)&gt;&#39; onmouseover=&#39;alert(1)"`));
});

test('evidence links: only http(s) hrefs and same-origin download paths render as links', () => {
  const { JoyOKR } = load(VIEW_FILES);
  const html = JoyOKR.views.helpers.evidenceList([
    { evidence_type: 'url', label: P, url: 'javascript:alert(1)', user_name: P, created_at: '' },
    { evidence_type: 'url', label: 'ok', url: 'https://docs.example.com/x?a="b"', user_name: '', created_at: '' },
    { evidence_type: 'url', label: 'data', url: 'data:text/html,<script>alert(1)</script>', user_name: '', created_at: '' },
    { evidence_type: 'file', label: 'file', url: '/api/okr/evidence/3/download/', user_name: '', created_at: '' },
    { evidence_type: 'file', label: 'evil file', url: '//evil.example/x/download/', user_name: '', created_at: '' },
  ]);
  assertSafeMarkup(html, 'evidenceList');
  const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map((match) => match[1]);
  assert.deepEqual(hrefs, ['https://docs.example.com/x?a=%22b%22', '/api/okr/evidence/3/download/']);
  assert.ok(html.includes('rel="noopener noreferrer"'));
});

// -- full screens: Django JSON with payloads -> api.js -> views.js ----------------------

function poisonedRoutes() {
  const person = { id: 7, name: P };
  const objective = (overrides = {}) => objectiveSummary({
    id: 12, title: P, owner: person, cycle: { id: 3, name: P, end_date: '2026-12-31' }, department_name: P, team_name: P, ...overrides,
  });
  const kr = (overrides = {}) => keyResult({ title: P, unit: P, objective_title: P, owner: person, ...overrides });
  const attention = { key_result_id: 21, key_result_title: P, objective_title: P, reason: 'stale', progress: '10.00', expected_progress: '50.00', days_overdue: 2, last_checkin_at: null, owner: person };
  const cycle = { id: 3, name: P, cycle_type: 'quarter', start_date: '2026-10-01', end_date: '2026-12-31', status: 'active' };
  const checkin = { id: 1, created_at: '2026-11-05T09:30:12.345678+07:00', author: person, previous_value: '1', current_value: '2', previous_progress: '10.00', current_progress: '20.00', confidence: P, update_text: P, blockers: P, next_steps: P };
  return {
    'GET /api/okr/objectives/': { body: page([objective(), objective({ id: 'x"><img>' }), objective({ id: 14, approval_status: 'approved', health: 'on_track' })]) },
    'GET /api/okr/objectives/12/': { body: objectiveDetail({ title: P, description: P, owner: person, cycle: { id: 3, name: P, end_date: '2026-12-31' }, key_results: [kr(), kr({ id: '1 onmouseover=x' })], rejection_reason: P, approval_status: 'rejected' }) },
    'GET /api/okr/cycles/': { body: page([
      { id: 3, name: P, cycle_type: P, start_date: '2026-10-01', end_date: '2026-12-31', status: 'active', parent_id: 4, objective_count: P },
      { id: 4, name: P, cycle_type: 'year', start_date: '2026-01-01', end_date: '2026-12-31', status: 'closed', parent_id: null, objective_count: 2 },
      { id: 5, name: P, cycle_type: 'quarter', start_date: '2027-01-01', end_date: '2027-03-31', status: P, parent_id: P, objective_count: 0 },
    ]) },
    'GET /api/okr/comments/': { body: page([{ id: 1, content: P, created_at: '2026-11-05T09:30:00+07:00', author: person, parent_id: null }]) },
    'GET /api/okr/key-results/21/': { body: kr({ permissions: { can_checkin: true, can_add_evidence: true } }) },
    'GET /api/okr/checkins/': { body: page([checkin]) },
    'GET /api/okr/evidence/': { body: page([{ id: 1, evidence_type: 'url', label: P, url: 'javascript:alert(1)', created_at: '2026-11-05T09:30:00+07:00', author: person }, { id: 2, evidence_type: 'file', label: P, file_name: P, file_size: 1, download_path: '/okr/evidence/2/download/', created_at: '2026-11-05T09:30:00+07:00', author: person }]) },
    'GET /api/okr/activity/': { body: page([{ id: 1, action: P, created_at: '2026-11-05T09:30:00+07:00', actor: person, summary: P, key_result: null }]) },
    'GET /api/okr/notifications/': { body: page([{ id: 5, type: P, title: P, message: P, is_read: false, created_at: '2026-11-05T09:30:00+07:00', reference: { type: 'objective', id: 12, available: true } }, { id: 6, type: 'comment', title: P, message: P, is_read: false, created_at: 'bad-date', reference: { type: P, id: P, available: false } }], { unread_count: 2 }) },
    'GET /api/okr/approvals/': { body: page([{ objective: objective({ approval_status: 'submitted' }), submitted_at: '2026-11-04T08:00:00+07:00', key_results: [{ title: P, weight: '50.00' }], checks: {}, can_approve: false, block_reason: P }]) },
    'GET /api/okr/departments/': { body: page([{ id: 1, name: P, okr_manager: person }]) },
    'GET /api/okr/teams/': { body: page([
      { id: 4, name: P, description: P, department_id: 1, department_name: P, manager: person, status: 'active', member_count: 2, members: [person, { id: P, name: P }] },
      { id: 5, name: P, description: P, department_id: null, department_name: null, manager: null, status: 'inactive', member_count: P, members: [] },
    ]) },
    'GET /api/okr/roles/': { body: page([
      { id: 3, employee: person, role: 'admin', granted_at: '2026-11-05T09:30:00+07:00', granted_by: person },
      { id: 4, employee: person, role: P, granted_at: P, granted_by: null },
    ]) },
    'GET /api/okr/roles/history/': { body: page([
      { id: 1, employee: person, role: 'department_manager', action: 'granted', department: { id: 1, name: P }, actor: person, created_at: '2026-11-05T09:30:00+07:00' },
      { id: 2, employee: person, role: P, action: P, department: null, actor: null, created_at: P },
    ]) },
    'GET /api/okr/settings/': {
      body: {
        warning_threshold: P, reminder_frequency: P, email_reminders: true,
        rating_ranges: [{ min: P, max: '0.3', label: P }, { min: '0.3', max: '0.7', label: P }, { min: '0.7', max: '0.9', label: P }, { min: '0.9', max: '1', label: P }],
        reminder_schedule: { scheduled: false, command: P, channels: [P, 'in_app'], email_available: false },
      },
    },
    'GET /api/okr/people/': { body: page([{ id: 7, name: P, department_id: 1, department_name: P, manager_id: null }]) },
    'GET /api/okr/dashboard/company/': {
      body: {
        cycle, progress: { value: '40.00', objective_count: 1, total_weight: '100.00' }, expected_progress: '50.00',
        objective_stats: { total: 1, on_track: 1, at_risk: 0, off_track: 0, completed: 0 }, key_result_count: 2, stale_count: 1,
        attention: [attention], teams: [{ id: 4, name: P, objective_count: 1, progress: '30.00', health: 'at_risk' }], featured: [objective({ approval_status: 'approved', health: 'on_track' })],
      },
    },
    'GET /api/okr/reports/': {
      body: {
        cycle, progress: { value: '40.00', objective_count: 1, total_weight: '100.00' }, expected_progress: '50.00', objective_stats: {}, key_result_count: 1, stale_count: 1,
        rows: [objective()], by_scope: [{ label: P, count: P, progress: '40.00' }], by_owner: [{ label: P, count: 1, progress: '40.00' }], trend: [{ date: '2026-11-01', progress: P }],
        checkin_attention_by_owner: [{ owner: person, overdue_count: 1, off_track_count: 0, items: [attention] }],
      },
    },
    'GET /api/okr/tree/': { body: { cycle, expected_progress: '50.00', items: [{ ...objective({ scope: 'company' }), key_results: [{ id: 21, title: P, weight: '50.00', progress: '10.00', health: null, approval_status: 'draft', owner: person }], children: [{ ...objective({ id: 13 }), key_results: [], children: [] }] }], unlinked_items: [{ ...objective({ id: 15 }), key_results: [], children: [] }] } },
    'GET /api/okr/checkins/queue/': { body: page([{ ...kr({ permissions: { can_checkin: true, can_add_evidence: true } }), latest_checkin: checkin }]) },
    'GET /api/okr/checkins/management/': {
      body: {
        cycle, can_manage: true, scope: 'company', summary: { employee_count: 1, key_result_count: 1, never_checked_in: 0, checkin_count: 1 }, expected_progress: '50.00',
        departments: [{ id: 1, name: P, teams: [{ id: 4, name: P, people: [{ employee: person, objectives: [{ id: 12, title: P, scope: 'personal', progress: '10.00', key_results: [{ id: 21, title: P, unit: P, current_value: '1', target_value: '2', progress: '50.00', health: 'on_track', latest_checkin_at: checkin.created_at, checkins: [checkin] }] }] }] }] }],
        unassigned: [{ employee: person, objectives: [] }],
      },
    },
  };
}

const SCREENS = [
  ['dashboard', {}],
  ['my-okrs', {}],
  ['company-okrs', {}],
  ['tree', {}],
  ['teams', {}],
  ['check-ins', {}],
  ['reports', {}],
  ['notifications', {}],
  ['objective-detail', { id: 12 }],
  ['key-result-detail', { id: 21 }],
  ['admin/approvals', {}],
  ['admin/roles', {}],
  ['admin/departments', {}],
  ['admin/teams', {}],
  ['admin/cycles', {}],
  ['admin/settings', {}],
];

for (const [name, params] of SCREENS) {
  test(`screen "${name}" renders Django data with payloads safely`, async () => {
    const search = `?search=${encodeURIComponent(P)}&cycle_id=${encodeURIComponent(P)}`;
    const { JoyOKR, calls } = load(VIEW_FILES, {
      routes: poisonedRoutes(),
      location: makeLocation('/okr/x', search),
      config: makeConfig({ capabilities: ADMIN_CAPS, currentUser: { id: 7, display_name: P, capabilities: ADMIN_CAPS } }),
    });
    const outlet = { innerHTML: '' };
    const rendered = await JoyOKR.views.render(name, { params, outlet, isCurrent: () => true });
    assert.equal(rendered, true);
    assert.ok(outlet.innerHTML.length > 100, 'something rendered');
    assertSafeMarkup(outlet.innerHTML, name);
    // URL query values never reach the API unvalidated (the cycle_id payload is dropped;
    // the dashboard drill-down sends the numeric cycle id from the response).
    calls.forEach((call) => {
      if ('cycle' in call.query) assert.match(call.query.cycle, /^\d+$/, `${name}: invalid cycle id forwarded`);
    });
    if (name.startsWith('admin/')) assert.ok(!outlet.innerHTML.includes('Sẽ hoàn thiện ở giai đoạn W3'), `${name}: W3 placeholder is gone`);
  });
}

test('search box keeps the escaped query value', async () => {
  const { JoyOKR } = load(VIEW_FILES, { routes: poisonedRoutes(), location: makeLocation('/okr/my-okrs', `?search=${encodeURIComponent(XSS.tag)}`) });
  const outlet = { innerHTML: '' };
  await JoyOKR.views.render('my-okrs', { params: {}, outlet, isCurrent: () => true });
  assert.ok(outlet.innerHTML.includes('name="search" value="&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"'));
});

test('key result detail: no javascript: evidence link, file evidence via /api download path, delete button carries the objective id', async () => {
  const { JoyOKR } = load(VIEW_FILES, { routes: poisonedRoutes(), config: makeConfig({ capabilities: ADMIN_CAPS }) });
  const outlet = { innerHTML: '' };
  await JoyOKR.views.render('key-result-detail', { params: { id: 21 }, outlet, isCurrent: () => true });
  const html = outlet.innerHTML;
  assert.ok(!html.includes('javascript:'));
  assert.ok(html.includes('href="/api/okr/evidence/2/download/"'));
  assert.ok(html.includes('data-action="open-checkin"'), 'permissions.can_checkin shows CHECK-IN');
  assert.ok(html.includes('data-action="delete-key-result"') && html.includes('data-objective-id="12"'));
});
