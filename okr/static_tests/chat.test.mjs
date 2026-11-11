// OKR chat web module (chat.js, 2026-11-09): every server value is escaped,
// the feature stays invisible while the flag is off, the "Trao đổi" button
// opens the room through POST /api/okr/chat/rooms/, and chat notifications
// link to the room.
import test from 'node:test';
import assert from 'node:assert/strict';
import { XSS, load, makeConfig } from './harness.mjs';

const FILES = ['domain.js', 'api.js', 'store.js', 'chat.js'];
const P = `${XSS.tag}${XSS.attr}`;

function chatModule(config = makeConfig({ chatEnabled: true }), routes = {}) {
  return load(FILES, { config, routes });
}

function assertSafe(html, label) {
  assert.ok(!html.includes('<img src=x'), `${label}: raw injected <img>`);
  assert.ok(!/<script/i.test(html), `${label}: <script>`);
  // Every real tag: once its double-quoted values are blanked, no event handler attribute is left.
  for (const tag of html.match(/<[a-zA-Z][^>]*>/g) || []) {
    const bare = tag.replace(/"[^"]*"/g, '""');
    assert.ok(!/\son[a-z]+\s*=/i.test(bare), `${label}: handler attribute in ${tag}`);
    assert.ok(!bare.includes("'"), `${label}: single-quoted attribute in ${tag}`);
  }
  assert.ok(!html.includes("' onmouseover="), `${label}: single-quote break-out`);
}

test('message markup escapes content, names, reply excerpts and file names', () => {
  const { JoyOKR } = chatModule();
  const html = JoyOKR.chat.messageHtml({
    id: 5,
    content: P,
    sender: { id: 2, name: P, avatar: `javascript:${P}` },
    created_at: '2026-11-09T08:00:00Z',
    reply_to: { id: 4, sender_name: P, excerpt: P, recalled: false },
    attachments: [{ id: 9, name: P, size: 2048, is_image: true }, { id: '10"><b', name: P, size: 10, is_image: false }],
    permissions: { edit: true, recall: true },
  });
  assertSafe(html, 'message');
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!html.includes('javascript:'), 'non-relative avatar URLs are dropped');
  assert.ok(html.includes('/api/okr/chat/attachments/9/download/?inline=1'));
  assert.ok(html.includes('/api/okr/chat/attachments/0/download/'), 'ids are coerced to numbers');
  assert.ok(html.includes('data-chat-edit="5"') && html.includes('data-chat-recall="5"'));
});

test('recalled messages show neither content, files nor actions', () => {
  const { JoyOKR } = chatModule();
  const html = JoyOKR.chat.messageHtml({
    id: 6, content: 'bí mật', recalled: true, sender: { name: 'An' }, attachments: [{ id: 1, name: 'x.pdf' }],
    permissions: { edit: true, recall: true },
  });
  assert.ok(!html.includes('bí mật'));
  assert.ok(!html.includes('/download/'));
  assert.ok(!html.includes('data-chat-edit') && !html.includes('data-chat-recall'));
  assert.ok(html.includes('Tin nhắn đã được thu hồi'));
});

test('room list escapes titles and previews and filters by kind', () => {
  const { JoyOKR } = chatModule();
  const rooms = [
    { id: 1, kind: 'team', title: P, unread: 3, last_message: { sender_name: P, excerpt: P, created_at: '2026-11-09T08:00:00Z' } },
    { id: 2, kind: 'direct', title: 'Bình', partner: { name: 'Bình' }, unread: 0 },
    { id: 3, kind: 'objective', title: 'Tăng doanh thu', unread: 0 },
  ];
  const all = JoyOKR.chat.roomList(rooms, 1, '', '');
  assertSafe(all, 'room list');
  assert.ok(all.includes('data-route="messages/1"') && all.includes('is-active'));
  assert.ok(all.includes('3 tin chưa đọc'));
  const okr = JoyOKR.chat.roomList(rooms, 0, 'okr', '');
  assert.ok(okr.includes('Tăng doanh thu') && !okr.includes('Bình'));
  const searched = JoyOKR.chat.roomList(rooms, 0, '', 'tang doanh');
  assert.ok(searched.includes('Tăng doanh thu') && !searched.includes('Bình'), 'accent-insensitive search');
});

test('nothing chat-related shows while the flag is off', async () => {
  const { JoyOKR } = chatModule(makeConfig({ chatEnabled: false }));
  assert.equal(JoyOKR.chat.openButton('objective', 12), '');
  assert.equal(JoyOKR.chat.enabled(), false);
  await assert.rejects(() => JoyOKR.chat.renderPage({}), /chưa được bật/);
});

