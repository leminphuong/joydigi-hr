// W3 admin forms and confirmations in forms.js: cycle, clone, team, team
// members, department OKR manager, OKR role, settings; and server messages
// (400 cycle_closed, 409 last_admin) reaching the user.
import test from 'node:test';
import assert from 'node:assert/strict';
import { ADMIN_CAPS, load, makeConfig, makeDocument, objectiveDetail, owner, page } from './harness.mjs';

const FORM_FILES = ['domain.js', 'api.js', 'store.js', 'forms.js'];
const IMG = '<img src=x onerror=alert(1)>';

/** FormData stand-in reading a fake form's entries (forms.js uses new FormData(form)). */
class FakeFormData {
  constructor(form) { this.items = form ? [...form.entries] : []; }
  entries() { return this.items[Symbol.iterator](); }
  get(name) { return this.items.find(([key]) => key === name)?.[1] ?? null; }
  getAll(name) { return this.items.filter(([key]) => key === name).map(([, value]) => value); }
  append(name, value) { this.items.push([name, value]); }
}

function fakeButton() {
  const handlers = [];
  return { addEventListener(_type, handler) { handlers.push(handler); }, click() { handlers.splice(0).forEach((handler) => handler()); } };
}

/** A form with entries, a submit button, a [data-form-error] summary (unless noSummary) and field error slots. */
function fakeForm(values, { selector = '[data-dialog-form]', elements = {}, noSummary = false } = {}) {
  const fieldErrors = {};
  const summary = { textContent: '', hidden: true };
  const submitButton = { disabled: false, setAttribute() {}, removeAttribute() {} };
  const form = {
    entries: Array.isArray(values) ? values : Object.entries(values),
    elements,
    summary,
    fieldErrors,
    reportValidity: () => true,
    closest: (wanted) => (wanted === selector ? form : null),
    querySelector(wanted) {
      if (wanted === '[type="submit"]') return submitButton;
      if (wanted === '[data-form-error]') return noSummary ? null : summary;
      const field = wanted.match(/^\[data-field-error="(.+)"\]$/);
      if (field) return (fieldErrors[field[1]] ||= { textContent: '', closest: () => null });
      return null;
    },
    querySelectorAll: (wanted) => (wanted === '.joyokr-field__error' ? Object.values(fieldErrors) : []),
  };
  return form;
}

function setup(routes, { settingsForm = null, config = makeConfig({ capabilities: ADMIN_CAPS, warningThreshold: 15 }) } = {}) {
  const listeners = {};
  const content = { innerHTML: '' };
  const confirmButtons = {};
  let dialogForm = null;
  const dialog = {
    open: false,
    showModal() { this.open = true; },
    close() { this.open = false; },
    addEventListener() {},
    querySelector(wanted) {
      if (wanted === '[data-dialog-content]') return content;
      if (wanted === '[data-dialog-form]') return dialogForm;
      const choice = wanted.match(/^\[data-confirm-value="(yes|no)"\]$/);
      if (choice) return (confirmButtons[choice[1]] = fakeButton());
      return null;
    },
  };
  const document = makeDocument({
    querySelector(wanted) {
      if (wanted === '[data-dialog]') return dialog;
      if (wanted === '[data-settings-form]') return settingsForm;
      return null;
    },
    addEventListener(type, handler) { listeners[type] = handler; },
  });
  const loaded = load(FORM_FILES, { routes, config, document, extra: { FormData: FakeFormData, HTMLElement: class HTMLElement {} } });
  const notes = [];
  const navigations = [];
  loaded.JoyOKR.notify = (message, tone = 'success') => notes.push({ message, tone });
  loaded.JoyOKR.navigate = (route, options) => navigations.push({ route, options });
  return {
    ...loaded,
    content,
    confirmButtons,
    dialog,
    listeners,
    notes,
    navigations,
    useForm(form) { dialogForm = form; },
  };
}

