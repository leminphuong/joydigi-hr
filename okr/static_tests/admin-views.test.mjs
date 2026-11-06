// W3 admin screens (roles, departments, teams, cycles, settings) rendered
// from Django-shaped data through api.js, and the configured at-risk
// threshold in the views' own health colours.
import test from 'node:test';
import assert from 'node:assert/strict';
import { ADMIN_CAPS, load, makeConfig, makeLocation, objectiveDetail, keyResult, owner, page } from './harness.mjs';

const VIEW_FILES = ['domain.js', 'api.js', 'store.js', 'views.js'];
const IMG = '<img src=x onerror=alert(1)>';

function cycleRow(overrides = {}) {
  return { id: 3, name: 'Q4 2026', cycle_type: 'quarter', start_date: '2026-10-01', end_date: '2026-12-31', status: 'active', parent_id: null, objective_count: 0, ...overrides };
}

function adminRoutes(overrides = {}) {
  return {
    'GET /api/okr/cycles/': { body: page([
      cycleRow({ id: 3, status: 'active', objective_count: 12, parent_id: 4 }),
      cycleRow({ id: 4, name: 'Năm 2026', cycle_type: 'year', start_date: '2026-01-01', status: 'closed', objective_count: 30 }),
      cycleRow({ id: 5, name: 'Q1 2027', start_date: '2027-01-01', end_date: '2027-03-31', status: 'draft' }),
    ]) },
    'GET /api/okr/teams/': { body: page([
      { id: 4, name: 'Team A', description: '', department_id: 1, department_name: 'Kinh doanh', manager: owner(8, 'Trần Quản Lý'), status: 'active', member_count: 2, members: [owner(7, 'Nguyễn Văn A'), owner(9, 'Lê Thị B')] },
      { id: 5, name: 'Team cũ', description: '', department_id: null, department_name: null, manager: null, status: 'inactive', member_count: 0, members: [] },
    ]) },
    'GET /api/okr/departments/': { body: page([{ id: 1, name: 'Kinh doanh', okr_manager: owner(8, 'Trần Quản Lý') }, { id: 2, name: 'Kỹ thuật', okr_manager: null }]) },
    'GET /api/okr/roles/': { body: page([
      { id: 3, employee: owner(8, 'Trần Quản Lý'), role: 'admin', granted_at: '2026-11-05T09:30:00+07:00', granted_by: owner(1, 'Người cấp') },
      { id: 4, employee: owner(9, 'Lê Thị B'), role: 'manager', granted_at: '2026-11-04T09:30:00+07:00', granted_by: null },
    ]) },
    'GET /api/okr/roles/history/': { body: page([
      { id: 2, employee: owner(9, 'Lê Thị B'), role: 'department_manager', action: 'granted', department: { id: 1, name: 'Kinh doanh' }, actor: owner(8, 'Trần Quản Lý'), created_at: '2026-11-05T11:00:00+07:00' },
      { id: 1, employee: owner(7, 'Nguyễn Văn A'), role: 'admin', action: 'revoked', department: null, actor: null, created_at: '2026-11-04T08:00:00+07:00' },
    ]) },
    'GET /api/okr/settings/': { body: {
      warning_threshold: '15.00', reminder_frequency: 'biweekly', email_reminders: true,
      rating_ranges: [{ min: '0.00', max: '0.30', label: 'Chưa đạt' }, { min: '0.30', max: '0.70', label: 'Cần cố gắng' }, { min: '0.70', max: '0.90', label: 'Đạt' }, { min: '0.90', max: '1.00', label: 'Xuất sắc' }],
      reminder_schedule: { scheduled: false, command: 'okr_checkin_reminders', channels: ['in_app'], email_available: false },
    } },
    ...overrides,
  };
}

async function renderScreen(name, { routes = adminRoutes(), search = '', config = makeConfig({ capabilities: ADMIN_CAPS }) } = {}) {
  const { JoyOKR, calls } = load(VIEW_FILES, { routes, config, location: makeLocation(`/okr/${name}`, search) });
  const outlet = { innerHTML: '' };
  const rendered = await JoyOKR.views.render(name, { params: {}, outlet, isCurrent: () => true });
  assert.equal(rendered, true);
  return { html: outlet.innerHTML, calls };
}

/** Table rows of the first <tbody> whose text includes `needle`. */
function rowWith(html, needle) {
  return (html.match(/<tr>[\s\S]*?<\/tr>/g) || []).find((row) => row.includes(needle)) || '';
}