test('"Trao đổi" opens the room with POST rooms/ and navigates to it', async () => {
  const { JoyOKR, calls } = chatModule(makeConfig({ chatEnabled: true }), {
    'POST /api/okr/chat/rooms/': { status: 200, body: { id: 41, kind: 'objective', title: 'X' } },
  });
  const routes = [];
  JoyOKR.navigate = (route) => routes.push(route);
  const button = JoyOKR.chat.openButton('objective', 12);
  assert.ok(button.includes('data-chat-kind="objective"') && button.includes('data-chat-target="12"'));
  assert.equal(JoyOKR.chat.openButton('objective', `12"><img src=x>`), '', 'a non-numeric id renders nothing');
  await JoyOKR.chat.openFor('objective', 12);
  const call = calls.find((c) => c.path === '/api/okr/chat/rooms/');
  assert.deepEqual(JSON.parse(JSON.stringify(call.body)), { kind: 'objective', objective_id: 12 });
  assert.equal(call.headers['X-CSRFToken'], 'csrf-token-123');
  assert.deepEqual(routes, ['messages/41']);
});

test('chat notifications link to their room, unavailable ones link nowhere', () => {
  const { JoyOKR } = chatModule();
  const { mapNotification } = JoyOKR.api.mappers;
  const open = mapNotification({ id: 1, type: 'chat_mention', reference: { type: 'chat_message', id: 7, available: true, room_id: 41 } });
  assert.equal(open.reference_route, 'messages/41');
  const gone = mapNotification({ id: 2, type: 'chat_mention', reference: { type: 'chat_message', id: 7, available: false, room_id: null } });
  assert.equal(gone.reference_route, '');
});

// -- 2026-11-11 ------------------------------------------------------------------

test('mentions are highlighted without letting typed or named HTML through', () => {
  const { JoyOKR } = chatModule();
  const html = JoyOKR.chat.messageHtml({
    id: 6,
    content: `Chào @Bình ${P} @${P}`,
    sender: { id: 2, name: 'An' },
    created_at: '2026-11-11T08:00:00Z',
    mentions: [{ id: 3, name: 'Bình' }, { id: 4, name: P }],
    permissions: {},
  });
  assertSafe(html, 'mentions');
  assert.ok(html.includes('<mark class="joyokr-chat-mention">@Bình</mark>'));
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
});

function fakeXhr() {
  const instances = [];
  class FakeXHR {
    constructor() {
      this.headers = {};
      this.upload = {};
      instances.push(this);
    }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    send(body) { this.body = body; }
    abort() { this.onabort?.(); }
  }
  return { FakeXHR, instances };
}

test('file uploads send the session CSRF header, report progress, can be cancelled and map errors', async () => {
  const { FakeXHR, instances } = fakeXhr();
  const { JoyOKR } = load(FILES, { config: makeConfig({ chatEnabled: true }), extra: { XMLHttpRequest: FakeXHR } });
  const seen = [];
  const first = JoyOKR.api.upload('chat/attachments/', new FormData(), { onProgress: (fraction) => seen.push(fraction) });
  const xhr = instances[0];
  assert.equal(xhr.method, 'POST');
  assert.equal(xhr.url, '/api/okr/chat/attachments/');
  assert.equal(xhr.headers['X-CSRFToken'], 'csrf-token-123');
  assert.equal(xhr.withCredentials, true);
  xhr.upload.onprogress({ lengthComputable: true, loaded: 5, total: 10 });
  xhr.status = 201;
  xhr.responseText = '{"id": 7}';
  xhr.onload();
  assert.equal((await first.promise).id, 7); // parsed in the sandbox realm: compare the field
  assert.deepEqual(seen, [0.5]);

  const cancelled = JoyOKR.api.upload('chat/attachments/', new FormData());
  cancelled.abort();
  await assert.rejects(cancelled.promise, (error) => error.code === 'aborted' && /hủy/.test(error.message));

  const refused = JoyOKR.api.upload('chat/attachments/', new FormData());
  instances[2].status = 400;
  instances[2].responseText = JSON.stringify({ code: 'validation_error', message: 'Tệp không hợp lệ.' });
  instances[2].onload();
  await assert.rejects(refused.promise, (error) => error.status === 400 && error.message === 'Tệp không hợp lệ.');
});

test('2026-11-11: every chat element shown with display: is hidden again by [hidden]', async () => {
  // A class rule with display: beats the browser's [hidden] default, so the
  // "new messages" jump button showed on every room open. Guard all of them.
  const { readFile } = await import('node:fs/promises');
  const css = await readFile(new URL('../static/okr_web/css/chat.css', import.meta.url), 'utf8');
  const js = await readFile(new URL('../static/okr_web/js/chat.js', import.meta.url), 'utf8');
  const shown = new Set();
  for (const [, selectors, body] of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    if (!/display\s*:\s*(?!none)/.test(body)) continue;
    for (const selector of selectors.split(',')) {
      const simple = selector.trim().match(/^\.(joyokr-chat-[\w-]+)$/);
      if (simple) shown.add(simple[1]);
    }
  }
  const toggled = new Set();
  for (const [, classes] of js.matchAll(/<\w+[^>]*class="([^"]+)"[^>]*\shidden[\s>]/g)) {
    for (const name of classes.split(/\s+/)) if (name.startsWith('joyokr-chat-')) toggled.add(name);
  }
  const unguarded = [...toggled].filter((name) => shown.has(name) && !css.includes(`.${name}[hidden]`));
  assert.ok(toggled.has('joyokr-chat-jump'), 'the scan finds the jump button');
  assert.deepEqual(unguarded, []);
});