async function settle() {
  for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

function cycleRow(overrides = {}) {
  return { id: 3, name: 'Q4 2026', cycle_type: 'quarter', start_date: '2026-10-01', end_date: '2026-12-31', status: 'active', parent_id: null, objective_count: 4, ...overrides };
}

const CYCLES = page([
  cycleRow(),
  cycleRow({ id: 4, name: 'Năm 2026', cycle_type: 'year', status: 'closed', objective_count: 9 }),
  cycleRow({ id: 5, name: 'Q1 2027', status: 'draft', objective_count: 0, parent_id: 4 }),
  cycleRow({ id: 6, name: 'Q2 2027', status: 'draft', objective_count: 0 }),
]);

const PEOPLE = page([
  { id: 7, name: 'Nguyễn Văn A', department_id: 1, department_name: 'Kinh doanh', manager_id: null },
  { id: 8, name: 'Trần Quản Lý', department_id: 1, department_name: 'Kinh doanh', manager_id: null },
  { id: 9, name: IMG, department_id: null, department_name: null, manager_id: null },
]);

const TEAM = { id: 4, name: 'Team A', description: 'Mô tả', department_id: 1, department_name: 'Kinh doanh', manager: owner(8, 'Trần Quản Lý'), status: 'active', member_count: 1, members: [owner(7, 'Nguyễn Văn A')] };

test('formNames lists the restored and new admin forms', () => {
  const { JoyOKR } = setup({});
  for (const name of ['department-manager', 'team', 'team-members', 'cycle', 'clone', 'role', 'settings']) {
    assert.ok(JoyOKR.forms.formNames.includes(name), name);
  }
  for (const gone of ['user', 'profile', 'password', 'avatar', 'department']) assert.ok(!JoyOKR.forms.formNames.includes(gone), gone);
});

test('cycle form: "Chu kỳ cha" is a select of the other cycles; "no parent" is sent as null', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/cycles/': { body: CYCLES },
    'GET /api/okr/cycles/5/': { body: cycleRow({ id: 5, name: 'Q1 2027', status: 'draft', parent_id: 4 }) },
    'PUT /api/okr/cycles/5/': { body: cycleRow({ id: 5 }) },
    'POST /api/okr/cycles/': { status: 201, body: cycleRow({ id: 7 }) },
  });
  const edit = await JoyOKR.forms.helpers.buildDefinition('cycle', { id: 5 });
  assert.equal(edit.title, 'Sửa chu kỳ');
  assert.ok(!edit.body.includes('type="number"'), 'no numeric parent id input');
  assert.ok(edit.body.includes('<select name="parent_id" ><option value="">— Không có —</option>'));
  assert.ok(edit.body.includes('<option value="4" selected>Năm 2026 · Năm</option>'));
  assert.ok(!edit.body.includes('<option value="5"'), 'a cycle is not its own parent');
  await edit.submit(fakeForm({ name: 'Q1 2027', cycle_type: 'quarter', start_date: '2027-01-01', end_date: '2027-03-31', parent_id: '' }));

  const create = await JoyOKR.forms.helpers.buildDefinition('cycle', {});
  assert.equal(create.title, 'Tạo chu kỳ');
  await create.submit(fakeForm({ name: 'Q3 2027', cycle_type: 'quarter', start_date: '2027-07-01', end_date: '2027-09-30', parent_id: '4' }));
  assert.deepEqual(calls.filter((call) => call.method !== 'GET').map((call) => [call.method, call.path, call.body.parent_id]), [
    ['PUT', '/api/okr/cycles/5/', null],
    ['POST', '/api/okr/cycles/', 4],
  ]);
});

test('400 cycle_closed: saving a closed cycle shows the server message in the dialog', async () => {
  const closed = 'Chu kỳ đã đóng. Hãy mở lại chu kỳ trước khi sửa.';
  const app = setup({
    'GET /api/okr/cycles/': { body: CYCLES },
    'GET /api/okr/cycles/4/': { body: cycleRow({ id: 4, name: 'Năm 2026', status: 'closed' }) },
    'PUT /api/okr/cycles/4/': { status: 400, body: { code: 'cycle_closed', message: closed, fields: {} } },
  });
  const form = fakeForm({ name: 'Năm 2026', cycle_type: 'year', start_date: '2026-01-01', end_date: '2026-12-31', parent_id: '' });
  app.useForm(form);
  await app.JoyOKR.forms.open('cycle', { id: 4 });
  assert.ok(app.content.innerHTML.includes('Sửa chu kỳ'));
  app.listeners.submit({ target: form, preventDefault() {} });
  await settle();
  assert.equal(form.summary.textContent, closed);
  assert.equal(form.summary.hidden, false);
  assert.ok(app.dialog.open, 'the dialog stays open');
  assert.equal(app.calls.filter((call) => call.method === 'PUT').length, 1);
});

