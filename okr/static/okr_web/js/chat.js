(function registerChat(globalScope) {
  'use strict';

  // OKR chat for the web (2026-11-09). Talks to /api/okr/chat/ through
  // JoyOKR.api.send (HR web session + CSRF). Every piece of server data is
  // escaped before it reaches innerHTML; the server enforces every permission
  // again, this file only decides what to show.
  //
  // Sync without WebSocket: the open room polls `?after=<last id>` every 5 s
  // (edits/recalls through `changed_since`), the room list every 30 s, both
  // paused while the tab is hidden and slowed down after errors.

  const namespace = globalScope.JoyOKR = globalScope.JoyOKR || {};
  const config = () => globalScope.JoyOKRConfig || {};
  const enabled = () => config().chatEnabled === true;

  const ROOM_POLL_MS = 5000;
  const LIST_POLL_MS = 30000;
  const MAX_BACKOFF_MS = 60000;
  const MAX_FILES = 5;
  const MAX_FILE_BYTES = 10 * 1024 * 1024;
  const MAX_LENGTH = 4000;
  const ACCEPT = '.jpg,.jpeg,.png,.webp,.pdf,.doc,.docx,.xls,.xlsx';
  const EMOJI = ['👍', '❤️', '😀', '😂', '🎉', '🙏', '👏', '✅', '🔥', '💡', '📌', '⏰', '🚀', '🤝', '😅', '👀'];
  const KIND_LABELS = Object.freeze({
    objective: 'Objective', key_result: 'Key Result', team: 'Team', department: 'Phòng ban', direct: '1–1',
  });
  const FILTERS = Object.freeze([['', 'Tất cả'], ['team', 'Team'], ['department', 'Phòng ban'], ['okr', 'OKR'], ['direct', '1–1']]);

  let page = null; // state of the mounted chat page
  let prepared = null; // data renderPage fetched, handed to mount
  let listenersReady = false;

  // -- helpers -------------------------------------------------------------------

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"'`]/g, (character) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;',
    })[character]);
  }

  function id(value) {
    const number = Number(value);
    return Number.isInteger(number) && number > 0 ? number : 0;
  }

  function call(method, path, options = {}) {
    return namespace.api.send(method, `chat/${path}`, options);
  }

  function clientId() {
    return `web-${namespace.api.newClientRequestId()}`;
  }

  function clock(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    const today = new Date();
    const sameDay = date.toDateString() === today.toDateString();
    return sameDay ? `${pad(date.getHours())}:${pad(date.getMinutes())}`
      : `${pad(date.getHours())}:${pad(date.getMinutes())} ${pad(date.getDate())}/${pad(date.getMonth() + 1)}`;
  }

  function fileSize(bytes) {
    const n = Number(bytes || 0);
    if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1).replace('.', ',')} MB`;
    return `${Math.max(1, Math.round(n / 1024))} KB`;
  }

  function avatar(person, label) {
    const name = person?.name || label || '?';
    const url = typeof person?.avatar === 'string' && person.avatar.startsWith('/') ? person.avatar : '';
    if (url) return `<img class="joyokr-chat-avatar" src="${escapeHtml(url)}" alt="" loading="lazy">`;
    return `<span class="joyokr-chat-avatar" aria-hidden="true">${escapeHtml(name.trim().slice(0, 1).toUpperCase() || '?')}</span>`;
  }

  function fold(text) {
    return String(text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/đ/g, 'd').replace(/Đ/g, 'D').toLowerCase();
  }

  function notify(message) {
    if (typeof namespace.notify === 'function') namespace.notify(message);
  }

  // -- sidebar badge -----------------------------------------------------------------

  async function refreshBadge() {
    if (!enabled()) return;
    const badge = document.querySelector('[data-chat-count]');
    if (!badge) return;
    try {
      const result = await call('GET', 'unread-count/');
      const unread = Number(result?.unread || 0);
      badge.textContent = unread > 99 ? '99+' : String(unread);
      badge.hidden = unread === 0;
    } catch (error) {
      // The badge is a hint; a failed refresh never disturbs the page.
    }
  }

  // -- "Trao đổi" button on Objective / Key Result pages ---------------------------------

  function openButton(kind, targetId) {
    if (!enabled() || !id(targetId)) return '';
    return `<button type="button" class="joyokr-button joyokr-button--small" data-chat-open data-chat-kind="${escapeHtml(kind)}" data-chat-target="${id(targetId)}">Trao đổi</button>`;
  }

  async function openFor(kind, targetId) {
    const field = { objective: 'objective_id', key_result: 'key_result_id', team: 'team_id', department: 'department_id', direct: 'employee_id' }[kind];
    if (!field || !id(targetId)) return;
    try {
      const room = await call('POST', 'rooms/', { body: { kind, [field]: id(targetId) } });
      namespace.navigate(`messages/${id(room.id)}`);
    } catch (error) {
      notify(error?.message || 'Chưa mở được cuộc trò chuyện.');
    }
  }

  // -- page rendering -------------------------------------------------------------------------

  async function renderPage(params = {}) {
    if (!enabled()) throw new Error('Tính năng trao đổi chưa được bật.');
    const roomId = id(params.id);
    const [rooms, room, history] = await Promise.all([
      call('GET', 'rooms/'),
      roomId ? call('GET', `rooms/${roomId}/`) : Promise.resolve(null),
      roomId ? call('GET', `rooms/${roomId}/messages/`) : Promise.resolve(null),
    ]);
    prepared = { roomId, rooms: rooms?.results || [], room, history };
    return `<article class="joyokr-page joyokr-chat-page">
      <header class="joyokr-page-head"><div><h1>Tin nhắn</h1><p>Trao đổi theo Team, phòng ban, Objective/Key Result và 1–1.</p></div>
        <div class="joyokr-page-actions"><button type="button" class="joyokr-button joyokr-button--small joyokr-button--primary" data-chat-new>Nhắn tin mới</button></div></header>
      <div class="joyokr-chat-layout${roomId ? ' has-room' : ''}">
        <aside class="joyokr-card joyokr-chat-rooms" aria-label="Danh sách cuộc trò chuyện">
          <div class="joyokr-chat-rooms__tools">
            <input type="search" data-chat-room-search placeholder="Tìm cuộc trò chuyện" aria-label="Tìm cuộc trò chuyện">
            <div class="joyokr-chat-filters" role="group" aria-label="Lọc theo loại">${FILTERS.map(([value, label], index) => `<button type="button" data-chat-filter="${value}" class="${index === 0 ? 'is-active' : ''}">${label}</button>`).join('')}</div>
            <div class="joyokr-chat-people" data-chat-people hidden>
              <input type="search" data-chat-people-search placeholder="Tìm đồng nghiệp trong công ty" aria-label="Tìm đồng nghiệp">
              <ul data-chat-people-list></ul>
            </div>
          </div>
          <ul class="joyokr-chat-room-list" data-chat-room-list>${roomList(prepared.rooms, roomId, '', '')}</ul>
        </aside>
        <section class="joyokr-card joyokr-chat-room" data-chat-room="${roomId}">${room ? roomPane(room) : emptyPane()}</section>
      </div></article>`;
  }

  function emptyPane() {
    return '<div class="joyokr-empty joyokr-chat-empty"><h2>Chọn một cuộc trò chuyện</h2><p>Phòng Team và phòng ban của bạn đã có sẵn ở danh sách bên trái.</p></div>';
  }

  function matchesFilter(room, filter) {
    if (!filter) return true;
    if (filter === 'okr') return room.kind === 'objective' || room.kind === 'key_result';
    return room.kind === filter;
  }

  function roomList(rooms, activeId, filter, search) {
    const term = fold(search).trim();
    const rows = rooms.filter((room) => matchesFilter(room, filter) && (!term || fold(room.title).includes(term)));
    if (!rows.length) return '<li class="joyokr-chat-room-list__empty">Chưa có cuộc trò chuyện.</li>';
    return rows.map((room) => {
      const last = room.last_message;
      const unread = Number(room.unread || 0);
      return `<li><a class="joyokr-chat-room-item${id(room.id) === activeId ? ' is-active' : ''}" href="/okr/messages/${id(room.id)}" data-route="messages/${id(room.id)}">
        ${avatar(room.partner, room.title)}
        <span class="joyokr-chat-room-item__main"><span class="joyokr-chat-room-item__title">${escapeHtml(room.title)}</span>
          <span class="joyokr-chat-room-item__meta">${escapeHtml(KIND_LABELS[room.kind] || '')}${last ? ` · ${escapeHtml(last.sender_name)}: ${escapeHtml(last.excerpt)}` : ''}</span></span>
        <span class="joyokr-chat-room-item__side">${last ? `<time>${escapeHtml(clock(last.created_at))}</time>` : ''}${unread ? `<span class="joyokr-chat-unread" aria-label="${unread} tin chưa đọc">${unread > 99 ? '99+' : unread}</span>` : ''}</span>
      </a></li>`;
    }).join('');
  }

  function targetLink(room) {
    const target = room.target || {};
    if (room.kind === 'objective' && id(target.objective_id)) return `<button type="button" class="joyokr-link" data-route="objectives/${id(target.objective_id)}">Mở Objective</button>`;
    if (room.kind === 'key_result' && id(target.key_result_id)) return `<button type="button" class="joyokr-link" data-route="key-results/${id(target.key_result_id)}">Mở Key Result</button>`;
    return '';
  }

  function roomPane(room) {
    const owner = room.target?.owner?.name;
    const canPost = room.permissions?.post !== false;
    return `<header class="joyokr-chat-room__head">
        <button type="button" class="joyokr-icon-button joyokr-chat-back" data-route="messages" aria-label="Quay lại danh sách">‹</button>
        <div class="joyokr-chat-room__title"><h2>${escapeHtml(room.title)}</h2>
          <p>${escapeHtml(KIND_LABELS[room.kind] || '')}${owner ? ` · Phụ trách: ${escapeHtml(owner)}` : ''} ${targetLink(room)}</p></div>
        <button type="button" class="joyokr-button joyokr-button--small" data-chat-members-toggle aria-expanded="false">Thành viên</button>
      </header>
      <div class="joyokr-chat-members" data-chat-members hidden></div>
      <div class="joyokr-chat-scroll" data-chat-scroll>
        <button type="button" class="joyokr-button joyokr-button--small joyokr-chat-older" data-chat-older hidden>Tải tin cũ hơn</button>
        <div class="joyokr-chat-list" data-chat-list aria-live="polite"></div>
        <button type="button" class="joyokr-button joyokr-button--small joyokr-chat-jump" data-chat-jump hidden>Có tin nhắn mới ↓</button>
      </div>
      <p class="joyokr-chat-status" data-chat-status role="status"></p>
      ${canPost ? composer() : '<p class="joyokr-chat-readonly">Người nhận không còn làm việc tại công ty. Bạn vẫn xem được lịch sử nhưng không gửi được tin mới.</p>'}`;
  }

  function composer() {
    return `<form class="joyokr-chat-composer" data-chat-form novalidate>
        <div class="joyokr-chat-bar" data-chat-reply-bar hidden></div>
        <div class="joyokr-chat-bar" data-chat-edit-bar hidden><span>Đang sửa tin nhắn</span><button type="button" data-chat-cancel-edit>Hủy</button></div>
        <ul class="joyokr-chat-files" data-chat-files hidden></ul>
        <div class="joyokr-chat-mentions" data-chat-mention-list role="listbox" hidden></div>
        <div class="joyokr-chat-emoji" data-chat-emoji-list hidden>${EMOJI.map((emoji) => `<button type="button" data-chat-emoji="${emoji}" aria-label="Chèn ${emoji}">${emoji}</button>`).join('')}</div>
        <textarea rows="2" maxlength="${MAX_LENGTH}" data-chat-input placeholder="Viết tin nhắn… (@ để nhắc tên, Enter để gửi, Shift+Enter xuống dòng)" aria-label="Nội dung tin nhắn"></textarea>
        <div class="joyokr-chat-toolbar">
          <button type="button" class="joyokr-icon-button" data-chat-emoji-toggle aria-label="Chèn emoji">🙂</button>
          <label class="joyokr-icon-button" aria-label="Đính kèm ảnh hoặc tệp">📎<input type="file" data-chat-file-input accept="${ACCEPT}" multiple hidden></label>
          <span class="joyokr-chat-toolbar__space"></span>
          <button type="submit" class="joyokr-button joyokr-button--primary joyokr-button--small" data-chat-send>Gửi</button>
        </div>
      </form>`;
  }

  // The text escaped, then each "@Name" of a person actually mentioned
  // wrapped in <mark> — both sides escaped, so nothing typed becomes HTML.
  function withMentions(content, mentions) {
    let html = escapeHtml(content);
    (mentions || []).forEach((person) => {
      const name = escapeHtml(`@${person?.name || ''}`);
      if (name.length > 1) html = html.split(name).join(`<mark class="joyokr-chat-mention">${name}</mark>`);
    });
    return html;
  }

  function messageHtml(message) {
    const recalled = message.recalled === true;
    const name = message.sender?.name || 'Thành viên';
    const reply = message.reply_to && !recalled
      ? `<blockquote class="joyokr-chat-quote"><strong>${escapeHtml(message.reply_to.sender_name)}</strong> ${message.reply_to.recalled ? 'Tin nhắn đã được thu hồi' : escapeHtml(message.reply_to.excerpt)}</blockquote>` : '';
    const files = recalled ? '' : (message.attachments || []).map((file) => {
      const url = `/api/okr/chat/attachments/${id(file.id)}/download/`;
      if (file.is_image) return `<a class="joyokr-chat-image" href="${url}?inline=1" target="_blank" rel="noopener"><img src="${url}?inline=1" alt="${escapeHtml(file.name)}" loading="lazy"></a>`;
      return `<a class="joyokr-chat-file" href="${url}">📄 ${escapeHtml(file.name)} <small>${escapeHtml(fileSize(file.size))}</small></a>`;
    }).join('');
    const actions = recalled || message.pending ? '' : [
      `<button type="button" data-chat-reply="${id(message.id)}">Trả lời</button>`,
      message.permissions?.edit ? `<button type="button" data-chat-edit="${id(message.id)}">Sửa</button>` : '',
      message.permissions?.recall ? `<button type="button" data-chat-recall="${id(message.id)}">Thu hồi</button>` : '',
    ].join('');
    const body = recalled
      ? `<div class="joyokr-chat-bubble is-recalled">${message.recalled_by_moderator ? 'Tin nhắn đã bị quản lý ẩn' : 'Tin nhắn đã được thu hồi'}</div>`
      : `${message.content ? `<div class="joyokr-chat-bubble">${withMentions(message.content, message.mentions)}</div>` : ''}${files ? `<div class="joyokr-chat-attachments">${files}</div>` : ''}`;
    const state = message.failed
      ? `<span class="joyokr-chat-failed">Gửi lỗi · <button type="button" data-chat-retry>Gửi lại</button></span>`
      : message.pending ? '<span class="joyokr-chat-pending">Đang gửi…</span>' : '';
    return `<article class="joyokr-chat-msg${message.is_mine ? ' is-mine' : ''}${recalled ? ' is-recalled' : ''}" data-message-id="${id(message.id) || escapeHtml(message.client_message_id)}">
      ${avatar(message.sender, name)}
      <div class="joyokr-chat-msg__main"><header><strong>${escapeHtml(name)}</strong> <time>${escapeHtml(clock(message.created_at))}</time>${message.edited_at && !recalled ? ' <span class="joyokr-chat-edited">đã sửa</span>' : ''}</header>
        ${reply}${body}${state}${actions ? `<div class="joyokr-chat-msg__actions">${actions}</div>` : ''}</div></article>`;
  }

  // -- mounted page ------------------------------------------------------------------------------

  function mount(outlet, viewName) {
    stop();
    if (!['messages', 'messages-room'].includes(viewName) || !prepared) return;
    const root = outlet.querySelector('.joyokr-chat-page');
    if (!root) return;
    const data = prepared;
    prepared = null;
    page = {
      root,
      roomId: data.roomId,
      room: data.room,
      rooms: data.rooms,
      filter: '',
      search: '',
      messages: new Map(),
      firstId: 0,
      lastId: 0,
      serverTime: data.history?.server_time || '',
      hasMoreBefore: Boolean(data.history?.has_more_before),
      backoff: ROOM_POLL_MS,
      roomTimer: null,
      listTimer: null,
      alive: true,
      sending: false,
      pendingSend: null,
      replyTo: null,
      editing: null,
      files: [],
      mentions: new Map(),
      mentionTimer: null,
      readTimer: null,
    };
    bind(root);
    if (data.history) {
      merge(data.history.results || []);
      scrollToBottom();
      markReadSoon();
    }
    scheduleRoom(ROOM_POLL_MS);
    scheduleList();
  }

  function stop() {
    if (!page) return;
    page.files.forEach((entry) => {
      if (entry.state === 'uploading') entry.request?.abort();
      releasePreview(entry);
    });
    page.alive = false;
    clearTimeout(page.roomTimer);
    clearTimeout(page.listTimer);
    clearTimeout(page.mentionTimer);
    clearTimeout(page.readTimer);
    page = null;
  }

  function el(selector) {
    return page?.root.querySelector(selector) || null;
  }

  function setStatus(text) {
    const status = el('[data-chat-status]');
    if (status) status.textContent = text || '';
  }

  function sortedMessages() {
    return [...page.messages.values()].sort((a, b) => (id(a.id) || Number.MAX_SAFE_INTEGER) - (id(b.id) || Number.MAX_SAFE_INTEGER));
  }

  function merge(rows) {
    if (!page || !rows?.length) return;
    rows.forEach((message) => {
      const key = id(message.id);
      if (!key) return;
      // A confirmed message replaces its pending copy (same client id).
      if (message.client_message_id) page.messages.delete(`pending:${message.client_message_id}`);
      page.messages.set(key, message);
      page.firstId = page.firstId ? Math.min(page.firstId, key) : key;
      page.lastId = Math.max(page.lastId, key);
    });
    renderMessages();
  }

  function renderMessages() {
    const list = el('[data-chat-list]');
    if (!list) return;
    const scroller = el('[data-chat-scroll]');
    const nearBottom = scroller ? scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80 : true;
    const rows = sortedMessages();
    const newest = rows.reduce((top, message) => Math.max(top, id(message.id)), 0);
    list.innerHTML = rows.length ? rows.map(messageHtml).join('') : '<p class="joyokr-chat-none">Chưa có tin nhắn. Hãy bắt đầu cuộc trò chuyện.</p>';
    const older = el('[data-chat-older]');
    if (older) older.hidden = !page.hasMoreBefore;
    // Reading older messages: never yank the view down — say there is more.
    if (nearBottom) scrollToBottom();
    else if (newest > (page.seenId || 0)) {
      const jump = el('[data-chat-jump]');
      if (jump) jump.hidden = false;
    }
    if (nearBottom) page.seenId = newest;
  }

  function scrollToBottom() {
    const scroller = el('[data-chat-scroll]');
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
    const jump = el('[data-chat-jump]');
    if (jump) jump.hidden = true;
    if (page) page.seenId = page.lastId;
  }

  function scheduleRoom(delay) {
    if (!page || !page.roomId) return;
    clearTimeout(page.roomTimer);
    page.roomTimer = setTimeout(pollRoom, delay);
  }

  async function pollRoom() {
    if (!page?.alive || !page.roomId) return;
    if (document.hidden) {
      scheduleRoom(ROOM_POLL_MS);
      return;
    }
    const current = page;
    try {
      const query = { after: current.lastId };
      if (current.serverTime) query.changed_since = current.serverTime;
      const result = await call('GET', `rooms/${current.roomId}/messages/`, { query });
      if (!current.alive || page !== current) return;
      current.serverTime = result.server_time || current.serverTime;
      merge([...(result.results || []), ...(result.changed || [])]);
      if ((result.results || []).length) markReadSoon();
      current.backoff = ROOM_POLL_MS;
      setStatus('');
      scheduleRoom(result.has_more_after ? 200 : ROOM_POLL_MS);
    } catch (error) {
      if (!current.alive || page !== current) return;
      if (Number(error?.status) === 404) {
        setStatus('Bạn không còn quyền xem cuộc trò chuyện này.');
        return;
      }
      current.backoff = Math.min(current.backoff * 2, MAX_BACKOFF_MS);
      setStatus(`Mất kết nối, sẽ thử lại sau ${Math.round(current.backoff / 1000)} giây…`);
      scheduleRoom(current.backoff);
    }
  }

  function scheduleList() {
    if (!page) return;
    clearTimeout(page.listTimer);
    page.listTimer = setTimeout(async () => {
      if (!page?.alive) return;
      if (!document.hidden) {
        try {
          const result = await call('GET', 'rooms/');
          if (page?.alive) {
            page.rooms = result?.results || [];
            renderRoomList();
          }
        } catch (error) {
          // The list refresh retries on the next tick.
        }
        refreshBadge();
      }
      scheduleList();
    }, LIST_POLL_MS);
  }

  function renderRoomList() {
    const list = el('[data-chat-room-list]');
    if (list) list.innerHTML = roomList(page.rooms, page.roomId, page.filter, page.search);
  }

  function markReadSoon() {
    if (!page?.roomId || !page.lastId) return;
    clearTimeout(page.readTimer);
    const current = page;
    current.readTimer = setTimeout(async () => {
      if (!current.alive || document.hidden) return;
      try {
        await call('POST', `rooms/${current.roomId}/read/`, { body: { message_id: current.lastId } });
        const row = current.rooms.find((room) => id(room.id) === current.roomId);
        if (row) {
          row.unread = 0;
          renderRoomList();
        }
        refreshBadge();
      } catch (error) {
        // Read position is retried with the next message.
      }
    }, 600);
  }

  async function loadOlder() {
    if (!page?.roomId || !page.firstId) return;
    const scroller = el('[data-chat-scroll]');
    const before = scroller ? scroller.scrollHeight : 0;
    try {
      const result = await call('GET', `rooms/${page.roomId}/messages/`, { query: { before: page.firstId } });
      page.hasMoreBefore = Boolean(result.has_more_before);
      const keep = scroller ? scroller.scrollTop : 0;
      merge(result.results || []);
      renderMessages();
      if (scroller) scroller.scrollTop = scroller.scrollHeight - before + keep;
    } catch (error) {
      setStatus(error?.message || 'Không tải được tin cũ hơn.');
    }
  }

  // -- composer --------------------------------------------------------------------------------

  function input() {
    return el('[data-chat-input]');
  }

  function insertAtCaret(text) {
    const box = input();
    if (!box) return;
    const start = box.selectionStart ?? box.value.length;
    const end = box.selectionEnd ?? box.value.length;
    box.value = `${box.value.slice(0, start)}${text}${box.value.slice(end)}`;
    const caret = start + text.length;
    box.setSelectionRange(caret, caret);
    box.focus();
  }

  const IMAGE_FILE = /\.(jpe?g|png|webp)$/i;

  function releasePreview(entry) {
    if (entry.previewUrl && globalScope.URL?.revokeObjectURL) globalScope.URL.revokeObjectURL(entry.previewUrl);
    entry.previewUrl = '';
  }

  function fileState(entry) {
    if (entry.state === 'uploading') return `<progress max="100" value="${Math.round(entry.progress * 100)}" aria-label="Đang tải lên"></progress> <small>${Math.round(entry.progress * 100)}%</small>`;
    if (entry.state === 'error') return `<small class="joyokr-chat-file-error">${escapeHtml(entry.error)}</small>`;
    if (entry.state === 'done') return '<small>Sẵn sàng</small>';
    return '';
  }

  function renderFiles() {
    const list = el('[data-chat-files]');
    if (!list) return;
    list.hidden = page.files.length === 0;
    list.innerHTML = page.files.map((entry) => {
      const preview = entry.previewUrl ? `<img class="joyokr-chat-file-preview" src="${escapeHtml(entry.previewUrl)}" alt="">` : '<span aria-hidden="true">📄</span>';
      const label = entry.state === 'uploading' ? 'Hủy tải lên' : 'Bỏ tệp';
      return `<li class="is-${escapeHtml(entry.state)}">${preview}<span>${escapeHtml(entry.file.name)} <small>${escapeHtml(fileSize(entry.file.size))}</small></span>${fileState(entry)}<button type="button" data-chat-file-remove="${escapeHtml(entry.key)}" aria-label="${label}">×</button></li>`;
    }).join('');
  }

  // Each file uploads as soon as it is picked, with a progress bar and a
  // cancel button; Gửi waits for the uploads still running.
  function startUpload(entry, current) {
    const body = new FormData();
    body.append('room_id', String(current.roomId));
    body.append('file', entry.file);
    entry.state = 'uploading';
    entry.progress = 0;
    const request = namespace.api.upload('chat/attachments/', body, {
      onProgress: (fraction) => {
        entry.progress = fraction;
        if (current.alive && page === current) renderFiles();
      },
    });
    entry.request = request;
    entry.promise = request.promise.then((uploaded) => {
      entry.attachmentId = id(uploaded?.id);
      entry.state = 'done';
    }, (error) => {
      entry.state = error?.code === 'aborted' ? 'cancelled' : 'error';
      // The server's reason for this file ("Định dạng tệp không được hỗ trợ.")
      // beats the generic "Dữ liệu chưa hợp lệ.".
      entry.error = error?.fields?.file || error?.message || 'Không tải được tệp.';
    }).finally(() => {
      if (current.alive && page === current) renderFiles();
    });
  }

  function addFiles(fileList) {
    for (const file of Array.from(fileList || [])) {
      if (page.files.length >= MAX_FILES) {
        setStatus(`Mỗi tin nhắn tối đa ${MAX_FILES} tệp.`);
        break;
      }
      if (file.size > MAX_FILE_BYTES) {
        setStatus(`"${file.name}" lớn hơn 10 MB, không gửi được.`);
        continue;
      }
      // Dropped or pasted files skip the picker's filter; the server checks
      // again, this only saves a useless upload.
      const extension = (/\.[^.]+$/.exec(file.name || '') || [''])[0].toLowerCase();
      if (!ACCEPT.split(',').includes(extension)) {
        setStatus(`"${file.name}": chỉ gửi được ảnh (jpg, png, webp), PDF, Word hoặc Excel.`);
        continue;
      }
      page.fileKeys = (page.fileKeys || 0) + 1;
      const entry = {
        key: `f${page.fileKeys}`, file, attachmentId: 0, state: 'queued', progress: 0, error: '',
        previewUrl: IMAGE_FILE.test(file.name) && globalScope.URL?.createObjectURL ? globalScope.URL.createObjectURL(file) : '',
      };
      page.files.push(entry);
      startUpload(entry, page);
    }
    page.pendingSend = null;
    renderFiles();
  }

  function removeFile(key) {
    const index = page.files.findIndex((entry) => entry.key === key);
    if (index < 0) return;
    const [entry] = page.files.splice(index, 1);
    if (entry.state === 'uploading') entry.request?.abort();
    // Uploaded but not sent: drop it on the server too (cleanup would later).
    if (entry.attachmentId) call('DELETE', `attachments/${entry.attachmentId}/`).catch(() => {});
    releasePreview(entry);
    page.pendingSend = null;
    renderFiles();
  }

  function setReply(messageId) {
    const message = page.messages.get(messageId);
    const bar = el('[data-chat-reply-bar]');
    if (!message || !bar) return;
    page.replyTo = messageId;
    bar.hidden = false;
    bar.innerHTML = `<span>Trả lời <strong>${escapeHtml(message.sender?.name || '')}</strong>: ${escapeHtml(String(message.content || '').slice(0, 80))}</span><button type="button" data-chat-cancel-reply>Hủy</button>`;
    input()?.focus();
  }

  function clearReply() {
    page.replyTo = null;
    const bar = el('[data-chat-reply-bar]');
    if (bar) {
      bar.hidden = true;
      bar.innerHTML = '';
    }
  }

  function setEditing(messageId) {
    const message = page.messages.get(messageId);
    if (!message?.permissions?.edit) return;
    page.editing = messageId;
    const box = input();
    if (box) {
      box.value = message.content || '';
      box.focus();
    }
    const bar = el('[data-chat-edit-bar]');
    if (bar) bar.hidden = false;
    const send = el('[data-chat-send]');
    if (send) send.textContent = 'Lưu';
  }

  function clearEditing() {
    page.editing = null;
    const bar = el('[data-chat-edit-bar]');
    if (bar) bar.hidden = true;
    const send = el('[data-chat-send]');
    if (send) send.textContent = 'Gửi';
    const box = input();
    if (box) box.value = '';
  }

  function setSending(sending) {
    page.sending = sending;
    const send = el('[data-chat-send]');
    if (send) send.disabled = sending;
  }

  function mentionIds(content) {
    const ids = [];
    page.mentions.forEach((personId, name) => {
      if (content.includes(`@${name}`)) ids.push(personId);
    });
    return ids;
  }

  async function submit() {
    if (!page?.roomId || page.sending) return;
    const box = input();
    const content = String(box?.value || '').trim();
    if (page.editing) {
      if (!content) return;
      setSending(true);
      try {
        const changed = await call('PATCH', `messages/${page.editing}/`, { body: { content } });
        merge([changed]);
        clearEditing();
        setStatus('');
      } catch (error) {
        setStatus(error?.message || 'Chưa lưu được tin nhắn.');
      } finally {
        setSending(false);
      }
      return;
    }
    if (!content && !page.files.length) return;
    if (content.length > MAX_LENGTH) {
      setStatus(`Tin nhắn tối đa ${MAX_LENGTH} ký tự.`);
      return;
    }
    // One client id per draft: a retry of the same draft can never post twice.
    const signature = `${content}|${page.files.map((entry) => entry.file.name).join(',')}|${page.replyTo || ''}`;
    if (!page.pendingSend || page.pendingSend.signature !== signature) {
      page.pendingSend = { signature, client_message_id: clientId() };
    }
    const draft = page.pendingSend;
    const current = page;
    const pendingKey = `pending:${draft.client_message_id}`;
    current.messages.set(pendingKey, {
      client_message_id: draft.client_message_id, content, is_mine: true, pending: true,
      sender: { name: config().currentUser?.display_name || '' }, created_at: new Date().toISOString(),
    });
    renderMessages();
    scrollToBottom();
    setSending(true);
    try {
      if (current.files.some((entry) => entry.state === 'uploading')) setStatus('Đang tải tệp lên…');
      await Promise.all(current.files.map((entry) => entry.promise));
      const broken = current.files.find((entry) => entry.state !== 'done');
      if (broken) {
        throw new Error(broken.state === 'cancelled' ? `Đã hủy tải "${broken.file.name}". Bỏ tệp này rồi gửi lại.` : `"${broken.file.name}": ${broken.error || 'chưa tải lên được.'}`);
      }
      const message = await call('POST', `rooms/${current.roomId}/messages/`, {
        body: {
          content,
          client_message_id: draft.client_message_id,
          reply_to: current.replyTo || null,
          attachment_ids: current.files.map((entry) => entry.attachmentId),
          mention_ids: mentionIds(content),
        },
      });
      if (!current.alive) return;
      current.messages.delete(pendingKey);
      merge([message]);
      current.pendingSend = null;
      current.files.forEach(releasePreview);
      current.files = [];
      current.mentions.clear();
      renderFiles();
      clearReply();
      if (box) box.value = '';
      setStatus('');
      scrollToBottom();
    } catch (error) {
      if (!current.alive) return;
      const pending = current.messages.get(pendingKey);
      if (pending) pending.failed = true;
      renderMessages();
      setStatus(error?.message || 'Gửi chưa thành công. Nội dung vẫn được giữ để gửi lại.');
    } finally {
      if (current.alive) setSending(false);
    }
  }

  async function recall(messageId) {
    if (!globalScope.confirm('Thu hồi tin nhắn này? Nội dung và tệp sẽ bị gỡ với mọi người.')) return;
    try {
      merge([await call('DELETE', `messages/${messageId}/`)]);
    } catch (error) {
      setStatus(error?.message || 'Chưa thu hồi được tin nhắn.');
    }
  }

  async function toggleMembers(button) {
    const box = el('[data-chat-members]');
    if (!box) return;
    const open = box.hidden;
    box.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (!open) return;
    box.innerHTML = '<p>Đang tải…</p>';
    try {
      const result = await call('GET', `rooms/${page.roomId}/members/`);
      box.innerHTML = `<ul>${(result.results || []).map((person) => `<li>${avatar(person)} ${escapeHtml(person.name)}</li>`).join('') || '<li>Chưa có thành viên.</li>'}</ul>`;
    } catch (error) {
      box.innerHTML = `<p>${escapeHtml(error?.message || 'Không tải được danh sách thành viên.')}</p>`;
    }
  }

  function mentionToken() {
    const box = input();
    if (!box) return null;
    const before = box.value.slice(0, box.selectionStart ?? box.value.length);
    const match = before.match(/@([\p{L}\p{N}_.]{0,30})$/u);
    return match ? { term: match[1], start: before.length - match[0].length } : null;
  }

  function lookupMentions() {
    clearTimeout(page.mentionTimer);
    const token = mentionToken();
    const list = el('[data-chat-mention-list]');
    if (!token || !list) {
      if (list) list.hidden = true;
      return;
    }
    const current = page;
    current.mentionTimer = setTimeout(async () => {
      try {
        const result = await call('GET', `rooms/${current.roomId}/members/`, { query: { q: token.term } });
        if (!current.alive) return;
        const people = (result.results || []).filter((person) => id(person.id) !== id(config().currentUser?.id)).slice(0, 8);
        list.hidden = people.length === 0;
        list.innerHTML = people.map((person) => `<button type="button" role="option" data-chat-mention="${id(person.id)}" data-chat-mention-name="${escapeHtml(person.name)}">${avatar(person)} ${escapeHtml(person.name)}</button>`).join('');
      } catch (error) {
        list.hidden = true;
      }
    }, 250);
  }

  function chooseMention(personId, name) {
    const box = input();
    const token = mentionToken();
    if (!box || !token) return;
    const caret = box.selectionStart ?? box.value.length;
    box.value = `${box.value.slice(0, token.start)}@${name} ${box.value.slice(caret)}`;
    const next = token.start + name.length + 2;
    box.setSelectionRange(next, next);
    page.mentions.set(name, personId);
    const list = el('[data-chat-mention-list]');
    if (list) list.hidden = true;
    box.focus();
  }

  // -- people picker (new 1–1) ------------------------------------------------------------------

  async function searchPeople(term) {
    const list = el('[data-chat-people-list]');
    if (!list) return;
    try {
      const result = await namespace.api.send('GET', 'people/', { query: { q: term, page_size: 20 } });
      const me = id(config().currentUser?.id);
      const people = (result.results || []).filter((person) => id(person.id) !== me);
      list.innerHTML = people.map((person) => `<li><button type="button" data-chat-direct="${id(person.id)}">${avatar(person)} ${escapeHtml(person.name)}${person.department_name ? ` <small>${escapeHtml(person.department_name)}</small>` : ''}</button></li>`).join('') || '<li>Không tìm thấy ai.</li>';
    } catch (error) {
      list.innerHTML = `<li>${escapeHtml(error?.message || 'Không tải được danh sách.')}</li>`;
    }
  }

  // -- events -------------------------------------------------------------------------------

  function bind(root) {
    root.addEventListener('click', (event) => {
      if (!page) return;
      const target = event.target.closest('button, a');
      if (!target) return;
      if (target.matches('[data-chat-filter]')) {
        page.filter = target.dataset.chatFilter || '';
        root.querySelectorAll('[data-chat-filter]').forEach((button) => button.classList.toggle('is-active', button === target));
        renderRoomList();
      } else if (target.matches('[data-chat-new]')) {
        const box = el('[data-chat-people]');
        if (box) {
          box.hidden = !box.hidden;
          if (!box.hidden) {
            el('[data-chat-people-search]')?.focus();
            searchPeople('');
          }
        }
      } else if (target.matches('[data-chat-direct]')) {
        openFor('direct', target.dataset.chatDirect);
      } else if (target.matches('[data-chat-older]')) {
        loadOlder();
      } else if (target.matches('[data-chat-reply]')) {
        setReply(id(target.dataset.chatReply));
      } else if (target.matches('[data-chat-cancel-reply]')) {
        clearReply();
      } else if (target.matches('[data-chat-edit]')) {
        setEditing(id(target.dataset.chatEdit));
      } else if (target.matches('[data-chat-cancel-edit]')) {
        clearEditing();
      } else if (target.matches('[data-chat-recall]')) {
        recall(id(target.dataset.chatRecall));
      } else if (target.matches('[data-chat-retry]')) {
        submit();
      } else if (target.matches('[data-chat-emoji-toggle]')) {
        const list = el('[data-chat-emoji-list]');
        if (list) list.hidden = !list.hidden;
      } else if (target.matches('[data-chat-emoji]')) {
        insertAtCaret(target.dataset.chatEmoji);
      } else if (target.matches('[data-chat-file-remove]')) {
        removeFile(target.dataset.chatFileRemove);
      } else if (target.matches('[data-chat-jump]')) {
        scrollToBottom();
      } else if (target.matches('[data-chat-members-toggle]')) {
        toggleMembers(target);
      } else if (target.matches('[data-chat-mention]')) {
        chooseMention(id(target.dataset.chatMention), target.dataset.chatMentionName || '');
      }
    });
    root.addEventListener('submit', (event) => {
      if (!event.target.matches('[data-chat-form]')) return;
      event.preventDefault();
      submit();
    });
    root.addEventListener('keydown', (event) => {
      if (event.target.matches?.('[data-chat-input]') && event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        submit();
      }
    });
    root.addEventListener('input', (event) => {
      if (!page) return;
      if (event.target.matches('[data-chat-input]')) lookupMentions();
      else if (event.target.matches('[data-chat-room-search]')) {
        page.search = event.target.value;
        renderRoomList();
      } else if (event.target.matches('[data-chat-people-search]')) searchPeople(event.target.value);
    });
    // scroll does not bubble: listen in the capture phase. Back at the
    // bottom by hand, the "new messages" button goes away.
    root.addEventListener('scroll', (event) => {
      if (!page || !event.target.matches?.('[data-chat-scroll]')) return;
      const scroller = event.target;
      if (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80) {
        const jump = el('[data-chat-jump]');
        if (jump) jump.hidden = true;
        page.seenId = page.lastId;
      }
    }, true);
    root.addEventListener('change', (event) => {
      if (!page || !event.target.matches('[data-chat-file-input]')) return;
      addFiles(event.target.files);
      event.target.value = '';
    });
  }

  function setupGlobalListeners() {
    if (listenersReady) return;
    listenersReady = true;
    document.addEventListener('click', (event) => {
      const button = event.target.closest?.('[data-chat-open]');
      if (!button) return;
      event.preventDefault();
      openFor(button.dataset.chatKind, button.dataset.chatTarget);
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && page?.alive) {
        // Back to the tab: catch up at once instead of waiting for the timer.
        scheduleRoom(0);
        refreshBadge();
      }
    });
  }

  if (typeof document !== 'undefined') setupGlobalListeners();

  namespace.chat = Object.freeze({
    renderPage,
    mount,
    stop,
    openButton,
    openFor,
    refreshBadge,
    messageHtml,
    roomList,
    escapeHtml,
    enabled,
  });
})(globalThis);
