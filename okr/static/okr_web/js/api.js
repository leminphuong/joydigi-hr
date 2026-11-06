/* JOYOKR v1.6.45 API client, ported to the Django OKR REST API.
 *
 * Views and forms keep calling JoyOKR.api.request('<JOYOKR endpoint>', ...)
 * exactly as they did against WordPress. This file translates each call to
 * the Django path, query and body (okr/api/urls.py) and maps the Django JSON
 * (okr/api/render.py) back to the field names the JOYOKR screens read.
 *
 * Auth is the HR web login session cookie: every request is same-origin
 * with credentials, and every unsafe method sends X-CSRFToken. Nothing is
 * written to localStorage or sessionStorage.
 */
(function registerApi(globalScope) {
  'use strict';

  const namespace = globalScope.JoyOKR = globalScope.JoyOKR || {};
  const DEFAULT_TIMEOUT_MS = 15000;
  const DEFAULT_PAGE_SIZE = 20;
  const MAX_PAGE_SIZE = 100;
  const MAX_ID = Number.MAX_SAFE_INTEGER;
  const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
  const LOCKED_STATUSES = new Set(['submitted', 'approved']);
  const EDITABLE_CYCLE_STATUSES = new Set(['draft', 'active']);
  const SCOPES = ['company', 'department', 'team', 'personal'];
  const APPROVAL_STATUSES = ['draft', 'submitted', 'approved', 'rejected'];
  const PROGRESS_FORMULA = 'Σ(tiến độ × trọng số) / Σ(trọng số), chỉ tính Objective cấp Công ty đã duyệt.';
  const CYCLE_CACHE_MS = 60000;
  // JOYOKR's at-risk threshold; used only when JoyOKRConfig.warningThreshold is missing or invalid.
  const DEFAULT_WARNING_THRESHOLD = 15;
  const CYCLE_STATUSES = ['draft', 'active', 'closed'];
  const CYCLE_TYPES = ['month', 'quarter', 'year', 'custom'];
  const RECORD_STATUSES = ['active', 'inactive'];
  const OKR_ROLES = ['admin', 'manager'];
  const REMINDER_FREQUENCIES = ['weekly', 'biweekly', 'monthly'];
  // W4 report / check-in management filters (Django HEALTH_VALUES, CHECKIN_STATUSES).
  const HEALTH_VALUES = ['on_track', 'at_risk', 'off_track', 'completed'];
  const CHECKIN_STATUSES = ['overdue', 'due', 'never', 'ok'];
  const CYCLE_SELECTED_BY = ['request', 'default'];

  // Django field name -> JOYOKR form field name, so validation messages land
  // under the right input (forms.js looks up data-field-error="<name>").
  const OBJECTIVE_FIELDS = Object.freeze({ owner_id: 'owner_user_id', parent_id: 'parent_objective_id' });
  const KEY_RESULT_FIELDS = Object.freeze({ owner_id: 'owner_user_id' });
  const EVIDENCE_FIELDS = Object.freeze({ key_result: 'label' });
  const TEAM_FIELDS = Object.freeze({ manager_id: 'manager_user_id', employee_ids: 'user_ids' });
  const DEPARTMENT_FIELDS = Object.freeze({ manager_id: 'manager_user_id' });
  // Cycle (name, cycle_type, start_date, end_date, parent_id, destination_cycle_id),
  // role (employee_id, role) and settings fields keep their Django names in the forms.

  class ApiError extends Error {
    constructor(message, status, fields = {}, data = {}) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.fields = fields || {};
      this.data = data || {};
      this.code = this.data.code || '';
    }
  }

  let cycleCache = null;
  let loginRedirectStarted = false;

  function config() {
    return globalScope.JoyOKRConfig || {};
  }

  // -- transport -----------------------------------------------------------------

  /** Sends one request to the Django OKR API and returns the parsed JSON. */
  async function send(method, path, options = {}) {
    const cfg = config();
    const verb = String(method || 'GET').toUpperCase();
    const url = buildUrl(cfg.root, path, options.query);
    const headers = { Accept: 'application/json' };
    const fetchOptions = { method: verb, headers, credentials: 'same-origin' };
    const timeoutMs = normalizeTimeout(options.timeoutMs ?? cfg.apiTimeoutMs ?? DEFAULT_TIMEOUT_MS);
    const controller = typeof globalScope.AbortController === 'function' ? new globalScope.AbortController() : null;
    let timeoutId = null;
    let timedOut = false;

    if (UNSAFE_METHODS.has(verb)) headers['X-CSRFToken'] = String(cfg.csrfToken || '');
    if (controller) fetchOptions.signal = controller.signal;
    if (isFormData(options.body)) {
      fetchOptions.body = options.body; // the browser sets the multipart boundary
    } else if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      fetchOptions.body = JSON.stringify(options.body);
    }

    const timeoutError = () => new ApiError(
      'Yêu cầu mất quá nhiều thời gian. Vui lòng thử lại.',
      408,
      {},
      { code: 'request_timeout', status: 408, endpoint: String(path), timeout_ms: timeoutMs },
    );

    const requestPromise = (async () => {
      let response;
      try {
        response = await globalScope.fetch(url, fetchOptions);
      } catch (error) {
        if (timedOut) throw timeoutError();
        throw new ApiError('Không thể kết nối máy chủ. Vui lòng kiểm tra mạng và thử lại.', 0, {}, { code: 'network_error', status: 0 });
      }
      const payload = await parsePayload(response);
      if (!response.ok) throw toApiError(response.status, payload, options.fieldNames);
      return payload;
    })();

    const timeoutPromise = new Promise((_resolve, reject) => {
      timeoutId = globalScope.setTimeout(() => {
        timedOut = true;
        if (controller) controller.abort();
        reject(timeoutError());
      }, timeoutMs);
    });

    try {
      return await Promise.race([requestPromise, timeoutPromise]);
    } catch (error) {
      if (timedOut && error?.name === 'AbortError') throw timeoutError();
      throw error;
    } finally {
      if (timeoutId !== null) globalScope.clearTimeout(timeoutId);
      requestPromise.catch(() => {});
    }
  }

  function normalizeTimeout(value) {
    const timeout = Number(value);
    return Number.isFinite(timeout) && timeout > 0 ? timeout : DEFAULT_TIMEOUT_MS;
  }

  /** Same-origin path under the API root, e.g. "/api/okr/objectives/12/?page=2". */
  function buildUrl(root, path, query = {}) {
    const origin = globalScope.location?.origin || 'http://localhost';
    const base = new URL(String(root || '/api/okr/'), origin);
    const url = new URL(String(path || '').replace(/^\/+/, ''), base);
    if (url.origin !== base.origin || !url.pathname.startsWith(base.pathname)) {
      throw new ApiError('Đường dẫn API không hợp lệ.', 0, {}, { code: 'invalid_path', status: 0 });
    }
    Object.entries(query || {}).forEach(([key, value]) => {
      if (value !== '' && value !== null && value !== undefined) url.searchParams.set(key, String(value));
    });
    return `${url.pathname}${url.search}`;
  }

  async function parsePayload(response) {
    if (response.status === 204) return {};
    const text = await response.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch {
      throw new ApiError('Máy chủ trả về dữ liệu không hợp lệ.', response.status, {}, { code: 'invalid_response', status: response.status });
    }
  }

  /** Django {code, message, fields} -> JOYOKR ApiError {message, status, fields, data}. */
  function toApiError(status, payload, fieldNames) {
    const body = payload && typeof payload === 'object' ? payload : {};
    const code = typeof body.code === 'string' ? body.code : '';
    const fields = mapErrorFields(body.fields, fieldNames);
    let message = typeof body.message === 'string' && body.message.trim()
      ? body.message
      : (config().strings?.error || 'Đã có lỗi xảy ra. Vui lòng thử lại.');
    if (status === 401) {
      message = 'Phiên đăng nhập đã hết hạn. Đang chuyển tới trang đăng nhập…';
      redirectToLogin();
    }
    // 403 csrf_failed keeps the server's message ("... Vui lòng tải lại trang.")
    // and is never retried, so a stale token cannot cause a request loop.
    return new ApiError(message, status, fields, { code, status, fields });
  }

  function mapErrorFields(fields, names = {}) {
    const result = {};
    if (!fields || typeof fields !== 'object') return result;
    Object.entries(fields).forEach(([key, value]) => {
      const message = Array.isArray(value) ? value[0] : value;
      if (message === undefined || message === null) return;
      result[names[key] || key] = String(message);
    });
    return result;
  }

  function redirectToLogin() {
    if (loginRedirectStarted) return;
    loginRedirectStarted = true;
    const location = globalScope.location;
    const next = `${location?.pathname || '/'}${location?.search || ''}`;
    const loginUrl = config().loginUrl || '/login/';
    location?.assign?.(`${loginUrl}?next=${encodeURIComponent(next)}`);
  }

  function isFormData(value) {
    return typeof globalScope.FormData === 'function' && value instanceof globalScope.FormData;
  }

  // -- small value helpers ---------------------------------------------------------

  function toId(value) {
    const number = typeof value === 'number' ? value : (/^\s*\d+\s*$/.test(String(value ?? '')) ? Number(value) : NaN);
    return Number.isSafeInteger(number) && number > 0 && number <= MAX_ID ? number : 0;
  }

  function idParam(value) {
    return toId(value) || undefined;
  }

  function idOrNull(value) {
    return toId(value) || null;
  }

  function text(value) {
    return value === undefined || value === null ? '' : String(value);
  }

  function oneOf(value, allowed) {
    return allowed.includes(value) ? value : undefined;
  }

  function pageSize(value) {
    const size = toId(value);
    return size ? Math.min(size, MAX_PAGE_SIZE) : DEFAULT_PAGE_SIZE;
  }

  function pageQuery(query = {}) {
    return { page: idParam(query.page), page_size: pageSize(query.per_page ?? query.page_size) };
  }

  function currentUserId() {
    return toId(config().currentUser?.id);
  }

  function capabilities() {
    return new Set(config().capabilities || config().currentUser?.capabilities || []);
  }

  function isAdmin() {
    return capabilities().has('joyokr_manage_settings');
  }

  function ownerParam(value) {
    if (value === 'me') return 'me';
    const id = toId(value);
    if (!id) return undefined;
    return id === currentUserId() ? 'me' : id;
  }

  function fold(value) {
    return text(value).normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/đ/g, 'd').replace(/Đ/g, 'D').toLowerCase().trim();
  }

  /**
   * The company's at-risk threshold (percentage points) from JoyOKRConfig,
   * which Django fills from OKRSettings; JOYOKR's 15 when missing or invalid.
   */
  function warningThreshold() {
    const value = config().warningThreshold;
    const number = typeof value === 'number' ? value : (typeof value === 'string' && value.trim() ? Number(value) : NaN);
    return Number.isFinite(number) && number >= 0 && number <= 100 ? number : DEFAULT_WARNING_THRESHOLD;
  }

  function healthOf(progress, expected) {
    const domain = globalScope.JoyOKRDomain;
    return domain ? domain.status(Number(progress), Number(expected), warningThreshold()) : null;
  }

  /** The URL itself when it is an absolute http(s) URL, else ''. */
  function safeHttpUrl(value) {
    const raw = text(value).trim();
    if (!/^https?:\/\//i.test(raw)) return '';
    try {
      const url = new URL(raw);
      return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
    } catch {
      return '';
    }
  }

  function apiBase() {
    return String(config().root || '/api/okr/').replace(/\/okr\/?$/, '') || '/api';
  }

  /** "/api" + download_path, only for the authorised evidence download endpoint. */
  function evidenceDownloadUrl(path) {
    const raw = text(path);
    return /^\/okr\/evidence\/[1-9]\d{0,15}\/download\/$/.test(raw) ? `${apiBase()}${raw}` : '';
  }

  /** 32 lowercase hex characters (128 random bits) for check-in idempotency. */
  function newClientRequestId() {
    const bytes = new Uint8Array(16);
    const cryptoApi = globalScope.crypto;
    if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') cryptoApi.getRandomValues(bytes);
    else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  }

  function validClientRequestId(value) {
    const raw = text(value).trim();
    return /^[A-Za-z0-9_-]{8,64}$/.test(raw) ? raw : '';
  }

  // -- response mappers (Django shape -> JOYOKR shape) --------------------------------

  function toPage(payload, mapItem, query = {}) {
    const results = Array.isArray(payload?.results) ? payload.results : [];
    const perPage = pageSize(query.per_page ?? query.page_size);
    const total = Number(payload?.count) || 0;
    return {
      items: results.map(mapItem),
      total,
      page: toId(query.page) || 1,
      per_page: perPage,
      total_pages: Math.max(1, Math.ceil(total / perPage)),
    };
  }

  function results(payload) {
    return Array.isArray(payload?.results) ? payload.results : [];
  }

  function personFields(person, idKey, nameKey) {
    return { [idKey]: toId(person?.id), [nameKey]: text(person?.name) };
  }

  function mapObjective(item) {
    if (!item || typeof item !== 'object') return item;
    const cycle = item.cycle && typeof item.cycle === 'object' ? item.cycle : {};
    return {
      ...item,
      id: toId(item.id),
      ...personFields(item.owner, 'owner_user_id', 'owner_name'),
      cycle_id: toId(cycle.id),
      cycle_name: text(cycle.name),
      end_date: text(cycle.end_date),
      parent_objective_id: toId(item.parent_id) || null,
      progress_is_provisional: item.provisional === true,
    };
  }

  function mapKeyResult(item) {
    if (!item || typeof item !== 'object') return item;
    const permissions = item.permissions && typeof item.permissions === 'object' ? item.permissions : {};
    return {
      ...item,
      id: toId(item.id),
      objective_id: toId(item.objective_id),
      ...personFields(item.owner, 'owner_user_id', 'owner_name'),
      can_checkin: permissions.can_checkin === true,
      can_add_evidence: permissions.can_add_evidence === true,
    };
  }

  /**
   * JOYOKR's can_edit (can_modify_definition), rebuilt from what Django sends.
   * The server re-checks every write; this only decides which buttons show.
   */
  function canEditObjective(objective, keyResults) {
    if (objective.cycle_status && !EDITABLE_CYCLE_STATUSES.has(objective.cycle_status)) return false;
    if (isAdmin()) return true;
    if (LOCKED_STATUSES.has(objective.approval_status)) return false;
    const me = currentUserId();
    if (me && objective.owner_user_id === me) return true;
    if (!capabilities().has('joyokr_edit_managed') || !['department', 'team'].includes(objective.scope)) return false;
    // A key result owned by someone else answers can_edit_okr for this
    // objective's department/team (permissions.can_add_evidence).
    const signal = keyResults.find((kr) => kr.owner_user_id !== me);
    return signal ? signal.can_add_evidence === true : true;
  }

  function mapObjectiveDetail(item, cycleStatus = '') {
    const base = mapObjective(item);
    if (!base || typeof base !== 'object') return base;
    const permissions = item.permissions && typeof item.permissions === 'object' ? item.permissions : {};
    base.key_results = (Array.isArray(item.key_results) ? item.key_results : []).map(mapKeyResult);
    base.can_submit = permissions.can_submit === true;
    base.can_approve = permissions.can_approve === true;
    base.can_comment = permissions.can_comment !== false;
    base.cycle_status = text(cycleStatus);
    base.parent_title = text(item.parent?.title);
    base.can_edit = typeof permissions.can_edit === 'boolean'
      ? permissions.can_edit
      : canEditObjective(base, base.key_results);
    return base;
  }

  function mapKeyResultDetail(item, objective) {
    const base = mapKeyResult(item);
    base.objective_approval_status = text(objective?.approval_status);
    base.scope = text(objective?.scope);
    const permissions = item.permissions && typeof item.permissions === 'object' ? item.permissions : {};
    base.can_edit = typeof permissions.can_edit === 'boolean'
      ? permissions.can_edit
      : base.can_add_evidence && (isAdmin() || (
        !LOCKED_STATUSES.has(base.approval_status) && !LOCKED_STATUSES.has(base.objective_approval_status)
      ));
    return base;
  }

  function mapCheckin(item) {
    return { ...item, id: toId(item?.id), user_name: text(item?.author?.name) };
  }

  function mapComment(item) {
    return { ...item, id: toId(item?.id), user_name: text(item?.author?.name) };
  }

  function mapActivity(item) {
    return { ...item, id: toId(item?.id), actor_name: text(item?.actor?.name) };
  }

  function mapEvidence(item) {
    const isFile = item?.evidence_type === 'file';
    return {
      id: toId(item?.id),
      evidence_type: isFile ? 'file' : 'url',
      label: text(item?.label || item?.file_name),
      created_at: item?.created_at || '',
      user_name: text(item?.author?.name),
      file_name: text(item?.file_name),
      file_size: Number(item?.file_size) || 0,
      // Links only for http(s); files only through the authorised endpoint.
      url: isFile ? evidenceDownloadUrl(item?.download_path) : safeHttpUrl(item?.url),
    };
  }

  function mapAttention(item, expected) {
    const progress = item?.progress;
    const expectedProgress = item?.expected_progress ?? expected;
    return {
      id: toId(item?.key_result_id),
      title: text(item?.key_result_title),
      objective_title: text(item?.objective_title),
      ...personFields(item?.owner, 'owner_user_id', 'owner_name'),
      progress,
      expected_progress: expectedProgress,
      health: item?.reason === 'off_track' ? 'off_track' : healthOf(progress, expectedProgress),
      checkin_overdue: item?.reason === 'stale',
      overdue_days: Number(item?.days_overdue) || 0,
      latest_checkin_at: item?.last_checkin_at || null,
      reason: item?.reason,
    };
  }

  function progressDefinition(progress) {
    return {
      formula: PROGRESS_FORMULA,
      objective_count: Number(progress?.objective_count) || 0,
      total_weight: progress?.total_weight ?? '0',
    };
  }

  /**
   * How the server picked the shown cycle (W4). Several cycles may be active
   * at once; screens say which one is shown and, for a default pick, why.
   */
  function mapCycleSelection(raw) {
    if (!raw || typeof raw !== 'object') return null;
    return {
      selected_id: toId(raw.selected_id),
      selected_by: oneOf(raw.selected_by, CYCLE_SELECTED_BY) || 'default',
      rule: text(raw.rule),
      active_cycles: (Array.isArray(raw.active_cycles) ? raw.active_cycles : []).map((cycle) => ({
        id: toId(cycle?.id),
        name: text(cycle?.name),
        start_date: text(cycle?.start_date),
        end_date: text(cycle?.end_date),
      })),
    };
  }

  /**
   * The filters Django applied ({scope, department_id, team_id, owner_id,
   * health, status}) under the JOYOKR query names the toolbars use
   * (owner_id -> owner_user_id). Invalid values are dropped.
   */
  function mapFilters(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    const out = {};
    const scope = oneOf(source.scope, SCOPES);
    const department = toId(source.department_id);
    const team = toId(source.team_id);
    const ownerId = toId(source.owner_id);
    const health = oneOf(source.health, HEALTH_VALUES);
    const status = oneOf(source.status, CHECKIN_STATUSES);
    if (scope) out.scope = scope;
    if (department) out.department_id = department;
    if (team) out.team_id = team;
    if (ownerId) out.owner_user_id = ownerId;
    if (health) out.health = health;
    if (status) out.status = status;
    return out;
  }

  function mapGroups(rows) {
    return (Array.isArray(rows) ? rows : []).map((row) => ({
      label: text(row?.label),
      count: Number(row?.count) || 0,
      progress: row?.progress ?? '0',
    }));
  }

  function mapRatingBand(band) {
    return { label: text(band?.label), min: text(band?.min), max: text(band?.max), count: Number(band?.count) || 0 };
  }

  function mapOverdueKeyResult(item) {
    return {
      id: toId(item?.id),
      title: text(item?.title),
      objective_id: toId(item?.objective_id),
      objective_title: text(item?.objective_title),
      ...personFields(item?.owner, 'owner_user_id', 'owner_name'),
      due_date: text(item?.due_date),
      days_late: Number(item?.days_late) || 0,
      progress: item?.progress ?? '0',
    };
  }

  function mapHistoryEntry(entry) {
    const keyResult = entry?.key_result && typeof entry.key_result === 'object' ? entry.key_result : {};
    return {
      ...mapCheckin(entry),
      key_result_id: toId(keyResult.id),
      key_result_title: text(keyResult.title),
      unit: text(keyResult.unit),
      objective_title: text(entry?.objective_title),
      ...personFields(entry?.owner, 'owner_user_id', 'owner_name'),
    };
  }

  function groupByHealth(objectives) {
    const details = { all: objectives, on_track: [], at_risk: [], off_track: [], completed: [] };
    objectives.forEach((item) => { if (details[item.health]) details[item.health].push(item); });
    return details;
  }

  function mapCompanyDashboard(data, approved) {
    const expected = data?.expected_progress;
    const attention = (Array.isArray(data?.attention) ? data.attention : []).map((item) => mapAttention(item, expected));
    const teams = (Array.isArray(data?.teams) ? data.teams : []).map((team) => ({ ...team, id: toId(team.id) }));
    const featured = (Array.isArray(data?.featured) ? data.featured : []).map(mapObjective);
    const filters = mapFilters(data?.filters);
    return {
      cycle: data?.cycle || null,
      cycle_selection: mapCycleSelection(data?.cycle_selection),
      filters,
      has_filters: Object.keys(filters).length > 0,
      // The official company figure; never narrowed by the filters.
      progress: data?.progress?.value ?? 0,
      progress_definition: progressDefinition(data?.progress),
      // Weighted progress of the filtered objectives (W4).
      selection_progress: data?.selection_progress ?? data?.progress?.value ?? 0,
      expected_progress: expected,
      objective_stats: data?.objective_stats || {},
      in_progress_count: Number(data?.in_progress_count) || 0,
      pending_approval_count: Number(data?.pending_approval_count) || 0,
      key_result_count: Number(data?.key_result_count) || 0,
      key_results_completed: Number(data?.key_results_completed) || 0,
      stale_checkins: Number(data?.stale_count) || 0,
      // attention holds the 100 most urgent items; attention_total counts them all.
      attention_total: Math.max(Number(data?.attention_total) || 0, attention.length),
      active_team_count: teams.length,
      teams,
      objectives: featured,
      attention_key_results: attention,
      stale_checkin_items: attention.filter((item) => item.checkin_overdue),
      objective_details: groupByHealth(approved || featured),
    };
  }

  function mapReport(data) {
    const expected = data?.expected_progress;
    const filters = mapFilters(data?.filters);
    const history = (Array.isArray(data?.checkin_history) ? data.checkin_history : []).map(mapHistoryEntry);
    const checkinCount = Math.max(Number(data?.checkin_count) || 0, history.length);
    return {
      cycle: data?.cycle || null,
      cycle_selection: mapCycleSelection(data?.cycle_selection),
      filters,
      has_filters: Object.keys(filters).length > 0,
      summary: {
        progress: data?.progress?.value ?? 0,
        progress_definition: progressDefinition(data?.progress),
        selection_progress: data?.selection_progress ?? data?.progress?.value ?? 0,
        expected_progress: expected,
        key_result_count: Number(data?.key_result_count) || 0,
        stale_checkins: Number(data?.stale_count) || 0,
        checkin_count: checkinCount,
        objective_stats: data?.objective_stats || {},
      },
      trend: Array.isArray(data?.trend) ? data.trend : [],
      by_scope: Array.isArray(data?.by_scope) ? data.by_scope : [],
      by_owner: Array.isArray(data?.by_owner) ? data.by_owner : [],
      by_department: mapGroups(data?.by_department),
      by_team: mapGroups(data?.by_team),
      by_rating: (Array.isArray(data?.by_rating) ? data.by_rating : []).map(mapRatingBand),
      overdue_key_results: (Array.isArray(data?.overdue_key_results) ? data.overdue_key_results : []).map(mapOverdueKeyResult),
      checkin_count: checkinCount,
      checkin_history: history,
      rows: (Array.isArray(data?.rows) ? data.rows : []).map(mapObjective),
      checkin_attention_by_owner: (Array.isArray(data?.checkin_attention_by_owner) ? data.checkin_attention_by_owner : []).map((group) => ({
        ...personFields(group.owner, 'owner_user_id', 'owner_name'),
        overdue_count: Number(group.overdue_count) || 0,
        off_track_count: Number(group.off_track_count) || 0,
        items: (Array.isArray(group.items) ? group.items : []).map((item) => mapAttention(item, expected)),
      })),
    };
  }

  function mapTreeNode(node, expected) {
    const base = mapObjective(node);
    base.key_results = (Array.isArray(node?.key_results) ? node.key_results : []).map((kr) => ({
      ...kr,
      id: toId(kr.id),
      ...personFields(kr.owner, 'owner_user_id', 'owner_name'),
      expected_progress: kr.expected_progress ?? expected,
    }));
    base.children = (Array.isArray(node?.children) ? node.children : []).map((child) => mapTreeNode(child, expected));
    return base;
  }

  function mapTree(data) {
    const expected = data?.expected_progress;
    return {
      cycle: data?.cycle || null,
      cycle_selection: mapCycleSelection(data?.cycle_selection),
      expected_progress: expected,
      items: (Array.isArray(data?.items) ? data.items : []).map((node) => mapTreeNode(node, expected)),
      unlinked_items: (Array.isArray(data?.unlinked_items) ? data.unlinked_items : []).map((node) => mapTreeNode(node, expected)),
    };
  }

  function mapManagement(data) {
    const personNode = (node) => ({
      id: toId(node?.employee?.id),
      display_name: text(node?.employee?.name),
      objectives: (Array.isArray(node?.objectives) ? node.objectives : []).map((objective) => ({
        ...objective,
        id: toId(objective.id),
        key_results: (Array.isArray(objective.key_results) ? objective.key_results : []).map((kr) => {
          const checkins = (Array.isArray(kr.checkins) ? kr.checkins : []).map(mapCheckin);
          return {
            ...kr,
            id: toId(kr.id),
            due_date: text(kr.due_date),
            checkin_status: oneOf(kr.checkin_status, CHECKIN_STATUSES) || '',
            // checkins holds only the newest 5; checkin_total counts them all.
            checkin_total: Math.max(Number(kr.checkin_total) || 0, checkins.length),
            checkins,
          };
        }),
      })),
    });
    const summary = data?.summary && typeof data.summary === 'object' ? data.summary : {};
    const filters = mapFilters(data?.filters);
    return {
      ...data,
      can_manage: data?.can_manage === true,
      cycle_selection: mapCycleSelection(data?.cycle_selection),
      filters,
      has_filters: Object.keys(filters).length > 0,
      summary: {
        ...summary,
        overdue_count: Number(summary.overdue_count) || 0,
        due_count: Number(summary.due_count) || 0,
      },
      departments: (Array.isArray(data?.departments) ? data.departments : []).map((department) => ({
        id: toId(department.id),
        name: text(department.name),
        teams: (Array.isArray(department.teams) ? department.teams : []).map((team) => ({
          id: toId(team.id),
          name: text(team.name),
          people: (Array.isArray(team.people) ? team.people : []).map(personNode),
        })),
      })),
      unassigned: (Array.isArray(data?.unassigned) ? data.unassigned : []).map(personNode),
    };
  }

  function mapNotification(item) {
    const reference = item?.reference && typeof item.reference === 'object' ? item.reference : null;
    const referenceId = toId(reference?.id);
    const available = reference ? reference.available !== false : true;
    let route = '';
    if (reference && available && referenceId) {
      if (item.type === 'approval_requested') route = 'admin/approvals';
      else if (reference.type === 'objective') route = `objectives/${referenceId}`;
      else if (reference.type === 'key_result') route = `key-results/${referenceId}`;
    }
    return {
      id: toId(item?.id),
      type: text(item?.type),
      title: text(item?.title),
      message: text(item?.message),
      is_read: item?.is_read ? 1 : 0,
      created_at: item?.created_at || '',
      reference_type: text(reference?.type),
      reference_id: referenceId,
      reference_state: reference && !available ? 'missing' : '',
      reference_route: route,
    };
  }

  function mapApprovals(payload, search) {
    let items = results(payload).map((entry) => {
      const objective = mapObjective(entry?.objective || {});
      return {
        ...objective,
        submitted_at: entry?.submitted_at || '',
        updated_at: entry?.submitted_at || '',
        approval_key_results: Array.isArray(entry?.key_results) ? entry.key_results : [],
        checks: entry?.checks || {},
        can_approve: entry?.can_approve === true,
        block_reason: text(entry?.block_reason),
      };
    });
    const queueTotal = Number(payload?.count) || items.length;
    const needle = fold(search);
    if (needle) items = items.filter((item) => fold(item.title).includes(needle));
    const total = needle ? items.length : queueTotal;
    return {
      items,
      total,
      queue_total: queueTotal,
      objective_count: total,
      key_result_count: items.reduce((sum, item) => sum + item.approval_key_results.length, 0),
      page: 1,
      per_page: MAX_PAGE_SIZE,
      total_pages: 1,
    };
  }

  function mapCycle(item) {
    if (!item || typeof item !== 'object') return item;
    const cycle = { ...item, id: toId(item.id), parent_id: toId(item.parent_id) || null };
    if (has(item, 'objective_count')) cycle.objective_count = Number(item.objective_count) || 0;
    return cycle;
  }

  function mapPerson(person) {
    return { id: toId(person?.id), display_name: text(person?.name) };
  }

  function mapTeam(item) {
    if (!item || typeof item !== 'object') return item;
    const members = Array.isArray(item.members) ? item.members.map(mapPerson) : [];
    return {
      ...item,
      id: toId(item.id),
      name: text(item.name),
      description: text(item.description),
      department_id: toId(item.department_id) || null,
      department_name: text(item.department_name),
      ...personFields(item.manager, 'manager_user_id', 'manager_name'),
      status: text(item.status) || 'active',
      members,
      member_count: Number(item.member_count ?? members.length) || 0,
    };
  }

  function mapDepartment(item) {
    if (!item || typeof item !== 'object') return item;
    return {
      ...item,
      id: toId(item.id),
      name: text(item.name),
      status: 'active',
      ...personFields(item.okr_manager, 'manager_user_id', 'manager_name'),
    };
  }

  function mapRole(item) {
    return {
      id: toId(item?.id),
      ...personFields(item?.employee, 'employee_id', 'employee_name'),
      role: text(item?.role),
      granted_at: item?.granted_at || '',
      ...personFields(item?.granted_by, 'granted_by_id', 'granted_by_name'),
    };
  }

  function mapRoleEvent(item) {
    const department = item?.department && typeof item.department === 'object' ? item.department : null;
    return {
      id: toId(item?.id),
      ...personFields(item?.employee, 'employee_id', 'employee_name'),
      role: text(item?.role),
      action: text(item?.action),
      department_id: toId(department?.id) || null,
      department_name: text(department?.name),
      ...personFields(item?.actor, 'actor_id', 'actor_name'),
      created_at: item?.created_at || '',
    };
  }

  function mapSettings(data) {
    const schedule = data?.reminder_schedule && typeof data.reminder_schedule === 'object' ? data.reminder_schedule : {};
    const threshold = Number(data?.warning_threshold);
    return {
      warning_threshold: Number.isFinite(threshold) ? threshold : DEFAULT_WARNING_THRESHOLD,
      reminder_frequency: oneOf(data?.reminder_frequency, REMINDER_FREQUENCIES) || 'weekly',
      email_reminders: data?.email_reminders === true,
      rating_ranges: (Array.isArray(data?.rating_ranges) ? data.rating_ranges : []).map((range) => ({
        min: text(range?.min),
        max: text(range?.max),
        label: text(range?.label),
      })),
      reminder_schedule: {
        scheduled: schedule.scheduled === true,
        command: text(schedule.command),
        channels: (Array.isArray(schedule.channels) ? schedule.channels : []).map(text),
        email_available: schedule.email_available === true,
      },
    };
  }

  /** Keeps the client-side health colours in step with the stored threshold (Django "15.00"). */
  function rememberThreshold(data) {
    const raw = data?.warning_threshold;
    const threshold = typeof raw === 'number' || (typeof raw === 'string' && raw.trim()) ? Number(raw) : NaN;
    const cfg = globalScope.JoyOKRConfig;
    if (cfg && typeof cfg === 'object' && Number.isFinite(threshold) && threshold >= 0 && threshold <= 100) {
      cfg.warningThreshold = threshold;
    }
  }

  // -- request bodies (JOYOKR names -> Django names) -----------------------------------

  function plainBody(body) {
    if (isFormData(body)) {
      const result = {};
      for (const [key, value] of body.entries()) if (typeof value === 'string') result[key] = value;
      return result;
    }
    return body && typeof body === 'object' ? body : {};
  }

  function has(body, key) {
    return Object.prototype.hasOwnProperty.call(body, key);
  }

  function objectiveBody(input) {
    const body = plainBody(input);
    const out = {};
    if (has(body, 'title')) out.title = text(body.title);
    if (has(body, 'description')) out.description = text(body.description);
    if (has(body, 'cycle_id')) out.cycle_id = idOrNull(body.cycle_id);
    if (has(body, 'scope')) out.scope = text(body.scope);
    if (has(body, 'department_id')) out.department_id = idOrNull(body.department_id);
    if (has(body, 'team_id')) out.team_id = idOrNull(body.team_id);
    if (has(body, 'owner_user_id') || has(body, 'owner_id')) out.owner_id = idOrNull(body.owner_user_id ?? body.owner_id);
    if (has(body, 'parent_objective_id') || has(body, 'parent_id')) out.parent_id = idOrNull(body.parent_objective_id ?? body.parent_id);
    if (has(body, 'weight')) out.weight = text(body.weight);
    if (has(body, 'priority')) out.priority = text(body.priority);
    return out;
  }

  function keyResultBody(input) {
    const body = plainBody(input);
    const out = {};
    if (has(body, 'objective_id')) out.objective_id = idOrNull(body.objective_id);
    ['title', 'description', 'unit', 'start_value', 'current_value', 'target_value', 'weight', 'priority', 'due_date'].forEach((key) => {
      if (has(body, key)) out[key] = text(body[key]);
    });
    if (has(body, 'owner_user_id') || has(body, 'owner_id')) out.owner_id = idOrNull(body.owner_user_id ?? body.owner_id);
    return out;
  }

  function checkinBody(keyResultId, input) {
    const body = plainBody(input);
    const out = {
      key_result: keyResultId,
      current_value: text(body.current_value).trim(),
      confidence: Number(body.confidence),
      update_text: text(body.update_text),
      client_request_id: validClientRequestId(body.client_request_id) || newClientRequestId(),
    };
    if (has(body, 'blockers')) out.blockers = text(body.blockers);
    if (has(body, 'next_steps')) out.next_steps = text(body.next_steps);
    return out;
  }

  function targetParam(source, prefix = 'object') {
    const type = source?.[`${prefix}_type`];
    const id = toId(source?.[`${prefix}_id`]);
    if (!id) return null;
    if (type === 'objective') return { objective: id };
    if (type === 'key_result') return { key_result: id };
    return null;
  }

  function invalidTarget() {
    return new ApiError('Thiếu Objective hoặc Key Result.', 400, {}, { code: 'validation_error', status: 400 });
  }

  function evidenceRequest(input) {
    const read = (key) => (isFormData(input) ? input.get(key) : input?.[key]);
    const keyResult = toId(read('key_result_id') ?? read('key_result'));
    const label = text(read('label')).trim();
    const url = text(read('url')).trim();
    const file = read('file');
    const checkin = toId(read('checkin'));
    if (!keyResult) return { error: new ApiError('Thiếu Key Result.', 400, {}, { code: 'validation_error', status: 400 }) };
    if (url) {
      if (!safeHttpUrl(url)) {
        const message = 'Đường dẫn phải bắt đầu bằng http:// hoặc https://.';
        return { error: new ApiError(message, 400, { url: message }, { code: 'validation_error', status: 400, fields: { url: message } }) };
      }
      const body = { key_result: keyResult, evidence_type: 'url', url, label };
      if (checkin) body.checkin = checkin;
      return { body };
    }
    if (file && typeof file === 'object' && Number(file.size) > 0 && typeof globalScope.FormData === 'function') {
      const body = new globalScope.FormData();
      body.append('key_result', String(keyResult));
      body.append('evidence_type', 'file');
      body.append('file', file, file.name || 'evidence');
      body.append('label', label);
      if (checkin) body.append('checkin', String(checkin));
      return { body };
    }
    const message = 'Vui lòng nhập liên kết hoặc chọn một tệp.';
    return { error: new ApiError(message, 400, { url: message }, { code: 'validation_error', status: 400, fields: { url: message } }) };
  }

  /** A form number as a JSON number; blank or non-numeric text goes as-is so Django reports it. */
  function numberOrText(value) {
    if (typeof value === 'number') return Number.isFinite(value) ? value : text(value);
    const raw = text(value).trim();
    if (!raw) return '';
    const number = Number(raw);
    return Number.isFinite(number) ? number : raw;
  }

  function checked(value) {
    return value === true || ['true', 'on', '1', 'yes'].includes(text(value).toLowerCase());
  }

  function cycleBody(input) {
    const body = plainBody(input);
    return {
      name: text(body.name).trim(),
      cycle_type: oneOf(body.cycle_type, CYCLE_TYPES) || 'custom',
      start_date: text(body.start_date).trim(),
      end_date: text(body.end_date).trim(),
      // JOYOKR's "no parent" was 0 (or an empty select).
      parent_id: idOrNull(body.parent_id),
    };
  }

  function teamBody(input) {
    const body = plainBody(input);
    return {
      name: text(body.name).trim(),
      description: text(body.description),
      department_id: idOrNull(body.department_id),
      manager_id: idOrNull(body.manager_user_id ?? body.manager_id),
    };
  }

  function idList(values) {
    const list = Array.isArray(values) ? values : (values === undefined || values === null || values === '' ? [] : [values]);
    return [...new Set(list.map(toId).filter(Boolean))];
  }

  function teamMembersBody(input) {
    if (isFormData(input)) return { employee_ids: idList(input.getAll('user_ids').concat(input.getAll('employee_ids'))) };
    const body = plainBody(input);
    return { employee_ids: idList(body.user_ids ?? body.employee_ids) };
  }

  function roleBody(input) {
    const body = plainBody(input);
    return {
      employee_id: idOrNull(body.employee_id ?? body.user_id),
      role: oneOf(text(body.role), OKR_ROLES) || text(body.role),
    };
  }

  function ratingRangesFrom(body) {
    if (Array.isArray(body.rating_ranges)) return body.rating_ranges;
    // The settings form's flat rating_<i>_min / _max / _label inputs.
    const ranges = [];
    for (let index = 0; has(body, `rating_${index}_label`); index += 1) {
      ranges.push({ min: body[`rating_${index}_min`], max: body[`rating_${index}_max`], label: body[`rating_${index}_label`] });
    }
    return ranges.length ? ranges : null;
  }

  /** Partial settings update: only the keys the form sent. */
  function settingsBody(input) {
    const body = plainBody(input);
    const out = {};
    if (has(body, 'warning_threshold')) out.warning_threshold = numberOrText(body.warning_threshold);
    if (has(body, 'reminder_frequency')) out.reminder_frequency = text(body.reminder_frequency);
    if (has(body, 'email_reminders')) out.email_reminders = checked(body.email_reminders);
    const ranges = ratingRangesFrom(body);
    if (ranges) {
      out.rating_ranges = ranges.map((range) => ({
        min: numberOrText(range?.min),
        max: numberOrText(range?.max),
        label: text(range?.label).trim(),
      }));
    }
    return out;
  }

  // -- composite reads -------------------------------------------------------------

  function cycleStatuses() {
    const now = Date.now();
    if (!cycleCache || now - cycleCache.storedAt > CYCLE_CACHE_MS) {
      const promise = send('GET', 'cycles/', { query: { page_size: MAX_PAGE_SIZE } })
        .then((payload) => new Map(results(payload).map((cycle) => [toId(cycle.id), text(cycle.status)])))
        .catch(() => new Map());
      cycleCache = { storedAt: now, promise };
    }
    return cycleCache.promise;
  }

  async function getObjective(id) {
    const [detail, statuses] = await Promise.all([send('GET', `objectives/${id}/`), cycleStatuses()]);
    return mapObjectiveDetail(detail, statuses.get(toId(detail?.cycle?.id)) || '');
  }

  async function getKeyResult(id) {
    const item = await send('GET', `key-results/${id}/`);
    let objective = null;
    const objectiveId = toId(item?.objective_id);
    if (objectiveId) {
      try {
        objective = await send('GET', `objectives/${objectiveId}/`);
      } catch (error) {
        if (error?.status === 401) throw error;
      }
    }
    return mapKeyResultDetail(item, objective);
  }

  /**
   * Shared W4 reporting filters (dashboard, report): JOYOKR's department_id /
   * team_id / owner_user_id become department / team / owner (employee ids,
   * never "me"); scope only when it is a real scope.
   */
  function reportingQuery(query) {
    return {
      cycle: idParam(query.cycle_id ?? query.cycle),
      scope: oneOf(query.scope, SCOPES),
      department: idParam(query.department_id ?? query.department),
      team: idParam(query.team_id ?? query.team),
      owner: idParam(query.owner_user_id ?? query.owner),
    };
  }

  function managementQuery(query) {
    return {
      cycle: idParam(query.cycle_id ?? query.cycle),
      department: idParam(query.department_id ?? query.department),
      team: idParam(query.team_id ?? query.team),
      owner: idParam(query.owner_user_id ?? query.owner),
      status: oneOf(query.status, CHECKIN_STATUSES),
    };
  }

  async function getCompanyDashboard(query, options) {
    const data = await send('GET', 'dashboard/company/', { query: reportingQuery(query) });
    let approved = null;
    const cycleId = toId(data?.cycle?.id);
    if (cycleId && options.summaryOnly !== true) {
      // The drill-down lists use the filters the server applied, so they
      // list exactly the objectives the KPI cards count.
      const applied = mapFilters(data?.filters);
      try {
        const payload = await send('GET', 'objectives/', {
          query: {
            cycle: cycleId,
            status: 'approved',
            page_size: MAX_PAGE_SIZE,
            scope: applied.scope,
            department: idParam(applied.department_id),
            team: idParam(applied.team_id),
            owner: idParam(applied.owner_user_id),
          },
        });
        approved = results(payload).map(mapObjective);
      } catch (error) {
        if (error?.status === 401) throw error;
      }
    }
    return mapCompanyDashboard(data, approved);
  }

  function objectiveListQuery(query) {
    return {
      ...pageQuery(query),
      q: text(query.search ?? query.q).trim() || undefined,
      cycle: idParam(query.cycle_id ?? query.cycle),
      status: oneOf(query.approval_status, APPROVAL_STATUSES),
      scope: oneOf(query.scope, SCOPES),
      department: idParam(query.department_id ?? query.department),
      team: idParam(query.team_id ?? query.team),
      owner: ownerParam(query.owner_user_id ?? query.owner),
      order_by: oneOf(query.order_by, ['updated', 'title', 'progress']),
    };
  }

  // -- JOYOKR endpoint table ----------------------------------------------------------

  const ROUTES = [
    ['GET', /^dashboard$/, ({ query, options }) => getCompanyDashboard(query, options)],
    ['GET', /^reports$/, ({ query }) => send('GET', 'reports/', {
      query: { ...reportingQuery(query), health: oneOf(query.health, HEALTH_VALUES) },
    }).then(mapReport)],
    ['GET', /^tree$/, ({ query }) => send('GET', 'tree/', { query: { cycle: idParam(query.cycle_id ?? query.cycle) } }).then(mapTree)],

    ['GET', /^objectives$/, ({ query }) => send('GET', 'objectives/', { query: objectiveListQuery(query) }).then((payload) => toPage(payload, mapObjective, query))],
    ['POST', /^objectives$/, ({ body }) => send('POST', 'objectives/', { body: objectiveBody(body), fieldNames: OBJECTIVE_FIELDS }).then((item) => mapObjectiveDetail(item))],
    ['GET', /^objectives\/(\d+)$/, ({ id }) => getObjective(id)],
    ['PUT', /^objectives\/(\d+)$/, ({ id, body }) => send('PUT', `objectives/${id}/`, { body: objectiveBody(body), fieldNames: OBJECTIVE_FIELDS }).then((item) => mapObjectiveDetail(item))],
    ['DELETE', /^objectives\/(\d+)$/, ({ id }) => send('DELETE', `objectives/${id}/`)],
    ['POST', /^objectives\/(\d+)\/submit$/, ({ id }) => send('POST', `objectives/${id}/submit/`)],
    ['POST', /^objectives\/(\d+)\/approve$/, ({ id }) => send('POST', `objectives/${id}/approve/`)],
    ['POST', /^objectives\/(\d+)\/reject$/, ({ id, body }) => send('POST', `objectives/${id}/reject/`, { body: { reason: text(plainBody(body).reason).trim() } })],
    ['POST', /^objectives\/(\d+)\/normalize-kr-weights$/, ({ id }) => send('POST', `objectives/${id}/normalize-kr-weights/`).then((item) => mapObjectiveDetail(item))],

    ['POST', /^key-results$/, ({ body }) => send('POST', 'key-results/', { body: keyResultBody(body), fieldNames: KEY_RESULT_FIELDS }).then(mapKeyResult)],
    ['GET', /^key-results\/(\d+)$/, ({ id }) => getKeyResult(id)],
    ['PUT', /^key-results\/(\d+)$/, ({ id, body }) => send('PUT', `key-results/${id}/`, { body: keyResultBody(body), fieldNames: KEY_RESULT_FIELDS }).then(mapKeyResult)],
    ['DELETE', /^key-results\/(\d+)$/, ({ id }) => send('DELETE', `key-results/${id}/`)],
    ['GET', /^key-results\/(\d+)\/checkins$/, ({ id }) => send('GET', 'checkins/', { query: { key_result: id, page_size: MAX_PAGE_SIZE } }).then((payload) => results(payload).map(mapCheckin))],
    ['POST', /^key-results\/(\d+)\/checkins$/, ({ id, body }) => send('POST', 'checkins/', { body: checkinBody(id, body) })],

    ['GET', /^checkin-queue$/, ({ query }) => send('GET', 'checkins/queue/', {
      query: { cycle: idParam(query.cycle_id ?? query.cycle), page_size: MAX_PAGE_SIZE },
    }).then((payload) => results(payload).map((row) => ({
      ...mapKeyResult(row),
      latest_checkin: row.latest_checkin ? mapCheckin(row.latest_checkin) : null,
    })))],
    ['GET', /^checkin-management-tree$/, ({ query }) => send('GET', 'checkins/management/', { query: managementQuery(query) }).then(mapManagement)],

    ['GET', /^comments$/, ({ query }) => {
      const target = targetParam(query);
      if (!target) return Promise.reject(invalidTarget());
      return send('GET', 'comments/', { query: { ...target, page_size: MAX_PAGE_SIZE } }).then((payload) => results(payload).map(mapComment));
    }],
    ['POST', /^comments$/, ({ body }) => {
      const values = plainBody(body);
      const target = targetParam(values);
      if (!target) return Promise.reject(invalidTarget());
      return send('POST', 'comments/', { body: { ...target, content: text(values.content) } });
    }],
    ['GET', /^activity$/, ({ query }) => {
      const target = targetParam(query);
      if (!target) return Promise.reject(invalidTarget());
      return send('GET', 'activity/', { query: { ...target, page_size: MAX_PAGE_SIZE } }).then((payload) => results(payload).map(mapActivity));
    }],
    ['GET', /^evidence$/, ({ query }) => {
      const keyResult = idParam(query.key_result_id ?? query.key_result);
      if (!keyResult) return Promise.reject(invalidTarget());
      return send('GET', 'evidence/', { query: { key_result: keyResult, page_size: MAX_PAGE_SIZE } }).then((payload) => results(payload).map(mapEvidence));
    }],
    ['POST', /^evidence$/, ({ body }) => {
      const prepared = evidenceRequest(body);
      if (prepared.error) return Promise.reject(prepared.error);
      return send('POST', 'evidence/', { body: prepared.body, fieldNames: EVIDENCE_FIELDS }).then((item) => ({ ...mapEvidence(item), message: 'Đã thêm bằng chứng.' }));
    }],

    ['GET', /^approvals$/, ({ query }) => send('GET', 'approvals/', { query: { page_size: MAX_PAGE_SIZE } }).then((payload) => mapApprovals(payload, query.search))],

    ['GET', /^notifications$/, ({ query }) => send('GET', 'notifications/', { query: pageQuery(query) }).then((payload) => ({
      ...toPage(payload, mapNotification, query),
      unread: Number(payload?.unread_count) || 0,
    }))],
    [['POST', 'PUT'], /^notifications\/(\d+)\/read$/, ({ id }) => send('POST', `notifications/${id}/read/`)],
    [['POST', 'PUT'], /^notifications\/read-all$/, () => send('POST', 'notifications/read-all/')],

    // Cycles. A status of "all" (or none) lists every cycle.
    ['GET', /^cycles$/, ({ query }) => send('GET', 'cycles/', {
      query: { ...pageQuery(query), status: oneOf(query.status, CYCLE_STATUSES), cycle_type: oneOf(query.cycle_type, CYCLE_TYPES) },
    }).then((payload) => toPage(payload, mapCycle, query))],
    ['POST', /^cycles$/, ({ body }) => send('POST', 'cycles/', { body: cycleBody(body) }).then(mapCycle)],
    ['GET', /^cycles\/(\d+)$/, ({ id }) => send('GET', `cycles/${id}/`).then(mapCycle)],
    ['PUT', /^cycles\/(\d+)$/, ({ id, body }) => send('PUT', `cycles/${id}/`, { body: cycleBody(body) }).then(mapCycle)],
    [['POST', 'PUT'], /^cycles\/(\d+)\/status$/, ({ id, body }) => send('POST', `cycles/${id}/status/`, {
      body: { status: text(plainBody(body).status) },
    }).then((item) => ({ ...mapCycle(item), message: 'Đã cập nhật trạng thái chu kỳ.' }))],
    ['POST', /^cycles\/(\d+)\/clone$/, ({ id, body }) => send('POST', `cycles/${id}/clone/`, {
      body: { destination_cycle_id: idOrNull(plainBody(body).destination_cycle_id) },
    }).then((result) => ({ ...result, destination_cycle_id: toId(result?.destination_cycle_id), objective_count: Number(result?.objective_count) || 0 }))],

    // Teams. JOYOKR hid a team with DELETE and brought it back with /restore.
    ['GET', /^teams$/, ({ query }) => send('GET', 'teams/', {
      query: { ...pageQuery(query), status: oneOf(query.status, RECORD_STATUSES) },
    }).then((payload) => toPage(payload, mapTeam, query))],
    ['POST', /^teams$/, ({ body }) => send('POST', 'teams/', { body: teamBody(body), fieldNames: TEAM_FIELDS }).then(mapTeam)],
    ['GET', /^teams\/(\d+)$/, ({ id }) => send('GET', `teams/${id}/`).then(mapTeam)],
    ['PUT', /^teams\/(\d+)$/, ({ id, body }) => send('PUT', `teams/${id}/`, { body: teamBody(body), fieldNames: TEAM_FIELDS }).then(mapTeam)],
    ['DELETE', /^teams\/(\d+)$/, ({ id }) => send('POST', `teams/${id}/status/`, { body: { status: 'inactive' } })
      .then((item) => ({ ...mapTeam(item), message: 'Đã ẩn team.' }))],
    ['POST', /^teams\/(\d+)\/restore$/, ({ id }) => send('POST', `teams/${id}/status/`, { body: { status: 'active' } })
      .then((item) => ({ ...mapTeam(item), message: 'Đã khôi phục team.' }))],
    [['POST', 'PUT'], /^teams\/(\d+)\/status$/, ({ id, body }) => send('POST', `teams/${id}/status/`, {
      body: { status: text(plainBody(body).status) },
    }).then(mapTeam)],
    ['PUT', /^teams\/(\d+)\/members$/, ({ id, body }) => send('PUT', `teams/${id}/members/`, { body: teamMembersBody(body), fieldNames: TEAM_FIELDS })
      .then((item) => ({ ...mapTeam(item), message: 'Đã cập nhật thành viên team.' }))],

    // Departments belong to the HR web: OKR only names their OKR manager.
    ['GET', /^departments$/, ({ query }) => send('GET', 'departments/', { query: pageQuery(query) }).then((payload) => toPage(payload, mapDepartment, query))],
    ['PUT', /^departments\/(\d+)(?:\/okr-manager)?$/, ({ id, body }) => {
      const values = plainBody(body);
      return send('PUT', `departments/${id}/okr-manager/`, {
        body: { manager_id: idOrNull(values.manager_user_id ?? values.manager_id) },
        fieldNames: DEPARTMENT_FIELDS,
      }).then((item) => ({ ...mapDepartment(item), message: 'Đã cập nhật quản lý OKR của phòng ban.' }));
    }],
    ['GET', /^people$/, ({ query }) => send('GET', 'people/', {
      query: { ...pageQuery(query), q: text(query.search ?? query.q).trim() || undefined, department: idParam(query.department_id ?? query.department) },
    }).then((payload) => toPage(payload, (person) => ({ ...person, id: toId(person.id), display_name: text(person.name) }), query))],

    // OKR roles (admin only): separate from HR permissions.
    ['GET', /^roles$/, ({ query }) => send('GET', 'roles/', { query: pageQuery(query) }).then((payload) => toPage(payload, mapRole, query))],
    ['POST', /^roles$/, ({ body }) => send('POST', 'roles/', { body: roleBody(body) }).then((item) => ({ ...mapRole(item), message: 'Đã gán vai trò OKR.' }))],
    ['DELETE', /^roles\/(\d+)$/, ({ id }) => send('DELETE', `roles/${id}/`)],
    ['GET', /^(?:role-history|roles\/history)$/, ({ query }) => send('GET', 'roles/history/', { query: pageQuery(query) }).then((payload) => toPage(payload, mapRoleEvent, query))],

    // Settings: every employee may read; admins save a partial update.
    ['GET', /^settings$/, () => send('GET', 'settings/').then((data) => {
      rememberThreshold(data);
      return mapSettings(data);
    })],
    ['PUT', /^settings$/, ({ body }) => send('PUT', 'settings/', { body: settingsBody(body) }).then((data) => {
      rememberThreshold(data);
      return { ...mapSettings(data), message: 'Đã lưu cài đặt.' };
    })],
  ];

  function unsupported(endpoint, method) {
    return new ApiError(
      'Chức năng này không có trên OKR của Web HR.',
      501,
      {},
      { code: 'unsupported_endpoint', status: 501, endpoint: String(endpoint), method },
    );
  }

  /**
   * JOYOKR's request(endpoint, {method, query, body}) on top of the Django API.
   * Endpoints JOYOKR had but Django does not (users, profile, chat, demo
   * seeding, system status, notification sync, admin transfer, standalone KR
   * workflow, creating/hiding/restoring HR departments) are rejected here
   * without a network call.
   */
  async function request(endpoint, options = {}) {
    const method = String(options.method || 'GET').toUpperCase();
    const path = String(endpoint ?? '').replace(/^\/+|\/+$/g, '');
    const query = options.query && typeof options.query === 'object' ? options.query : {};
    for (const [methods, pattern, handler] of ROUTES) {
      const allowed = Array.isArray(methods) ? methods : [methods];
      if (!allowed.includes(method)) continue;
      const match = path.match(pattern);
      if (!match) continue;
      const id = match[1] === undefined ? undefined : toId(match[1]);
      if (match[1] !== undefined && !id) throw unsupported(endpoint, method);
      if (method !== 'GET') cycleCache = null;
      return handler({ id, match, query, body: options.body, options });
    }
    throw unsupported(endpoint, method);
  }

  namespace.api = Object.freeze({
    request,
    send,
    ApiError,
    newClientRequestId,
    safeHttpUrl,
    evidenceDownloadUrl,
    warningThreshold,
    mappers: Object.freeze({
      mapObjective,
      mapObjectiveDetail,
      mapKeyResult,
      mapKeyResultDetail,
      mapEvidence,
      mapNotification,
      mapCompanyDashboard,
      mapReport,
      mapTree,
      mapManagement,
      mapCycleSelection,
      mapFilters,
      mapApprovals,
      mapCycle,
      mapTeam,
      mapDepartment,
      mapRole,
      mapRoleEvent,
      mapSettings,
      toPage,
    }),
  });
})(globalThis);