test('clone form: sources are non-draft cycles, destinations are draft cycles', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/cycles/': { body: CYCLES },
    'POST /api/okr/cycles/3/clone/': { body: { message: 'Đã sao chép OKR sang chu kỳ mới.', objective_count: 4, destination_cycle_id: 6 } },
  });
  const definition = await JoyOKR.forms.helpers.buildDefinition('clone', {});
  const [source, destination] = definition.body.split('name="destination_cycle_id"');
  assert.ok(source.includes('value="3"') && source.includes('value="4"'));
  assert.ok(!source.includes('value="5"') && !source.includes('value="6"'));
  assert.ok(destination.includes('value="5"') && destination.includes('value="6"'));
  assert.ok(!destination.includes('value="3"') && !destination.includes('value="4"'));
  const result = await definition.submit(fakeForm({ source_cycle_id: '3', destination_cycle_id: '6' }));
  assert.deepEqual(calls.at(-1).body, { destination_cycle_id: 6 });
  assert.equal(calls.at(-1).path, '/api/okr/cycles/3/clone/');
  assert.equal(result.message, 'Đã sao chép OKR sang chu kỳ mới.');
});

test('team form: department select, manager from people, PUT with manager_id', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/departments/': { body: page([{ id: 1, name: 'Kinh doanh', okr_manager: null }, { id: 2, name: 'Kỹ thuật', okr_manager: null }]) },
    'GET /api/okr/people/': { body: PEOPLE },
    'GET /api/okr/teams/4/': { body: TEAM },
    'PUT /api/okr/teams/4/': { body: TEAM },
  });
  const definition = await JoyOKR.forms.helpers.buildDefinition('team', { id: 4 });
  assert.equal(definition.title, 'Sửa team');
  assert.ok(definition.body.includes('<option value="1" selected>Kinh doanh</option>'));
  assert.ok(definition.body.includes('<option value="8" selected>Trần Quản Lý · Kinh doanh</option>'));
  assert.ok(definition.body.includes('&lt;img src=x onerror=alert(1)&gt;') && !definition.body.includes('<img'));
  await definition.submit(fakeForm({ name: 'Team A', department_id: '2', manager_user_id: '7', description: 'Mới' }));
  assert.deepEqual(calls.at(-1).body, { name: 'Team A', description: 'Mới', department_id: 2, manager_id: 7 });
});