function assertNoInjectedMarkup(html, label) {
  assert.ok(!html.includes('<img'), `${label}: <img> element`);
  for (const tag of html.match(/<[a-zA-Z][^>]*>/g) || []) {
    const bare = tag.replace(/"[^"]*"/g, '""');
    assert.ok(!/\son[a-z]+\s*=/i.test(bare), `${label}: on* attribute in ${tag}`);
  }
}

test('admin/cycles: status toolbar, objective count, closed rows offer "Mở lại" and no "Sửa", clone card', async () => {
  const { html, calls } = await renderScreen('admin/cycles');
  const cycleCall = calls.find((call) => call.path === '/api/okr/cycles/');
  assert.deepEqual(cycleCall.query, { page_size: '100' }, 'no status filter by default ("Tất cả")');
  assert.match(html, /<form class="joyokr-toolbar joyokr-section" data-filter-form data-filter-route="admin\/cycles"><select class="joyokr-select" name="status"/);
  for (const [value, label] of [['all', 'Tất cả'], ['draft', 'Nháp'], ['active', 'Đang hoạt động'], ['closed', 'Đã đóng']]) {
    assert.ok(html.includes(`<option value="${value}" ${value === 'all' ? 'selected' : ''}>${label}</option>`), `${value} option`);
  }

  const active = rowWith(html, 'Q4 2026');
  assert.ok(active.includes('<td>12</td>'), 'objective count');
  assert.ok(active.includes('Quý · thuộc Năm 2026'), 'cycle type and parent name');
  assert.ok(active.includes('data-action="edit-cycle"'));
  assert.ok(active.includes('data-action="cycle-status" data-id="3" data-status="closed" data-current="active">Đóng chu kỳ'));
  assert.ok(active.includes('joyokr-badge--active">Đang hoạt động'));

  const closed = rowWith(html, 'Năm 2026</strong>');
  assert.ok(!closed.includes('data-action="edit-cycle"'), 'closed cycles cannot be edited');
  assert.ok(closed.includes('data-status="active" data-current="closed">Mở lại'));
  assert.ok(closed.includes('<td>30</td>'));
  assert.ok(closed.includes('joyokr-badge--closed">Đã đóng'));

  const draft = rowWith(html, 'Q1 2027');
  assert.ok(draft.includes('data-action="edit-cycle"'));
  assert.ok(draft.includes('data-status="active" data-current="draft">Kích hoạt'));
  assert.ok(draft.includes('joyokr-badge--draft">Nháp'));

  assert.ok(html.includes('Clone OKR sang chu kỳ mới') && html.includes('data-action="clone-cycle"'));
  assert.ok(html.includes('data-action="open-cycle"'));
});

test('admin/cycles: ?status=closed is sent to the API, ?status=all is not', async () => {
  const closed = await renderScreen('admin/cycles', { search: '?status=closed' });
  assert.equal(closed.calls.find((call) => call.path === '/api/okr/cycles/').query.status, 'closed');
  assert.ok(closed.html.includes('<option value="closed" selected>Đã đóng</option>'));
  const all = await renderScreen('admin/cycles', { search: '?status=all' });
  assert.ok(!('status' in all.calls.find((call) => call.path === '/api/okr/cycles/').query));
});

test('admin/teams: JOYOKR table with members, status toolbar and Sửa / Thành viên / Ẩn / Khôi phục', async () => {
  const { html, calls } = await renderScreen('admin/teams');
  assert.equal(calls.find((call) => call.path === '/api/okr/teams/').query.status, 'active', 'JOYOKR shows active teams by default');
  assert.ok(html.includes('<option value="active" selected>Đang hoạt động</option><option value="inactive" >Đã ẩn</option><option value="all" >Tất cả</option>'));
  const active = rowWith(html, 'Team A');
  assert.ok(active.includes('Nguyễn Văn A, Lê Thị B'));
  assert.ok(active.includes('Kinh doanh') && active.includes('Trần Quản Lý'));
  for (const action of ['edit-team', 'team-members', 'delete-team']) assert.ok(active.includes(`data-action="${action}" data-id="4"`), action);
  assert.ok(!active.includes('restore-team'));
  const inactive = rowWith(html, 'Team cũ');
  assert.ok(inactive.includes('data-action="restore-team" data-id="5">Khôi phục'));
  assert.ok(inactive.includes('joyokr-badge--inactive">Đã ẩn'));
  assert.ok(!inactive.includes('delete-team'));
  assert.ok(html.includes('data-action="open-team"'));

  const all = await renderScreen('admin/teams', { search: '?status=all' });
  assert.ok(!('status' in all.calls.find((call) => call.path === '/api/okr/teams/').query));
});

