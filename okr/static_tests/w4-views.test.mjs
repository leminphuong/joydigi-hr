// W4 screens: dashboard filters and KPI cards, report sections, the cycle
// notice, the OKR tree links and the check-in management toolbar, rendered
// from Django-shaped data (including injection payloads) through api.js.
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ADMIN_CAPS, EMPLOYEE_CAPS, MANAGER_CAPS, XSS, keyResult, load, makeConfig, makeLocation, objectiveSummary, page,
} from './harness.mjs';

const P = `${XSS.tag}${XSS.attr}`;
const ESCAPED_P = '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;&#39; onmouseover=&#39;alert(1)';
const VIEW_FILES = ['domain.js', 'api.js', 'store.js', 'views.js'];

/** Same checks as escape.test.mjs: no raw tags, handlers or unquoted break-outs; numeric route ids. */
function assertSafeMarkup(html, label) {
  assert.ok(!html.includes('<img'), `${label}: raw <img>`);
  assert.ok(!html.includes('"><img'), `${label}: attribute break-out`);
  assert.ok(!html.includes("' onmouseover="), `${label}: single-quote break-out`);
  assert.ok(!/<script/i.test(html), `${label}: <script>`);
  for (const tag of html.match(/<[a-zA-Z][^>]*>/g) || []) {
    const bare = tag.replace(/"[^"]*"/g, '""');
    assert.ok(!/\son[a-z]+\s*=/i.test(bare), `${label}: inline handler in ${tag}`);
    assert.ok(!bare.includes("'"), `${label}: stray quote in ${tag}`);
    assert.ok(!/javascript:/i.test(tag), `${label}: javascript: URL in ${tag}`);
  }
  for (const [, kind, id] of html.matchAll(/data-route="(objectives|key-results)\/([^"]*)"/g)) {
    assert.match(id, /^\d+$/, `${label}: ${kind} route id "${id}"`);
  }
}

const CYCLE = { id: 3, name: 'Q4 2026', cycle_type: 'quarter', start_date: '2026-10-01', end_date: '2026-12-31', status: 'active' };
const RULE = 'Khi chưa chọn chu kỳ: dùng chu kỳ đang hoạt động có ngày hôm nay nằm trong khoảng thời gian.';

function selection({ count = 2, by = 'default', name = 'Q4 2026', selectedId = 3 } = {}) {
  const cycles = [
    { id: 3, name, start_date: '2026-10-01', end_date: '2026-12-31' },
    { id: 9, name: 'Q1 2027', start_date: '2027-01-01', end_date: '2027-03-31' },
    { id: 10, name: 'Năm 2026', start_date: '2026-01-01', end_date: '2026-12-31' },
  ];
  return { selected_id: selectedId, selected_by: by, rule: RULE, active_cycles: cycles.slice(0, count) };
}

function checkin(overrides = {}) {
  return {
    id: 1, created_at: '2026-11-05T09:30:00+07:00', author: { id: 9, name: 'Lê Văn C' }, previous_value: '2', current_value: '4',
    previous_progress: '20.00', current_progress: '40.00', confidence: 4, update_text: 'Ký thêm 2 hợp đồng', blockers: '', next_steps: '',
    ...overrides,
  };
}

function managementKr(overrides = {}) {
  return {
    id: 21, title: 'Doanh thu 10 tỷ', unit: 'tỷ', current_value: '4', target_value: '10', progress: '40.00', health: 'at_risk',
    due_date: '2026-12-31', checkin_status: 'ok', checkin_total: 1, latest_checkin_at: '2026-11-05T09:30:00+07:00', checkins: [checkin()],
    ...overrides,
  };
}