test('team members form: people checkboxes, name filter, PUT employee_ids; employee_ids errors show inline', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/people/': { body: PEOPLE },
    'GET /api/okr/teams/4/': { body: TEAM },
    'PUT /api/okr/teams/4/members/': ({ body }) => (body.employee_ids.includes(99)
      ? { status: 400, body: { code: 'validation_error', message: 'Dữ liệu chưa hợp lệ.', fields: { employee_ids: 'Nhân sự không hợp lệ: 99.' } } }
      : { body: TEAM }),
  });
  const definition = await JoyOKR.forms.helpers.buildDefinition('team-members', { id: 4 });
  assert.equal(definition.title, 'Thành viên · Team A');
  assert.ok(definition.body.includes('<input type="checkbox" name="user_ids" value="7" checked>Nguyễn Văn A'));
  assert.ok(definition.body.includes('<input type="checkbox" name="user_ids" value="8" >Trần Quản Lý'));
  assert.ok(!definition.body.includes('<img'), 'names are escaped');
  assert.ok(definition.body.includes('data-field-error="user_ids"'));
  assert.ok(definition.body.includes('data-member-name="nguyen van a"'));

  // The filter hides non-matching choices without unchecking them.
  let onInput = null;
  const filter = { value: '', addEventListener(_type, handler) { onInput = handler; } };
  const choices = ['nguyen van a', 'tran quan ly'].map((memberName) => ({ dataset: { memberName }, hidden: false }));
  const emptyNote = { hidden: true };
  definition.initialize({
    querySelector: (wanted) => (wanted === '[data-member-filter]' ? filter : wanted === '[data-member-empty]' ? emptyNote : null),
    querySelectorAll: () => choices,
  });
  filter.value = 'Trần';
  onInput();
  assert.deepEqual(choices.map((choice) => choice.hidden), [true, false]);
  filter.value = 'zzz';
  onInput();
  assert.equal(emptyNote.hidden, false);

  await definition.submit(fakeForm([['user_ids', '7'], ['user_ids', '8']]));
  assert.deepEqual(calls.at(-1).body, { employee_ids: [7, 8] });
  await assert.rejects(definition.submit(fakeForm([['user_ids', '99']])), (error) => error.fields.user_ids === 'Nhân sự không hợp lệ: 99.');
});

test('department manager form: people plus "— Không gán —", PUT departments/{id}/okr-manager/', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/people/': { body: PEOPLE },
    'GET /api/okr/departments/': { body: page([{ id: 1, name: 'Kinh doanh', okr_manager: owner(8, 'Trần Quản Lý') }]) },
    'PUT /api/okr/departments/1/okr-manager/': ({ body }) => ({ body: { id: 1, name: 'Kinh doanh', okr_manager: body.manager_id ? owner(body.manager_id, 'X') : null } }),
  });
  const definition = await JoyOKR.forms.helpers.buildDefinition('department-manager', { id: 1 });
  assert.equal(definition.title, 'Gán quản lý OKR');
  assert.ok(definition.body.includes('<option value="">— Không gán —</option>'));
  assert.ok(definition.body.includes('<option value="8" selected>Trần Quản Lý · Kinh doanh</option>'));
  await definition.submit(fakeForm({ manager_user_id: '' }));
  await definition.submit(fakeForm({ manager_user_id: '7' }));
  assert.deepEqual(calls.filter((call) => call.method === 'PUT').map((call) => call.body), [{ manager_id: null }, { manager_id: 7 }]);
  await assert.rejects(JoyOKR.forms.helpers.buildDefinition('department-manager', { id: 99 }), /Không tìm thấy phòng ban/);
});

test('role form: employee from people, role Quản trị OKR / Quản lý OKR; 409 role_exists keeps the message', async () => {
  const { JoyOKR, calls } = setup({
    'GET /api/okr/people/': { body: PEOPLE },
    'POST /api/okr/roles/': ({ body }) => (body.employee_id === 8
      ? { status: 409, body: { code: 'role_exists', message: 'Nhân sự đã có vai trò này.', fields: {} } }
      : { status: 201, body: { id: 5, employee: owner(7, 'Nguyễn Văn A'), role: body.role, granted_at: '2026-11-05T09:30:00+07:00', granted_by: null } }),
  });
  const definition = await JoyOKR.forms.helpers.buildDefinition('role', {});
  assert.ok(definition.body.includes('<option value="admin" >Quản trị OKR</option><option value="manager" selected>Quản lý OKR</option>'));
  assert.ok(definition.body.includes('<select name="employee_id" required>'));
  const result = await definition.submit(fakeForm({ employee_id: '7', role: 'admin' }));
  assert.deepEqual(calls.at(-1).body, { employee_id: 7, role: 'admin' });
  assert.equal(result.message, 'Đã gán vai trò OKR.');
  await assert.rejects(definition.submit(fakeForm({ employee_id: '8', role: 'admin' })), (error) => error.status === 409 && error.message === 'Nhân sự đã có vai trò này.');
});