test('admin/departments: HR-owned list with the OKR manager and one action, no create/hide/restore', async () => {
  const { html } = await renderScreen('admin/departments');
  assert.ok(html.includes('<p>Phòng ban do Web HR quản lý; ở đây chỉ gán người quản lý OKR.</p>'));
  assert.ok(html.includes('<th>Phòng ban</th><th>Quản lý OKR</th><th></th>'));
  assert.ok(rowWith(html, 'Kinh doanh').includes('Trần Quản Lý'));
  assert.ok(rowWith(html, 'Kỹ thuật').includes('Chưa gán'));
  assert.ok(html.includes('data-action="assign-department-manager" data-id="2">Gán quản lý OKR'));
  for (const action of ['open-department', 'edit-department', 'delete-department', 'restore-department']) assert.ok(!html.includes(action), action);
});

test('admin/roles: OKR roles separate from HR, grant/revoke table and history card', async () => {
  const { html, calls } = await renderScreen('admin/roles');
  assert.deepEqual(calls.filter((call) => call.path.startsWith('/api/okr/roles')).map((call) => [call.path, call.query.page_size]), [
    ['/api/okr/roles/', '100'], ['/api/okr/roles/history/', '100'],
  ]);
  assert.ok(html.includes('<h1>Vai trò OKR</h1>'));
  assert.ok(html.includes('quản trị viên HR không tự động là Quản trị OKR'));
  assert.ok(html.includes('data-action="open-role" >+ Gán vai trò'));
  assert.ok(html.includes('<th>Nhân sự</th><th>Vai trò</th><th>Cấp bởi</th><th>Ngày cấp</th><th></th>'));
  const admin = rowWith(html, 'Người cấp');
  assert.ok(admin.includes('Trần Quản Lý') && admin.includes('>Quản trị OKR</span>'));
  assert.ok(admin.includes(new Intl.DateTimeFormat('vi-VN').format(new Date('2026-11-05T09:30:00+07:00'))), 'granted date');
  assert.ok(admin.includes('data-action="revoke-role" data-id="3" data-role="admin">Thu hồi'));
  assert.ok(rowWith(html, 'data-role="manager"').includes('>Quản lý OKR</span>'));

  assert.ok(html.includes('Lịch sử cấp/thu hồi quyền'));
  assert.ok(html.includes('<th>Thời gian</th><th>Nhân sự</th><th>Vai trò</th><th>Thao tác</th><th>Người thực hiện</th>'));
  const departmentGrant = rowWith(html, 'Quản lý OKR phòng ban · Kinh doanh');
  assert.ok(departmentGrant.includes('joyokr-badge--approved">Cấp'));
  assert.ok(rowWith(html, 'Nguyễn Văn A').includes('joyokr-badge--rejected">Thu hồi'));
});

test('admin/roles: the history card shows at most 100 changes', async () => {
  const events = Array.from({ length: 120 }, (_, index) => ({
    id: index + 1, employee: owner(7, `Người ${index}`), role: 'manager', action: 'granted', department: null, actor: null, created_at: '2026-11-05T09:30:00+07:00',
  }));
  const { html } = await renderScreen('admin/roles', { routes: adminRoutes({ 'GET /api/okr/roles/history/': { body: page(events, { count: 250 }) } }) });
  const history = html.slice(html.indexOf('Lịch sử cấp/thu hồi quyền'));
  assert.equal((history.match(/<tr>/g) || []).length - 1, 100, '100 rows plus the header');
  assert.ok(history.includes('Hiển thị 100 thay đổi gần nhất trên tổng số 250.'));
});

