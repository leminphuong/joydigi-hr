(function registerViews(globalScope) {
  'use strict';

  const namespace = globalScope.JoyOKR = globalScope.JoyOKR || {};
  const api = () => namespace.api;
  const config = () => globalScope.JoyOKRConfig || {};

  const labels = {
    on_track: 'Đúng tiến độ', at_risk: 'Có rủi ro', off_track: 'Chậm tiến độ', completed: 'Hoàn thành',
    draft: 'Nháp', submitted: 'Chờ duyệt', approved: 'Đã duyệt', rejected: 'Từ chối', deleted: 'Đã xóa',
    active: 'Đang hoạt động', closed: 'Đã đóng', inactive: 'Đã ẩn', company: 'Công ty', department: 'Phòng ban', team: 'Team', personal: 'Cá nhân',
    high: 'Cao', medium: 'Trung bình', low: 'Thấp', weekly: 'Hàng tuần', biweekly: 'Hai tuần/lần', monthly: 'Hàng tháng',
    month: 'Tháng', quarter: 'Quý', year: 'Năm', custom: 'Tùy chỉnh',
  };

  // OKR roles are granted in OKR itself, apart from HR permissions.
  const ROLE_LABELS = Object.freeze({ admin: 'Quản trị OKR', manager: 'Quản lý OKR', department_manager: 'Quản lý OKR phòng ban' });
  // Existing app.css badge tones: dark for admins, blue for managers, green for department managers.
  const ROLE_TONES = Object.freeze({ admin: 'company', manager: 'department', department_manager: 'team' });
  const ROLE_HISTORY_LIMIT = 100;
  const CHANNEL_LABELS = Object.freeze({ in_app: 'Thông báo trong ứng dụng', email: 'Email' });
  const CYCLE_STATUS_FILTERS = Object.freeze([['all', 'Tất cả'], ['draft', 'Nháp'], ['active', 'Đang hoạt động'], ['closed', 'Đã đóng']]);
  const ORGANIZATION_STATUS_FILTERS = Object.freeze([['active', 'Đang hoạt động'], ['inactive', 'Đã ẩn'], ['all', 'Tất cả']]);
  const SCOPE_VALUES = Object.freeze(['company', 'department', 'team', 'personal']);
  // JOYOKR's "KR cần chú ý theo người" card lists the first 8 attention items.
  const ATTENTION_CARD_LIMIT = 8;
  const HEALTH_VALUES = Object.freeze(['on_track', 'at_risk', 'off_track', 'completed']);
  // Check-in management status (Django reports.checkin_status): label and app.css badge tone.
  const CHECKIN_STATUS = Object.freeze({
    overdue: Object.freeze({ label: 'Quá hạn check-in', tone: 'off_track' }),
    due: Object.freeze({ label: 'Đến hạn', tone: 'at_risk' }),
    never: Object.freeze({ label: 'Chưa check-in', tone: 'pending' }),
    ok: Object.freeze({ label: 'Đúng hạn', tone: 'on_track' }),
  });

  const renderers = {
    'dashboard': renderDashboard,
    'my-okrs': renderMyOkrs,
    'company-okrs': renderCompanyOkrs,
    'tree': renderTree,
    'teams': renderTeams,
    'check-ins': renderCheckins,
    'reports': renderReports,
    'notifications': renderNotifications,
    'objective-detail': renderObjectiveDetail,
    'key-result-detail': renderKeyResultDetail,
    'admin/approvals': renderApprovals,
    // Users and profile belong to the HR web (app.js sends those routes there);
    // OKR grants its own roles instead.
    'admin/roles': renderAdminRoles,
    'admin/departments': renderAdminDepartments,
    'admin/teams': renderAdminTeams,
    'admin/cycles': renderAdminCycles,
    'admin/settings': renderAdminSettings,
  };

  /** Renders a named application view into the supplied outlet. */
  async function render(name, context) {
    const renderer = renderers[name];
    if (!renderer) throw new Error('Không tìm thấy trang.');
    const html = await renderer(context.params || {});
    if (context.isCurrent && !context.isCurrent()) return false;
    // Every renderer builds its markup with escapeHtml/escapeAttr (or numeric
    // coercion) around all API and URL data, so this sink only sees escaped text.
    context.outlet.innerHTML = html;
    if (name !== 'notifications') {
      try {
        const notificationSummary = await api().request('notifications', { query: { per_page: 1 } });
        if (!context.isCurrent || context.isCurrent()) updateNotificationCount(notificationSummary.unread || 0);
      } catch (error) {
        // Badge thông báo không được phép làm gián đoạn trang hiện tại.
      }
    }
    if (can('joyokr_approve') && !['dashboard', 'admin/approvals'].includes(name)) {
      try {
        const queue = await api().request('approvals');
        if (!context.isCurrent || context.isCurrent()) updateApprovalCount(queue.queue_total ?? queue.total ?? 0);
      } catch (error) {
        // Badge hàng chờ duyệt chỉ là thông tin bổ trợ, không chặn trang hiện tại.
      }
    }
    return !context.isCurrent || context.isCurrent();
  }

  async function renderDashboard() {
    const query = currentQuery();
    const [dashboard, lists] = await Promise.all([
      api().request('dashboard', { query }),
      filterLists(),
    ]);
    const filtered = dashboard.has_filters === true;
    let report = { trend: [] };
    let approvals = { total: 0, objective_count: 0, key_result_count: 0 };
    try {
      // The trend uses the filters the dashboard applied, so the chart and the
      // KPI cards describe the same objectives. Unfiltered it stays JOYOKR's
      // company-level trend, next to the official company progress.
      const reportQuery = filtered
        ? { cycle_id: query.cycle_id, ...dashboard.filters }
        : { cycle_id: query.cycle_id, scope: 'company' };
      report = await api().request('reports', { query: reportQuery });
    } catch (error) {
      // Dashboard vẫn hoạt động nếu phần report chưa có dữ liệu/quyền truy cập.
    }
    if (can('joyokr_approve')) {
      try {
        approvals = await api().request('approvals');
        updateApprovalCount(approvals.total || 0);
      } catch (error) {
        // Không chặn Dashboard nếu hàng chờ duyệt tạm thời không tải được.
      }
    }

    const stats = dashboard.objective_stats || {};
    const dashboardHealth = health(dashboard.progress, dashboard.expected_progress);
    const filterText = filterSummaryText(describeFilters(dashboard.filters, lists));
    // With a filter the cards show the selection's own progress; the hero keeps the official figure.
    const currentProgress = filtered ? dashboard.selection_progress : dashboard.progress;
    const currentHealth = health(currentProgress, dashboard.expected_progress);
    const pendingCount = Number(dashboard.pending_approval_count || 0);
    const staleCount = Number(dashboard.stale_checkins || 0);
    const progressCards = [
      dashboardStat('Key Result', number(dashboard.key_result_count || 0), '', 'target', '', `${number(dashboard.key_results_completed || 0)} hoàn thành`),
      dashboardStat('Đang thực hiện', number(dashboard.in_progress_count || 0), '', 'activity', '', 'Objective đã duyệt, chưa hoàn thành'),
      dashboardStat('Chờ duyệt', number(pendingCount), pendingCount ? 'at_risk' : '', 'approval', '', 'Objective đã gửi duyệt'),
      dashboardStat('Quá hạn check-in', number(staleCount), staleCount ? 'off_track' : '', 'clock', 'stale', 'Key Result chưa cập nhật đúng hạn'),
      dashboardStat(
        filtered ? 'Tiến độ theo bộ lọc / kỳ vọng' : 'Tiến độ hiện tại / kỳ vọng',
        `${number(currentProgress)}% / ${number(dashboard.expected_progress)}%`,
        currentHealth,
        'trending-up',
        '',
        filtered ? 'Tiến độ của các Objective đã lọc, không phải tiến độ chính thức toàn công ty' : 'Tiến độ chính thức toàn công ty',
      ),
    ].join('');
    const heroNote = filtered
      ? `Tiến độ chính thức toàn công ty, không áp dụng bộ lọc (${filterText}). Tiến độ theo bộ lọc nằm ở thẻ “Tiến độ theo bộ lọc / kỳ vọng”.`
      : 'So sánh tiến độ thực tế với tiến độ kỳ vọng của chu kỳ hiện tại.';
    const statCards = [
      dashboardStat('Tổng Objective', stats.total || 0, '', 'target', 'all'),
      dashboardStat('Đúng tiến độ', stats.on_track || 0, 'on_track', 'circle-check', 'on_track'),
      dashboardStat('Có rủi ro', stats.at_risk || 0, 'at_risk', 'triangle-alert', 'at_risk'),
      dashboardStat('Chậm tiến độ', stats.off_track || 0, 'off_track', 'trending-down', 'off_track'),
      dashboardStat('Hoàn thành', stats.completed || 0, 'completed', 'badge-check', 'completed'),
    ].join('');
    const approvalBanner = can('joyokr_approve') && Number(approvals.total || 0) > 0
      ? `<section class="joyokr-approval-banner joyokr-section">
          <div class="joyokr-approval-banner__icon" aria-hidden="true">${uiIcon('approval')}</div>
          <div class="joyokr-approval-banner__copy"><span class="joyokr-eyebrow">CẦN XỬ LÝ</span><h2>${number(approvals.total)} OKR đang chờ duyệt</h2><p>${number(approvals.objective_count || 0)} Objective · ${number(approvals.key_result_count || 0)} Key Result sẽ được duyệt cùng Objective.</p></div>
          <button class="joyokr-button joyokr-button--primary" type="button" data-route="admin/approvals">Mở hàng chờ duyệt</button>
        </section>`
      : '';

    const dashboardFilters = objectiveFilterToolbar('dashboard', query, lists);

    return page('Dashboard', cycleSubtitle(dashboard.cycle), cycleSelect(dashboard.cycle), `
      ${cycleNotice(dashboard.cycle_selection, dashboard.cycle)}
      ${dashboardFilters}
      <section class="joyokr-dashboard-hero joyokr-card">
        <div class="joyokr-dashboard-hero__main">
          <div class="joyokr-dashboard-hero__copy">
            <span class="joyokr-eyebrow">TỔNG QUAN CHU KỲ</span>
            <h2>Tiến độ OKR toàn công ty</h2>
            <p>${escapeHtml(heroNote)}</p>
          </div>
          <div class="joyokr-dashboard-score" data-tone="${escapeAttr(dashboardHealth)}">
            <span>Tiến độ</span>
            <strong>${number(dashboard.progress)}%</strong>
            ${badge(dashboardHealth)}
            <small class="joyokr-dashboard-score__formula">${escapeHtml(progressDefinitionText(dashboard.progress_definition))}</small>
          </div>
        </div>
        <div class="joyokr-dashboard-progress">
          ${progressBar(dashboard.progress, dashboard.expected_progress, dashboardHealth)}
          <div class="joyokr-dashboard-progress__meta">
            <span><b>${number(dashboard.progress)}%</b> thực tế</span>
            <span><b>${number(dashboard.expected_progress)}%</b> kỳ vọng</span>
          </div>
        </div>
        <div class="joyokr-dashboard-hero__metrics">
          <div><span>Key Result</span><strong>${number(dashboard.key_result_count || 0)}</strong></div>
          <button type="button" class="joyokr-dashboard-metric-button" data-action="dashboard-detail" data-dashboard-detail="stale" aria-controls="joyokr-dashboard-detail-stale"><span>KR cần check-in</span><strong class="${Number(dashboard.stale_checkins || 0) ? 'joyokr-text--at_risk' : ''}">${number(dashboard.stale_checkins || 0)}</strong><small>Xem KR & người phụ trách</small></button>
          <div><span>Team hoạt động</span><strong>${number(dashboard.active_team_count || 0)}</strong><small>Có Objective đã duyệt trong chu kỳ</small></div>
        </div>
      </section>

      ${approvalBanner}

      <section class="joyokr-section joyokr-dashboard-kpis">
        <div class="joyokr-section-heading"><div><span class="joyokr-eyebrow">OBJECTIVE</span><h2 class="joyokr-section-title">Sức khỏe mục tiêu</h2></div><span class="joyokr-section-note">Bấm chỉ số để xem Objective và người phụ trách</span></div>
        <div class="joyokr-grid joyokr-grid--5">${statCards}</div>
      </section>

      <section class="joyokr-section joyokr-dashboard-kpis">
        <div class="joyokr-section-heading"><div><span class="joyokr-eyebrow">KEY RESULT & CHECK-IN</span><h2 class="joyokr-section-title">Tiến độ thực hiện</h2></div><span class="joyokr-section-note">${escapeHtml(filtered ? `Theo bộ lọc: ${filterText}` : 'Mọi OKR đã duyệt bạn được xem trong chu kỳ')}</span></div>
        <div class="joyokr-grid joyokr-grid--5">${progressCards}</div>
      </section>

      ${dashboardDetailPanels(dashboard)}

      <section class="joyokr-dashboard-charts joyokr-section">
        ${dashboardLineChart(report.trend || [], currentProgress, dashboard.expected_progress, filtered ? 'Theo bộ lọc đang áp dụng' : '')}
        ${dashboardStatusChart(stats)}
      </section>

      <section class="joyokr-dashboard-team-section joyokr-section">
        ${dashboardTeamChart(dashboard.teams || [], dashboard.expected_progress)}
      </section>

      <section class="joyokr-grid joyokr-grid--2 joyokr-section joyokr-dashboard-bottom">
        ${card('Objective nổi bật', objectiveRows(dashboard.objectives || []), '<button class="joyokr-button joyokr-button--small" data-route="company-okrs">Xem tất cả</button>')}
        ${card('KR cần chú ý theo người', `${attentionKeyResultRows(dashboard.attention_key_results || [])}${attentionCapNote(Math.min(ATTENTION_CARD_LIMIT, (dashboard.attention_key_results || []).length), dashboard.attention_total, dashboardReportQuery(dashboard))}`, '<button class="joyokr-button joyokr-button--small" data-route="check-ins">Mở Check-in</button>')}
      </section>`);
  }

  async function renderMyOkrs() {
    const query = { ...currentQuery(), owner_user_id: config().currentUser.id };
    const result = await api().request('objectives', { query });
    return page('OKR của tôi', 'Objective và Key Result bạn đang sở hữu', actionButton('+ Tạo Objective', 'open-objective'), `
      ${okrToolbar('my-okrs')}
      ${objectiveTableCard(result, 'Bạn chưa có Objective trong chu kỳ này.')}`);
  }

  async function renderCompanyOkrs() {
    const query = currentQuery();
    // The objective list must not depend on the filter lists: a failed list
    // only empties its select and is reported (filterLists).
    const [result, filters] = await Promise.all([
      api().request('objectives', { query }),
      filterLists(),
    ]);
    return page('OKR công ty', 'Theo dõi mục tiêu ở mọi cấp trong tổ chức', (can('joyokr_edit_managed') || can('joyokr_manage_settings')) ? actionButton('+ Tạo Objective', 'open-objective') : '', `
      ${okrToolbar('company-okrs', filters)}${filterFailureNotice(filters)}
      ${objectiveTableCard(result, emptyObjectivesMessage(query))}`);
  }

  async function renderTree() {
    const result = await api().request('tree', { query: currentQuery() });
    const linked = result.items?.length
      ? result.items.map((item) => treeNode(item)).join('')
      : empty('Chưa có liên kết OKR', 'Tạo Objective cấp Công ty và liên kết mục tiêu cấp dưới để hình thành cây.');
    const unlinkedItems = result.unlinked_items || [];
    const unlinked = unlinkedItems.length
      ? `<section class="joyokr-card joyokr-tree joyokr-tree-unlinked"><div class="joyokr-tree__heading"><div><span class="joyokr-eyebrow">CẦN HOÀN THIỆN LIÊN KẾT</span><h2>Chưa liên kết</h2><p>Objective cấp Phòng ban, Team hoặc Cá nhân chưa có Objective cha khả dụng.</p></div><span class="joyokr-badge joyokr-badge--pending">${number(unlinkedItems.length)} gốc</span></div>${unlinkedItems.map((item) => treeNode(item)).join('')}</section>`
      : '';
    return page('Cây OKR', cycleSubtitle(result.cycle), cycleSelect(result.cycle), `
      ${cycleNotice(result.cycle_selection, result.cycle)}
      <section class="joyokr-card joyokr-tree"><div class="joyokr-tree__heading"><div><span class="joyokr-eyebrow">LIÊN KẾT MỤC TIÊU</span><h2>OKR đã liên kết</h2><p>Công ty → Phòng ban → Team → Cá nhân. Mỗi Objective dùng cùng trạng thái và mốc kỳ vọng với các màn hình khác.</p></div></div>${linked}</section>
      ${unlinked}`);
  }

  async function renderTeams() {
    const [teams, dashboard] = await Promise.all([
      api().request('teams', { query: { status: 'active' }, allPages: true }),
      // The progress columns come from the cycle dashboard, which answers 404
      // when no cycle is active (e.g. the only cycle is still a draft). The
      // team list itself does not depend on a cycle, so it must still show;
      // only an expired session keeps failing the page (login redirect).
      api().request('dashboard', { query: currentQuery(), summaryOnly: true }).catch((error) => {
        if (error?.status === 401) throw error;
        return { teams: [], cycle: null, expected_progress: 0, progressUnavailable: error?.message || 'Chưa tải được tiến độ.' };
      }),
    ]);
    const withProgress = !dashboard.progressUnavailable;
    const progressByTeam = new Map((dashboard.teams || []).map((team) => [Number(team.id), team]));
    const rows = (teams.items || []).map((team) => {
      const summary = progressByTeam.get(Number(team.id)) || {};
      const progressCells = withProgress
        ? `<td>${number(summary.objective_count || 0)}</td><td>${progressBar(summary.progress || 0, dashboard.expected_progress, health(summary.progress || 0, dashboard.expected_progress), true)}</td><td><strong>${number(summary.progress || 0)}%</strong></td>`
        : '<td>—</td><td>—</td><td></td>';
      return `<tr data-route="company-okrs" data-route-query="team_id=${escapeAttr(team.id)}" tabindex="0" role="link"><td><div class="joyokr-cell-title">${escapeHtml(team.name)}</div><div class="joyokr-cell-sub">${escapeHtml(team.department_name || '')}</div></td><td>${escapeHtml(team.manager_name || 'Chưa gán')}</td><td>${number(team.member_count)}</td>${progressCells}</tr>`;
    }).join('');
    const notice = withProgress
      ? ''
      : `<div class="joyokr-alert" role="note" data-teams-progress-notice><p>${escapeHtml(dashboard.progressUnavailable)} Danh sách team vẫn hiển thị; cột Objective và Tiến độ sẽ có khi có chu kỳ đang hoạt động.</p></div>`;
    return page('Teams', 'Tiến độ và nguồn lực theo team', cycleSelect(dashboard.cycle), `${notice}${tableCard(['Team', 'Manager', 'Thành viên', 'Objective', 'Tiến độ', ''], rows, 'Chưa có team hoạt động.')}`);
  }

  async function renderCheckins() {
    const query = currentQuery();
    const [items, managementTree] = await Promise.all([
      api().request('checkin-queue', { query }),
      api().request('checkin-management-tree', { query }),
    ]);
    const rows = items.map((item) => `<tr><td><div class="joyokr-cell-title">${escapeHtml(item.title)}</div><div class="joyokr-cell-sub">${escapeHtml(item.objective_title)}</div></td><td>${number(item.current_value)} / ${number(item.target_value)} ${escapeHtml(item.unit)}</td><td>${progressBar(item.progress, item.expected_progress, item.health, true)}</td><td>${badge(item.health)}</td><td>${item.latest_checkin ? dateTime(item.latest_checkin.created_at) : 'Chưa check-in'}</td><td>${actionButton('CHECK-IN', 'open-checkin', { id: item.id }, 'primary small')}</td></tr>`).join('');
    const personal = tableCard(['Key Result', 'Giá trị', 'Tiến độ', 'Trạng thái', 'Lần cuối', ''], rows, 'Bạn không có Key Result cần check-in.');
    // Plain employees (can_manage false) see nothing of the management tree,
    // and the filter lists are only fetched for those who can manage.
    const management = managementTree?.can_manage
      ? renderCheckinManagementTree(managementTree, query, await filterLists())
      : '';
    return page('Check-in', 'Check-in KR đã duyệt trong chu kỳ đang hoạt động, trước 10h thứ Hai hằng tuần', cycleSelect(managementTree?.cycle), `${cycleNotice(managementTree?.cycle_selection, managementTree?.cycle)}${personal}${management}`);
  }

  function renderCheckinManagementTree(tree, query = {}, lists = {}) {
    const summary = tree.summary || {};
    const departments = tree.departments || [];
    const unassigned = tree.unassigned || [];
    lazyCheckinChildren.clear();
    lazyCheckinTree = Number(summary.employee_count || 0) > LAZY_CHECKIN_PEOPLE;
    const departmentMarkup = departments.map(checkinDepartmentNode).join('');
    const unassignedMarkup = unassigned.length
      ? checkinGenericNode('unassigned', 'Chưa phân nhóm', `${unassigned.length} nhân viên`, unassigned.map((person) => checkinPersonNode(person, 'unassigned')).join(''), 'group')
      : '';
    const emptyMarkup = tree.has_filters
      ? empty('Không có nhân viên phù hợp bộ lọc', 'Hãy đổi phòng ban, team, nhân viên hoặc trạng thái check-in.')
      : empty('Chưa có nhân viên trong phạm vi quản lý', 'Khi có nhân viên thuộc phạm vi quản lý, dữ liệu check-in sẽ xuất hiện tại đây.');
    const treeMarkup = departmentMarkup || unassignedMarkup ? `${departmentMarkup}${unassignedMarkup}` : emptyMarkup;
    const statusChoices = Object.entries(CHECKIN_STATUS)
      .map(([value, status]) => `<option value="${escapeAttr(value)}" ${value === query.status ? 'selected' : ''}>${escapeHtml(status.label)}</option>`)
      .join('');
    const toolbar = `<form class="joyokr-toolbar joyokr-checkin-tree__filters" data-filter-form data-filter-route="check-ins" aria-label="Lọc check-in nhân viên">${entityOptions('department_id', 'Tất cả phòng ban', lists.departments || [], query.department_id, 'name')}${entityOptions('team_id', 'Tất cả team', lists.teams || [], query.team_id, 'name')}${entityOptions('owner_user_id', 'Tất cả nhân viên', lists.people || [], query.owner_user_id, 'display_name')}<select class="joyokr-select" name="status" aria-label="Trạng thái check-in"><option value="">Tất cả trạng thái check-in</option>${statusChoices}</select><input type="hidden" name="cycle_id" value="${escapeAttr(query.cycle_id || '')}"><button class="joyokr-button" type="submit">Lọc</button></form>`;
    const overdue = Number(summary.overdue_count || 0);
    const due = Number(summary.due_count || 0);

    return `<section class="joyokr-card joyokr-checkin-tree joyokr-section">
      <div class="joyokr-tree__heading"><div><span class="joyokr-eyebrow">QUẢN LÝ CHECK-IN</span><h2>Theo dõi Check-in nhân viên</h2><p>Công ty → Phòng ban → Team → Nhân viên → Objective → Key Result → Check-in. Dữ liệu chỉ đọc theo phạm vi quản lý của bạn.</p></div></div>
      ${toolbar}
      <div class="joyokr-checkin-tree__summary" aria-label="Tổng quan check-in nhân viên">
        <span><strong>${number(summary.employee_count || 0)}</strong> nhân viên</span>
        <span><strong>${number(summary.key_result_count || 0)}</strong> Key Result</span>
        <span><strong>${number(summary.never_checked_in || 0)}</strong> chưa check-in</span>
        <span><strong class="${overdue ? 'joyokr-text--off_track' : ''}">${number(overdue)}</strong> quá hạn</span>
        <span><strong class="${due ? 'joyokr-text--at_risk' : ''}">${number(due)}</strong> đến hạn</span>
        <span><strong>${number(summary.checkin_count || 0)}</strong> lượt check-in</span>
      </div>
      <div class="joyokr-checkin-tree__body">${treeMarkup}</div>
    </section>`;
  }

  /*
   * W4: a whole company's tree (400 people) was 230,000 DOM nodes and took
   * ~20 s to draw, because JOYOKR renders every department, team, person,
   * objective and KR up front. Above LAZY_CHECKIN_PEOPLE people the
   * department and team levels start collapsed and their children are only
   * built when opened (app.js asks renderLazyCheckinChildren). Smaller
   * scopes and filtered views keep JOYOKR's fully open tree.
   */
  const LAZY_CHECKIN_PEOPLE = 40;
  const lazyCheckinChildren = new Map();
  let lazyCheckinTree = false;

  function lazyChildren(id, build) {
    if (!lazyCheckinTree) return { children: build(), collapsed: false };
    lazyCheckinChildren.set(id, build);
    return { children: '', collapsed: true };
  }

  function renderLazyCheckinChildren(id) {
    const build = lazyCheckinChildren.get(String(id));
    if (!build) return null;
    lazyCheckinChildren.delete(String(id));
    return build();
  }

  function checkinDepartmentNode(department) {
    const id = `department-${department.id}`;
    const { children, collapsed } = lazyChildren(id, () => (department.teams || []).map((team) => checkinTeamNode(team, id)).join(''));
    const people = (department.teams || []).reduce((total, team) => total + (team.people || []).length, 0);
    return checkinGenericNode(id, department.name, `${(department.teams || []).length} Team · ${people} nhân viên`, children, 'department', false, collapsed);
  }

  function checkinTeamNode(team, parentId) {
    const id = `${parentId}-team-${team.id}`;
    const { children, collapsed } = lazyChildren(id, () => (team.people || []).map((person) => checkinPersonNode(person, id)).join(''));
    return checkinGenericNode(id, team.name, `${(team.people || []).length} nhân viên`, children, 'team', false, collapsed);
  }

  function checkinPersonNode(person, parentId) {
    const id = `${parentId}-person-${person.id}`;
    const children = (person.objectives || []).map((objective) => checkinObjectiveNode(objective, id)).join('');
    return checkinGenericNode(id, person.display_name, `${(person.objectives || []).length} Objective`, children || '<div class="joyokr-checkin-tree__empty">Chưa có Objective/KR đã duyệt trong chu kỳ này.</div>', 'person');
  }

  function checkinObjectiveNode(objective, parentId) {
    const id = `${parentId}-objective-${objective.id}`;
    const children = (objective.key_results || []).map((keyResult) => checkinKeyResultNode(keyResult, id)).join('');
    const title = `<button class="joyokr-link-button joyokr-checkin-tree__route" type="button" data-route="objectives/${routeId(objective.id)}">${escapeHtml(objective.title)}</button>`;
    return checkinGenericNode(id, title, `${(objective.key_results || []).length} KR`, children, 'objective', true);
  }

  function checkinKeyResultNode(keyResult, parentId) {
    const id = `${parentId}-kr-${keyResult.id}`;
    const history = keyResult.checkins || [];
    const total = Math.max(Number(keyResult.checkin_total) || 0, history.length);
    // Django embeds only the newest check-ins; the KR page has the full history.
    const cappedNote = total > history.length
      ? `<div class="joyokr-checkin-tree__more"><span class="joyokr-muted">${number(history.length)} lượt gần nhất</span><button class="joyokr-link-button" type="button" data-route="key-results/${routeId(keyResult.id)}">Xem toàn bộ ${number(total)} lượt</button></div>`
      : '';
    const historyMarkup = history.length
      ? `<ol class="joyokr-checkin-tree__timeline">${history.map((entry) => checkinHistoryItem(entry, keyResult.unit)).join('')}</ol>${cappedNote}`
      : '<div class="joyokr-checkin-tree__empty joyokr-checkin-tree__empty--warning">Chưa check-in</div>';
    const routeTitle = `<button class="joyokr-link-button joyokr-checkin-tree__route" type="button" data-route="key-results/${routeId(keyResult.id)}">${escapeHtml(keyResult.title)}</button>`;
    const meta = `<span>${number(keyResult.current_value)} / ${number(keyResult.target_value)} ${escapeHtml(keyResult.unit || '')}</span><span>${number(keyResult.progress)}%</span><span>Hạn ${date(keyResult.due_date)}</span><span>${number(total)} lượt check-in</span>${keyResult.latest_checkin_at ? `<span>Cuối: ${dateTime(keyResult.latest_checkin_at)}</span>` : '<span>Chưa check-in</span>'}`;
    return `<div class="joyokr-checkin-tree-node joyokr-checkin-tree-node--kr">
      <div class="joyokr-checkin-tree-node__row">
        <button class="joyokr-checkin-tree-node__toggle" type="button" data-action="toggle-checkin-tree-node" data-checkin-tree-target="${escapeAttr(id)}" aria-expanded="false" aria-controls="${escapeAttr(`joyokr-checkin-tree-details-${id}`)}" title="Mở lịch sử check-in"><span data-checkin-tree-toggle-icon aria-hidden="true">▸</span><span class="screen-reader-text">Mở/thu gọn lịch sử check-in</span></button>
        <span class="joyokr-checkin-tree-node__kind">KR</span>
        <div class="joyokr-checkin-tree-node__content"><strong>${routeTitle}</strong><div class="joyokr-checkin-tree-node__meta">${meta}</div></div>
        <div class="joyokr-checkin-tree-node__health">${checkinStatusBadge(keyResult.checkin_status)}${badge(keyResult.health)}</div>
      </div>
      <div class="joyokr-checkin-tree-node__details" id="${escapeAttr(`joyokr-checkin-tree-details-${id}`)}" data-checkin-tree-node-details="${escapeAttr(id)}" hidden>${historyMarkup}</div>
    </div>`;
  }

  function checkinHistoryItem(entry, unit) {
    const progress = `${number(entry.previous_progress)}% → ${number(entry.current_progress)}%`;
    const values = `${number(entry.previous_value)} → ${number(entry.current_value)} ${escapeHtml(unit || '')}`;
    return `<li class="joyokr-checkin-tree__checkin">
      <div class="joyokr-checkin-tree__checkin-head"><time>${dateTime(entry.created_at)}</time><strong>${progress}</strong></div>
      <div class="joyokr-checkin-tree__checkin-values">${values} · Confidence ${number(entry.confidence || 0)}/5</div>
      <p>${escapeHtml(entry.update_text || 'Không có nội dung cập nhật.')}</p>
      <div class="joyokr-checkin-tree__checkin-meta"><span><b>Vấn đề:</b> ${escapeHtml(entry.blockers || 'Không')}</span><span><b>Tiếp theo:</b> ${escapeHtml(entry.next_steps || '—')}</span></div>
    </li>`;
  }

  function checkinGenericNode(id, title, meta, children, kind, titleIsHtml = false, collapsed = false) {
    const detailsId = `joyokr-checkin-tree-details-${id}`;
    const lazy = collapsed && !children ? ` data-checkin-lazy="${escapeAttr(id)}"` : '';
    return `<div class="joyokr-checkin-tree-node joyokr-checkin-tree-node--${escapeAttr(kind)}">
      <div class="joyokr-checkin-tree-node__row">
        <button class="joyokr-checkin-tree-node__toggle" type="button" data-action="toggle-checkin-tree-node" data-checkin-tree-target="${escapeAttr(id)}" aria-expanded="${collapsed ? 'false' : 'true'}" aria-controls="${escapeAttr(detailsId)}" title="Thu gọn/mở rộng"><span data-checkin-tree-toggle-icon aria-hidden="true">${collapsed ? '▸' : '▾'}</span><span class="screen-reader-text">Thu gọn/mở rộng</span></button>
        <span class="joyokr-checkin-tree-node__kind">${escapeHtml(kind === 'department' ? 'PB' : kind === 'team' ? 'TEAM' : kind === 'person' ? 'NV' : kind === 'objective' ? 'O' : 'NHÓM')}</span>
        <div class="joyokr-checkin-tree-node__content"><strong>${titleIsHtml ? title : escapeHtml(title)}</strong><div class="joyokr-checkin-tree-node__meta">${escapeHtml(meta || '')}</div></div>
      </div>
      <div class="joyokr-checkin-tree-node__details" id="${escapeAttr(detailsId)}" data-checkin-tree-node-details="${escapeAttr(id)}"${lazy}${collapsed ? ' hidden' : ''}>${children}</div>
    </div>`;
  }

  async function renderReports() {
    const query = currentQuery();
    const [report, lists] = await Promise.all([
      api().request('reports', { query }),
      filterLists(),
    ]);
    const healthSelect = `<select class="joyokr-select" name="health" aria-label="Trạng thái Objective"><option value="">Tất cả trạng thái</option>${options(HEALTH_VALUES, query.health)}</select>`;
    const reportFilters = objectiveFilterToolbar('reports', query, lists, healthSelect);
    const filterPairs = describeFilters(report.filters, lists);
    const filtered = report.has_filters === true;
    const summary = report.summary || {};
    const trend = (report.trend || []).map((point) => `<div class="joyokr-trend__point"><span style="height:${Math.max(3, clamp(point.progress))}%"></span><small>${date(point.date)}</small><b>${number(point.progress)}%</b></div>`).join('');
    const scopeRows = (report.by_scope || []).map((row) => `<tr><td>${badge(row.label)}</td><td>${number(row.count)}</td><td>${progressBar(row.progress, summary.expected_progress, health(row.progress, summary.expected_progress), true)}</td><td><strong>${number(row.progress)}%</strong></td></tr>`).join('');
    const ownerRows = (report.by_owner || []).map((row) => `<tr><td>${escapeHtml(row.label)}</td><td>${number(row.count)}</td><td>${number(row.progress)}%</td></tr>`).join('');
    const attentionRows = (report.checkin_attention_by_owner || []).flatMap((group) => (group.items || []).map((item) => `<tr><td>${escapeHtml(group.owner_name || item.owner_name || '—')}</td><td><button class="joyokr-link-button" type="button" data-route="key-results/${routeId(item.id)}">${escapeHtml(item.title)}</button><div class="joyokr-cell-sub">${escapeHtml(item.objective_title || '')}</div></td><td>${badge(item.health)} ${item.checkin_overdue ? '<span class="joyokr-badge joyokr-badge--at_risk">Quá hạn</span>' : ''}</td><td>${number(item.progress)}%</td><td>${item.latest_checkin_at ? dateTime(item.latest_checkin_at) : 'Chưa check-in'}</td><td>${item.checkin_overdue ? `${number(item.overdue_days)} ngày` : '—'}</td></tr>`)).join('');
    const objectiveExportRows = (report.rows || []).map((item) => `<tr><td>${escapeHtml(item.title)}</td><td>${escapeHtml(labels[item.scope] || item.scope)}</td><td>${escapeHtml(item.owner_name || '—')}</td><td>${number(item.progress)}%</td><td>${escapeHtml(labels[item.health] || item.health || '—')}</td></tr>`).join('');
    const groupRows = (rows) => rows.map((row) => `<tr><td>${escapeHtml(row.label || 'Chưa xác định')}</td><td>${number(row.count)}</td><td><strong>${number(row.progress)}%</strong>${progressBar(row.progress, summary.expected_progress, health(row.progress, summary.expected_progress), true)}</td></tr>`).join('');
    const departmentRows = groupRows(report.by_department || []);
    const teamRows = groupRows(report.by_team || []);
    const ratingRows = (report.by_rating || []).map((band) => `<tr><td>${escapeHtml(band.label)}</td><td>${escapeHtml(ratingRange(band))}</td><td>${number(band.count)}</td></tr>`).join('');
    const overdueRows = (report.overdue_key_results || []).map((item) => `<tr><td>${escapeHtml(item.owner_name || '—')}</td><td><button class="joyokr-link-button" type="button" data-route="key-results/${routeId(item.id)}">${escapeHtml(item.title)}</button></td><td>${item.objective_id ? `<button class="joyokr-link-button" type="button" data-route="objectives/${routeId(item.objective_id)}">${escapeHtml(item.objective_title || '—')}</button>` : escapeHtml(item.objective_title || '—')}</td><td>${date(item.due_date)}</td><td><span class="joyokr-text--off_track">${number(item.days_late)} ngày</span></td><td>${number(item.progress)}%</td></tr>`).join('');
    const history = report.checkin_history || [];
    const historyTotal = Math.max(Number(report.checkin_count || 0), history.length);
    const historyNote = historyTotal > history.length
      ? `${number(history.length)} / tổng ${number(historyTotal)} lượt · mới nhất trước`
      : `${number(historyTotal)} lượt · mới nhất trước`;
    const historyRows = history.map((entry) => {
      const unit = entry.unit ? ` ${entry.unit}` : '';
      return `<tr><td>${dateTime(entry.created_at)}</td><td>${escapeHtml(entry.owner_name || '—')}</td><td><button class="joyokr-link-button" type="button" data-route="key-results/${routeId(entry.key_result_id)}">${escapeHtml(entry.key_result_title || '—')}</button></td><td>${number(entry.previous_value)} → ${number(entry.current_value)}${escapeHtml(unit)}</td><td>${number(entry.previous_progress)}% → ${number(entry.current_progress)}%</td><td>${number(entry.confidence || 0)}/5</td><td>${escapeHtml(entry.update_text || '—')}</td><td>${escapeHtml(entry.user_name || '—')}</td></tr>`;
    }).join('');
    const filterText = filterSummaryText(filterPairs);
    const statGrid = filtered
      ? `<div class="joyokr-grid joyokr-grid--2">${stat('Tiến độ OKR toàn công ty', `${number(summary.progress)}%`, '', progressDefinitionText(summary.progress_definition))}${stat('Tiến độ theo bộ lọc', `${number(summary.selection_progress)}%`, '', `Bộ lọc: ${filterText}. Σ(tiến độ × trọng số) / Σ(trọng số) của các Objective đã lọc.`)}${stat('Key Result', summary.key_result_count || 0)}${stat('Cần check-in', summary.stale_checkins || 0, 'at_risk')}</div>`
      : `<div class="joyokr-grid joyokr-grid--3">${stat('Tiến độ OKR toàn công ty', `${number(summary.progress)}%`, '', progressDefinitionText(summary.progress_definition))}${stat('Key Result', summary.key_result_count || 0)}${stat('Cần check-in', summary.stale_checkins || 0, 'at_risk')}</div>`;
    const reportActions = `${cycleSelect(report.cycle)}${actionButton('Xuất CSV', 'export-report-csv', {}, 'small')}${actionButton('Xuất PDF', 'print-report', {}, 'small')}`;
    return page('Báo cáo', cycleSubtitle(report.cycle), reportActions, `${reportPrintHeader(report.cycle, filterPairs)}${cycleNotice(report.cycle_selection, report.cycle)}${reportFilters}
      ${statGrid}
      <section class="joyokr-card joyokr-card--pad joyokr-section"><h2 class="joyokr-section-title">Xu hướng check-in</h2><div class="joyokr-trend">${trend || '<p class="joyokr-muted">Chưa có lịch sử check-in.</p>'}</div></section>
      <section class="joyokr-grid joyokr-grid--2 joyokr-section">${card('Theo cấp OKR', simpleTable(['Cấp', 'Objective', 'Tiến độ', ''], scopeRows))}${card('Theo owner', simpleTable(['Owner', 'Objective', 'Tiến độ'], ownerRows))}</section>
      <section class="joyokr-grid joyokr-grid--2 joyokr-section">${card('Theo phòng ban', reportExportTable('Theo phòng ban', ['Phòng ban', 'Objective', 'Tiến độ'], departmentRows, 'Không có Objective phù hợp bộ lọc.'))}${card('Theo team', reportExportTable('Theo team', ['Team', 'Objective', 'Tiến độ'], teamRows, 'Không có Objective phù hợp bộ lọc.'))}</section>
      <section class="joyokr-card joyokr-section"><div class="joyokr-card__head"><h2>Theo mức độ hoàn thành</h2><span class="joyokr-muted">Điểm = tiến độ / 100, theo thang xếp loại của công ty</span></div>${reportExportTable('Theo mức độ hoàn thành', ['Mức độ', 'Khoảng hoàn thành', 'Objective'], ratingRows, 'Chưa cấu hình thang xếp loại.')}</section>
      <section class="joyokr-card joyokr-section"><div class="joyokr-card__head"><h2>KR quá hạn check-in theo người</h2><span class="joyokr-muted">Quản lý theo owner</span></div>${reportExportTable('KR cần chú ý', ['Owner', 'Key Result', 'Trạng thái', 'Tiến độ', 'Check-in cuối', 'Quá hạn'], attentionRows, 'Không có KR quá hạn hoặc chậm tiến độ.')}</section>
      <section class="joyokr-card joyokr-section"><div class="joyokr-card__head"><h2>KR quá hạn hoàn thành</h2><span class="joyokr-muted">Đã quá hạn và chưa đạt 100%</span></div>${reportExportTable('KR quá hạn hoàn thành', ['Owner', 'Key Result', 'Objective', 'Hạn', 'Trễ', 'Tiến độ'], overdueRows, 'Không có KR quá hạn hoàn thành.')}</section>
      <section class="joyokr-card joyokr-section"><div class="joyokr-card__head"><h2>Lịch sử check-in</h2><span class="joyokr-muted" data-checkin-history-count>${escapeHtml(historyNote)}</span></div>${reportExportTable('Lịch sử check-in', ['Thời gian', 'Owner', 'Key Result', 'Giá trị', 'Tiến độ', 'Confidence', 'Nội dung', 'Người check-in'], historyRows, 'Chưa có check-in phù hợp bộ lọc.')}</section>
      <section class="joyokr-card joyokr-section"><div class="joyokr-card__head"><h2>Chi tiết Objective</h2></div>${reportExportTable('Objective', ['Objective', 'Cấp', 'Owner', 'Tiến độ', 'Trạng thái'], objectiveExportRows, 'Không có Objective phù hợp bộ lọc.')}</section>`);
  }

  async function renderNotifications() {
    const result = await api().request('notifications', { query: currentQuery() });
    const items = (result.items || []).map((item) => {
      const legacyRoute = item.type === 'approval_requested'
        ? 'admin/approvals'
        : item.reference_type === 'objective' && item.reference_id
          ? `objectives/${item.reference_id}`
          : item.reference_type === 'key_result' && item.reference_id
            ? `key-results/${item.reference_id}`
            : '';
      const referenceRoute = Object.prototype.hasOwnProperty.call(item, 'reference_route') ? item.reference_route : legacyRoute;
      const isStale = ['deleted', 'missing', 'resolved', 'forbidden'].includes(item.reference_state);
      const openLabel = item.type === 'approval_requested' ? 'Duyệt ngay' : 'Xem';
      const openButton = referenceRoute ? `<button class="joyokr-button joyokr-button--small ${item.type === 'approval_requested' ? 'joyokr-button--primary' : ''}" type="button" data-route="${escapeAttr(referenceRoute)}">${openLabel}</button>` : '';
      const stateLabel = item.reference_state === 'deleted' ? 'OKR đã xóa' : item.reference_state === 'missing' ? 'Liên kết không còn tồn tại' : item.reference_state === 'resolved' ? 'Đã xử lý' : item.reference_state === 'forbidden' ? 'Không còn quyền truy cập' : '';
      const readButton = Number(item.is_read) ? '' : actionButton('Đã đọc', 'read-notification', { id: item.id }, 'small');
      return `<li class="joyokr-list-item ${isStale ? 'is-stale' : ''}"><span class="joyokr-notification-dot ${Number(item.is_read) ? 'is-read' : ''}"></span><div class="joyokr-list-item__body"><div class="joyokr-list-item__title">${escapeHtml(item.title)}</div><p class="joyokr-muted">${escapeHtml(item.message || '')}</p><div class="joyokr-list-item__meta">${dateTime(item.created_at)}${stateLabel ? ` · <span class="joyokr-reference-state">${escapeHtml(stateLabel)}</span>` : ''}</div></div><div class="joyokr-list-item__actions">${openButton}${readButton}</div></li>`;
    }).join('');
    updateNotificationCount(result.unread || 0);
    // JOYOKR's "Đồng bộ dữ liệu" button and MySQL storage card are WordPress
    // features; Django refreshes approval requests whenever this list loads.
    const actions = result.unread ? actionButton('Đánh dấu tất cả đã đọc', 'read-all-notifications', {}, 'small') : '';
    const staleCount = (result.items || []).filter((item) => item.reference_state).length;
    const dataSummary = `<section class="joyokr-grid joyokr-grid--3 joyokr-section">${stat('Thông báo', number(result.total || 0))}${stat('Chưa đọc', number(result.unread || 0), result.unread ? 'at_risk' : '')}</section>${staleCount ? `<p class="joyokr-data-note">Có ${number(staleCount)} thông báo không còn liên kết OKR hợp lệ; nút thao tác của chúng đã được ẩn.</p>` : ''}`;
    return page('Thông báo', `${number(result.unread || 0)} thông báo chưa đọc`, actions, `${dataSummary}<section class="joyokr-card">${items ? `<ul class="joyokr-list">${items}</ul>${pagination(result)}` : empty('Chưa có dữ liệu thông báo', 'Các cập nhật liên quan đến OKR sẽ xuất hiện tại đây.')}</section>`);
  }

  async function renderObjectiveDetail(params) {
    const [item, comments] = await Promise.all([
      api().request(`objectives/${params.id}`),
      api().request('comments', { query: { object_type: 'objective', object_id: params.id } }),
    ]);
    const workflow = workflowButtons('objective', item);
    const canEditObjective = item.can_edit === true;
    const krRows = (item.key_results || []).map((kr) => `<tr data-route="key-results/${routeId(kr.id)}" tabindex="0" role="link"><td><div class="joyokr-cell-title">${escapeHtml(kr.title)}</div><div class="joyokr-cell-sub">${escapeHtml(kr.owner_name || '')}</div></td><td>${number(kr.weight)}%</td><td>${number(kr.current_value)} / ${number(kr.target_value)} ${escapeHtml(kr.unit)}</td><td>${progressBar(kr.progress, item.expected_progress, health(kr.progress, item.expected_progress), true)}</td><td>${date(kr.due_date)}</td></tr>`).join('');
    const krWeightTotal = Number(item.kr_weight_total ?? (item.key_results || []).reduce((total, kr) => total + Number(kr.weight || 0), 0));
    const krWeightValid = item.kr_weight_valid ?? Math.abs(krWeightTotal - 100) <= 0.01;
    const krWeightNotice = krWeightValid
      ? `<p class="joyokr-cell-sub">Tổng trọng số KR: ${number(krWeightTotal)}% · hợp lệ để gửi duyệt.</p>`
      : `<div class="joyokr-weight-warning"><strong>Tổng trọng số KR: ${number(krWeightTotal)}% — phải bằng 100%.</strong><span>Objective chưa thể gửi duyệt.</span>${canEditObjective && (item.key_results || []).length >= 2 ? actionButton('Chia đều trọng số', 'normalize-kr-weights', { id: item.id }, 'small') : ''}</div>`;
    return page(item.title, raw(`${badge(item.scope)} ${escapeHtml(item.owner_name || '')}`), `${canEditObjective ? actionButton('Sửa', 'edit-objective', { id: item.id }, 'small') : ''} ${workflow} ${canEditObjective ? actionButton('Xóa', 'delete-objective', { id: item.id }, 'danger small') : ''}`, `
      <section class="joyokr-card joyokr-card--pad">${detailGrid([
        ['Owner', item.owner_name], ['Chu kỳ', item.cycle_name], ['Trọng số', `${number(item.weight)}%`],
        ['Tiến độ', progressText(item)], ['Kỳ vọng', item.progress_is_provisional ? '—' : `${number(item.expected_progress)}%`], ['Trạng thái', raw(objectiveHealthBadge(item))],
        ['Duyệt', raw(badge(item.approval_status))], ['Ưu tiên', labels[item.priority] || item.priority], ['Mô tả', item.description || '—'],
        ...(item.rejection_reason ? [['Lý do yêu cầu sửa', item.rejection_reason]] : []),
      ])}<p class="joyokr-cell-sub joyokr-section">Trọng số Objective là tỷ trọng khi tổng hợp các Objective cùng cấp trong cùng nhóm/Objective cha; với cấp Công ty là giữa các Objective Công ty trong cùng chu kỳ.</p></section>
      <section class="joyokr-card joyokr-section"><div class="joyokr-card__head"><div><h2>Key Results (${(item.key_results || []).length})</h2><p>Kết quả then chốt đo lường Objective. Tạo 2–4 KR, tổng trọng số 100%. Chỉ cần gửi duyệt Objective; khi Objective được duyệt, toàn bộ KR sẽ được duyệt cùng lúc.</p>${krWeightNotice}</div>${canEditObjective ? actionButton('+ Add Key Result', 'open-key-result', { objectiveId: item.id }, 'primary small') : ''}</div>${simpleTable(['Key Result', 'Trọng số', 'Giá trị', 'Tiến độ', 'Hạn'], krRows, 'Chưa có Key Result.')}</section>
      ${commentCard('objective', item.id, comments, true, item.can_comment !== false)}`);
  }

  async function renderKeyResultDetail(params) {
    const [item, checkins, comments, evidence, activity] = await Promise.all([
      api().request(`key-results/${params.id}`), api().request(`key-results/${params.id}/checkins`),
      api().request('comments', { query: { object_type: 'key_result', object_id: params.id } }),
      api().request('evidence', { query: { key_result_id: params.id } }),
      api().request('activity', { query: { object_type: 'key_result', object_id: params.id } }),
    ]);
    const canEditKeyResult = item.can_edit === true;
    // Django computes the check-in rule (owner, KR + Objective approved, cycle active).
    const canCheckin = item.can_checkin === true;
    const actions = `${canCheckin ? actionButton('CHECK-IN', 'open-checkin', { id: item.id }, 'primary') : ''} ${canEditKeyResult ? actionButton('Sửa', 'edit-key-result', { id: item.id }, 'small') : ''} ${canEditKeyResult ? actionButton('Xóa', 'delete-key-result', { id: item.id, objectiveId: item.objective_id }, 'danger small') : ''}`;
    const checkinHint = canCheckin ? '' : '<p class="joyokr-muted">Check-in chỉ dành cho Owner của KR sau khi Objective cha đã được duyệt và chu kỳ đang hoạt động. KR được duyệt tự động cùng Objective.</p>';
    return page(item.title, raw(`<button class="joyokr-link" data-route="objectives/${routeId(item.objective_id)}">${escapeHtml(item.objective_title)}</button>`), actions, `
      <section class="joyokr-card joyokr-card--pad"><div class="joyokr-grid joyokr-grid--3"><div><span class="joyokr-muted">Tiến độ</span><strong class="joyokr-score joyokr-score--block">${number(item.progress)}%</strong>${progressBar(item.progress, item.expected_progress, item.health)}</div><div>${detailGrid([['Giá trị', `${number(item.current_value)} / ${number(item.target_value)} ${item.unit}`], ['Trạng thái', raw(badge(item.health))]])}</div><div>${detailGrid([['Owner', item.owner_name], ['Confidence', checkins[0]?.confidence ? `${number(checkins[0].confidence)}/5` : '—']])}</div></div></section>
      ${checkinHint}<div class="joyokr-tabs" role="tablist" aria-label="Chi tiết Key Result"><button id="joyokr-tab-overview" role="tab" aria-selected="true" aria-controls="joyokr-panel-overview" class="is-active" data-action="switch-tab" data-tab-target="overview">Overview</button><button id="joyokr-tab-checkins" role="tab" aria-selected="false" aria-controls="joyokr-panel-checkins" tabindex="-1" data-action="switch-tab" data-tab-target="checkins">Check-ins</button><button id="joyokr-tab-comments" role="tab" aria-selected="false" aria-controls="joyokr-panel-comments" tabindex="-1" data-action="switch-tab" data-tab-target="comments">Comments</button><button id="joyokr-tab-evidence" role="tab" aria-selected="false" aria-controls="joyokr-panel-evidence" tabindex="-1" data-action="switch-tab" data-tab-target="evidence">Evidence</button><button id="joyokr-tab-activity" role="tab" aria-selected="false" aria-controls="joyokr-panel-activity" tabindex="-1" data-action="switch-tab" data-tab-target="activity">Activity</button></div>
      <section id="joyokr-panel-overview" role="tabpanel" aria-labelledby="joyokr-tab-overview" data-tab="overview">${card('Thông tin', detailGrid([['Mô tả', item.description || '—'], ['Đơn vị', item.unit], ['Trọng số', `${number(item.weight)}%`], ['Ưu tiên', labels[item.priority] || item.priority], ['Hạn', date(item.due_date)], ['Phê duyệt', item.objective_approval_status === 'approved' ? 'Đã duyệt cùng Objective' : 'Theo trạng thái Objective'] ]))}</section>
      <section id="joyokr-panel-checkins" role="tabpanel" aria-labelledby="joyokr-tab-checkins" data-tab="checkins" hidden>${card('Lịch sử check-in', timeline(checkins, (entry) => `<strong>${number(entry.previous_progress)}% → ${number(entry.current_progress)}%</strong> — ${escapeHtml(entry.update_text || '')}<div class="joyokr-cell-sub">Confidence ${number(entry.confidence)}/5 · Vấn đề: ${escapeHtml(entry.blockers || 'Không')} · Tiếp theo: ${escapeHtml(entry.next_steps || '—')}</div>`), canCheckin ? actionButton('+ Check-in', 'open-checkin', { id: item.id }, 'primary small') : '')}</section>
      <section id="joyokr-panel-comments" role="tabpanel" aria-labelledby="joyokr-tab-comments" data-tab="comments" hidden>${commentCard('key_result', item.id, comments, false)}</section>
      <section id="joyokr-panel-evidence" role="tabpanel" aria-labelledby="joyokr-tab-evidence" data-tab="evidence" hidden>${card('Bằng chứng', evidenceList(evidence), item.can_add_evidence ? actionButton('+ Thêm bằng chứng', 'open-evidence', { id: item.id }, 'primary small') : '')}</section>
      <section id="joyokr-panel-activity" role="tabpanel" aria-labelledby="joyokr-tab-activity" data-tab="activity" hidden>${card('Hoạt động', timeline(activity, (entry) => `<strong>${escapeHtml(entry.actor_name || 'Hệ thống')}</strong> ${escapeHtml(entry.summary || activityLabel(entry.action))}`))}</section>`);
  }

  async function renderApprovals() {
    const query = currentQuery();
    const result = await api().request('approvals', { query: { search: query.search || '' } });
    updateApprovalCount(result.queue_total ?? result.total ?? 0);

    const rows = (result.items || []).map((item) => {
      const route = `objectives/${routeId(item.id)}`;
      const cycleName = escapeHtml(item.cycle_name || '');
      // Django pre-checks the approve rules; a blocked Objective shows why
      // instead of an approve button the server would refuse.
      const approveControl = item.can_approve === false
        ? `<span class="joyokr-approval-block" role="note">${escapeHtml(item.block_reason || 'Chưa thể duyệt Objective này.')}</span>`
        : actionButton('Duyệt Objective', 'objective-approve', { id: item.id }, 'primary small');
      return `<tr>
        <td><div class="joyokr-cell-title">${escapeHtml(item.title)}</div><div class="joyokr-cell-sub">${number(item.key_result_count || 0)} Key Result sẽ được duyệt cùng</div></td>
        <td><div class="joyokr-cell-title">${escapeHtml(item.owner_name || '—')}</div><div class="joyokr-cell-sub">${badge(item.scope)}</div></td>
        <td>${cycleName || '—'}</td>
        <td>${dateTime(item.updated_at)}</td>
        <td><div class="joyokr-approval-actions">
          <button class="joyokr-button joyokr-button--small" type="button" data-route="${escapeAttr(route)}">Xem chi tiết</button>
          ${approveControl}
          ${actionButton('Yêu cầu sửa', 'objective-reject', { id: item.id }, 'danger small')}
        </div></td>
      </tr>`;
    }).join('');

    const filter = `<form class="joyokr-toolbar joyokr-approval-toolbar" data-filter-form data-filter-route="admin/approvals">
      <input class="joyokr-search" type="search" name="search" value="${escapeAttr(query.search || '')}" placeholder="Tìm Objective…">
      <button class="joyokr-button" type="submit">Lọc</button>
    </form>`;

    const summary = `<section class="joyokr-grid joyokr-grid--3 joyokr-approval-summary">
      ${dashboardStat('Objective chờ duyệt', number(result.total || 0), 'at_risk', 'approval')}
      ${dashboardStat('Objective', number(result.objective_count || 0), '', 'target')}
      ${dashboardStat('KR đi kèm', number(result.key_result_count || 0), '', 'circle-check')}
    </section>`;

    const body = rows
      ? tableCard(['Objective', 'Người gửi', 'Chu kỳ', 'Gửi lúc', 'Thao tác'], rows, 'Không có Objective chờ duyệt.')
      : `<section class="joyokr-card joyokr-card--pad">${empty('Không có Objective chờ duyệt', 'Mọi Objective bạn phụ trách đã được xử lý.')}</section>`;

    return page(
      'Duyệt OKR',
      'Duyệt Objective một lần; toàn bộ Key Result thuộc Objective được duyệt tự động',
      '',
      `<section class="joyokr-approval-intro joyokr-card joyokr-card--pad">
        <div><span class="joyokr-eyebrow">WORKFLOW PHÊ DUYỆT</span><h2>Duyệt Objective và toàn bộ Key Result trong một lần</h2><p class="joyokr-muted">Hệ thống kiểm tra 2–4 KR, tổng trọng số 100%, target, đơn vị và deadline trước khi duyệt. Không cần gửi hoặc duyệt từng KR riêng.</p></div>
      </section>
      ${summary}
      <section class="joyokr-section">${filter}${body}</section>`
    );
  }

  /** OKR roles (admin / manager) and the grant/revoke history. HR permissions are not OKR roles. */
  async function renderAdminRoles() {
    const query = currentQuery();
    const [roles, history] = await Promise.all([
      api().request('roles', { query: { page: query.page, per_page: 100 } }),
      api().request('role-history', { query: { per_page: ROLE_HISTORY_LIMIT } }),
    ]);
    const rows = (roles.items || []).map((item) => `<tr><td><strong>${escapeHtml(item.employee_name || '—')}</strong></td><td>${roleBadge(item.role)}</td><td>${escapeHtml(item.granted_by_name || '—')}</td><td>${dateOnly(item.granted_at)}</td><td><div class="joyokr-inline-actions">${actionButton('Thu hồi', 'revoke-role', { id: item.id, role: item.role }, 'danger small')}</div></td></tr>`).join('');
    const events = (history.items || []).slice(0, ROLE_HISTORY_LIMIT);
    const historyRows = events.map((event) => {
      const roleName = event.role === 'department_manager'
        ? `${ROLE_LABELS.department_manager}${event.department_name ? ` · ${event.department_name}` : ''}`
        : (ROLE_LABELS[event.role] || event.role || '—');
      const action = event.action === 'revoked' ? toneBadge('Thu hồi', 'rejected') : toneBadge('Cấp', 'approved');
      return `<tr><td>${dateTime(event.created_at)}</td><td>${escapeHtml(event.employee_name || '—')}</td><td>${toneBadge(roleName, ROLE_TONES[event.role] || 'pending')}</td><td>${action}</td><td>${escapeHtml(event.actor_name || '—')}</td></tr>`;
    }).join('');
    const historyTotal = Number(history.total || 0);
    const historyNote = historyTotal > events.length
      ? `Hiển thị ${number(events.length)} thay đổi gần nhất trên tổng số ${number(historyTotal)}.`
      : 'Mọi lần cấp và thu hồi vai trò OKR, mới nhất trước.';
    return page(
      'Vai trò OKR',
      'Quyền OKR được cấp riêng, tách biệt với quyền của Web HR: quản trị viên HR không tự động là Quản trị OKR.',
      actionButton('+ Gán vai trò', 'open-role', {}, 'primary'),
      `<section class="joyokr-card">${simpleTable(['Nhân sự', 'Vai trò', 'Cấp bởi', 'Ngày cấp', ''], rows, 'Chưa có vai trò OKR nào.')}${pagination(roles)}</section>
      <section class="joyokr-card joyokr-section"><div class="joyokr-card__head"><div><h2>Lịch sử cấp/thu hồi quyền</h2><p class="joyokr-muted">${escapeHtml(historyNote)}</p></div></div>${simpleTable(['Thời gian', 'Nhân sự', 'Vai trò', 'Thao tác', 'Người thực hiện'], historyRows, 'Chưa có thay đổi vai trò.')}</section>`,
    );
  }

  /** HR departments are read-only here; an admin only names each one's OKR manager. */
  async function renderAdminDepartments() {
    const result = await api().request('departments', { query: { page: currentQuery().page, per_page: 100 } });
    const rows = (result.items || []).map((item) => `<tr><td><strong>${escapeHtml(item.name)}</strong></td><td>${item.manager_name ? escapeHtml(item.manager_name) : '<span class="joyokr-muted">Chưa gán</span>'}</td><td><div class="joyokr-inline-actions">${actionButton('Gán quản lý OKR', 'assign-department-manager', { id: item.id }, 'small')}</div></td></tr>`).join('');
    return page('Quản lý phòng ban', 'Phòng ban do Web HR quản lý; ở đây chỉ gán người quản lý OKR.', '', `<section class="joyokr-card">${simpleTable(['Phòng ban', 'Quản lý OKR', ''], rows, 'Chưa có phòng ban trong Web HR.')}${pagination(result)}</section>`);
  }

  async function renderAdminTeams() {
    const query = currentQuery();
    const status = ORGANIZATION_STATUS_FILTERS.some(([value]) => value === query.status) ? query.status : 'active';
    const result = await api().request('teams', { query: { ...query, status, per_page: 100 } });
    const rows = (result.items || []).map((item) => {
      const actions = item.status === 'inactive'
        ? actionButton('Khôi phục', 'restore-team', { id: item.id }, 'primary small')
        : `${actionButton('Sửa', 'edit-team', { id: item.id }, 'small')}${actionButton('Thành viên', 'team-members', { id: item.id }, 'small')}${actionButton('Ẩn', 'delete-team', { id: item.id }, 'danger small')}`;
      const members = (item.members || []).map((member) => member.display_name).filter(Boolean).join(', ');
      return `<tr><td><strong>${escapeHtml(item.name)}</strong></td><td>${badge(item.status)}</td><td>${escapeHtml(item.department_name || 'Chưa gán')}</td><td>${escapeHtml(item.manager_name || 'Chưa gán')}</td><td>${escapeHtml(members || '—')}<div class="joyokr-cell-sub">${number(item.member_count)} thành viên</div></td><td><div class="joyokr-inline-actions">${actions}</div></td></tr>`;
    }).join('');
    return page('Quản lý teams', `${number(result.total || 0)} team`, actionButton('+ Tạo team', 'open-team', {}, 'primary'), `${statusToolbar('admin/teams', status, ORGANIZATION_STATUS_FILTERS)}<section class="joyokr-card">${simpleTable(['Team', 'Trạng thái', 'Phòng ban', 'Manager', 'Thành viên', ''], rows, status === 'inactive' ? 'Không có team đã ẩn.' : 'Chưa có team.')}${pagination(result)}</section>`);
  }

  async function renderAdminCycles() {
    const query = currentQuery();
    const status = CYCLE_STATUS_FILTERS.some(([value]) => value === query.status) ? query.status : 'all';
    const result = await api().request('cycles', { query: { ...query, status, per_page: 100 } });
    const names = new Map((result.items || []).map((item) => [Number(item.id), item.name]));
    const rows = (result.items || []).map((item) => {
      const type = labels[item.cycle_type] || item.cycle_type || '';
      const parentName = item.parent_id ? names.get(Number(item.parent_id)) : '';
      // Django refuses to edit a closed cycle: it has to be reopened first.
      const edit = item.status === 'closed' ? '' : actionButton('Sửa', 'edit-cycle', { id: item.id }, 'small');
      return `<tr><td><strong>${escapeHtml(item.name)}</strong><div class="joyokr-cell-sub">${escapeHtml(parentName ? `${type} · thuộc ${parentName}` : type)}</div></td><td>${date(item.start_date)}</td><td>${date(item.end_date)}</td><td>${number(item.objective_count || 0)}</td><td>${badge(item.status)}</td><td><div class="joyokr-inline-actions">${edit}${cycleActions(item)}</div></td></tr>`;
    }).join('');
    const emptyMessage = status === 'all' ? 'Chưa có chu kỳ.' : `Không có chu kỳ ở trạng thái ${labels[status]}.`;
    return page('Chu kỳ OKR', 'Quản lý chu kỳ tháng, quý, năm và tùy chỉnh', actionButton('+ Tạo chu kỳ', 'open-cycle', {}, 'primary'), `${statusToolbar('admin/cycles', status, CYCLE_STATUS_FILTERS)}<section class="joyokr-card">${simpleTable(['Chu kỳ', 'Bắt đầu', 'Kết thúc', 'Objective', 'Trạng thái', ''], rows, emptyMessage)}${pagination(result)}</section>${card('Clone OKR sang chu kỳ mới', `<p class="joyokr-muted">Sao chép Objective và Key Result; tiến độ về 0 và không sao chép check-in.</p>${actionButton('Clone OKR', 'clone-cycle', {}, 'primary')}`, '')}`);
  }

  async function renderAdminSettings() {
    const settings = await api().request('settings');
    const ranges = settings.rating_ranges || [];
    const ratingRows = ranges.map((range) => `<tr><td>${number(range.min)} – ${number(range.max)}</td><td><span class="joyokr-badge">${escapeHtml(range.label)}</span></td></tr>`).join('');
    // As in JOYOKR, only the labels are edited; min/max travel back unchanged.
    const ratingInputs = ranges.map((range, index) => `<input type="hidden" name="rating_${index}_min" value="${escapeAttr(range.min)}"><input type="hidden" name="rating_${index}_max" value="${escapeAttr(range.max)}"><label class="joyokr-field"><span>Nhãn ${index + 1}</span><input name="rating_${index}_label" value="${escapeAttr(range.label)}" required></label>`).join('');
    const schedule = settings.reminder_schedule || {};
    const scheduleBadge = schedule.scheduled ? toneBadge('Đang chạy', 'approved') : toneBadge('Chưa kích hoạt trên máy chủ', 'at_risk');
    const channels = (schedule.channels || []).map((channel) => CHANNEL_LABELS[channel] || channel).join(', ') || '—';
    const reminderCard = `<section class="joyokr-card joyokr-card--pad joyokr-section"><h2 class="joyokr-section-title">Nhắc check-in</h2>
      <div class="joyokr-grid joyokr-grid--2 joyokr-section"><label class="joyokr-field"><span>Chênh lệch tối đa để tính “Có rủi ro” (%)</span><input type="number" name="warning_threshold" min="0" max="100" step="0.01" value="${escapeAttr(settings.warning_threshold)}" required><small class="joyokr-field__error" data-field-error="warning_threshold"></small></label><label class="joyokr-field"><span>Chu kỳ nhắc check-in</span><select name="reminder_frequency">${options(['weekly', 'biweekly', 'monthly'], settings.reminder_frequency)}</select><small class="joyokr-field__error" data-field-error="reminder_frequency"></small></label></div>
      <label class="joyokr-check joyokr-section"><input type="checkbox" name="email_reminders" ${settings.email_reminders ? 'checked' : ''}> Gửi email cùng thông báo trong hệ thống</label><p class="joyokr-cell-sub">Máy chủ hiện chưa gửi email; tùy chọn chỉ được lưu lại.</p></section>`;
    // What really runs on the server, apart from what is configured above.
    const scheduleCard = `<section class="joyokr-card joyokr-card--pad joyokr-section"><h2 class="joyokr-section-title">Trạng thái lịch nhắc</h2><p class="joyokr-muted">Cấu hình ở trên chỉ được lưu lại; bảng này cho biết máy chủ thực sự đang chạy gì.</p>${detailGrid([
      ['Tần suất đã cấu hình', labels[settings.reminder_frequency] || settings.reminder_frequency || '—'],
      ['Lịch chạy tự động', raw(scheduleBadge)],
      ['Kênh', channels],
      ['Email', schedule.email_available ? 'Có' : 'Chưa hỗ trợ'],
      ['Lệnh', raw(`<code>${escapeHtml(schedule.command || 'okr_checkin_reminders')}</code>`)],
    ])}</section>`;
    return page('Cài đặt hệ thống', 'Cấu hình chấm điểm và nhắc check-in', actionButton('Lưu cài đặt', 'save-settings', {}, 'primary'), `
      <form data-settings-form novalidate>${card('Rating Rule (OKR Score 0.0 → 1.0)', `${simpleTable(['Khoảng điểm', 'Rating'], ratingRows)}<div data-rating-inputs>${ratingInputs}<small class="joyokr-field__error" data-field-error="rating_ranges"></small></div>`)}
        ${reminderCard}
      </form>
      ${scheduleCard}`);
  }

  /** Page frame. The subtitle is plain text unless wrapped with raw(). */
  function page(title, subtitle, actions, body) {
    const subtitleHtml = subtitle?.joyokrHtml === true ? subtitle.value : escapeHtml(subtitle ?? '');
    return `<article class="joyokr-page"><header class="joyokr-page-head"><div><h1>${escapeHtml(stripTags(title))}</h1>${subtitleHtml ? `<p>${subtitleHtml}</p>` : ''}</div>${actions ? `<div class="joyokr-page-actions">${actions}</div>` : ''}</header>${body}</article>`;
  }

  function card(title, body, action = '') {
    return `<section class="joyokr-card"><div class="joyokr-card__head"><h2>${escapeHtml(title)}</h2>${action}</div><div class="joyokr-card__body">${body}</div></section>`;
  }

  function tableCard(headings, rows, emptyMessage) {
    return `<section class="joyokr-card">${simpleTable(headings, rows, emptyMessage)}</section>`;
  }

  function simpleTable(headings, rows, emptyMessage = 'Chưa có dữ liệu.') {
    if (!rows) return empty(emptyMessage, 'Hãy tạo dữ liệu mới hoặc thay đổi bộ lọc.');
    return `<div class="joyokr-table-wrap"><table class="joyokr-table"><thead><tr>${headings.map((heading) => `<th>${escapeHtml(heading)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function reportExportTable(name, headings, rows, emptyMessage) {
    if (!rows) return empty(emptyMessage, 'Dữ liệu sẽ xuất hiện khi có KR/Objective phù hợp.');
    return `<div class="joyokr-table-wrap"><table class="joyokr-table" data-report-export data-report-export-name="${escapeAttr(name)}"><thead><tr>${headings.map((heading) => `<th>${escapeHtml(heading)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function objectiveTableCard(result, message) {
    const rows = (result.items || []).map((item) => `<tr data-route="objectives/${routeId(item.id)}" tabindex="0" role="link"><td><div class="joyokr-cell-title">${escapeHtml(item.title)}</div><div class="joyokr-cell-sub">${escapeHtml(item.owner_name || '')}</div></td><td>${badge(item.scope)}</td><td>${escapeHtml(item.cycle_name || '')}</td><td>${number(item.weight)}%</td><td>${progressBar(item.progress, item.expected_progress || 0, item.health, true)}<div class="joyokr-cell-sub">${escapeHtml(progressText(item))}</div></td><td>${objectiveHealthBadge(item)}</td><td>${badge(item.approval_status)}</td></tr>`).join('');
    return `<section class="joyokr-card">${simpleTable(['Objective', 'Cấp', 'Chu kỳ', 'Trọng số', 'Tiến độ', 'Trạng thái', 'Duyệt'], rows, message)}${pagination(result)}</section>`;
  }

  function dashboardStat(label, value, tone = '', icon = 'target', detailKey = '', meta = '') {
    const classes = `joyokr-stat joyokr-stat--dashboard ${detailKey ? 'joyokr-stat--button' : ''} ${tone ? `joyokr-stat--${escapeAttr(tone)}` : ''}`;
    const content = `<div class="joyokr-stat__top"><span class="joyokr-stat__icon" aria-hidden="true">${uiIcon(icon)}</span><span class="joyokr-stat__label">${escapeHtml(label)}</span></div><div class="joyokr-stat__value ${tone ? `joyokr-text--${escapeAttr(tone)}` : ''}">${escapeHtml(value)}</div>${meta ? `<div class="joyokr-stat__meta">${escapeHtml(meta)}</div>` : ''}${detailKey ? '<span class="joyokr-stat__hint">Xem chi tiết</span>' : ''}`;
    if (!detailKey) return `<div class="${classes}">${content}</div>`;
    return `<button class="${classes}" type="button" data-action="dashboard-detail" data-dashboard-detail="${escapeAttr(detailKey)}" aria-controls="joyokr-dashboard-detail-${escapeAttr(detailKey)}">${content}</button>`;
  }

  function dashboardDetailPanels(dashboard) {
    const details = dashboard.objective_details || {};
    const panels = [
      ['all', 'Tất cả Objective', details.all || [], 'objective'],
      ['on_track', 'Objective đúng tiến độ', details.on_track || [], 'objective'],
      ['at_risk', 'Objective có rủi ro', details.at_risk || [], 'objective'],
      ['off_track', 'Objective chậm tiến độ', details.off_track || [], 'objective'],
      ['completed', 'Objective đã hoàn thành', details.completed || [], 'objective'],
      ['stale', 'KR cần check-in', dashboard.stale_checkin_items || [], 'key_result'],
    ];
    // attention is capped server-side (most urgent first); stale_count counts every stale KR.
    const staleShown = (dashboard.stale_checkin_items || []).length;
    const staleNote = attentionCapNote(staleShown, dashboard.stale_checkins, dashboardReportQuery(dashboard));
    return `<section class="joyokr-section joyokr-dashboard-detail-region" aria-live="polite">${panels.map(([key, title, items, type]) => `<div id="joyokr-dashboard-detail-${escapeAttr(key)}" class="joyokr-card joyokr-dashboard-detail-panel" data-dashboard-detail-panel="${escapeAttr(key)}" hidden><div class="joyokr-card__head"><div><span class="joyokr-eyebrow">CHI TIẾT NHANH</span><h2 tabindex="-1">${escapeHtml(title)}</h2></div><span class="joyokr-section-note">${number(items.length)} mục</span></div><div class="joyokr-card__body">${type === 'key_result' ? dashboardKeyResultRows(items) : dashboardObjectiveRows(items)}${key === 'stale' ? staleNote : ''}</div></div>`).join('')}</section>`;
  }

  /**
   * "Hiển thị x / N" when the dashboard shows only part of a list, with a
   * button to the report (same cycle and filters), which lists everything.
   */
  function attentionCapNote(shown, total, reportQuery = '') {
    const shownCount = Number(shown) || 0;
    const totalCount = Number(total) || 0;
    if (totalCount <= shownCount) return '';
    const queryAttribute = reportQuery ? ` data-route-query="${escapeAttr(reportQuery)}"` : '';
    return `<div class="joyokr-attention-cap" role="note" data-attention-cap><span>Hiển thị ${number(shownCount)} / ${number(totalCount)}, xem đầy đủ trong Báo cáo.</span><button class="joyokr-button joyokr-button--small" type="button" data-route="reports"${queryAttribute}>Mở Báo cáo</button></div>`;
  }

  /** "cycle_id=3&team_id=4" for the report link: the dashboard's cycle and applied filters, all validated. */
  function dashboardReportQuery(dashboard) {
    const params = new URLSearchParams();
    const cycleId = routeId(dashboard?.cycle?.id);
    if (cycleId !== '0') params.set('cycle_id', cycleId);
    const filters = dashboard?.filters || {};
    if (SCOPE_VALUES.includes(filters.scope)) params.set('scope', filters.scope);
    ['department_id', 'team_id', 'owner_user_id'].forEach((key) => {
      const id = routeId(filters[key]);
      if (id !== '0') params.set(key, id);
    });
    return params.toString();
  }

  function dashboardObjectiveRows(items) {
    if (!items.length) return empty('Không có Objective phù hợp', 'Chỉ số này hiện không có Objective nào.');
    return `<ul class="joyokr-list joyokr-dashboard-drill-list">${items.map((item) => `<li class="joyokr-list-item" data-route="objectives/${routeId(item.id)}" tabindex="0" role="link"><div class="joyokr-list-item__body"><div class="joyokr-list-item__title">${escapeHtml(item.title)}</div><div class="joyokr-list-item__meta">${escapeHtml(item.owner_name || 'Chưa gán')} · ${number(item.progress)}%</div></div>${item.health ? badge(item.health) : ''}</li>`).join('')}</ul>`;
  }

  function dashboardKeyResultRows(items) {
    if (!items.length) return empty('Không có KR cần check-in', 'Các Key Result trong chu kỳ đều đã được cập nhật đúng hạn.');
    return `<ul class="joyokr-list joyokr-dashboard-drill-list">${items.map((item) => `<li class="joyokr-list-item" data-route="key-results/${routeId(item.id)}" tabindex="0" role="link"><div class="joyokr-list-item__body"><div class="joyokr-list-item__title">${escapeHtml(item.title)}</div><div class="joyokr-list-item__meta">${escapeHtml(item.owner_name || 'Chưa gán')} · ${escapeHtml(item.objective_title || '')}</div><div class="joyokr-list-item__meta">${item.latest_checkin_at ? `Check-in gần nhất: ${dateTime(item.latest_checkin_at)}` : 'Chưa từng check-in'} · ${number(item.progress)}%</div></div>${badge(item.health)}</li>`).join('')}</ul>`;
  }

  function attentionKeyResultRows(items) {
    if (!items.length) return empty('Không có KR cần chú ý', 'Chưa có KR chậm tiến độ hoặc quá hạn check-in.');
    return `<ul class="joyokr-list">${items.slice(0, ATTENTION_CARD_LIMIT).map((item) => `<li class="joyokr-list-item" data-route="key-results/${routeId(item.id)}" tabindex="0" role="link"><div class="joyokr-list-item__body"><div class="joyokr-list-item__title">${escapeHtml(item.title)}</div><div class="joyokr-list-item__meta">${escapeHtml(item.owner_name || 'Chưa gán')} · ${escapeHtml(item.objective_title || '')}</div><div class="joyokr-list-item__meta">${item.checkin_overdue ? 'Quá hạn check-in' : 'Đã check-in'} · ${number(item.progress)}%</div></div>${badge(item.health)}</li>`).join('')}</ul>`;
  }

  function uiIcon(name) {
    const icons = {
      target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/>',
      'circle-check': '<circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/>',
      'triangle-alert': '<path d="M10.3 3.8 2.8 17a2 2 0 0 0 1.7 3h15a2 2 0 0 0 1.7-3L13.7 3.8a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
      'trending-down': '<path d="m3 7 6 6 4-4 8 8"/><path d="M15 17h6v-6"/>',
      'trending-up': '<path d="m3 17 6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
      activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
      clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
      approval: '<path d="M9 5h6M9 3h6a2 2 0 0 1 2 2v1h1a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h1V5a2 2 0 0 1 2-2Z"/><path d="m8.5 14 2.2 2.2 4.8-5.2"/>',
      'badge-check': '<path d="M12 3 14 5l2.8-.2.2 2.8 2 2-2 2 .2 2.8-2.8-.2-2 2-2-2-2.8.2.2-2.8-2-2 2-2-.2-2.8L10 5l2-2Z"/><path d="m9.5 12 1.7 1.7 3.5-3.6"/>',
      sparkles: '<path d="m12 3 1.2 3.2L16.5 7.5l-3.3 1.3L12 12l-1.2-3.2-3.3-1.3 3.3-1.3L12 3Z"/><path d="m18.5 13 .7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7.7-1.8Z"/>',
    };
    const paths = icons[name] || icons.target;
    return `<svg class="joyokr-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" focusable="false">${paths}</svg>`;
  }

  function dashboardLineChart(points, progress, expected, note = '') {
    const clean = (points || []).map((point) => ({ date: point.date, progress: clamp(point.progress) })).filter((point) => point.date);
    const noteText = note ? ` · ${escapeHtml(note)}` : '';
    if (!clean.length) {
      return `<section class="joyokr-card joyokr-chart-card joyokr-chart-card--wide"><div class="joyokr-card__head"><div><h2>Xu hướng tiến độ</h2><p>Tiến độ trung bình theo các lần check-in${noteText}</p></div><span class="joyokr-chart-chip">${number(progress)}%</span></div><div class="joyokr-chart-empty"><span class="joyokr-chart-empty__icon">${uiIcon('sparkles')}</span><strong>Chưa đủ dữ liệu để vẽ xu hướng</strong><p>Biểu đồ sẽ tự xuất hiện khi có lịch sử check-in.</p></div></section>`;
    }

    const width = 760;
    const height = 250;
    const left = 42;
    const right = 18;
    const top = 22;
    const bottom = 36;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const x = (index) => left + (clean.length === 1 ? plotWidth / 2 : (plotWidth * index) / (clean.length - 1));
    const y = (value) => top + plotHeight - (clamp(value) / 100) * plotHeight;
    const polyline = clean.map((point, index) => `${x(index).toFixed(1)},${y(point.progress).toFixed(1)}`).join(' ');
    const area = clean.length === 1
      ? ''
      : `M ${x(0).toFixed(1)} ${y(clean[0].progress).toFixed(1)} ${clean.slice(1).map((point, index) => `L ${x(index + 1).toFixed(1)} ${y(point.progress).toFixed(1)}`).join(' ')} L ${x(clean.length - 1).toFixed(1)} ${top + plotHeight} L ${x(0).toFixed(1)} ${top + plotHeight} Z`;
    const yTicks = [0, 25, 50, 75, 100];
    const labelIndexes = Array.from(new Set([0, Math.floor((clean.length - 1) / 2), clean.length - 1]));
    const dots = clean.map((point, index) => `<circle class="joyokr-chart-dot" cx="${x(index).toFixed(1)}" cy="${y(point.progress).toFixed(1)}" r="4"><title>${escapeHtml(shortDate(point.date))}: ${number(point.progress)}%</title></circle>`).join('');
    const expectedY = y(expected).toFixed(1);

    return `<section class="joyokr-card joyokr-chart-card joyokr-chart-card--wide">
      <div class="joyokr-card__head"><div><h2>Xu hướng tiến độ</h2><p>Tiến độ trung bình theo lịch sử check-in${noteText}</p></div><div class="joyokr-chart-legend"><span><i class="joyokr-chart-legend__dot"></i>Thực tế</span><span><i class="joyokr-chart-legend__dash"></i>Kỳ vọng hiện tại</span></div></div>
      <div class="joyokr-chart-card__body">
        <svg class="joyokr-line-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Biểu đồ xu hướng tiến độ OKR">
          ${yTicks.map((tick) => `<line class="joyokr-chart-gridline" x1="${left}" y1="${y(tick)}" x2="${width - right}" y2="${y(tick)}"></line><text class="joyokr-chart-axis" x="${left - 9}" y="${y(tick) + 4}" text-anchor="end">${tick}%</text>`).join('')}
          <line class="joyokr-chart-expected" x1="${left}" y1="${expectedY}" x2="${width - right}" y2="${expectedY}"></line>
          ${area ? `<path class="joyokr-chart-area" d="${area}"></path>` : ''}
          <polyline class="joyokr-chart-line" points="${polyline}"></polyline>
          ${dots}
          ${labelIndexes.map((index) => `<text class="joyokr-chart-axis joyokr-chart-axis--x" x="${x(index)}" y="${height - 10}" text-anchor="${index === 0 ? 'start' : index === clean.length - 1 ? 'end' : 'middle'}">${escapeHtml(shortDate(clean[index].date))}</text>`).join('')}
        </svg>
      </div>
    </section>`;
  }

  function dashboardStatusChart(stats) {
    const parts = [
      ['Đúng tiến độ', Number(stats.on_track) || 0, 'on-track'],
      ['Có rủi ro', Number(stats.at_risk) || 0, 'at-risk'],
      ['Chậm tiến độ', Number(stats.off_track) || 0, 'off-track'],
      ['Hoàn thành', Number(stats.completed) || 0, 'completed'],
    ];
    const total = parts.reduce((sum, item) => sum + item[1], 0);
    let cursor = 0;
    const segments = parts.filter((item) => item[1] > 0).map((item) => {
      const start = cursor;
      cursor += total ? (item[1] / total) * 100 : 0;
      return `var(--joyokr-chart-${item[2]}) ${start.toFixed(2)}% ${cursor.toFixed(2)}%`;
    });
    const gradient = segments.length ? `conic-gradient(${segments.join(',')})` : 'conic-gradient(var(--joyokr-border) 0 100%)';
    const legend = parts.map(([label, value, tone]) => `<li><span class="joyokr-chart-key joyokr-chart-key--${tone}"></span><span>${escapeHtml(label)}</span><strong>${number(value)}</strong></li>`).join('');

    return `<section class="joyokr-card joyokr-chart-card joyokr-chart-card--donut">
      <div class="joyokr-card__head"><div><h2>Trạng thái Objective</h2><p>Phân bổ sức khỏe mục tiêu</p></div></div>
      <div class="joyokr-donut-layout">
        <div class="joyokr-donut" style="background:${gradient}" role="img" aria-label="${number(total)} Objective"><div><strong>${number(total)}</strong><span>Objective</span></div></div>
        <ul class="joyokr-chart-legend-list">${legend}</ul>
      </div>
    </section>`;
  }

  function dashboardTeamChart(items, expected) {
    const teams = (items || []).slice().sort((a, b) => Number(b.progress || 0) - Number(a.progress || 0)).slice(0, 8);
    const body = teams.length ? `<div class="joyokr-team-chart">${teams.map((team, index) => {
      const value = clamp(team.progress || 0);
      const expectedValue = Number(team.expected_progress ?? expected);
      const tone = team.health || health(value, expectedValue);
      return `<div class="joyokr-team-chart__row">
        <div class="joyokr-team-chart__rank">${index + 1}</div>
        <div class="joyokr-team-chart__name"><strong>${escapeHtml(team.name)}</strong><span>${number(team.objective_count || 0)} Objective${team.member_count === undefined ? '' : ` · ${number(team.member_count)} thành viên`}</span></div>
        <div class="joyokr-team-chart__progress">${progressBar(team.progress || 0, team.expected_progress ?? expected, team.health || health(value, expectedValue), true)}<div class="joyokr-team-chart__meta"><span>Kỳ vọng ${number(expectedValue)}%</span>${badge(tone)}</div></div>
        <strong class="joyokr-team-chart__value">${number(value)}%</strong>
      </div>`;
    }).join('')}</div>` : empty('Chưa có dữ liệu Team', 'Tạo team và gán Objective để xem biểu đồ so sánh.');

    return `<section class="joyokr-card joyokr-chart-card joyokr-team-card"><div class="joyokr-card__head"><div><h2>So sánh tiến độ theo Team</h2><p>Cùng màu sức khỏe và mốc kỳ vọng với Objective trên toàn hệ thống</p></div><button class="joyokr-button joyokr-button--small" data-route="teams">Xem Teams</button></div><div class="joyokr-card__body">${body}</div></section>`;
  }

  function shortDate(value) {
    if (!value) return '';
    const parsed = new Date(`${String(value).slice(0, 10)}T00:00:00`);
    return Number.isNaN(parsed.valueOf()) ? '' : new Intl.DateTimeFormat('vi-VN', { day: '2-digit', month: '2-digit' }).format(parsed);
  }

  function objectiveRows(items) {
    if (!items.length) return empty('Chưa có Objective', 'Tạo Objective đầu tiên để bắt đầu.');
    return `<ul class="joyokr-list">${items.map((item) => `<li class="joyokr-list-item" data-route="objectives/${routeId(item.id)}" tabindex="0" role="link"><div class="joyokr-list-item__body"><div class="joyokr-list-item__title">${escapeHtml(item.title)}</div><div class="joyokr-list-item__meta">${escapeHtml(item.owner_name || '')} · ${number(item.progress)}%</div>${progressBar(item.progress, item.expected_progress || 0, item.health, true)}</div>${badge(item.health)}</li>`).join('')}</ul>`;
  }

  function teamRows(items) {
    if (!items.length) return empty('Chưa có team', 'Admin có thể tạo team trong phần Quản trị.');
    return `<ul class="joyokr-list">${items.slice(0, 6).map((item) => `<li class="joyokr-list-item"><div class="joyokr-list-item__body"><div class="joyokr-list-item__title">${escapeHtml(item.name)}</div><div class="joyokr-list-item__meta">${number(item.objective_count || 0)} Objective · ${number(item.member_count || 0)} thành viên</div>${progressBar(item.progress, item.expected_progress || 0, item.health || health(item.progress, item.expected_progress || 0), true)}</div><strong>${number(item.progress)}%</strong></li>`).join('')}</ul>`;
  }

  function treeNode(item) {
    const keyResults = item.key_results || [];
    const children = item.children || [];
    const hasDetails = keyResults.length > 0 || children.length > 0;
    const detailId = `joyokr-tree-details-${item.id}`;
    const healthMarkup = objectiveHealthBadge(item);
    const details = hasDetails ? `<div class="joyokr-tree-node__details" id="${escapeAttr(detailId)}" data-tree-node-details="${escapeAttr(item.id)}">
      ${keyResults.length ? `<div class="joyokr-tree-kr-list">${keyResults.map(treeKrRow).join('')}</div>` : ''}
      ${children.length ? `<div class="joyokr-tree-node__children">${children.map(treeNode).join('')}</div>` : ''}
    </div>` : '';
    return `<div class="joyokr-tree-node">
      <div class="joyokr-tree-node__card">
        ${hasDetails ? `<button class="joyokr-tree-node__toggle" type="button" data-action="toggle-tree-node" data-tree-target="${escapeAttr(item.id)}" aria-expanded="true" aria-controls="${escapeAttr(detailId)}" title="Thu gọn/mở rộng"><span data-tree-toggle-icon aria-hidden="true">▾</span><span class="screen-reader-text">Thu gọn/mở rộng</span></button>` : '<span class="joyokr-tree-node__toggle-spacer" aria-hidden="true"></span>'}
        ${badge(item.scope)}
        <div class="joyokr-tree-node__main" data-route="objectives/${routeId(item.id)}" tabindex="0" role="link"><strong>${escapeHtml(item.title)}</strong><div class="joyokr-cell-sub">${escapeHtml(item.owner_name || '')}</div></div>
        <div class="joyokr-tree-node__progress">${progressBar(item.progress, item.expected_progress || 0, item.health, true)}<div class="joyokr-tree-node__progress-meta"><strong>${number(item.progress)}%</strong><span>Kỳ vọng ${number(item.expected_progress || 0)}%</span></div></div>
        <div class="joyokr-tree-node__status">${healthMarkup}<span class="joyokr-cell-sub">${item.progress_is_provisional ? 'Tiến độ tạm tính' : 'Theo chu kỳ hiện tại'}</span></div>
      </div>
      ${details}
    </div>`;
  }

  function treeKrRow(item) {
    return `<div class="joyokr-tree-kr" data-route="key-results/${routeId(item.id)}" tabindex="0" role="link">
      <span class="joyokr-tree-kr__type">KR</span>
      <div class="joyokr-tree-kr__main"><strong>${escapeHtml(item.title)}</strong><div class="joyokr-cell-sub">${escapeHtml(item.owner_name || '')} · Hạn ${date(item.due_date)}</div></div>
      <div class="joyokr-tree-kr__progress">${progressBar(item.progress, item.expected_progress || 0, item.health, true)}<div class="joyokr-tree-node__progress-meta"><strong>${number(item.progress)}%</strong><span>Kỳ vọng ${number(item.expected_progress || 0)}%</span></div></div>
      <div class="joyokr-tree-kr__status">${objectiveHealthBadge(item)}</div>
    </div>`;
  }

  function commentCard(type, id, comments, section = true, canComment = true) {
    const content = (comments || []).length ? `<ul class="joyokr-list">${comments.map((item) => `<li class="joyokr-list-item">${avatar({ display_name: item.user_name })}<div class="joyokr-list-item__body"><strong>${escapeHtml(item.user_name || '')}</strong><p>${escapeHtml(item.content)}</p><div class="joyokr-list-item__meta">${dateTime(item.created_at)}</div></div></li>`).join('')}</ul>` : empty('Chưa có bình luận', 'Trao đổi về tiến độ và trở ngại tại đây.');
    const result = card('Bình luận', content, canComment ? actionButton('+ Bình luận', 'open-comment', { objectType: type, objectId: id }, 'primary small') : '');
    return section ? `<section class="joyokr-section">${result}</section>` : result;
  }

  function evidenceList(items) {
    if (!items.length) return empty('Chưa có bằng chứng', 'Thêm liên kết hoặc tệp từ mỗi lần cập nhật.');
    return `<ul class="joyokr-list">${items.map((item) => {
      const isFile = item.evidence_type === 'file';
      // Links render only for http(s); files only via the authorised API download path.
      const href = isFile ? safeDownloadPath(item.url) : safeHttpUrl(item.url);
      const label = escapeHtml(item.label || item.file_name || 'Bằng chứng');
      const link = !href
        ? `<span class="joyokr-link joyokr-link--disabled">${label}</span>`
        : isFile
          ? `<a class="joyokr-link" href="${escapeAttr(href)}" rel="noopener" download>${label}</a>`
          : `<a class="joyokr-link" href="${escapeAttr(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      return `<li class="joyokr-list-item"><span aria-hidden="true">${isFile ? '▧' : '↗'}</span><div class="joyokr-list-item__body">${link}<div class="joyokr-list-item__meta">${escapeHtml(item.user_name || '')} · ${dateTime(item.created_at)}</div></div></li>`;
    }).join('')}</ul>`;
  }

  function timeline(items, content) {
    if (!items.length) return empty('Chưa có lịch sử', 'Các thay đổi sẽ xuất hiện tại đây.');
    return `<ol class="joyokr-timeline">${items.map((item) => `<li><time>${dateTime(item.created_at)}</time><p>${content(item)}</p></li>`).join('')}</ol>`;
  }

  function detailGrid(entries) {
    return `<dl class="joyokr-kv">${entries.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${value?.joyokrHtml === true ? value.value : escapeHtml(value ?? '—')}</dd>`).join('')}</dl>`;
  }

  function stat(label, value, tone = '', meta = '') {
    return `<div class="joyokr-stat"><div class="joyokr-stat__label">${escapeHtml(label)}</div><div class="joyokr-stat__value ${tone ? `joyokr-text--${escapeAttr(tone)}` : ''}">${escapeHtml(value)}</div>${meta ? `<div class="joyokr-stat__meta">${escapeHtml(meta)}</div>` : ''}</div>`;
  }

  function progressDefinitionText(definition) {
    return definition?.formula ? `Công thức: ${definition.formula}` : '';
  }

  function progressBar(progress, expected = 0, status = 'on_track', thin = false) {
    const tones = { on_track: 'success', at_risk: 'warning', off_track: 'danger', completed: 'completed' };
    const tone = status ? (tones[status] || 'success') : 'pending';
    return `<div class="joyokr-progress ${thin ? 'joyokr-progress--thin' : ''}" data-tone="${escapeAttr(tone)}" title="${number(progress)}%"><div class="joyokr-progress__fill" style="width:${clamp(progress)}%"></div>${Number(expected) > 0 ? `<span class="joyokr-progress__mark" style="left:${clamp(expected)}%"></span>` : ''}</div>`;
  }

  function progressText(item) {
    return `${number(item.progress)}%${item.progress_is_provisional ? ' · tạm tính' : ''}`;
  }

  function objectiveHealthBadge(item) {
    if (item.approval_status !== 'approved') return '<span class="joyokr-badge joyokr-badge--pending">Chưa duyệt</span>';
    return item.health ? badge(item.health) : '<span class="joyokr-muted">—</span>';
  }

  function badge(value) {
    const safe = String(value ?? 'draft');
    return `<span class="joyokr-badge joyokr-badge--${escapeAttr(safe)}">${escapeHtml(labels[safe] || safe.replaceAll('_', ' '))}</span>`;
  }

  function actionButton(label, action, data = {}, tones = '') {
    const toneClasses = tones.split(' ').filter(Boolean).map((tone) => `joyokr-button--${escapeAttr(tone)}`).join(' ');
    const attributes = Object.entries(data).map(([key, value]) => `data-${camelToKebab(key)}="${escapeAttr(value)}"`).join(' ');
    return `<button class="joyokr-button ${toneClasses}" type="button" data-action="${escapeAttr(action)}" ${attributes}>${escapeHtml(label)}</button>`;
  }

  function workflowButtons(type, item) {
    if (type !== 'objective') return '';
    const status = item.approval_status;
    if (item.can_submit && ['draft', 'rejected'].includes(status)) return actionButton('Gửi duyệt', `${type}-submit`, { id: item.id }, 'small');
    const canSelfApprove = can('joyokr_manage_settings');
    if (item.can_approve && status === 'submitted' && can('joyokr_approve') && (Number(item.owner_user_id) !== Number(config().currentUser?.id) || canSelfApprove)) return `${actionButton('Duyệt', `${type}-approve`, { id: item.id }, 'primary small')} ${actionButton('Yêu cầu sửa', `${type}-reject`, { id: item.id }, 'danger small')}`;
    return '';
  }

  function cycleActions(item) {
    if (item.status === 'draft') return actionButton('Kích hoạt', 'cycle-status', { id: item.id, status: 'active', current: 'draft' }, 'small');
    if (item.status === 'active') return actionButton('Đóng chu kỳ', 'cycle-status', { id: item.id, status: 'closed', current: 'active' }, 'small');
    if (item.status === 'closed') return actionButton('Mở lại', 'cycle-status', { id: item.id, status: 'active', current: 'closed' }, 'small');
    return '';
  }

  /** JOYOKR's organizationStatusToolbar with the given [value, label] choices. */
  function statusToolbar(route, status, choices) {
    return `<form class="joyokr-toolbar joyokr-section" data-filter-form data-filter-route="${escapeAttr(route)}"><select class="joyokr-select" name="status" aria-label="Lọc theo trạng thái">${choices.map(([value, label]) => `<option value="${escapeAttr(value)}" ${value === status ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('')}</select><button class="joyokr-button" type="submit">Lọc</button></form>`;
  }

  function toneBadge(label, tone) {
    return `<span class="joyokr-badge joyokr-badge--${escapeAttr(tone)}">${escapeHtml(label)}</span>`;
  }

  function roleBadge(role) {
    return toneBadge(ROLE_LABELS[role] || role || '—', ROLE_TONES[role] || 'pending');
  }

  function cycleSelect(cycle) {
    return `<button class="joyokr-button" type="button" data-action="choose-cycle" data-id="${escapeAttr(cycle?.id || '')}">${escapeHtml(cycle?.name || 'Chọn chu kỳ')} ▾</button>`;
  }

  /**
   * W4 notice shared by the dashboard, report, tree and check-in pages: with
   * two or more active cycles, say which one is shown and, when the server
   * picked it by default, the rule it used. Nothing for one active cycle.
   */
  function cycleNotice(selection, cycle) {
    const active = Array.isArray(selection?.active_cycles) ? selection.active_cycles : [];
    if (active.length < 2) return '';
    const selectedId = Number(selection.selected_id || cycle?.id || 0);
    const shown = active.find((item) => Number(item.id) === selectedId) || null;
    const shownName = shown?.name || cycle?.name || 'Chưa xác định';
    const items = active.map((item) => {
      const current = Number(item.id) === selectedId;
      return `<li${current ? ' class="is-current"' : ''}><strong>${escapeHtml(item.name)}</strong> · ${date(item.start_date)} – ${date(item.end_date)}${current ? ' <span class="joyokr-badge joyokr-badge--active">Đang hiển thị</span>' : ''}</li>`;
    }).join('');
    const reason = selection.selected_by === 'default'
      ? `<p class="joyokr-cycle-notice__rule"><b>Chọn tự động:</b> ${escapeHtml(selection.rule || 'Hệ thống chọn chu kỳ đang hoạt động phù hợp nhất.')}</p>`
      : `<p class="joyokr-cycle-notice__rule">Bạn đã chọn chu kỳ này${shown ? '' : ' (không thuộc các chu kỳ đang hoạt động)'}. Dùng nút chọn chu kỳ để đổi.</p>`;
    return `<div class="joyokr-alert joyokr-cycle-notice" role="note" data-cycle-notice>
      <p class="joyokr-cycle-notice__head"><strong>Có ${number(active.length)} chu kỳ đang hoạt động.</strong> Đang hiển thị: <strong>${escapeHtml(shownName)}</strong>.</p>
      <ul class="joyokr-cycle-notice__list">${items}</ul>
      ${reason}
    </div>`;
  }

  /**
   * Departments, teams and people for the filter selects, every page of each
   * (a company with more than 100 people or departments must still see them
   * all). A failed list only empties its select and is named in `failed`, so
   * the page can say it could not load it instead of looking like 'no data'.
   * An expired session still fails the page (login redirect).
   */
  async function filterLists() {
    const failed = [];
    const itemsOf = (label, promise) => promise.then((result) => result?.items || []).catch((error) => {
      if (error?.status === 401) throw error;
      failed.push(label);
      return [];
    });
    const [departments, teams, people] = await Promise.all([
      itemsOf('phòng ban', api().request('departments', { query: {}, allPages: true })),
      itemsOf('team', api().request('teams', { query: { status: 'active' }, allPages: true })),
      itemsOf('nhân viên', api().request('people', { query: {}, allPages: true })),
    ]);
    return { departments, teams, people, failed };
  }

  /** Names the filter lists that could not be loaded (not the same as 'none exist'). */
  function filterFailureNotice(lists = {}) {
    const failed = Array.isArray(lists.failed) ? lists.failed : [];
    if (!failed.length) return '';
    return `<div class="joyokr-alert" role="alert" data-filter-failure>Không tải được danh sách ${escapeHtml(failed.join(', '))} cho bộ lọc. Hãy tải lại trang; nếu vẫn lỗi, báo quản trị hệ thống.</div>`;
  }

  /** 'Nothing matches the filters' only when a filter is set; otherwise say there is nothing yet. */
  function emptyObjectivesMessage(query = {}) {
    const filtered = ['search', 'q', 'scope', 'department_id', 'team_id', 'owner_user_id', 'approval_status', 'cycle_id']
      .some((key) => query[key] !== undefined && query[key] !== '');
    return filtered
      ? 'Chưa có Objective phù hợp với bộ lọc.'
      : 'Chưa có Objective nào bạn được xem. Objective của người khác chỉ hiện sau khi được duyệt.';
  }

  /**
   * JOYOKR's report toolbar (scope, department, team, owner, Lọc), shared by
   * the dashboard and the report. The hidden cycle_id keeps a cycle the user
   * picked; without one the server keeps choosing by its default rule.
   */
  function objectiveFilterToolbar(route, query, lists = {}, extra = '') {
    return `${filterFailureNotice(lists)}<form class="joyokr-toolbar" data-filter-form data-filter-route="${escapeAttr(route)}"><select class="joyokr-select" name="scope"><option value="">Tất cả cấp</option>${options(SCOPE_VALUES, query.scope)}</select>${entityOptions('department_id', 'Tất cả phòng ban', lists.departments || [], query.department_id, 'name')}${entityOptions('team_id', 'Tất cả team', lists.teams || [], query.team_id, 'name')}${entityOptions('owner_user_id', 'Tất cả owner', lists.people || [], query.owner_user_id, 'display_name')}${extra}<input type="hidden" name="cycle_id" value="${escapeAttr(query.cycle_id || '')}"><button class="joyokr-button" type="submit">Lọc</button></form>`;
  }

  /** Applied filters (api.js mapFilters) as human-readable [label, value] pairs. */
  function describeFilters(filters = {}, lists = {}) {
    const nameOf = (items, id) => {
      const found = (items || []).find((item) => Number(item.id) === Number(id));
      return found ? String(found.name || found.display_name || `#${id}`) : `#${id}`;
    };
    const pairs = [];
    if (!filters || typeof filters !== 'object') return pairs;
    if (filters.scope) pairs.push(['Cấp', labels[filters.scope] || String(filters.scope)]);
    if (filters.department_id) pairs.push(['Phòng ban', nameOf(lists.departments, filters.department_id)]);
    if (filters.team_id) pairs.push(['Team', nameOf(lists.teams, filters.team_id)]);
    if (filters.owner_user_id) pairs.push(['Owner', nameOf(lists.people, filters.owner_user_id)]);
    if (filters.health) pairs.push(['Trạng thái', labels[filters.health] || String(filters.health)]);
    if (filters.status) pairs.push(['Check-in', CHECKIN_STATUS[filters.status]?.label || String(filters.status)]);
    return pairs;
  }

  /** Plain text; callers escape it. */
  function filterSummaryText(pairs) {
    return pairs.length ? pairs.map(([label, value]) => `${label}: ${value}`).join(' · ') : 'Không có';
  }

  function checkinStatusBadge(status) {
    const known = CHECKIN_STATUS[status];
    return known ? toneBadge(known.label, known.tone) : '';
  }

  /** "0% – 30%" from a rating band whose min/max are scores (0.0 – 1.0). */
  function ratingRange(band) {
    const bound = (value) => (String(value ?? '').trim() !== '' && Number.isFinite(Number(value)) ? `${number(Number(value) * 100)}%` : '—');
    return `${bound(band?.min)} – ${bound(band?.max)}`;
  }

  /**
   * Report header shown only when printing (body.joyokr-print-report). Its
   * data-report-* attributes also give the CSV its context lines; app.js
   * fills data-report-exported-at at export/print time.
   */
  function reportPrintHeader(cycle, filterPairs) {
    const cycleText = cycleSubtitle(cycle);
    const filters = filterPairs.length
      ? filterPairs.map(([label, value]) => `<li data-report-filter data-label="${escapeAttr(label)}" data-value="${escapeAttr(value)}">${escapeHtml(label)}: ${escapeHtml(value)}</li>`).join('')
      : '<li>Không có (mọi OKR đã duyệt bạn được xem)</li>';
    return `<header class="joyokr-print-header" data-report-meta data-report-title="Báo cáo OKR" data-report-cycle="${escapeAttr(cycleText)}" data-report-cycle-name="${escapeAttr(cycle?.name || '')}">
      <span class="joyokr-eyebrow">JOYDIGI OKR</span>
      <h2>Báo cáo OKR</h2>
      <dl class="joyokr-kv"><dt>Chu kỳ</dt><dd>${escapeHtml(cycleText)}</dd><dt>Bộ lọc</dt><dd><ul class="joyokr-print-header__filters">${filters}</ul></dd><dt>Xuất lúc</dt><dd data-report-exported-at>—</dd></dl>
    </header>`;
  }

  function okrToolbar(route, entities = {}) {
    const query = currentQuery();
    const organizationFilters = entities.departments || entities.teams || entities.people
      ? `${entityOptions('department_id', 'Tất cả phòng ban', entities.departments || [], query.department_id, 'name')}${entityOptions('team_id', 'Tất cả team', entities.teams || [], query.team_id, 'name')}${entityOptions('owner_user_id', 'Tất cả người phụ trách', entities.people || [], query.owner_user_id, 'display_name')}`
      : '';
    return `<form class="joyokr-toolbar" data-filter-form data-filter-route="${escapeAttr(route)}"><input class="joyokr-search" type="search" name="search" value="${escapeAttr(query.search || '')}" placeholder="Tìm Objective…"><select class="joyokr-select" name="scope"><option value="">Tất cả cấp</option>${options(['company', 'department', 'team', 'personal'], query.scope)}</select>${organizationFilters}<select class="joyokr-select" name="approval_status"><option value="">Tất cả trạng thái</option>${options(['draft', 'submitted', 'approved', 'rejected'], query.approval_status)}</select><button class="joyokr-button" type="submit">Lọc</button></form>`;
  }

  function pagination(result) {
    const pages = Math.max(1, Math.trunc(Number(result?.total_pages)) || 1);
    if (!result || pages <= 1) return '';
    const current = Math.min(pages, Math.max(1, Math.trunc(Number(result.page)) || 1));
    return `<nav class="joyokr-pagination" aria-label="Phân trang"><button class="joyokr-button joyokr-button--small" data-action="change-page" data-page="${Math.max(1, current - 1)}" ${current <= 1 ? 'disabled' : ''}>Trước</button><span>Trang ${number(current)}/${number(pages)}</span><button class="joyokr-button joyokr-button--small" data-action="change-page" data-page="${Math.min(pages, current + 1)}" ${current >= pages ? 'disabled' : ''}>Sau</button></nav>`;
  }

  function empty(title, message) {
    return `<div class="joyokr-empty"><div class="joyokr-empty__icon">◎</div><h2>${escapeHtml(title)}</h2><p>${escapeHtml(message)}</p></div>`;
  }

  function avatar(user, large = false) {
    const name = user?.display_name || user?.user_name || '?';
    const imagePath = safeImagePath(user?.avatar_url);
    const content = imagePath ? `<img src="${escapeAttr(imagePath)}" alt="">` : escapeHtml(String(name).trim().slice(0, 2).toUpperCase());
    return `<span class="joyokr-avatar ${large ? 'joyokr-avatar--lg' : 'joyokr-avatar--sm'}">${content}</span>`;
  }

  function options(values, selected) {
    return values.map((value) => `<option value="${escapeAttr(value)}" ${value === selected ? 'selected' : ''}>${escapeHtml(labels[value] || value)}</option>`).join('');
  }

  function entityOptions(name, placeholder, items = [], selected = '') {
    return `<select class="joyokr-select" name="${escapeAttr(name)}"><option value="">${escapeHtml(placeholder)}</option>${items.map((item) => `<option value="${escapeAttr(item.id)}" ${Number(item.id) === Number(selected) ? 'selected' : ''}>${escapeHtml(item.name || item.display_name)}</option>`).join('')}</select>`;
  }

  function activityLabel(action) {
    return ({ checked_in: 'đã check-in', approved: 'đã duyệt', submitted: 'đã gửi duyệt', rejected: 'đã từ chối', commented: 'đã bình luận', evidence_added: 'đã thêm bằng chứng', cycle_cloned: 'đã clone chu kỳ' })[action] || action;
  }

  function health(progress, expected) {
    return globalScope.JoyOKRDomain.status(Number(progress), Number(expected), warningThreshold());
  }

  /** The company's at-risk threshold (JoyOKRConfig.warningThreshold via api.js; 15 if unset). */
  function warningThreshold() {
    return typeof api()?.warningThreshold === 'function' ? api().warningThreshold() : 15;
  }

  function currentQuery() {
    return Object.fromEntries(new URLSearchParams(globalScope.location.search));
  }

  function updateNotificationCount(count) {
    const element = document.querySelector('[data-notification-count]');
    if (!element) return;
    element.textContent = String(count);
    element.hidden = Number(count) === 0;
  }

  function updateApprovalCount(count) {
    const element = document.querySelector('[data-approval-count]');
    if (!element) return;
    element.textContent = String(count);
    element.hidden = Number(count) === 0;
  }

  function can(capability) {
    return (config().capabilities || []).includes(capability);
  }

  /** Plain text; page() escapes it. */
  function cycleSubtitle(cycle) {
    return cycle ? `${cycle.name ?? ''} · ${date(cycle.start_date)} – ${date(cycle.end_date)}` : 'Chưa có chu kỳ';
  }

  function clamp(value) {
    return Math.min(100, Math.max(0, Number(value) || 0));
  }

  function number(value) {
    return new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 2 }).format(Number(value) || 0);
  }

  function date(value) {
    if (!value) return '—';
    const parsed = new Date(`${String(value).slice(0, 10)}T00:00:00`);
    return Number.isNaN(parsed.valueOf()) ? '—' : new Intl.DateTimeFormat('vi-VN').format(parsed);
  }

  /**
   * Date-time from the API. Django sends ISO 8601 with an offset
   * ("2026-11-05T09:30:12.345678+07:00"); JOYOKR's MySQL "YYYY-MM-DD HH:MM:SS"
   * strings had no zone and were UTC. Fractions are cut to milliseconds so
   * every browser parses them. Returns a Date or null.
   */
  function parseDateTime(value) {
    if (value === null || value === undefined || value === '') return null;
    if (value instanceof Date) return Number.isNaN(value.valueOf()) ? null : value;
    const match = String(value).trim().match(/^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(:\d{2})?(\.\d+)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$/i);
    if (!match) return null;
    const [, day, hoursMinutes, seconds = ':00', fraction = '', zone = ''] = match;
    if (!hoursMinutes) {
      const localMidnight = new Date(`${day}T00:00:00`);
      return Number.isNaN(localMidnight.valueOf()) ? null : localMidnight;
    }
    const millis = fraction ? `.${fraction.slice(1, 4).padEnd(3, '0')}` : '';
    let offset = 'Z';
    if (zone && zone.toUpperCase() !== 'Z') {
      const digits = zone.replace(':', '');
      offset = `${digits.slice(0, 3)}:${(digits.slice(3) || '00').padEnd(2, '0')}`;
    }
    const parsed = new Date(`${day}T${hoursMinutes}${seconds}${millis}${offset}`);
    return Number.isNaN(parsed.valueOf()) ? null : parsed;
  }

  /** Calendar date of an API date-time in the browser time zone ("dd/mm/yyyy"). */
  function dateOnly(value) {
    const parsed = parseDateTime(value);
    return parsed ? new Intl.DateTimeFormat('vi-VN').format(parsed) : '—';
  }

  /** Same display as JOYOKR ("dd/mm/yyyy, hh:mm" in vi-VN, browser time zone). */
  function dateTime(value) {
    const parsed = parseDateTime(value);
    return parsed ? new Intl.DateTimeFormat('vi-VN', { dateStyle: 'short', timeStyle: 'short' }).format(parsed) : '—';
  }

  const HTML_ESCAPES = Object.freeze({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' });

  /** Escapes text for HTML element content and quoted attribute values. */
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"'`]/g, (character) => HTML_ESCAPES[character]);
  }

  function escapeAttr(value) {
    return escapeHtml(value);
  }

  function camelToKebab(value) {
    return value.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
  }

  /** Removes tag-like sequences as text; never parses the value as HTML. */
  function stripTags(value) {
    return String(value ?? '')
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/<\/?[A-Za-z][^<>]*>/g, '');
  }

  function raw(value) {
    return { joyokrHtml: true, value };
  }

  /** Positive integer id for route attributes, "0" for anything else. */
  function routeId(value) {
    const id = Number(value);
    return Number.isSafeInteger(id) && id > 0 ? String(id) : '0';
  }

  /** Absolute http(s) URL, else '' (blocks javascript:, data:, vbscript: ...). */
  function safeHttpUrl(value) {
    const text = String(value ?? '').trim();
    if (!/^https?:\/\//i.test(text)) return '';
    try {
      const url = new URL(text);
      return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
    } catch {
      return '';
    }
  }

  /** Same-origin API download path ("/api/okr/evidence/12/download/"), else ''. */
  function safeDownloadPath(value) {
    const text = String(value ?? '');
    return /^\/(?!\/)[A-Za-z0-9/_-]+\/download\/$/.test(text) && !text.includes('..') ? text : '';
  }

  function safeImagePath(value) {
    const text = String(value ?? '');
    return /^\/(?!\/)[A-Za-z0-9/_.-]+$/.test(text) && !text.includes('..') ? text : '';
  }

  namespace.views = Object.freeze({
    render,
    renderers,
    renderLazyCheckinChildren,
    helpers: Object.freeze({
      escapeHtml, escapeAttr, stripTags, parseDateTime, dateTime, date, routeId, safeHttpUrl, safeDownloadPath, page, raw, evidenceList,
      cycleNotice, describeFilters, checkinStatusBadge, ratingRange, attentionCapNote,
    }),
  });
})(globalThis);