test('409 last_admin: revoking the last OKR admin shows the server message to the user', async () => {
  const lastAdmin = 'Không thể gỡ quản trị OKR cuối cùng của công ty.';
  const app = setup({ 'DELETE /api/okr/roles/3/': { status: 409, body: { code: 'last_admin', message: lastAdmin, fields: {} } } });
  const pending = app.JoyOKR.forms.handleAction('revoke-role', { dataset: { id: '3', role: 'admin' } });
  assert.ok(app.content.innerHTML.includes('Thu hồi vai trò OKR?'));
  assert.ok(app.content.innerHTML.includes('Quản trị OKR'));
  app.confirmButtons.yes.click();
  await pending;
  assert.deepEqual(app.calls.map((call) => `${call.method} ${call.path}`), ['DELETE /api/okr/roles/3/']);
  assert.deepEqual(app.notes, [{ message: lastAdmin, tone: 'error' }]);
  assert.deepEqual(app.navigations, [], 'the page is not refreshed on failure');
});

test('revoke role: cancelling sends nothing; confirming a manager revoke refreshes the page', async () => {
  const app = setup({ 'DELETE /api/okr/roles/4/': { body: { message: 'Đã gỡ vai trò.' } } });
  const cancelled = app.JoyOKR.forms.handleAction('revoke-role', { dataset: { id: '4', role: 'manager' } });
  app.confirmButtons.no.click();
  await cancelled;
  assert.equal(app.calls.length, 0);
  const confirmed = app.JoyOKR.forms.handleAction('revoke-role', { dataset: { id: '4', role: 'manager' } });
  app.confirmButtons.yes.click();
  await confirmed;
  assert.deepEqual(app.notes, [{ message: 'Đã thu hồi vai trò OKR.', tone: 'success' }]);
  assert.equal(app.navigations.length, 1);
});

test('cycle status confirmation says closing blocks updates, then POSTs status/', async () => {
  const app = setup({ 'POST /api/okr/cycles/3/status/': ({ body }) => ({ body: cycleRow({ status: body.status }) }) });
  const closing = app.JoyOKR.forms.handleAction('cycle-status', { dataset: { id: '3', status: 'closed', current: 'active' } });
  assert.ok(app.content.innerHTML.includes('Đóng chu kỳ?'));
  assert.ok(app.content.innerHTML.includes('chặn mọi cập nhật OKR'));
  app.confirmButtons.yes.click();
  await closing;
  const reopening = app.JoyOKR.forms.handleAction('cycle-status', { dataset: { id: '4', status: 'active', current: 'closed' } });
  assert.ok(app.content.innerHTML.includes('Mở lại chu kỳ?'));
  app.confirmButtons.no.click();
  await reopening;
  assert.deepEqual(app.calls.map((call) => [call.method, call.path, call.body]), [['POST', '/api/okr/cycles/3/status/', { status: 'closed' }]]);
  assert.deepEqual(app.notes, [{ message: 'Đã cập nhật trạng thái chu kỳ.', tone: 'success' }]);
});

test('hide and restore team confirmations map to status inactive / active', async () => {
  const app = setup({ 'POST /api/okr/teams/4/status/': ({ body }) => ({ body: { ...TEAM, status: body.status } }) });
  const hiding = app.JoyOKR.forms.handleAction('delete-team', { dataset: { id: '4' } });
  assert.ok(app.content.innerHTML.includes('Ẩn team?'));
  app.confirmButtons.yes.click();
  await hiding;
  const restoring = app.JoyOKR.forms.handleAction('restore-team', { dataset: { id: '4' } });
  assert.ok(app.content.innerHTML.includes('Khôi phục team?'));
  app.confirmButtons.yes.click();
  await restoring;
  assert.deepEqual(app.calls.map((call) => call.body), [{ status: 'inactive' }, { status: 'active' }]);
  assert.deepEqual(app.notes.map((note) => note.message), ['Đã ẩn team.', 'Đã khôi phục team.']);
});