/** Django-shaped W4 payloads; `text` fills every server string. */
function routes({ text = 'Kinh doanh', filters = {}, active = 2, by = 'default', canManage = true, keyResults, attentionTotal = 1, history, checkinCount } = {}) {
  const person = { id: 7, name: text };
  const attention = { key_result_id: 21, key_result_title: text, objective_title: text, reason: 'stale', progress: '10.00', expected_progress: '50.00', days_overdue: 2, last_checkin_at: null, owner: person };
  const historyRows = history || [{ ...checkin({ update_text: text, author: person }), key_result: { id: 21, title: text, unit: text }, objective_title: text, owner: person }];
  return {
    'GET /api/okr/departments/': { body: page([{ id: 1, name: text, okr_manager: null }]) },
    'GET /api/okr/teams/': { body: page([{ id: 4, name: text, description: '', department_id: 1, department_name: text, manager: null, status: 'active', member_count: 1 }]) },
    'GET /api/okr/people/': { body: page([{ id: 7, name: text, department_id: 1, department_name: text, manager_id: null }]) },
    'GET /api/okr/objectives/': { body: page([objectiveSummary({ id: 14, title: text, approval_status: 'approved', health: 'on_track', owner: person })]) },
    'GET /api/okr/approvals/': { body: page([]) },
    'GET /api/okr/notifications/': { body: page([], { unread_count: 0 }) },
    'GET /api/okr/dashboard/company/': {
      body: {
        cycle: CYCLE, cycle_selection: selection({ count: active, by, name: text }), filters,
        progress: { value: '40.00', objective_count: 1, total_weight: '100.00' }, selection_progress: '62.50', expected_progress: '50.00',
        objective_stats: { total: 3, on_track: 1, at_risk: 1, off_track: 0, completed: 1 },
        in_progress_count: 2, pending_approval_count: 1, key_result_count: 6, key_results_completed: 2, stale_count: 3,
        attention: [attention], attention_total: attentionTotal,
        teams: [{ id: 4, name: text, objective_count: 1, progress: '30.00', health: 'at_risk' }],
        featured: [objectiveSummary({ title: text, approval_status: 'approved', health: 'on_track', owner: person })],
      },
    },
    'GET /api/okr/reports/': {
      body: {
        cycle: CYCLE, cycle_selection: selection({ count: active, by, name: text }), filters,
        progress: { value: '40.00', objective_count: 1, total_weight: '100.00' }, selection_progress: '62.50', expected_progress: '50.00',
        objective_stats: {}, key_result_count: 2, stale_count: 1,
        rows: [objectiveSummary({ title: text, owner: person })], by_scope: [{ label: 'team', count: 1, progress: '40.00' }], by_owner: [{ label: text, count: 1, progress: '40.00' }],
        by_department: [{ label: text, count: 2, progress: '45.00' }], by_team: [{ label: text, count: 1, progress: '30.00' }],
        by_rating: [{ label: text, min: '0', max: '0.3', count: 1 }, { label: 'Đạt', min: '0.7', max: '1', count: 2 }],
        overdue_key_results: [{ id: 21, title: text, objective_id: 12, objective_title: text, owner: person, due_date: '2026-10-31', days_late: 5, progress: '40.00' }],
        checkin_count: checkinCount ?? historyRows.length,
        checkin_history: historyRows,
        trend: [{ date: '2026-11-01', progress: '20.00' }],
        checkin_attention_by_owner: [{ owner: person, overdue_count: 1, off_track_count: 0, items: [attention] }],
      },
    },
    'GET /api/okr/tree/': {
      body: {
        cycle: CYCLE, cycle_selection: selection({ count: active, by, name: text }), expected_progress: '50.00',
        items: [{ ...objectiveSummary({ id: 12, title: text, scope: 'company', approval_status: 'approved', health: 'on_track', owner: person }), key_results: [{ id: 21, title: text, weight: '50.00', progress: '10.00', health: 'off_track', approval_status: 'approved', owner: person, due_date: '2026-12-31' }], children: [{ ...objectiveSummary({ id: 13, title: text, owner: person }), key_results: [{ id: 23, title: text, weight: '100.00', progress: '0.00', health: null, approval_status: 'draft', owner: person, due_date: null }], children: [] }] }],
        unlinked_items: [{ ...objectiveSummary({ id: 15, title: text, owner: person }), key_results: [{ id: 25, title: text, weight: '100.00', progress: '5.00', health: null, approval_status: 'draft', owner: person, due_date: '2026-12-01' }], children: [] }],
      },
    },
    'GET /api/okr/checkins/queue/': { body: page([{ ...keyResult({ title: text, permissions: { can_checkin: true, can_add_evidence: true } }), latest_checkin: null }]) },
    'GET /api/okr/checkins/management/': {
      body: canManage
        ? {
          cycle: CYCLE, cycle_selection: selection({ count: active, by, name: text }), filters, can_manage: true, scope: 'company',
          summary: { employee_count: 1, key_result_count: 4, never_checked_in: 1, checkin_count: 31, overdue_count: 1, due_count: 1 }, expected_progress: '50.00',
          departments: [{ id: 1, name: text, teams: [{ id: 4, name: text, people: [{ employee: person, objectives: [{ id: 12, title: text, scope: 'personal', progress: '40.00', key_results: keyResults || [
            managementKr({ id: 21, title: text, unit: text, checkin_status: 'overdue', checkin_total: 30, checkins: [checkin({ update_text: text, author: person })] }),
            managementKr({ id: 22, checkin_status: 'due', checkin_total: 1 }),
            managementKr({ id: 23, checkin_status: 'never', checkin_total: 0, checkins: [], latest_checkin_at: null }),
            managementKr({ id: 24, checkin_status: 'ok', checkin_total: 1 }),
          ] }] }] }] }],
          unassigned: [],
        }
        : { cycle: CYCLE, cycle_selection: selection({ count: active, by, name: text }), filters: {}, can_manage: false, scope: 'self', summary: {}, departments: [], unassigned: [], expected_progress: null },
    },
  };
}

