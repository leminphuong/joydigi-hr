// app.js: deep links built from appUrl ("/okr/"), HR-owned routes sent to
// the HR web, route sanitising and the escaped error state.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {
  ADMIN_CAPS, EMPLOYEE_CAPS, MANAGER_CAPS, XSS, load, makeConfig, makeDocument, makeLocation, plain, source,
} from './harness.mjs';

function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function bootApp({ pathname = '/okr/', search = '', initialRoute = 'dashboard', render, capabilities = EMPLOYEE_CAPS, api } = {}) {
  const listeners = {};
  const outlet = {
    innerHTML: '',
    querySelector: () => null,
    querySelectorAll: () => [],
    setAttribute() {},
    removeAttribute() {},
  };
  const shell = { classList: { add() {}, remove() {}, toggle() {} } };
  const document = makeDocument({
    querySelector(selector) {
      if (selector === '[data-joyokr-app]') return shell;
      if (selector === '#joyokr-view') return outlet;
      return null;
    },
    addEventListener(type, handler) { listeners[type] = handler; },
  });
  const location = makeLocation(pathname, search);
  const history = [];
  const pushOrReplace = (kind) => (_state, _title, url) => {
    history.push({ kind, url: `${url.pathname}${url.search}` });
    location.pathname = url.pathname;
    location.search = url.search;
  };
  const rendered = [];
  const windowListeners = {};
  const { sandbox, calls } = load([], {
    location,
    document,
    config: makeConfig({ initialRoute, capabilities }),
    extra: {
      history: { pushState: pushOrReplace('push'), replaceState: pushOrReplace('replace') },
      addEventListener(type, handler) { windowListeners[type] = handler; },
      scrollTo() {},
      console: { ...console, error() {} },
      matchMedia: () => ({ matches: false }),
      JoyOKR: {
        store: { setState() {}, getState: () => ({}) },
        views: {
          async render(name, context) {
            rendered.push({ name, params: { ...context.params } });
            if (render) return render(name, context);
            context.outlet.innerHTML = `<h1>${name}</h1>`;
            return true;
          },
        },
        ...(api ? { api } : {}),
      },
    },
  });
  return { sandbox, outlet, location, history, rendered, listeners, windowListeners, calls };
}

async function start(options) {
  const app = bootApp(options);
  vm.runInContext(source('app.js'), app.sandbox, { filename: 'app.js' });
  await tick();
  return app;
}

test('a deep link /okr/objectives/12 renders the objective detail and keeps its URL', async () => {
  const app = await start({ pathname: '/okr/objectives/12', initialRoute: 'objectives/12' });
  assert.deepEqual(app.history, [{ kind: 'replace', url: '/okr/objectives/12' }]);
  assert.equal(app.rendered[0].name, 'objective-detail');
  assert.equal(app.rendered[0].params.id, 12);
});

test('navigate builds same-origin URLs from appUrl and keeps the query', async () => {
  const app = await start();
  assert.deepEqual(app.history, [{ kind: 'replace', url: '/okr/' }], 'dashboard is the app root');
  app.sandbox.JoyOKR.navigate('my-okrs', { query: { search: 'doanh thu', page: 2, empty: '' } });
  app.sandbox.JoyOKR.navigate('key-results/21');
  await tick();
  assert.deepEqual(app.history.slice(1), [
    { kind: 'push', url: '/okr/my-okrs?search=doanh+thu&page=2' },
    { kind: 'push', url: '/okr/key-results/21' },
  ]);
  assert.deepEqual(app.rendered.slice(1).map((entry) => entry.name), ['my-okrs', 'key-result-detail']);
});

test('popstate reads the route back from location relative to /okr/', async () => {
  const app = await start();
  app.location.pathname = '/okr/key-results/5';
  app.windowListeners.popstate();
  await tick();
  assert.equal(app.rendered.at(-1).name, 'key-result-detail');
  assert.equal(app.rendered.at(-1).params.id, 5);
});

test('profile and admin/users go to the HR web instead of rendering', async () => {
  const app = await start();
  const before = app.rendered.length;
  app.sandbox.JoyOKR.navigate('profile');
  app.sandbox.JoyOKR.navigate('admin/users');
  await tick();
  assert.equal(app.rendered.length, before);
  assert.deepEqual(app.location.assigned, ['/', '/']);
  assert.equal(app.history.length, 1, 'no /okr/profile history entry is created');
});

test('odd route strings fall back to the dashboard instead of leaving /okr/', async () => {
  const app = await start();
  app.sandbox.JoyOKR.navigate('../../logout');
  app.sandbox.JoyOKR.navigate('//evil.example/x');
  app.sandbox.JoyOKR.navigate('objectives/12"><img src=x>');
  await tick();
  app.history.slice(1).forEach((entry) => assert.equal(entry.url, '/okr/'));
  assert.deepEqual(app.rendered.slice(1).map((entry) => entry.name), ['dashboard', 'dashboard', 'dashboard']);
});