function settingsForm(threshold = '30') {
  return fakeForm([
    ['rating_0_min', '0.00'], ['rating_0_max', '0.30'], ['rating_0_label', 'Yếu'],
    ['rating_1_min', '0.30'], ['rating_1_max', '0.70'], ['rating_1_label', 'Cần cố gắng'],
    ['rating_2_min', '0.70'], ['rating_2_max', '0.90'], ['rating_2_label', 'Đạt'],
    ['rating_3_min', '0.90'], ['rating_3_max', '1.00'], ['rating_3_label', 'Xuất sắc'],
    ['warning_threshold', threshold], ['reminder_frequency', 'monthly'], ['email_reminders', 'on'],
  ], { selector: '[data-settings-form]', elements: { email_reminders: { checked: true } }, noSummary: true });
}

test('settings: "Lưu cài đặt" PUTs converted values, saves the threshold and refreshes', async () => {
  const form = settingsForm();
  const app = setup({
    'PUT /api/okr/settings/': ({ body }) => ({ body: { ...body, warning_threshold: '30.00', rating_ranges: [], reminder_schedule: { scheduled: false, command: 'okr_checkin_reminders', channels: ['in_app'], email_available: false } } }),
  }, { settingsForm: form });
  await app.JoyOKR.forms.handleAction('save-settings', {});
  const put = app.calls.at(-1);
  assert.equal(put.method, 'PUT');
  assert.equal(put.path, '/api/okr/settings/');
  assert.deepEqual(put.body, {
    warning_threshold: 30,
    reminder_frequency: 'monthly',
    email_reminders: true,
    rating_ranges: [
      { min: 0, max: 0.3, label: 'Yếu' }, { min: 0.3, max: 0.7, label: 'Cần cố gắng' }, { min: 0.7, max: 0.9, label: 'Đạt' }, { min: 0.9, max: 1, label: 'Xuất sắc' },
    ],
  });
  assert.equal(app.sandbox.JoyOKRConfig.warningThreshold, 30);
  assert.deepEqual(app.notes, [{ message: 'Đã lưu cài đặt.', tone: 'success' }]);
  assert.equal(app.navigations.length, 1);

  // Enter inside the settings form saves the same way instead of a native GET submit.
  let prevented = false;
  app.listeners.submit({ target: form, preventDefault() { prevented = true; } });
  await settle();
  assert.ok(prevented);
  assert.equal(app.calls.filter((call) => call.method === 'PUT').length, 2);
});

test('settings: field errors show under the inputs and in a toast', async () => {
  const form = settingsForm('150');
  const message = 'Ngưỡng phải từ 0 đến 100.';
  const app = setup({
    'PUT /api/okr/settings/': { status: 400, body: { code: 'validation_error', message: 'Dữ liệu chưa hợp lệ.', fields: { warning_threshold: message } } },
  }, { settingsForm: form });
  await app.JoyOKR.forms.handleAction('save-settings', {});
  assert.equal(app.calls.at(-1).body.warning_threshold, 150);
  assert.equal(form.fieldErrors.warning_threshold.textContent, message);
  assert.deepEqual(app.notes, [{ message, tone: 'error' }]);
  assert.equal(app.sandbox.JoyOKRConfig.warningThreshold, 15, 'a refused save keeps the old threshold');
});

test('admin actions open the matching forms', async () => {
  const app = setup({ 'GET /api/okr/people/': { body: PEOPLE } });
  await app.JoyOKR.forms.handleAction('open-role', { dataset: {} });
  assert.ok(app.content.innerHTML.includes('Gán vai trò OKR'));
  assert.ok(app.dialog.open);
});

test('key result form: a new key result is proposed to the objective owner, not to whoever creates it', async () => {
  // Assignment on the web: the admin (id 7) adds a key result to an objective owned by employee 8,
  // who must be able to check in on it from the app.
  const { JoyOKR } = setup({
    'GET /api/okr/objectives/12/': { body: objectiveDetail({ owner: owner(8, 'Trần Quản Lý'), key_results: [] }) },
    'GET /api/okr/people/': { body: PEOPLE },
  });
  const definition = await JoyOKR.forms.helpers.buildDefinition('key-result', { objectiveId: 12 });
  assert.match(definition.body, /<option value="8" selected>/);
  assert.doesNotMatch(definition.body, /<option value="7" selected>/);
});