async function render(name, { search = '', capabilities = ADMIN_CAPS, ...options } = {}) {
  const { JoyOKR, calls } = load(VIEW_FILES, {
    routes: routes(options),
    location: makeLocation('/okr/x', search),
    config: makeConfig({ capabilities, currentUser: { id: 7, display_name: 'Người dùng', capabilities } }),
  });
  const outlet = { innerHTML: '' };
  const rendered = await JoyOKR.views.render(name, { params: {}, outlet, isCurrent: () => true });
  assert.equal(rendered, true);
  return { html: outlet.innerHTML, calls, JoyOKR };
}

function callsTo(calls, path) {
  return calls.filter((call) => call.path === path);
}

// -- escaping ---------------------------------------------------------------------------

for (const name of ['dashboard', 'reports', 'tree', 'check-ins']) {
  test(`W4 "${name}": payloads in every new section are escaped`, async () => {
    const filters = { scope: 'team', department_id: 1, team_id: 4, owner_id: 7, health: 'at_risk', status: 'overdue' };
    const { html } = await render(name, { text: P, filters, search: `?cycle_id=${encodeURIComponent(P)}&department_id=1` });
    assertSafeMarkup(html, name);
    assert.ok(html.includes(ESCAPED_P), `${name}: the payload is shown as text`);
    assert.ok(html.includes('data-cycle-notice'), `${name}: cycle notice rendered (payload cycle name)`);
  });
}

test('W4 report: team/department labels, overdue KR title, history update_text and owner names are escaped in their sections', async () => {
  const filters = { department_id: 1, health: 'at_risk' };
  const { html } = await render('reports', { text: P, filters });
  const section = (name) => {
    const match = html.match(new RegExp(`data-report-export-name="${name}"[\\s\\S]*?</table>`));
    assert.ok(match, `section ${name}`);
    return match[0];
  };
  for (const name of ['Theo phòng ban', 'Theo team', 'Theo mức độ hoàn thành', 'KR quá hạn hoàn thành', 'Lịch sử check-in']) {
    const markup = section(name);
    assertSafeMarkup(markup, name);
    assert.ok(markup.includes(ESCAPED_P), `${name} shows the escaped payload`);
  }
  // Filter names in the print header are escaped too, in text and data attributes.
  const header = html.match(/<header class="joyokr-print-header"[\s\S]*?<\/header>/)[0];
  assertSafeMarkup(header, 'print header');
  assert.ok(header.includes(`data-value="${ESCAPED_P}"`));
});

test('W4 check-ins: KR titles, units, names and history text from the management tree are escaped', async () => {
  const { html } = await render('check-ins', { text: P });
  const tree = html.slice(html.indexOf('joyokr-checkin-tree '));
  assertSafeMarkup(tree, 'management tree');
  assert.ok(tree.includes(`<p>${ESCAPED_P}</p>`), 'update_text');
  assert.ok(tree.includes(`<option value="7" >${ESCAPED_P}</option>`), 'owner option from people');
});

// -- cycle notice -------------------------------------------------------------------------

