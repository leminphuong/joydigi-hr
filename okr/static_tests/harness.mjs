// Shared harness for the OKR web UI tests: loads the browser scripts from
// okr/static/okr_web/js into a vm context with a stub fetch, location and
// document. Not a test file itself (node --test only picks *.test.mjs).
import fs from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

const JS_DIR = new URL('../static/okr_web/js/', import.meta.url);
export const ORIGIN = 'https://hr.test';

export function source(name) {
  return fs.readFileSync(new URL(name, JS_DIR), 'utf8');
}

/** JSON round trip, so objects built inside the vm compare with deepStrictEqual. */
export function plain(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

export const EMPLOYEE_CAPS = ['joyokr_view', 'joyokr_edit_own', 'joyokr_checkin', 'joyokr_comment', 'joyokr_view_reports'];
export const MANAGER_CAPS = [...EMPLOYEE_CAPS, 'joyokr_edit_managed', 'joyokr_approve'];
export const ADMIN_CAPS = [...MANAGER_CAPS, 'joyokr_manage_organization', 'joyokr_manage_cycles', 'joyokr_manage_settings'];

export function makeConfig(overrides = {}) {
  const capabilities = overrides.capabilities || EMPLOYEE_CAPS;
  return {
    root: '/api/okr/',
    appUrl: '/okr/',
    hrUrl: '/',
    logoutUrl: '/logout/',
    initialRoute: 'dashboard',
    loggedIn: true,
    csrfToken: 'csrf-token-123',
    currentUser: { id: 7, display_name: 'Người dùng', roles: ['joyokr_employee'], capabilities },
    capabilities,
    strings: { loading: 'Đang tải dữ liệu…', error: 'Đã có lỗi xảy ra. Vui lòng thử lại.', empty: 'Chưa có dữ liệu.' },
    ...overrides,
  };
}

/** A fetch stub. `routes` maps "METHOD /path/" to {status, body} or a function returning one. */
export function makeFetch(routes = {}) {
  const calls = [];
  async function fetch(url, init = {}) {
    const parsed = new URL(url, ORIGIN);
    const method = String(init.method || 'GET').toUpperCase();
    const call = {
      url: String(url),
      path: parsed.pathname,
      query: Object.fromEntries(parsed.searchParams),
      method,
      headers: { ...(init.headers || {}) },
      credentials: init.credentials,
      rawBody: init.body,
      body: typeof init.body === 'string' ? JSON.parse(init.body) : init.body,
      signal: init.signal,
    };
    calls.push(call);
    let handler = routes[`${method} ${parsed.pathname}`] ?? routes['*'];
    if (typeof handler === 'function') handler = await handler(call);
    if (handler === undefined) handler = { status: 404, body: { code: 'not_found', message: 'Không tìm thấy dữ liệu.', fields: {} } };
    if (handler === 'network-error') throw new TypeError('Failed to fetch');
    if (handler === 'hang') {
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
      });
    }
    const status = handler.status ?? 200;
    const text = handler.raw !== undefined ? handler.raw : (handler.body === undefined ? '' : JSON.stringify(handler.body));
    return { ok: status >= 200 && status < 300, status, text: async () => text };
  }
  return { fetch, calls };
}

export function makeLocation(pathname = '/okr/', search = '') {
  const assigned = [];
  return {
    origin: ORIGIN,
    href: `${ORIGIN}${pathname}${search}`,
    pathname,
    search,
    assigned,
    assign(url) { assigned.push(String(url)); },
  };
}

/** A document whose createElement throws, proving escapers never parse HTML. */
export function makeDocument(extra = {}) {
  return {
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    createElement() { throw new Error('createElement must not be used by escapers'); },
    body: { classList: { add() {}, remove() {} }, append() {} },
    ...extra,
  };
}

/**
 * Loads the named scripts (in shell.html order) into a fresh context.
 * Returns {sandbox, JoyOKR, calls, location}.
 */
export function load(files, { config = makeConfig(), routes = {}, location = makeLocation(), document = makeDocument(), extra = {} } = {}) {
  const { fetch, calls } = makeFetch(routes);
  const sandbox = {
    console,
    URL,
    URLSearchParams,
    FormData,
    File,
    Blob,
    AbortController,
    TextEncoder,
    setTimeout,
    clearTimeout,
    crypto: webcrypto,
    EventTarget,
    CustomEvent: globalThis.CustomEvent || class CustomEvent extends Event { constructor(type, options = {}) { super(type); this.detail = options.detail; } },
    Event,
    fetch,
    location,
    document,
    JoyOKRConfig: config,
    ...extra,
  };
  sandbox.globalThis = sandbox;
  const context = vm.createContext(sandbox);
  files.forEach((name) => vm.runInContext(source(name), context, { filename: name }));
  return { sandbox, JoyOKR: sandbox.JoyOKR, calls, location };
}

// -- Django-shaped fixtures (okr/api/render.py) ----------------------------------------

export function owner(id = 7, name = 'Nguyễn Văn A') {
  return { id, name };
}

export function objectiveSummary(overrides = {}) {
  return {
    id: 12,
    title: 'Tăng doanh thu',
    scope: 'department',
    approval_status: 'draft',
    progress: '40.00',
    expected_progress: '50.00',
    health: null,
    provisional: true,
    key_result_count: 2,
    weight: '100.00',
    owner: owner(),
    cycle: { id: 3, name: 'Q4 2026', end_date: '2026-12-31' },
    department_name: 'Kinh doanh',
    team_name: null,
    parent_id: 2,
    child_count: 0,
    ...overrides,
  };
}

export function keyResult(overrides = {}) {
  return {
    id: 21,
    objective_id: 12,
    objective_title: 'Tăng doanh thu',
    title: 'Doanh thu 10 tỷ',
    unit: 'tỷ',
    start_value: '0',
    current_value: '4',
    target_value: '10',
    weight: '50.00',
    progress: '40.00',
    expected_progress: '50.00',
    health: null,
    due_date: '2026-12-31',
    approval_status: 'draft',
    owner: owner(),
    last_checkin_at: null,
    permissions: { can_checkin: false, can_add_evidence: true },
    ...overrides,
  };
}

export function objectiveDetail(overrides = {}) {
  return {
    ...objectiveSummary(),
    description: 'Mô tả',
    parent: { id: 2, title: 'Objective cha' },
    key_results: [keyResult(), keyResult({ id: 22, title: 'KR 2' })],
    permissions: { can_submit: true, can_comment: true, can_approve: false },
    comment_count: 0,
    rejection_reason: null,
    ...overrides,
  };
}

export function page(results, extra = {}) {
  return { count: results.length, next: null, previous: null, results, ...extra };
}

export function cyclesPage(statuses = { 3: 'active' }) {
  return page(Object.entries(statuses).map(([id, status]) => ({
    id: Number(id), name: `Chu kỳ ${id}`, cycle_type: 'quarter', start_date: '2026-10-01', end_date: '2026-12-31', status, objective_count: 1,
  })));
}

export const XSS = Object.freeze({
  tag: '"><img src=x onerror=alert(1)>',
  attr: "' onmouseover='alert(1)",
  backtick: '`${alert(1)}`',
});
