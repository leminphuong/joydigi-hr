(function bootApplication(globalScope) {
  'use strict';

  const namespace = globalScope.JoyOKR = globalScope.JoyOKR || {};

  // -- report export (pure; no DOM) ------------------------------------------------
  // JOYOKR exported CSV in the browser and PDF through window.print(); W4 keeps
  // exactly those two formats and adds the report context to both.

  const CSV_TITLE = 'Báo cáo OKR';
  // Pure numbers ("-5", "+1,5", "-5%", "1.234,5") stay as they are; anything else
  // starting like a formula is neutralised with a leading apostrophe.
  const PLAIN_NUMBER = /^[+-]?(?:\d+(?:[.,]\d+)*|[.,]\d+)%?$/;

  function plainText(value) {
    return value === undefined || value === null ? '' : String(value);
  }

  /** True when a spreadsheet could read the cell as a formula (=, +, -, @, tab, CR; also after spaces). */
  function needsFormulaGuard(cell) {
    if (!/^[=+\-@\t\r]/.test(cell) && !/^[  ]+[=+\-@]/.test(cell)) return false;
    return !PLAIN_NUMBER.test(cell);
  }

  /** One RFC 4180 field: always quoted, quotes doubled, formula guard applied to text. */
  function csvCell(value) {
    let cell = plainText(value);
    if (typeof value !== 'number' && needsFormulaGuard(cell)) cell = `'${cell}`;
    return `"${cell.replaceAll('"', '""')}"`;
  }

  function pad(value) {
    return String(value).padStart(2, '0');
  }

  /** A Date from a Date (any realm), a timestamp or an ISO string; null when invalid. */
  function toDate(value) {
    const candidate = value && typeof value.getTime === 'function' ? new Date(value.getTime()) : new Date(value ?? Date.now());
    return Number.isNaN(candidate.getTime()) ? null : candidate;
  }

  /** "05/11/2026 09:30" in the browser time zone; the same text in the CSV and the printed header. */
  function formatExportTime(value) {
    const parsed = toDate(value ?? new Date());
    if (!parsed) return plainText(value);
    return `${pad(parsed.getDate())}/${pad(parsed.getMonth() + 1)}/${parsed.getFullYear()} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
  }

  /** ASCII slug: Vietnamese diacritics stripped (đ -> d), lower case, a-z 0-9 and "-". */
  function slugify(value, maxLength = 48) {
    return plainText(value)
      .normalize('NFD')
      .replace(/[̀-ͯ]/g, '')
      .replace(/đ/g, 'd')
      .replace(/Đ/g, 'D')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, maxLength)
      .replace(/-+$/g, '');
  }

  /** Filters as [[label, value], ...]; a string or nothing is accepted too. */
  function filterPairs(filters) {
    if (!Array.isArray(filters)) return [];
    return filters
      .filter((pair) => Array.isArray(pair) && plainText(pair[1]).trim())
      .map(([label, value]) => [plainText(label).trim(), plainText(value).trim()]);
  }

  function filterLine(filters) {
    if (typeof filters === 'string') return filters.trim() || 'Không có';
    const pairs = filterPairs(filters);
    return pairs.length ? pairs.map(([label, value]) => (label ? `${label}: ${value}` : value)).join('; ') : 'Không có';
  }

  /**
   * The report CSV as a string: UTF-8 BOM, CRLF, every field quoted (RFC 4180).
   * First the context ("Báo cáo OKR", "Chu kỳ", "Bộ lọc", "Xuất lúc"), a blank
   * line, then each table's rows prefixed with the table name, as JOYOKR did.
   *   tables: [{ name, rows: [[header...], [cell...], ...] }]
   *   meta:   { title?, cycle, cycleName?, filters: [[label, value]] | string, exportedAt }
   */
  function buildReportCsv(tables, meta = {}) {
    const info = meta && typeof meta === 'object' ? meta : {};
    const lines = [];
    const push = (cells) => lines.push(cells.map(csvCell).join(','));
    push([plainText(info.title).trim() || CSV_TITLE]);
    push(['Chu kỳ', plainText(info.cycle).trim() || plainText(info.cycleName).trim() || 'Chưa có chu kỳ']);
    push(['Bộ lọc', filterLine(info.filters)]);
    push(['Xuất lúc', formatExportTime(info.exportedAt ?? new Date())]);
    lines.push('');
    (Array.isArray(tables) ? tables : []).forEach((table) => {
      const section = plainText(table?.name).trim() || 'Báo cáo';
      (Array.isArray(table?.rows) ? table.rows : []).forEach((row) => {
        if (Array.isArray(row) && row.length) push([section, ...row]);
      });
    });
    return `﻿${lines.join('\r\n')}\r\n`;
  }

  /** bao-cao-okr_<cycle>[_<filters>]_<YYYY-MM-DD>.csv, ASCII only. */
  function reportCsvFilename(meta = {}) {
    const info = meta && typeof meta === 'object' ? meta : {};
    const day = toDate(info.exportedAt ?? new Date()) || new Date();
    const cycleSlug = slugify(info.cycleName || info.cycle) || 'chu-ky';
    const filterSlug = typeof info.filters === 'string'
      ? slugify(info.filters, 60)
      : slugify(filterPairs(info.filters).map(([, value]) => value).join(' '), 60);
    const stamp = `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
    return `${['bao-cao-okr', cycleSlug, filterSlug, stamp].filter(Boolean).join('_')}.csv`;
  }

  namespace.app = Object.freeze({ buildReportCsv, reportCsvFilename, formatExportTime, slugify });

  const app = document.querySelector('[data-joyokr-app]');
  if (!app) return;

  const outlet = document.querySelector('#joyokr-view');
  const routePatterns = [
    ['objectives/:id', /^objectives\/(\d+)$/],
    ['key-results/:id', /^key-results\/(\d+)$/],
    ['static', /^(dashboard|my-okrs|company-okrs|tree|teams|check-ins|reports|notifications|admin\/approvals|admin\/roles|admin\/departments|admin\/teams|admin\/cycles|admin\/settings)$/],
  ];
  // JOYOKR routes whose job the HR web now does (user admin, own profile).
  const HR_OWNED_ROUTES = new Set(['profile', 'admin/users']);
  // Admin screens need these capabilities. This only decides what the browser
  // shows: Django enforces every permission again on each API call.
  const ROUTE_CAPABILITIES = Object.freeze({
    'admin/approvals': 'joyokr_approve',
    'admin/roles': 'joyokr_manage_organization',
    'admin/departments': 'joyokr_manage_organization',
    'admin/teams': 'joyokr_manage_organization',
    'admin/cycles': 'joyokr_manage_cycles',
    'admin/settings': 'joyokr_manage_settings',
  });
  // With no OKR cycle yet, administrators (manage_cycles) are sent to admin/cycles.
  const CYCLE_SETUP_READY = true;

  // Monotonic navigation id: only the latest route is allowed to paint the outlet.
  let renderSequence = 0;

  /** Same-origin URL of the SPA root (JoyOKRConfig.appUrl is a path such as "/okr/"). */
  function appBaseUrl() {
    const base = new URL(String(globalScope.JoyOKRConfig.appUrl || '/okr/'), globalScope.location.origin);
    if (base.origin !== globalScope.location.origin) return new URL('/okr/', globalScope.location.origin);
    if (!base.pathname.endsWith('/')) base.pathname += '/';
    return base;
  }

  /** Lower-case route path ("objectives/12"); anything else becomes "dashboard". */
  function normalizeRoute(route) {
    const normalized = String(route || 'dashboard').replace(/^\/+|\/+$/g, '');
    return /^[a-z0-9-]+(\/[a-z0-9-]+)*$/.test(normalized) ? normalized : 'dashboard';
  }

  /** Navigates to an application route without a full page load. */
  function navigate(route, options = {}) {
    const normalized = normalizeRoute(route);
    if (HR_OWNED_ROUTES.has(normalized)) {
      // Users and the personal profile are managed in the HR web; no /okr/ history entry.
      globalScope.location.assign(hrUrl());
      return;
    }
    const relativePath = normalized === 'dashboard' ? '' : normalized;
    const url = new URL(relativePath, appBaseUrl());
    Object.entries(options.query || {}).forEach(([key, value]) => {
      if (value !== '' && value !== null && value !== undefined) url.searchParams.set(key, String(value));
    });
    if (!options.replace) globalScope.history.pushState({ route: normalized }, '', url);
    else globalScope.history.replaceState({ route: normalized }, '', url);
    renderRoute(normalized);
  }

  async function renderRoute(route) {
    if (HR_OWNED_ROUTES.has(route)) {
      // Users and the personal profile are managed in the HR web.
      globalScope.location.assign(hrUrl());
      return;
    }
    const renderId = ++renderSequence;
    const isCurrent = () => renderId === renderSequence;
    const match = matchRoute(route);
    namespace.store.setState({ route, query: Object.fromEntries(new URLSearchParams(globalScope.location.search)) });
    closeMenu();
    if (!match || !namespace.views) {
      if (!isCurrent()) return;
      setRouteLoading(false);
      outlet.innerHTML = errorState('Không tìm thấy trang.', 'Quay lại Dashboard');
      outlet.querySelector('[data-route]')?.focus();
      return;
    }

    if (!canOpen(route)) {
      // No API call: the page would only fail with 403 anyway.
      setRouteLoading(false);
      outlet.innerHTML = forbiddenState();
      markActiveNavigation(route);
      outlet.querySelector('[data-route]')?.focus();
      return;
    }

    // Keep the current page visible while the next route loads. A slim top
    // progress indicator communicates activity without replacing the content.
    setRouteLoading(true);
    try {
      const rendered = await namespace.views.render(match.name, { route, params: match.params, outlet, isCurrent });
      if (!isCurrent() || rendered === false) return;
      markActiveNavigation(route);
      enhanceInterface(outlet);
      const heading = outlet.querySelector('h1');
      if (heading) {
        heading.tabIndex = -1;
        heading.focus({ preventScroll: true });
      }
      globalScope.scrollTo({ top: 0, behavior: 'auto' });
    } catch (error) {
      // A request from an older route may finish after the user has already
      // navigated elsewhere. Never let that obsolete response flash on screen.
      if (!isCurrent()) return;

      // When the installation truly has no OKR cycle yet, keep only the top loading
      // indicator active while verifying the cycle collection. Only after that
      // request finishes do we redirect administrators to cycle setup. This
      // prevents the transient "Chưa có chu kỳ..." error from flashing first.
      if (await redirectToCycleSetupWhenEmpty(route, error, isCurrent)) return;
      if (!isCurrent()) return;

      const isOkrDetail = ['objective-detail', 'key-result-detail'].includes(match.name);
      const fallbackRoute = error?.data?.redirect || (isOkrDetail ? 'company-okrs' : route);
      const fallbackLabel = isOkrDetail ? 'Quay lại danh sách OKR' : 'Thử lại';
      console.error('[JOYOKR] render failed', {
        route,
        view: match.name,
        status: error?.status || 0,
        code: error?.data?.code || '',
        message: error?.message || 'Unknown render error',
        error,
      });
      outlet.innerHTML = errorState(error.message || globalScope.JoyOKRConfig.strings.error, fallbackLabel, fallbackRoute);
      markActiveNavigation(route);
    } finally {
      // A stale route must never stop the loading indicator of a newer route.
      if (isCurrent()) setRouteLoading(false);
    }
  }

  async function redirectToCycleSetupWhenEmpty(route, error, isCurrent) {
    const canManageCycles = capabilities().has('joyokr_manage_cycles');
    const isNoCycleError = Number(error?.status || 0) === 404 && (
      error?.data?.code === 'no_cycle' ||
      error?.data?.code === 'joyokr_no_cycle' ||
      error?.message === 'Chưa có chu kỳ OKR đang hoạt động.'
    );

    if (!CYCLE_SETUP_READY || !isNoCycleError || route === 'admin/cycles' || !canManageCycles || !namespace.api?.request) return false;

    try {
      const cycles = await namespace.api.request('cycles', { query: { per_page: 1 } });
      if (!isCurrent()) return true;
      const hasCycle = Number(cycles?.total || 0) > 0 || (Array.isArray(cycles?.items) && cycles.items.length > 0);
      if (hasCycle) return false;
      navigate('admin/cycles', { replace: true });
      return true;
    } catch (cycleCheckError) {
      // If cycle verification itself fails, keep the original route error so
      // the user can retry instead of being redirected on uncertain data.
      return !isCurrent();
    }
  }


  function capabilities() {
    const config = globalScope.JoyOKRConfig || {};
    return new Set(config.capabilities || config.currentUser?.capabilities || []);
  }

  function canOpen(route) {
    const required = ROUTE_CAPABILITIES[route];
    return !required || capabilities().has(required);
  }

  function forbiddenState() {
    return `<section class="joyokr-empty"><div class="joyokr-empty__icon">!</div><h1>Bạn không có quyền truy cập trang này.</h1><p>Trang này dành cho người được cấp vai trò OKR phù hợp. Hãy liên hệ Quản trị OKR nếu bạn cần quyền.</p><button class="joyokr-button" type="button" data-route="dashboard">Quay lại Dashboard</button></section>`;
  }

  function setRouteLoading(loading) {
    const active = Boolean(loading);
    app.classList.toggle('is-route-loading', active);
    if (active) outlet.setAttribute?.('aria-busy', 'true');
    else outlet.removeAttribute?.('aria-busy');
  }

  function matchRoute(route) {
    for (const [kind, pattern] of routePatterns) {
      const match = route.match(pattern);
      if (!match) continue;
      if (kind === 'objectives/:id') return { name: 'objective-detail', params: { id: Number(match[1]) } };
      if (kind === 'key-results/:id') return { name: 'key-result-detail', params: { id: Number(match[1]) } };
      return { name: route, params: {} };
    }
    return null;
  }

  function markActiveNavigation(route) {
    const primary = route.replace(/^(objectives|key-results)\/\d+$/, 'company-okrs');
    document.querySelectorAll('[data-route]').forEach((link) => {
      const active = link.dataset.route === primary;
      link.classList.toggle('is-active', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }

  function openMenu() {
    document.querySelector('#joyokr-sidebar')?.classList.add('is-open');
    const overlay = document.querySelector('.joyokr-overlay');
    if (overlay) overlay.hidden = false;
    document.querySelector('[data-action="toggle-menu"]')?.setAttribute('aria-expanded', 'true');
  }

  function routeQuery(element) {
    return Object.fromEntries(new URLSearchParams(element?.dataset?.routeQuery || ''));
  }

  function closeMenu() {
    document.querySelector('#joyokr-sidebar')?.classList.remove('is-open');
    const overlay = document.querySelector('.joyokr-overlay');
    if (overlay) overlay.hidden = true;
    document.querySelector('[data-action="toggle-menu"]')?.setAttribute('aria-expanded', 'false');
  }

  function toggleTreeNode(trigger) {
    const target = trigger?.dataset?.treeTarget || '';
    if (!target) return;
    const details = document.querySelector(`[data-tree-node-details="${target}"]`);
    if (!details) return;
    const expanded = trigger.getAttribute('aria-expanded') !== 'false';
    trigger.setAttribute('aria-expanded', expanded ? 'false' : 'true');
    details.hidden = expanded;
    const icon = trigger.querySelector('[data-tree-toggle-icon]');
    if (icon) icon.textContent = expanded ? '▸' : '▾';
  }

  function toggleCheckinTreeNode(trigger) {
    const target = trigger?.dataset?.checkinTreeTarget || '';
    if (!target) return;
    const details = document.querySelector(`[data-checkin-tree-node-details="${target}"]`);
    if (!details) return;
    const expanded = trigger.getAttribute('aria-expanded') !== 'false';
    if (!expanded && details.dataset.checkinLazy) {
      // Large scopes build a department's or team's children on first open.
      const markup = namespace.views?.renderLazyCheckinChildren?.(details.dataset.checkinLazy);
      if (typeof markup === 'string') details.innerHTML = markup;
      delete details.dataset.checkinLazy;
    }
    trigger.setAttribute('aria-expanded', expanded ? 'false' : 'true');
    details.hidden = expanded;
    const icon = trigger.querySelector('[data-checkin-tree-toggle-icon]');
    if (icon) icon.textContent = expanded ? '▸' : '▾';
  }

  function showDashboardDetail(trigger) {
    const key = trigger?.dataset?.dashboardDetail || '';
    if (!key) return;
    let dashboardDetailPanel = null;
    document.querySelectorAll('[data-dashboard-detail-panel]').forEach((panel) => {
      const active = panel.dataset.dashboardDetailPanel === key;
      panel.hidden = !active;
      if (active) dashboardDetailPanel = panel;
    });
    document.querySelectorAll('[data-action="dashboard-detail"]').forEach((button) => {
      const active = button.dataset.dashboardDetail === key;
      button.setAttribute('aria-expanded', active ? 'true' : 'false');
    });
    if (!dashboardDetailPanel) return;
    dashboardDetailPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    dashboardDetailPanel.querySelector('h2')?.focus({ preventScroll: true });
  }


  /** Visible text of a table cell; separate child elements ("KR" + "Objective") stay separate words. */
  function cellText(cell) {
    const parts = cell.childNodes?.length ? [...cell.childNodes].map((node) => node.textContent || '') : [cell.textContent || ''];
    return parts.join(' ').replace(/\s+/g, ' ').trim();
  }

  /** Context of the report on screen, from the print header views.js renders. */
  function reportMetaFromPage(now) {
    const header = document.querySelector('[data-report-meta]');
    const filters = [...document.querySelectorAll('[data-report-filter]')].map((item) => [item.dataset.label || '', item.dataset.value || '']);
    return {
      title: header?.dataset.reportTitle || CSV_TITLE,
      cycle: header?.dataset.reportCycle || '',
      cycleName: header?.dataset.reportCycleName || '',
      filters,
      exportedAt: now,
    };
  }

  function exportReportCsv() {
    const tables = [...document.querySelectorAll('[data-report-export]')].map((table) => ({
      name: table.dataset.reportExportName || 'Báo cáo',
      rows: [...table.querySelectorAll('tr')].map((row) => [...row.querySelectorAll('th,td')].map(cellText)).filter((cells) => cells.length),
    }));
    if (!tables.length) return notify('Không có dữ liệu Báo cáo để xuất.', 'error');
    const meta = reportMetaFromPage(new Date());
    const blob = new Blob([buildReportCsv(tables, meta)], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = reportCsvFilename(meta);
    document.body.append(link);
    link.click();
    link.remove();
    globalScope.setTimeout(() => URL.revokeObjectURL(url), 0);
    notify('Đã xuất CSV Báo cáo.');
  }

  function printReport() {
    const now = new Date();
    const stamp = document.querySelector('[data-report-exported-at]');
    if (stamp) stamp.textContent = formatExportTime(now);
    // "Save as PDF" proposes the document title as the file name.
    const previousTitle = document.title;
    document.title = reportCsvFilename(reportMetaFromPage(now)).replace(/\.csv$/, '');
    document.body.classList.add('joyokr-print-report');
    globalScope.print();
    globalScope.setTimeout(() => {
      document.body.classList.remove('joyokr-print-report');
      document.title = previousTitle;
    }, 250);
  }

  function notify(message, tone = 'success') {
    const toast = document.querySelector('[data-toast]');
    if (!toast) return;
    toast.textContent = message;
    toast.dataset.tone = tone;
    toast.hidden = false;
    globalScope.clearTimeout(notify.timeoutId);
    notify.timeoutId = globalScope.setTimeout(() => { toast.hidden = true; }, 3500);
  }

  function errorState(message, buttonLabel, route = 'dashboard') {
    const okrFallback = route === 'company-okrs' ? 'Nội dung này có thể đã bị xóa hoặc liên kết dữ liệu đã thay đổi.' : 'Vui lòng tải lại hoặc quay về trang tổng quan.';
    return `<section class="joyokr-empty"><div class="joyokr-empty__icon">!</div><h1>${escapeHtml(message)}</h1><p>${escapeHtml(okrFallback)}</p><button class="joyokr-button" type="button" data-route="${escapeHtml(route)}">${escapeHtml(buttonLabel)}</button></section>`;
  }

  /** Adds lightweight depth, entrance and pointer-reactive effects without external libraries. */
  function enhanceInterface(root) {
    if (!root) return;

    const animated = root.querySelectorAll('.joyokr-card, .joyokr-stat, .joyokr-tree-node__card');
    animated.forEach((element, index) => {
      element.classList.add('joyokr-enter');
      element.style.setProperty('--joyokr-enter-delay', `${Math.min(index, 10) * 38}ms`);
      element.addEventListener('animationend', () => element.classList.remove('joyokr-enter'), { once: true });
    });

    const reducedMotion = globalScope.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    const finePointer = globalScope.matchMedia?.('(hover: hover) and (pointer: fine)').matches;
    if (reducedMotion || !finePointer) return;

    const tiltTargets = root.querySelectorAll('.joyokr-dashboard-hero, .joyokr-stat--dashboard, .joyokr-chart-card, .joyokr-dashboard-bottom > .joyokr-card');
    tiltTargets.forEach((element) => {
      if (element.dataset.joyokrTiltReady === '1') return;
      element.dataset.joyokrTiltReady = '1';
      element.classList.add('joyokr-tilt');

      let frameId = 0;
      const update = (event) => {
        globalScope.cancelAnimationFrame(frameId);
        frameId = globalScope.requestAnimationFrame(() => {
          const rect = element.getBoundingClientRect();
          if (!rect.width || !rect.height) return;
          const px = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
          const py = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
          const rotateX = (0.5 - py) * 4.5;
          const rotateY = (px - 0.5) * 5.5;
          element.style.setProperty('--joyokr-tilt-x', `${rotateX.toFixed(2)}deg`);
          element.style.setProperty('--joyokr-tilt-y', `${rotateY.toFixed(2)}deg`);
          element.style.setProperty('--joyokr-glow-x', `${(px * 100).toFixed(1)}%`);
          element.style.setProperty('--joyokr-glow-y', `${(py * 100).toFixed(1)}%`);
        });
      };
      const reset = () => {
        globalScope.cancelAnimationFrame(frameId);
        element.style.setProperty('--joyokr-tilt-x', '0deg');
        element.style.setProperty('--joyokr-tilt-y', '0deg');
        element.style.setProperty('--joyokr-glow-x', '50%');
        element.style.setProperty('--joyokr-glow-y', '50%');
      };

      element.addEventListener('pointermove', update, { passive: true });
      element.addEventListener('pointerleave', reset, { passive: true });
    });
  }

  const HTML_ESCAPES = Object.freeze({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' });

  /** Escapes text for HTML element content and quoted attribute values. */
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"'`]/g, (character) => HTML_ESCAPES[character]);
  }

  function hrUrl() {
    const value = String(globalScope.JoyOKRConfig.hrUrl || '/');
    return /^\/(?!\/)/.test(value) ? value : '/';
  }

  document.addEventListener('click', (event) => {
    const routeLink = event.target.closest('[data-route]');
    if (routeLink) {
      event.preventDefault();
      navigate(routeLink.dataset.route, { query: routeQuery(routeLink) });
      return;
    }
    const actionTarget = event.target.closest('[data-action]');
    const action = actionTarget?.dataset.action;
    if (action === 'toggle-menu') openMenu();
    if (action === 'close-menu') closeMenu();
    if (action === 'dashboard-detail') {
      showDashboardDetail(actionTarget);
      return;
    }
    if (action === 'toggle-tree-node') {
      toggleTreeNode(actionTarget);
      return;
    }
    if (action === 'toggle-checkin-tree-node') {
      toggleCheckinTreeNode(actionTarget);
      return;
    }
    if (action === 'export-report-csv') { exportReportCsv(); return; }
    if (action === 'print-report') { printReport(); return; }
    if (action && namespace.forms?.handleAction) namespace.forms.handleAction(action, actionTarget);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const routeTarget = event.target.closest('[data-route]');
    if (!routeTarget || routeTarget.matches('a, button, input, select, textarea')) return;
    event.preventDefault();
    navigate(routeTarget.dataset.route, { query: routeQuery(routeTarget) });
  });

  globalScope.addEventListener('popstate', () => renderRoute(routeFromLocation()));

  function routeFromLocation() {
    const base = appBaseUrl().pathname.replace(/\/$/, '');
    const path = globalScope.location.pathname;
    const relative = path === base || path.startsWith(`${base}/`) ? path.slice(base.length) : path;
    return relative.replace(/^\/+|\/+$/g, '') || 'dashboard';
  }

  namespace.navigate = navigate;
  namespace.notify = notify;
  namespace.escapeHtml = escapeHtml;
  navigate(globalScope.JoyOKRConfig.initialRoute || routeFromLocation(), { replace: true, query: Object.fromEntries(new URLSearchParams(globalScope.location.search)) });
})(globalThis);