test('cycle notice: only when two or more cycles are active; names the shown cycle and the default rule', async () => {
  const { JoyOKR } = load(VIEW_FILES);
  const { cycleNotice } = JoyOKR.views.helpers;
  assert.equal(cycleNotice(null, CYCLE), '');
  assert.equal(cycleNotice(selection({ count: 0 }), CYCLE), '');
  assert.equal(cycleNotice(selection({ count: 1 }), CYCLE), '');

  const twoByDefault = cycleNotice(selection({ count: 2 }), CYCLE);
  assert.ok(twoByDefault.includes('data-cycle-notice'));
  assert.ok(twoByDefault.includes('Có 2 chu kỳ đang hoạt động.'));
  assert.ok(twoByDefault.includes('Đang hiển thị: <strong>Q4 2026</strong>'));
  assert.ok(twoByDefault.includes('Q1 2027'));
  assert.ok(twoByDefault.includes(RULE), 'the rule is shown for a default pick');
  assert.equal((twoByDefault.match(/joyokr-badge--active">Đang hiển thị/g) || []).length, 1, 'exactly one cycle is marked as shown');

  const requested = cycleNotice(selection({ count: 3, by: 'request', selectedId: 9 }), { id: 9, name: 'Q1 2027' });
  assert.ok(requested.includes('Có 3 chu kỳ đang hoạt động.'));
  assert.ok(requested.includes('Đang hiển thị: <strong>Q1 2027</strong>'));
  assert.ok(!requested.includes(RULE), 'no rule when the user picked the cycle');
  assert.ok(requested.includes('Bạn đã chọn chu kỳ này.'));

  const closedPick = cycleNotice(selection({ count: 2, by: 'request', selectedId: 77 }), { id: 77, name: 'Q3 2026' });
  assert.ok(closedPick.includes('Đang hiển thị: <strong>Q3 2026</strong>'));
  assert.ok(closedPick.includes('không thuộc các chu kỳ đang hoạt động'));
});

for (const name of ['dashboard', 'reports', 'tree', 'check-ins']) {
  test(`cycle notice on "${name}": absent with one active cycle, present with two`, async () => {
    const single = await render(name, { active: 1 });
    assert.ok(!single.html.includes('data-cycle-notice'), 'one active cycle: no notice');
    const multiple = await render(name, { active: 2 });
    assert.ok(multiple.html.includes('data-cycle-notice'), 'two active cycles: notice');
    assert.ok(multiple.html.includes(RULE));
    assert.ok(multiple.html.includes('data-action="choose-cycle"'), 'the cycle picker stays');
  });
}

// -- dashboard ----------------------------------------------------------------------------

test('dashboard: filter toolbar, KPI cards and the trend use the same filters', async () => {
  const { html, calls } = await render('dashboard', {
    filters: { team_id: 4, owner_id: 7 },
    search: '?cycle_id=3&team_id=4&owner_user_id=7&health=at_risk',
  });
  assert.ok(html.includes('data-filter-route="dashboard"'));
  for (const name of ['scope', 'department_id', 'team_id', 'owner_user_id']) assert.ok(html.includes(`name="${name}"`), name);
  assert.ok(html.includes('<option value="4" selected>'), 'team filter kept selected');
  assert.deepEqual(callsTo(calls, '/api/okr/dashboard/company/')[0].query, { cycle: '3', team: '4', owner: '7' });
  const reportCall = callsTo(calls, '/api/okr/reports/')[0];
  assert.deepEqual(reportCall.query, { cycle: '3', team: '4', owner: '7' }, 'trend: same filters, no forced company scope, no health');
  for (const label of ['Key Result', 'Đang thực hiện', 'Chờ duyệt', 'Quá hạn check-in', 'Tiến độ theo bộ lọc / kỳ vọng']) {
    assert.ok(html.includes(`<span class="joyokr-stat__label">${label}</span>`), label);
  }
  assert.ok(html.includes('2 hoàn thành'));
  assert.ok(html.includes('62,5% / 50%'), 'selection progress on the card');
  assert.ok(html.includes('<strong>40%</strong>'), 'the hero keeps the official company progress');
  assert.ok(html.includes('không áp dụng bộ lọc'));
  assert.ok(html.includes('Theo bộ lọc: Team: Kinh doanh · Owner: Kinh doanh'));
});

test('dashboard without filters: official progress on the card and JOYOKR\'s company-level trend', async () => {
  const { html, calls } = await render('dashboard', { search: '?cycle_id=3' });
  assert.deepEqual(callsTo(calls, '/api/okr/reports/')[0].query, { cycle: '3', scope: 'company' });
  assert.ok(html.includes('<span class="joyokr-stat__label">Tiến độ hiện tại / kỳ vọng</span>'));
  assert.ok(html.includes('40% / 50%'));
  assert.ok(!html.includes('Tiến độ theo bộ lọc'));
});

test('dashboard: capped attention lists say "Hiển thị x / N" and link to the report with the same cycle and filters', async () => {
  const { html } = await render('dashboard', { filters: { team_id: 4 }, attentionTotal: 140 });
  const notes = html.match(/<div class="joyokr-attention-cap"[\s\S]*?<\/div>/g) || [];
  assert.equal(notes.length, 2, 'stale panel and "KR cần chú ý theo người"');
  assert.ok(notes[0].includes('Hiển thị 1 / 3, xem đầy đủ trong Báo cáo.'), 'stale panel: 1 shown of stale_count 3');
  assert.ok(notes[1].includes('Hiển thị 1 / 140, xem đầy đủ trong Báo cáo.'), 'attention card: 1 shown of attention_total 140');
  for (const note of notes) assert.ok(note.includes('data-route="reports" data-route-query="cycle_id=3&amp;team_id=4"'));

  const complete = await render('dashboard', { attentionTotal: 1 });
  const completeNotes = complete.html.match(/<div class="joyokr-attention-cap"/g) || [];
  assert.equal(completeNotes.length, 1, 'only the stale panel (3 stale, 1 shown) keeps its note');
});

// -- reports ------------------------------------------------------------------------------

test('reports: health select, selection progress next to the official one, new export sections', async () => {
  const filtered = await render('reports', { filters: { department_id: 1, health: 'at_risk' }, search: '?department_id=1&health=at_risk' });
  assert.deepEqual(callsTo(filtered.calls, '/api/okr/reports/')[0].query, { department: '1', health: 'at_risk' });
  assert.ok(filtered.html.includes('name="health"'));
  assert.ok(filtered.html.includes('<option value="">Tất cả trạng thái</option>'));
  assert.ok(filtered.html.includes('<option value="at_risk" selected>Có rủi ro</option>'));
  assert.ok(filtered.html.includes('Tiến độ OKR toàn công ty'));
  assert.ok(filtered.html.includes('<div class="joyokr-stat__label">Tiến độ theo bộ lọc</div><div class="joyokr-stat__value ">62,5%</div>'));
  assert.ok(filtered.html.includes('Bộ lọc: Phòng ban: Kinh doanh · Trạng thái: Có rủi ro'));
  for (const name of ['Theo phòng ban', 'Theo team', 'Theo mức độ hoàn thành', 'KR quá hạn hoàn thành', 'Lịch sử check-in', 'KR cần chú ý', 'Objective']) {
    assert.ok(filtered.html.includes(`data-report-export-name="${name}"`), name);
  }
  assert.ok(filtered.html.includes('<td>0% – 30%</td>'), 'rating band as a % range');
  assert.ok(filtered.html.includes('data-route="key-results/21">Kinh doanh</button>'), 'overdue KR links to its page');
  assert.ok(filtered.html.includes('data-route="objectives/12">Kinh doanh</button>'));
  assert.ok(filtered.html.includes('5 ngày'));
  assert.ok(filtered.html.includes('<td>2 → 4 Kinh doanh</td>'), 'value before → after with unit');
  assert.ok(filtered.html.includes('<td>20% → 40%</td>'));
  assert.ok(filtered.html.includes('<td>4/5</td>'));
  assert.ok(filtered.html.includes('data-report-filter data-label="Phòng ban" data-value="Kinh doanh"'));
  assert.ok(filtered.html.includes('data-report-exported-at'));

  const plainReport = await render('reports', {});
  assert.ok(!plainReport.html.includes('Tiến độ theo bộ lọc'), 'no selection figure without filters');
  assert.ok(plainReport.html.includes('<li>Không có (mọi OKR đã duyệt bạn được xem)</li>'));
});

test('reports: "x / tổng N lượt" only when the check-in history is capped', async () => {
  const entry = (id) => ({ ...checkin({ id }), key_result: { id: 21, title: 'KR', unit: 'tỷ' }, objective_title: 'O', owner: { id: 7, name: 'A' } });
  const capped = await render('reports', { history: [entry(1), entry(2)], checkinCount: 75 });
  assert.ok(capped.html.includes('<span class="joyokr-muted" data-checkin-history-count>2 / tổng 75 lượt · mới nhất trước</span>'));
  const whole = await render('reports', { history: [entry(1), entry(2)], checkinCount: 2 });
  assert.ok(whole.html.includes('<span class="joyokr-muted" data-checkin-history-count>2 lượt · mới nhất trước</span>'));
});

// -- tree ---------------------------------------------------------------------------------

test('tree: every Objective and KR node links to its detail page; expand/collapse stays', async () => {
  const { html } = await render('tree', {});
  for (const id of [12, 13, 15]) assert.ok(html.includes(`data-route="objectives/${id}"`), `objective ${id}`);
  for (const id of [21, 23, 25]) assert.ok(html.includes(`<div class="joyokr-tree-kr" data-route="key-results/${id}" tabindex="0" role="link">`), `KR ${id}`);
  assert.ok(html.includes('data-action="toggle-tree-node"'));
});

// -- check-in management --------------------------------------------------------------------

test('check-ins: status badges, due date, check-in totals, "quá hạn" / "đến hạn" chips and the "Xem toàn bộ" link', async () => {
  const { html } = await render('check-ins', { capabilities: MANAGER_CAPS });
  assert.ok(html.includes('<span class="joyokr-badge joyokr-badge--off_track">Quá hạn check-in</span>'));
  assert.ok(html.includes('<span class="joyokr-badge joyokr-badge--at_risk">Đến hạn</span>'));
  assert.ok(html.includes('<span class="joyokr-badge joyokr-badge--pending">Chưa check-in</span>'));
  assert.ok(html.includes('<span class="joyokr-badge joyokr-badge--on_track">Đúng hạn</span>'));
  assert.ok(html.includes('<span>Hạn 31/12/2026</span>'));
  assert.ok(html.includes('<span>30 lượt check-in</span>'));
  assert.ok(html.includes('<strong class="joyokr-text--off_track">1</strong> quá hạn'));
  assert.ok(html.includes('<strong class="joyokr-text--at_risk">1</strong> đến hạn'));
  assert.ok(html.includes('<strong>31</strong> lượt check-in'));
  // Only the newest check-ins are embedded; the KR page has the rest.
  assert.ok(html.includes('<button class="joyokr-link-button" type="button" data-route="key-results/21">Xem toàn bộ 30 lượt</button>'));
  assert.equal((html.match(/Xem toàn bộ \d+ lượt/g) || []).length, 1, 'no link when every check-in is embedded');
  // History keeps value / progress before -> after.
  assert.ok(html.includes('<strong>20% → 40%</strong>'));
  assert.ok(html.includes('2 → 4 tỷ · Confidence 4/5'));
});

test('check-ins: the manager toolbar drives checkins/management/ (department, team, owner, status)', async () => {
  const { html, calls } = await render('check-ins', {
    capabilities: MANAGER_CAPS,
    filters: { department_id: 1, team_id: 4, owner_id: 7, status: 'overdue' },
    search: '?cycle_id=3&department_id=1&team_id=4&owner_user_id=7&status=overdue',
  });
  assert.deepEqual(callsTo(calls, '/api/okr/checkins/management/')[0].query, { cycle: '3', department: '1', team: '4', owner: '7', status: 'overdue' });
  assert.deepEqual(callsTo(calls, '/api/okr/checkins/queue/')[0].query, { cycle: '3', page_size: '100' }, 'the personal queue is unchanged');
  const form = html.match(/<form class="joyokr-toolbar joyokr-checkin-tree__filters"[\s\S]*?<\/form>/)[0];
  assert.ok(form.includes('data-filter-route="check-ins"'));
  for (const name of ['department_id', 'team_id', 'owner_user_id', 'status']) assert.ok(form.includes(`name="${name}"`), name);
  assert.ok(form.includes('<option value="overdue" selected>Quá hạn check-in</option>'));
  for (const label of ['Đến hạn', 'Chưa check-in', 'Đúng hạn']) assert.ok(form.includes(`>${label}</option>`), label);
  assert.ok(form.includes('<input type="hidden" name="cycle_id" value="3">'));
  // Read-only: nothing in the tree checks in on someone's behalf.
  const tree = html.slice(html.indexOf('joyokr-checkin-tree '));
  assert.ok(!tree.includes('open-checkin'), 'no CHECK-IN action in the management tree');
});

test('check-ins: an employee (can_manage false) gets no toolbar, no tree and no filter-list requests', async () => {
  const { html, calls } = await render('check-ins', { capabilities: EMPLOYEE_CAPS, canManage: false, active: 1 });
  assert.ok(!html.includes('joyokr-checkin-tree'), 'no management tree');
  assert.ok(!html.includes('data-filter-route="check-ins"'), 'no toolbar');
  for (const path of ['/api/okr/departments/', '/api/okr/teams/', '/api/okr/people/']) assert.equal(callsTo(calls, path).length, 0, path);
  assert.ok(html.includes('data-action="open-checkin"'), 'the personal queue keeps its own CHECK-IN');
});