test('admin/settings: rating labels, reminders and the real reminder schedule; no WordPress cards', async () => {
  const { html } = await renderScreen('admin/settings');
  assert.ok(html.includes('data-action="save-settings" >Lưu cài đặt'));
  assert.ok(html.includes('<form data-settings-form'));
  assert.ok(html.includes('<input type="hidden" name="rating_0_min" value="0.00"><input type="hidden" name="rating_0_max" value="0.30">'));
  assert.ok(html.includes('<input name="rating_3_label" value="Xuất sắc" required>'));
  assert.ok(html.includes('name="warning_threshold" min="0" max="100" step="0.01" value="15"'));
  assert.ok(html.includes('<option value="biweekly" selected>Hai tuần/lần</option>'));
  assert.ok(html.includes('<input type="checkbox" name="email_reminders" checked>'));
  assert.ok(html.includes('Máy chủ hiện chưa gửi email; tùy chọn chỉ được lưu lại.'));

  const schedule = html.slice(html.indexOf('Trạng thái lịch nhắc'));
  assert.ok(schedule.includes('<dt>Tần suất đã cấu hình</dt><dd>Hai tuần/lần</dd>'));
  assert.ok(schedule.includes('<dt>Lịch chạy tự động</dt><dd><span class="joyokr-badge joyokr-badge--at_risk">Chưa kích hoạt trên máy chủ</span></dd>'));
  assert.ok(schedule.includes('<dt>Kênh</dt><dd>Thông báo trong ứng dụng</dd>'));
  assert.ok(schedule.includes('<dt>Email</dt><dd>Chưa hỗ trợ</dd>'));
  assert.ok(schedule.includes('<dt>Lệnh</dt><dd><code>okr_checkin_reminders</code></dd>'));
  assert.ok(!schedule.includes('<form'), 'the schedule card is outside the settings form');

  for (const wordpressOnly of ['Kết nối dữ liệu', 'Bảo mật WordPress', 'Workspace SSO', 'Telegram', 'Dữ liệu mẫu', 'seed-demo', 'transfer-admin-account']) {
    assert.ok(!html.includes(wordpressOnly), wordpressOnly);
  }

  const running = await renderScreen('admin/settings', {
    routes: adminRoutes({ 'GET /api/okr/settings/': { body: { ...adminRoutes()['GET /api/okr/settings/'].body, reminder_schedule: { scheduled: true, command: 'okr_checkin_reminders', channels: ['in_app'], email_available: false } } } }),
  });
  assert.ok(running.html.includes('joyokr-badge--approved">Đang chạy'));
  assert.ok(!running.html.includes('Chưa kích hoạt trên máy chủ'));
});

test('XSS: team, employee, department and cycle names like <img src=x onerror=alert(1)> stay text in the admin tables', async () => {
  const person = { id: 7, name: IMG };
  const routes = adminRoutes({
    'GET /api/okr/cycles/': { body: page([cycleRow({ name: IMG, parent_id: 4 }), cycleRow({ id: 4, name: IMG, status: 'closed' })]) },
    'GET /api/okr/teams/': { body: page([{ id: 4, name: IMG, description: IMG, department_id: 1, department_name: IMG, manager: person, status: 'active', member_count: 1, members: [person] }]) },
    'GET /api/okr/departments/': { body: page([{ id: 1, name: IMG, okr_manager: person }]) },
    'GET /api/okr/roles/': { body: page([{ id: 3, employee: person, role: 'admin', granted_at: '2026-11-05T09:30:00+07:00', granted_by: person }]) },
    'GET /api/okr/roles/history/': { body: page([{ id: 1, employee: person, role: 'department_manager', action: 'granted', department: { id: 1, name: IMG }, actor: person, created_at: '2026-11-05T09:30:00+07:00' }]) },
    'GET /api/okr/settings/': { body: { ...adminRoutes()['GET /api/okr/settings/'].body, rating_ranges: [{ min: '0', max: '1', label: IMG }] } },
  });
  for (const name of ['admin/cycles', 'admin/teams', 'admin/departments', 'admin/roles', 'admin/settings']) {
    const { html } = await renderScreen(name, { routes });
    assertNoInjectedMarkup(html, name);
    assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'), `${name}: the name is shown as text`);
  }
});

test('views use the configured threshold: with 30 a KR 20 points behind is at risk, with the default it is off track', async () => {
  const routes = {
    'GET /api/okr/objectives/12/': { body: objectiveDetail({ expected_progress: '50.00', key_results: [keyResult({ progress: '30.00', expected_progress: '50.00' })] }) },
    'GET /api/okr/cycles/': { body: page([cycleRow()]) },
    'GET /api/okr/comments/': { body: page([]) },
  };
  const render = async (config) => {
    const { JoyOKR } = load(VIEW_FILES, { routes, config });
    const outlet = { innerHTML: '' };
    await JoyOKR.views.render('objective-detail', { params: { id: 12 }, outlet, isCurrent: () => true });
    return outlet.innerHTML;
  };
  const relaxed = await render(makeConfig({ warningThreshold: 30 }));
  assert.ok(relaxed.includes('data-tone="warning"'));
  assert.ok(!relaxed.includes('data-tone="danger"'));
  const fallback = await render(makeConfig());
  assert.ok(fallback.includes('data-tone="danger"'), 'JOYOKR 15 when no threshold is configured');
});