test('render errors are shown escaped', async () => {
  const app = await start({ render: async () => { throw new Error(XSS.tag); } });
  assert.ok(app.outlet.innerHTML.includes('&quot;&gt;&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!app.outlet.innerHTML.includes('<img'));
});

test('app.js escapeHtml escapes & < > " \' and backtick', async () => {
  const app = await start();
  const escape = app.sandbox.JoyOKR.escapeHtml;
  assert.equal(escape(XSS.tag), '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;');
  assert.equal(escape("' onmouseover='"), '&#39; onmouseover=&#39;');
  assert.equal(escape('`a` & b'), '&#96;a&#96; &amp; b');
  assert.equal(escape(null), '');
});

const ADMIN_ROUTES = ['admin/approvals', 'admin/roles', 'admin/departments', 'admin/teams', 'admin/cycles', 'admin/settings'];
const FORBIDDEN = 'Bạn không có quyền truy cập trang này.';

async function visit(app, route) {
  app.sandbox.JoyOKR.navigate(route);
  await tick();
  return app.outlet.innerHTML;
}

test('admin routes need their capability: an employee gets the no-access page and no view or API call', async () => {
  const app = await start({ capabilities: EMPLOYEE_CAPS });
  for (const route of ADMIN_ROUTES) {
    const html = await visit(app, route);
    assert.ok(html.includes(`<h1>${FORBIDDEN}</h1>`), route);
    assert.ok(html.includes('data-route="dashboard"'), `${route}: a way back`);
    assert.equal(app.history.at(-1).url, `/okr/${route}`, `${route}: the URL is kept`);
  }
  assert.deepEqual(app.rendered.map((entry) => entry.name), ['dashboard'], 'no admin view was rendered');
  assert.equal(app.calls.length, 0, 'no API request');
});

test('a manager opens admin/approvals only; an admin opens every admin page, including admin/roles', async () => {
  const manager = await start({ capabilities: MANAGER_CAPS });
  for (const route of ADMIN_ROUTES) {
    const html = await visit(manager, route);
    if (route === 'admin/approvals') assert.equal(html, '<h1>admin/approvals</h1>');
    else assert.ok(html.includes(FORBIDDEN), route);
  }
  assert.deepEqual(manager.rendered.map((entry) => entry.name), ['dashboard', 'admin/approvals']);

  const admin = await start({ capabilities: ADMIN_CAPS });
  for (const route of ADMIN_ROUTES) assert.equal(await visit(admin, route), `<h1>${route}</h1>`);
  assert.deepEqual(admin.rendered.map((entry) => entry.name), ['dashboard', ...ADMIN_ROUTES]);
});

test('capabilities may come from currentUser when the top-level list is absent', async () => {
  const app = bootApp({ capabilities: ADMIN_CAPS });
  delete app.sandbox.JoyOKRConfig.capabilities;
  vm.runInContext(source('app.js'), app.sandbox, { filename: 'app.js' });
  await tick();
  assert.equal(await visit(app, 'admin/settings'), '<h1>admin/settings</h1>');
});

function noCycleRender(name, context) {
  if (name === 'dashboard') {
    throw Object.assign(new Error('Chưa có chu kỳ OKR đang hoạt động.'), { status: 404, data: { code: 'no_cycle' } });
  }
  context.outlet.innerHTML = `<h1>${name}</h1>`;
  return true;
}

test('no OKR cycle yet: an admin is sent to admin/cycles', async () => {
  const requests = [];
  const api = { request: async (endpoint, options) => { requests.push([endpoint, options?.query]); return { total: 0, items: [] }; } };
  const app = await start({ capabilities: ADMIN_CAPS, render: noCycleRender, api });
  await tick();
  await tick();
  assert.deepEqual(plain(requests), [['cycles', { per_page: 1 }]]);
  assert.deepEqual(app.rendered.map((entry) => entry.name), ['dashboard', 'admin/cycles']);
  assert.deepEqual(app.history.at(-1), { kind: 'replace', url: '/okr/admin/cycles' });
  assert.equal(app.outlet.innerHTML, '<h1>admin/cycles</h1>');
});

test('no OKR cycle yet: an employee (no manage_cycles) keeps the error and is not redirected', async () => {
  const requests = [];
  const api = { request: async (endpoint) => { requests.push(endpoint); return { total: 0, items: [] }; } };
  const app = await start({ capabilities: MANAGER_CAPS, render: noCycleRender, api });
  await tick();
  assert.deepEqual(requests, []);
  assert.deepEqual(app.rendered.map((entry) => entry.name), ['dashboard']);
  assert.ok(app.outlet.innerHTML.includes('Chưa có chu kỳ OKR đang hoạt động.'));
});

test('an admin is not redirected when cycles exist (e.g. only drafts or closed ones)', async () => {
  const api = { request: async () => ({ total: 2, items: [{ id: 1 }] }) };
  const app = await start({ capabilities: ADMIN_CAPS, render: noCycleRender, api });
  await tick();
  assert.deepEqual(app.rendered.map((entry) => entry.name), ['dashboard']);
  assert.ok(app.outlet.innerHTML.includes('Chưa có chu kỳ OKR đang hoạt động.'));
});
