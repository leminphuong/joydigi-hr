(function registerForms(globalScope) {
  'use strict';

  const namespace = globalScope.JoyOKR = globalScope.JoyOKR || {};
  // Picker lists load at most 20 pages of 100 (people, objectives, ...).
  const MAX_PICKER_PAGES = 20;
  let activeDefinition = null;
  let opener = null;

  // Users, profile, password and avatar forms are gone: the HR web owns them.
  // Departments come from HR too: OKR only assigns their OKR manager
  // ("department-manager"), and grants its own roles ("role").
  const formNames = Object.freeze([
    'objective', 'key-result', 'checkin', 'comment', 'evidence', 'department-manager', 'team', 'team-members',
    'cycle', 'choose-cycle', 'clone', 'role', 'settings', 'approval', 'rejection', 'confirmation',
  ]);

  /** Opens and hydrates an accessible application form dialog. */
  async function open(type, context = {}) {
    opener = document.activeElement;
    const dialog = getDialog();
    renderDialog('Đang tải…', '<div class="joyokr-loading"><span class="joyokr-spinner"></span><span>Đang tải biểu mẫu…</span></div>', false);
    if (!dialog.open) dialog.showModal();

    try {
      activeDefinition = await buildDefinition(type, context);
      renderForm(activeDefinition);
    } catch (error) {
      renderDialog('Không thể mở biểu mẫu', `<div class="joyokr-alert joyokr-alert--error">${escapeHtml(error.message || 'Đã có lỗi xảy ra.')}</div>`, true);
    }
  }

  /** Closes the active dialog and restores focus to its opener. */
  function close() {
    const dialog = getDialog();
    if (dialog.open) dialog.close();
    activeDefinition = null;
    if (opener instanceof HTMLElement) opener.focus();
  }

  /** Dispatches delegated page actions from the app shell. */
  async function handleAction(action, trigger) {
    try {
      if (action === 'switch-tab') return switchTab(trigger);
      if (action === 'change-page') return changePage(trigger.dataset.page);
      if (action === 'save-settings') return await submitSettings();
      if (action === 'cycle-status') return await changeCycleStatus(trigger);
      if (action === 'restore-team') return await confirmMutation('Khôi phục team?', 'Team sẽ xuất hiện lại trong các danh sách đang hoạt động.', `teams/${trigger.dataset.id}/restore`, 'POST');
      if (action === 'delete-team') return await confirmMutation('Ẩn team?', 'Team sẽ bị ẩn khỏi các danh sách đang hoạt động. Thành viên và Objective/KR hiện có vẫn được giữ nguyên; có thể khôi phục lại sau.', `teams/${trigger.dataset.id}`, 'DELETE');
      if (action === 'revoke-role') return await revokeRole(trigger);
      if (action === 'read-notification') return await mutate(`notifications/${trigger.dataset.id}/read`, 'POST', {}, 'Đã đọc thông báo.');
      if (action === 'read-all-notifications') return await mutate('notifications/read-all', 'POST', {}, 'Đã đọc tất cả thông báo.');
      if (action === 'normalize-kr-weights') return await confirmMutation('Chia đều trọng số KR?', 'Hệ thống sẽ chia tổng 100% đều cho các Key Result hiện có.', `objectives/${trigger.dataset.id}/normalize-kr-weights`, 'POST', undefined, 'confirmation', { message: 'Đã chia đều trọng số Key Result.' });
      if (action === 'delete-objective' || action === 'delete-key-result') return await deleteResource(action, trigger.dataset.id, trigger);
      if (/^objective-(submit|approve|reject)$/.test(action)) return await workflow(action, trigger.dataset.id);

      const mappings = {
        'open-objective': ['objective', {}], 'edit-objective': ['objective', { id: trigger.dataset.id }],
        'open-key-result': ['key-result', { objectiveId: trigger.dataset.objectiveId }], 'edit-key-result': ['key-result', { id: trigger.dataset.id }],
        'open-checkin': ['checkin', { id: trigger.dataset.id }],
        'open-comment': ['comment', { objectType: trigger.dataset.objectType, objectId: trigger.dataset.objectId }],
        'open-evidence': ['evidence', { id: trigger.dataset.id }],
        'choose-cycle': ['choose-cycle', { id: trigger.dataset.id }],
        'open-team': ['team', {}], 'edit-team': ['team', { id: trigger.dataset.id }],
        'team-members': ['team-members', { id: trigger.dataset.id }],
        'open-cycle': ['cycle', {}], 'edit-cycle': ['cycle', { id: trigger.dataset.id }],
        'clone-cycle': ['clone', {}],
        'assign-department-manager': ['department-manager', { id: trigger.dataset.id }],
        'open-role': ['role', {}],
      };
      if (mappings[action]) return await open(...mappings[action]);
    } catch (error) {
      namespace.notify(actionErrorMessage(error), 'error');
    }
  }

  function actionErrorMessage(error) {
    const fieldMessage = Object.values(error?.fields || {}).find((message) => typeof message === 'string' && message.trim());
    return fieldMessage || error?.message || 'Không thể thực hiện thao tác.';
  }

  async function buildDefinition(type, context) {
    switch (type) {
      case 'objective': return objectiveForm(context);
      case 'key-result': return keyResultForm(context);
      case 'checkin': return checkinForm(context);
      case 'comment': return commentForm(context);
      case 'evidence': return evidenceForm(context);
      case 'department-manager': return departmentManagerForm(context);
      case 'team': return teamForm(context);
      case 'team-members': return teamMembersForm(context);
      case 'cycle': return cycleForm(context);
      case 'choose-cycle': return chooseCycleForm(context);
      case 'clone': return cloneForm(context);
      case 'role': return roleForm(context);
      case 'rejection': return rejectionForm(context);
      default: throw new Error('Biểu mẫu không được hỗ trợ.');
    }
  }

  async function objectiveForm(context) {
    const [cycles, departments, teams, users, objectives, item] = await Promise.all([
      all('cycles'), all('departments', { status: 'active' }), all('teams', { status: 'active' }), all('people'),
      all('objectives'), context.id ? namespace.api.request(`objectives/${context.id}`) : Promise.resolve(null),
    ]);
    const currentUser = globalScope.JoyOKRConfig.currentUser;
    const capabilities = new Set(currentUser.capabilities || []);
    const isAdmin = capabilities.has('joyokr_manage_settings');
    const isManager = capabilities.has('joyokr_edit_managed');
    const scopeChoices = isAdmin ? ['company', 'department', 'team', 'personal'] : (isManager ? ['department', 'team', 'personal'] : ['personal']);
    const ownerChoices = (isAdmin || isManager) ? users : users.filter((candidate) => Number(candidate.id) === Number(currentUser.id));
    // Django's objective detail carries department/team names but not their
    // ids, and no priority: recover the ids by name, default priority to medium.
    const value = item
      ? { ...item, department_id: item.department_id ?? idByName(departments, item.department_name), team_id: item.team_id ?? idByName(teams, item.team_name), priority: item.priority || 'medium' }
      : { cycle_id: currentQuery().cycle_id || defaultCycleId(cycles), owner_user_id: currentUser.id, scope: 'personal', weight: 100, priority: 'medium' };
    return {
      type: 'objective', initialize: (form) => {
        const parent = form.elements.parent_objective_id;
        const departmentField = form.elements.department_id;
        const teamField = form.elements.team_id;
        const activeDepartmentIds = new Set(departments.map((department) => Number(department.id)));
        const eligibleTeams = teams.filter((team) => activeDepartmentIds.has(Number(team.department_id)));
        const refreshParents = () => {
          const selected = parent.value;
          const levels = { company: 0, department: 1, team: 2, personal: 3 };
          const choices = objectives.filter((candidate) => Number(candidate.id) !== Number(value.id) && Number(candidate.cycle_id) === Number(form.elements.cycle_id.value) && levels[candidate.scope] < levels[form.elements.scope.value]);
          parent.replaceChildren(new Option('— Chọn —', ''), ...choices.map((candidate) => new Option(candidate.title, candidate.id)));
          parent.value = choices.some((candidate) => String(candidate.id) === selected) ? selected : '';
        };
        const refreshTeams = () => {
          const selected = teamField.value;
          const departmentId = Number(departmentField.value || 0);
          const choices = eligibleTeams.filter((candidate) => !departmentId || Number(candidate.department_id) === departmentId);
          teamField.replaceChildren(new Option('— Chọn —', ''), ...choices.map((candidate) => new Option(candidate.name, candidate.id)));
          teamField.value = choices.some((candidate) => String(candidate.id) === selected) ? selected : '';
        };
        const syncObjectiveScopeFields = () => {
          const scope = form.elements.scope.value;
          departmentField.required = scope === 'department';
          teamField.required = scope === 'team';
          if (scope === 'company' || scope === 'personal') {
            departmentField.value = '';
            teamField.value = '';
          } else if (scope === 'department') {
            teamField.value = '';
          }
          refreshTeams();
          refreshParents();
        };
        form.elements.cycle_id.addEventListener('change', refreshParents);
        form.elements.scope.addEventListener('change', syncObjectiveScopeFields);
        departmentField.addEventListener('change', refreshTeams);
        refreshTeams();
        syncObjectiveScopeFields();
        if (item && !isAdmin) ['cycle_id', 'scope', 'department_id', 'team_id', 'owner_user_id'].forEach((name) => { form.elements[name].disabled = true; });
      }, title: item ? 'Sửa Objective' : 'Tạo Objective', method: item ? 'PUT' : 'POST', path: item ? `objectives/${item.id}` : 'objectives',
      body: `<p class="joyokr-muted">Objective là mục tiêu định tính muốn đạt. Các con số đo lường đặt ở Key Result bên dưới. Mỗi Objective nên có 2–4 KR, tổng trọng số 100%.</p>${field('title', 'Tên Objective', value.title, { required: true, help: 'Ví dụ: Nâng chất lượng hệ sinh thái game H5.' })}${textarea('description', 'Mô tả', value.description)}
        <div class="joyokr-grid joyokr-grid--2">${select('cycle_id', 'Chu kỳ', cycles, value.cycle_id, 'id', 'name', true)}${select('owner_user_id', 'Owner', ownerChoices, value.owner_user_id, 'id', 'display_name', true)}</div>
        <div class="joyokr-grid joyokr-grid--2">${selectValues('scope', 'Cấp OKR', scopeChoices, value.scope)}${numberField('weight', 'Trọng số (%)', value.weight, 0, 100, 1, 'Tỷ trọng khi tổng hợp các Objective cùng cấp trong cùng nhóm/Objective cha.')}</div>
        <div class="joyokr-grid joyokr-grid--2">${select('department_id', 'Phòng ban', departments, value.department_id, 'id', 'name')}${select('team_id', 'Team', teams, value.team_id, 'id', 'name')}</div>
        <div class="joyokr-grid joyokr-grid--2">${select('parent_objective_id', 'Aligned với', objectives.filter((candidate) => Number(candidate.id) !== Number(value.id)), value.parent_objective_id, 'id', 'title')}${selectValues('priority', 'Ưu tiên', ['low', 'medium', 'high'], value.priority)}</div>`,
      submit: (form) => namespace.api.request(item ? `objectives/${item.id}` : 'objectives', { method: item ? 'PUT' : 'POST', body: formObject(form) }),
    };
  }

  async function keyResultForm(context) {
    const usersPromise = all('people');
    const item = context.id ? await namespace.api.request(`key-results/${context.id}`) : null;
    const objectiveId = item?.objective_id || context.objectiveId;
    const objective = objectiveId ? await namespace.api.request(`objectives/${objectiveId}`) : null;
    const users = await usersPromise;
    const defaultWeight = remainingKrWeight(objective);
    const unitSuggestions = ['%', 'App', 'Game', 'Website', 'User', 'VNĐ', 'USD', 'Lượt', 'Bài', 'Extension'];
    const value = item
      ? { ...item, priority: item.priority || 'medium' }
      : { objective_id: context.objectiveId, owner_user_id: globalScope.JoyOKRConfig.currentUser.id, unit: '%', start_value: 0, current_value: 0, target_value: 100, weight: defaultWeight, priority: 'medium', due_date: objective?.end_date || '' };
    return {
      type: 'key-result', isNew: !item, title: item ? 'Sửa Key Result' : 'Tạo Key Result',
      body: `<p class="joyokr-muted">Key Result là kết quả đo được, không phải danh sách đầu việc. Bắt đầu là số thực tế đầu chu kỳ; Hiện tại ban đầu bằng Bắt đầu. Ví dụ: Hoàn thiện 10 game chạy ổn định trên web — đơn vị: game.</p>${hidden('objective_id', value.objective_id)}${field('title', 'Tên Key Result', value.title, { required: true })}${textarea('description', 'Mô tả', value.description)}${select('owner_user_id', 'Owner', users, value.owner_user_id, 'id', 'display_name', true)}
        <div class="joyokr-grid joyokr-grid--3">${numberField('start_value', 'Bắt đầu', value.start_value, undefined, undefined, 'any')}${numberField('current_value', 'Hiện tại', value.current_value, undefined, undefined, 'any')}${numberField('target_value', 'Mục tiêu', value.target_value, undefined, undefined, 'any')}</div>
        <div class="joyokr-grid joyokr-grid--3">${field('unit', 'Đơn vị', value.unit, { required: true, list: 'joyokr-unit-options' })}${numberField('weight', 'Trọng số (%)', value.weight, 0.01, 100, 0.01)}${selectValues('priority', 'Ưu tiên', ['low', 'medium', 'high'], value.priority)}</div><datalist id="joyokr-unit-options">${unitSuggestions.map((unit) => `<option value="${escapeAttr(unit)}"></option>`).join('')}</datalist>${field('due_date', 'Hạn hoàn thành', value.due_date, { type: 'date' })}`,
      submit: (form) => namespace.api.request(item ? `key-results/${item.id}` : 'key-results', { method: item ? 'PUT' : 'POST', body: formObject(form) }),
    };
  }

  async function checkinForm(context) {
    const item = await namespace.api.request(`key-results/${context.id}`);
    // One idempotency key per opened form: a manual retry of the same form
    // (after a timeout or network error) reuses it, so Django returns the
    // check-in it may already have stored instead of creating a second one.
    const clientRequestId = namespace.api.newClientRequestId();
    return {
      type: 'checkin', title: 'Check-in Key Result', submitLabel: 'Lưu check-in', clientRequestId,
      body: `<p class="joyokr-muted">${escapeHtml(item.title)}</p><p class="joyokr-muted">Cập nhật trước 10h thứ Hai hằng tuần. Ghi số liệu thực tế, kết quả đã làm, trở ngại (ghi Không nếu không có) và bước tiếp theo. Confidence 1–5 là mức tự tin đạt mục tiêu.</p>${numberField('current_value', `Giá trị hiện tại (${item.unit})`, item.current_value, undefined, undefined, 0.01)}${selectValues('confidence', 'Confidence', ['1', '2', '3', '4', '5'], '3')}${textarea('update_text', 'Kết quả đã làm', '', true)}${textarea('blockers', 'Vấn đề / trở ngại')}${textarea('next_steps', 'Bước tiếp theo')}`,
      submit: async (form) => {
        const result = await namespace.api.request(`key-results/${context.id}/checkins`, { method: 'POST', body: { ...formObject(form), client_request_id: clientRequestId } });
        return { ...result, message: 'Đã lưu check-in.' };
      },
    };
  }

  async function commentForm(context) {
    return {
      type: 'comment', title: 'Thêm bình luận', submitLabel: 'Gửi bình luận',
      body: `${hidden('object_type', context.objectType)}${hidden('object_id', context.objectId)}${textarea('content', 'Nội dung', '', true)}`,
      submit: (form) => namespace.api.request('comments', { method: 'POST', body: formObject(form) }),
    };
  }

  async function evidenceForm(context) {
    return {
      type: 'evidence', title: 'Thêm bằng chứng', submitLabel: 'Thêm bằng chứng', multipart: true,
      body: `${hidden('key_result_id', context.id)}${field('label', 'Tên bằng chứng', '', { required: true })}${field('url', 'Liên kết (nếu có)', '', { type: 'url', help: 'Dùng liên kết hoặc chọn một tệp bên dưới.' })}${field('file', 'Tệp bằng chứng', '', { type: 'file', accept: '.jpg,.jpeg,.png,.webp,.pdf,.doc,.docx,.xls,.xlsx', help: 'Tối đa 10 MB.' })}`,
      submit: (form) => {
        const body = new FormData(form);
        const file = body.get('file');
        if (file instanceof File && file.size > 10 * 1024 * 1024) throw new Error('Tệp không được vượt quá 10 MB.');
        return namespace.api.request('evidence', { method: 'POST', body });
      },
    };
  }

  /** People for a picker, labelled with their HR department so namesakes can be told apart. */
  function personChoices(people) {
    return people.map((person) => ({ id: person.id, label: person.department_name ? `${person.display_name} · ${person.department_name}` : person.display_name }));
  }

  /** HR owns departments: OKR only names (or clears) each department's OKR manager. */
  async function departmentManagerForm(context) {
    const [people, departments] = await Promise.all([all('people'), all('departments')]);
    const department = departments.find((candidate) => Number(candidate.id) === Number(context.id));
    if (!department) throw new Error('Không tìm thấy phòng ban.');
    return {
      type: 'department-manager', title: 'Gán quản lý OKR',
      body: `<p class="joyokr-muted">Phòng ban <strong>${escapeHtml(department.name)}</strong> do Web HR quản lý; ở đây chỉ chọn người quản lý OKR của phòng ban.</p>${select('manager_user_id', 'Quản lý OKR', personChoices(people), department.manager_user_id, 'id', 'label', false, '— Không gán —')}`,
      submit: (form) => namespace.api.request(`departments/${department.id}/okr-manager`, { method: 'PUT', body: formObject(form) }),
    };
  }

  async function teamForm(context) {
    const [departments, people, item] = await Promise.all([
      all('departments'), all('people'),
      context.id ? namespace.api.request(`teams/${context.id}`) : Promise.resolve(null),
    ]);
    return {
      type: 'team', title: item ? 'Sửa team' : 'Tạo team',
      body: `${field('name', 'Tên team', item?.name, { required: true })}${select('department_id', 'Phòng ban', departments, item?.department_id, 'id', 'name', false, '— Không thuộc phòng ban —')}${select('manager_user_id', 'Manager', personChoices(people), item?.manager_user_id, 'id', 'label', false, '— Chưa gán —')}${textarea('description', 'Mô tả', item?.description)}`,
      submit: (form) => namespace.api.request(item ? `teams/${item.id}` : 'teams', { method: item ? 'PUT' : 'POST', body: formObject(form) }),
    };
  }

  async function teamMembersForm(context) {
    const [people, team] = await Promise.all([all('people'), namespace.api.request(`teams/${context.id}`)]);
    const selected = new Set((team?.members || []).map((member) => Number(member.id)));
    const choices = people.map((person) => `<label class="joyokr-check joyokr-choice" data-member-choice data-member-name="${escapeAttr(fold(person.display_name))}"><input type="checkbox" name="user_ids" value="${escapeAttr(person.id)}" ${selected.has(Number(person.id)) ? 'checked' : ''}>${escapeHtml(person.display_name)}${person.department_name ? ` <small>${escapeHtml(person.department_name)}</small>` : ''}</label>`).join('');
    return {
      type: 'team-members', title: `Thành viên · ${team?.name || ''}`,
      body: `<label class="joyokr-field"><span>Lọc theo tên</span><input type="search" data-member-filter placeholder="Nhập tên nhân sự…" autocomplete="off"></label><div class="joyokr-choice-list">${choices || '<p class="joyokr-muted">Chưa có nhân sự đang làm việc.</p>'}</div><p class="joyokr-muted" data-member-empty hidden>Không có nhân sự phù hợp.</p><small class="joyokr-field__error" data-field-error="user_ids"></small>`,
      initialize: (form) => {
        // Client-side only: hidden choices stay checked and are still saved.
        const filter = form.querySelector('[data-member-filter]');
        const options = [...form.querySelectorAll('[data-member-choice]')];
        const emptyNote = form.querySelector('[data-member-empty]');
        filter?.addEventListener('input', () => {
          const needle = fold(filter.value);
          let visible = 0;
          options.forEach((option) => {
            const match = !needle || String(option.dataset.memberName || '').includes(needle);
            option.hidden = !match;
            if (match) visible += 1;
          });
          if (emptyNote) emptyNote.hidden = visible > 0 || !options.length;
        });
      },
      submit: (form) => namespace.api.request(`teams/${context.id}/members`, { method: 'PUT', body: { user_ids: new FormData(form).getAll('user_ids').map(Number) } }),
    };
  }

  async function cycleForm(context) {
    const [cycles, item] = await Promise.all([
      all('cycles'),
      context.id ? namespace.api.request(`cycles/${context.id}`) : Promise.resolve(null),
    ]);
    const typeLabels = { month: 'Tháng', quarter: 'Quý', year: 'Năm', custom: 'Tùy chỉnh' };
    // JOYOKR asked for the parent's numeric id; pick another cycle by name instead.
    const parents = cycles
      .filter((cycle) => Number(cycle.id) !== Number(item?.id))
      .map((cycle) => ({ id: cycle.id, label: `${cycle.name} · ${typeLabels[cycle.cycle_type] || cycle.cycle_type || ''}` }));
    return {
      type: 'cycle', title: item ? 'Sửa chu kỳ' : 'Tạo chu kỳ',
      body: `${field('name', 'Tên chu kỳ', item?.name, { required: true })}${selectValues('cycle_type', 'Loại chu kỳ', ['month', 'quarter', 'year', 'custom'], item?.cycle_type || 'month')}<div class="joyokr-grid joyokr-grid--2">${field('start_date', 'Ngày bắt đầu', item?.start_date, { type: 'date', required: true })}${field('end_date', 'Ngày kết thúc', item?.end_date, { type: 'date', required: true })}</div>${select('parent_id', 'Chu kỳ cha', parents, item?.parent_id, 'id', 'label', false, '— Không có —')}`,
      submit: (form) => namespace.api.request(item ? `cycles/${item.id}` : 'cycles', { method: item ? 'PUT' : 'POST', body: formObject(form) }),
    };
  }

  async function cloneForm() {
    const cycles = await all('cycles');
    const labelled = (items) => items.map((cycle) => ({ id: cycle.id, label: `${cycle.name} · ${Number(cycle.objective_count) || 0} Objective` }));
    return {
      type: 'clone', title: 'Clone OKR sang chu kỳ mới', submitLabel: 'Clone OKR',
      body: `${select('source_cycle_id', 'Từ chu kỳ', labelled(cycles.filter((item) => item.status !== 'draft')), '', 'id', 'label', true)}${select('destination_cycle_id', 'Sang chu kỳ nháp', labelled(cycles.filter((item) => item.status === 'draft')), '', 'id', 'label', true)}<p class="joyokr-muted">Chu kỳ đích phải là chu kỳ nháp chưa có Objective. Progress được đặt về 0; check-in, bình luận và bằng chứng không được sao chép.</p>`,
      submit: (form) => {
        const values = formObject(form);
        return namespace.api.request(`cycles/${values.source_cycle_id}/clone`, { method: 'POST', body: { destination_cycle_id: values.destination_cycle_id } });
      },
    };
  }

  /** Grants an OKR role. OKR roles are separate from HR permissions. */
  async function roleForm() {
    const people = await all('people');
    return {
      type: 'role', title: 'Gán vai trò OKR', submitLabel: 'Gán vai trò',
      body: `<p class="joyokr-muted">Vai trò OKR tách biệt với quyền Web HR: quản trị viên HR không tự động là Quản trị OKR.</p>${select('employee_id', 'Nhân sự', personChoices(people), '', 'id', 'label', true)}${selectValues('role', 'Vai trò', ['admin', 'manager'], 'manager')}<p class="joyokr-field__help">Quản trị OKR quản lý chu kỳ, phòng ban, team, cài đặt và vai trò OKR. Quản lý OKR duyệt và theo dõi OKR trong phạm vi được giao.</p>`,
      submit: (form) => namespace.api.request('roles', { method: 'POST', body: formObject(form) }),
    };
  }

  /**
   * The cycle a new Objective goes into when none is chosen: the server's rule
   * (okr.services.cycles.active_cycle) — the active cycle covering today, the
   * latest-starting one if several do; else the newest active cycle.
   */
  function defaultCycleId(cycles, today = new Date()) {
    const day = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
    const active = (cycles || []).filter((cycle) => cycle.status === 'active')
      .sort((a, b) => String(b.start_date).localeCompare(String(a.start_date)) || Number(b.id) - Number(a.id));
    const current = active.find((cycle) => String(cycle.start_date) <= day && day <= String(cycle.end_date));
    return (current || active[0])?.id;
  }

  async function chooseCycleForm(context) {
    const cycles = await all('cycles');
    return {
      type: 'cycle', title: 'Chọn chu kỳ', submitLabel: 'Áp dụng',
      body: select('cycle_id', 'Chu kỳ', cycles, context.id, 'id', 'name', true),
      submit: async (form) => {
        const query = currentQuery();
        query.cycle_id = form.elements.cycle_id.value;
        namespace.navigate(namespace.store.getState().route, { replace: true, query });
        return { localNavigation: true };
      },
    };
  }

  async function rejectionForm(context) {
    return {
      type: 'rejection', title: 'Yêu cầu chỉnh sửa OKR', submitLabel: 'Gửi yêu cầu chỉnh sửa',
      body: `<p class="joyokr-muted">Nội dung sẽ chuyển về trạng thái Từ chối để người sở hữu chỉnh sửa và gửi duyệt lại.</p>${textarea('reason', 'Nội dung cần chỉnh sửa', '', true)}`,
      submit: (form) => namespace.api.request(`${context.endpoint}/${context.id}/reject`, { method: 'POST', body: formObject(form) }),
    };
  }

  function renderForm(definition) {
    const body = `<form data-dialog-form data-form-type="${escapeAttr(definition.type)}" ${definition.multipart ? 'enctype="multipart/form-data"' : ''} novalidate><div class="joyokr-dialog__body">${definition.body}<div class="joyokr-alert joyokr-alert--error" data-form-error role="alert" hidden></div></div><div class="joyokr-dialog__foot"><button class="joyokr-button" type="button" data-dialog-close>Hủy</button><button class="joyokr-button joyokr-button--primary" type="submit">${escapeHtml(definition.submitLabel || 'Lưu')}</button></div></form>`;
    renderDialog(definition.title, body, false);
    const form = getDialog().querySelector('[data-dialog-form]');
    definition.initialize?.(form);
    if (definition.type === 'key-result' && definition.isNew) {
      const start = form.querySelector('[name="start_value"]');
      const current = form.querySelector('[name="current_value"]');
      let manuallyChanged = false;
      current.addEventListener('input', () => { manuallyChanged = true; });
      start.addEventListener('input', () => { if (!manuallyChanged) current.value = start.value; });
    }
    getDialog().querySelector('input:not([type="hidden"]), select, textarea, button')?.focus();
  }

  function renderDialog(title, body, closable) {
    const content = getDialog().querySelector('[data-dialog-content]');
    content.innerHTML = `<header class="joyokr-dialog__head"><div><h2 id="joyokr-dialog-title">${escapeHtml(title)}</h2></div>${closable ? '<button class="joyokr-icon-button" type="button" data-dialog-close aria-label="Đóng">×</button>' : ''}</header>${body}`;
  }

  async function submitActiveForm(form) {
    if (!activeDefinition) return;
    if (!form.reportValidity()) return;
    clearFormErrors(form);
    const submitButton = form.querySelector('[type="submit"]');
    submitButton.disabled = true;
    submitButton.setAttribute('aria-busy', 'true');
    try {
      const result = await activeDefinition.submit(form);
      close();
      if (!result?.localNavigation) {
        namespace.store.invalidate();
        namespace.notify(result?.message || 'Đã lưu thay đổi.');
        refreshCurrentRoute();
      }
    } catch (error) {
      showFormError(form, error);
    } finally {
      submitButton.disabled = false;
      submitButton.removeAttribute('aria-busy');
    }
  }

  function workflow(action, id) {
    // Key Results are submitted and approved with their Objective only
    // (Django has no standalone KR submit/approve endpoint).
    const [, , operation] = action.match(/^(objective)-(submit|approve|reject)$/);
    const endpoint = 'objectives';
    if (operation === 'reject') return open('rejection', { endpoint, id });
    const operationName = operation === 'approval' ? 'approval' : operation;
    const title = operation === 'approve' ? 'Duyệt Objective và toàn bộ KR?' : 'Gửi Objective để duyệt?';
    const message = operation === 'approve'
      ? 'Nếu dữ liệu hợp lệ, Objective và toàn bộ Key Result sẽ được duyệt cùng lúc.'
      : 'Chỉ cần gửi Objective; Manager hoặc Admin sẽ duyệt Objective và toàn bộ KR trong một lần.';
    return confirmMutation(title, message, `${endpoint}/${id}/${operation}`, 'POST', {}, operationName);
  }

  function deleteResource(action, id, trigger) {
    const endpoint = ({ 'delete-objective': 'objectives', 'delete-key-result': 'key-results' })[action];
    // Leaving the detail page of the deleted item avoids reloading a 404.
    const route = namespace.store.getState().route;
    let redirect = '';
    if (action === 'delete-objective' && route === `objectives/${id}`) redirect = 'my-okrs';
    if (action === 'delete-key-result' && route === `key-results/${id}`) {
      const objectiveId = Number(trigger?.dataset?.objectiveId);
      redirect = Number.isSafeInteger(objectiveId) && objectiveId > 0 ? `objectives/${objectiveId}` : 'my-okrs';
    }
    return confirmMutation('Xác nhận xóa?', 'Dữ liệu lịch sử liên quan được giữ lại để kiểm toán.', `${endpoint}/${id}`, 'DELETE', undefined, 'confirmation', { redirect });
  }

  function changeCycleStatus(trigger) {
    const { id, status, current } = trigger.dataset;
    let title = 'Kích hoạt chu kỳ?';
    let message = 'Chu kỳ sẽ chuyển sang Đang hoạt động để nhân sự check-in OKR đã duyệt. Chu kỳ đã đóng sẽ chặn mọi cập nhật OKR.';
    if (status === 'closed') {
      title = 'Đóng chu kỳ?';
      message = 'Chu kỳ đã đóng sẽ chặn mọi cập nhật OKR: không thể sửa Objective, Key Result hay check-in cho tới khi chu kỳ được mở lại.';
    } else if (current === 'closed') {
      title = 'Mở lại chu kỳ?';
      message = 'Chu kỳ sẽ hoạt động trở lại và cho phép cập nhật OKR. Khi đóng lại, chu kỳ sẽ chặn mọi cập nhật OKR.';
    }
    return confirmMutation(title, message, `cycles/${id}/status`, 'POST', { status });
  }

  /** Revokes an OKR role; Django refuses (409 last_admin) to remove the company's last OKR admin. */
  function revokeRole(trigger) {
    const message = trigger.dataset.role === 'admin'
      ? 'Người này sẽ mất quyền Quản trị OKR. Công ty luôn phải còn ít nhất một Quản trị OKR.'
      : 'Người này sẽ mất quyền Quản lý OKR.';
    return confirmMutation('Thu hồi vai trò OKR?', message, `roles/${trigger.dataset.id}`, 'DELETE', undefined, 'confirmation', { message: 'Đã thu hồi vai trò OKR.' });
  }

  /** Saves the settings page (rating labels, threshold, reminders); field errors show inline. */
  async function submitSettings() {
    const form = document.querySelector('[data-settings-form]');
    if (!form) return;
    if (typeof form.reportValidity === 'function' && !form.reportValidity()) return;
    clearFormErrors(form);
    const values = formObject(form);
    const ratingRanges = [];
    for (let index = 0; values[`rating_${index}_label`] !== undefined; index += 1) {
      ratingRanges.push({ min: values[`rating_${index}_min`], max: values[`rating_${index}_max`], label: values[`rating_${index}_label`] });
    }
    // api.js turns these into Django's types: number threshold, boolean email_reminders, numeric bands.
    const body = {
      warning_threshold: values.warning_threshold,
      reminder_frequency: values.reminder_frequency,
      email_reminders: form.elements?.email_reminders?.checked === true,
    };
    if (ratingRanges.length) body.rating_ranges = ratingRanges;
    try {
      await mutate('settings', 'PUT', body, 'Đã lưu cài đặt.');
    } catch (error) {
      showFormError(form, error);
      throw error;
    }
  }

  async function confirmMutation(title, message, path, method, body = undefined, kind = 'confirmation', after = {}) {
    const accepted = await confirm({ title, message, confirmLabel: 'Xác nhận', kind });
    if (accepted) await mutate(path, method, body, after.message || '', after.redirect || '');
  }

  /** Shows an application confirmation and resolves to the selected decision. */
  function confirm(options) {
    activeDefinition = { type: options.kind || 'confirmation' };
    opener = document.activeElement;
    const dialog = getDialog();
    renderDialog(options.title, `<div class="joyokr-dialog__body"><p>${escapeHtml(options.message)}</p></div><div class="joyokr-dialog__foot"><button class="joyokr-button" type="button" data-confirm-value="no">Hủy</button><button class="joyokr-button joyokr-button--danger" type="button" data-confirm-value="yes">${escapeHtml(options.confirmLabel || 'Xác nhận')}</button></div>`, false);
    if (!dialog.open) dialog.showModal();
    return new Promise((resolve) => {
      dialog.querySelector('[data-confirm-value="no"]').addEventListener('click', () => { close(); resolve(false); }, { once: true });
      dialog.querySelector('[data-confirm-value="yes"]').addEventListener('click', () => { close(); resolve(true); }, { once: true });
    });
  }

  async function mutate(path, method, body, message = '', redirect = '') {
    const result = await namespace.api.request(path, { method, body });
    namespace.store.invalidate();
    namespace.notify(message || result?.message || 'Đã cập nhật.');
    if (redirect) namespace.navigate(redirect);
    else refreshCurrentRoute();
    return result;
  }

  function switchTab(trigger) {
    const target = trigger.dataset.tabTarget;
    trigger.closest('[role="tablist"]').querySelectorAll('[role="tab"]').forEach((button) => {
      const active = button === trigger;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll('[data-tab]').forEach((panel) => { panel.hidden = panel.dataset.tab !== target; });
  }

  function changePage(page) {
    const query = currentQuery();
    query.page = page;
    namespace.navigate(namespace.store.getState().route, { replace: true, query });
  }

  function refreshCurrentRoute() {
    namespace.navigate(namespace.store.getState().route, { replace: true, query: currentQuery() });
  }

  function formObject(form) {
    const result = {};
    for (const [key, value] of new FormData(form).entries()) {
      if (value instanceof File) continue;
      result[key] = value;
    }
    form.querySelectorAll('input[type="checkbox"]').forEach((input) => {
      if (!result[input.name]) result[input.name] = input.checked;
    });
    return result;
  }

  function field(name, label, value = '', options = {}) {
    const type = options.type || 'text';
    return `<label class="joyokr-field"><span>${escapeHtml(label)}${options.required ? ' <b aria-hidden="true">*</b>' : ''}</span><input type="${escapeAttr(type)}" name="${escapeAttr(name)}" value="${type === 'file' ? '' : escapeAttr(value ?? '')}" ${options.required ? 'required' : ''} ${options.accept ? `accept="${escapeAttr(options.accept)}"` : ''} ${options.list ? `list="${escapeAttr(options.list)}"` : ''} autocomplete="off">${options.help ? `<small class="joyokr-field__help">${escapeHtml(options.help)}</small>` : ''}<small class="joyokr-field__error" data-field-error="${escapeAttr(name)}"></small></label>`;
  }

  function numberField(name, label, value = '', min, max, step = 1, help = '') {
    return `<label class="joyokr-field"><span>${escapeHtml(label)}</span><input type="number" name="${escapeAttr(name)}" value="${escapeAttr(value ?? '')}" step="${escapeAttr(step)}" ${min !== undefined ? `min="${escapeAttr(min)}"` : ''} ${max !== undefined ? `max="${escapeAttr(max)}"` : ''} required>${help ? `<small class="joyokr-field__help">${escapeHtml(help)}</small>` : ''}<small class="joyokr-field__error" data-field-error="${escapeAttr(name)}"></small></label>`;
  }

  function textarea(name, label, value = '', required = false) {
    return `<label class="joyokr-field"><span>${escapeHtml(label)}${required ? ' <b aria-hidden="true">*</b>' : ''}</span><textarea name="${escapeAttr(name)}" ${required ? 'required' : ''}>${escapeHtml(value ?? '')}</textarea><small class="joyokr-field__error" data-field-error="${escapeAttr(name)}"></small></label>`;
  }

  function select(name, label, items, selected, valueKey, labelKey, required = false, placeholder = '— Chọn —') {
    return `<label class="joyokr-field"><span>${escapeHtml(label)}</span><select name="${escapeAttr(name)}" ${required ? 'required' : ''}><option value="">${escapeHtml(placeholder)}</option>${items.map((item) => `<option value="${escapeAttr(item[valueKey])}" ${Number(item[valueKey]) === Number(selected) ? 'selected' : ''}>${escapeHtml(item[labelKey])}</option>`).join('')}</select><small class="joyokr-field__error" data-field-error="${escapeAttr(name)}"></small></label>`;
  }

  function selectValues(name, label, values, selected) {
    const translations = { company: 'Công ty', department: 'Phòng ban', team: 'Team', personal: 'Cá nhân', low: 'Thấp', medium: 'Trung bình', high: 'Cao', month: 'Tháng', quarter: 'Quý', year: 'Năm', custom: 'Tùy chỉnh', draft: 'Nháp', active: 'Hoạt động', inactive: 'Vô hiệu hóa', closed: 'Đã đóng', administrator: 'Admin', joyokr_manager: 'Manager', joyokr_employee: 'Employee', admin: 'Quản trị OKR', manager: 'Quản lý OKR', weekly: 'Hàng tuần', biweekly: 'Hai tuần/lần', monthly: 'Hàng tháng' };
    return `<label class="joyokr-field"><span>${escapeHtml(label)}</span><select name="${escapeAttr(name)}">${values.map((value) => `<option value="${escapeAttr(value)}" ${String(value) === String(selected) ? 'selected' : ''}>${escapeHtml(translations[value] || value)}</option>`).join('')}</select><small class="joyokr-field__error" data-field-error="${escapeAttr(name)}"></small></label>`;
  }

  function hidden(name, value) {
    return `<input type="hidden" name="${escapeAttr(name)}" value="${escapeAttr(value)}">`;
  }

  function remainingKrWeight(objective) {
    const usedWeight = (objective?.key_results || []).reduce((total, kr) => total + Number(kr.weight || 0), 0);
    return Math.max(0, Math.round((100 - usedWeight) * 100) / 100);
  }

  async function all(endpoint, query = {}) {
    const items = [];
    let page = 1;
    let pages = 1;
    do {
      const result = await namespace.api.request(endpoint, { query: { ...query, per_page: 100, page } });
      if (Array.isArray(result)) return result;
      items.push(...(result.items || []));
      pages = Math.min(MAX_PICKER_PAGES, Number(result.total_pages || 1));
      page += 1;
    } while (page <= pages);
    return items;
  }

  /** Id of the only item whose name matches exactly, else ''. */
  function idByName(items, name) {
    if (!name) return '';
    const matches = (items || []).filter((candidate) => String(candidate.name ?? '') === String(name));
    return matches.length === 1 ? matches[0].id : '';
  }

  function showFormError(form, error) {
    // Dialog forms have a summary box; the settings page shows the toast instead.
    const summary = form.querySelector('[data-form-error]');
    if (summary) {
      summary.textContent = error.message || 'Không thể lưu dữ liệu.';
      summary.hidden = false;
    }
    Object.entries(error.fields || {}).forEach(([name, message]) => {
      const target = form.querySelector(`[data-field-error="${cssEscape(name)}"]`);
      if (target) target.textContent = message;
    });
    const firstInvalid = form.querySelector('.joyokr-field__error:not(:empty)');
    firstInvalid?.closest('.joyokr-field')?.querySelector('input, select, textarea')?.focus();
  }

  function clearFormErrors(form) {
    form.querySelectorAll('.joyokr-field__error').forEach((element) => { element.textContent = ''; });
    const summary = form.querySelector('[data-form-error]');
    if (!summary) return;
    summary.hidden = true;
    summary.textContent = '';
  }

  function getDialog() {
    return document.querySelector('[data-dialog]');
  }

  function currentQuery() {
    return Object.fromEntries(new URLSearchParams(globalScope.location.search));
  }

  /** Lower-case text without Vietnamese accents, for the member name filter. */
  function fold(value) {
    return String(value ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/đ/g, 'd').replace(/Đ/g, 'D').toLowerCase().trim();
  }

  function cssEscape(value) {
    return globalScope.CSS?.escape ? globalScope.CSS.escape(value) : String(value).replace(/[^a-zA-Z0-9_-]/g, '');
  }

  const HTML_ESCAPES = Object.freeze({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' });

  /** Escapes text for HTML element content and quoted attribute values. */
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"'`]/g, (character) => HTML_ESCAPES[character]);
  }

  function escapeAttr(value) {
    return escapeHtml(value);
  }

  document.addEventListener('submit', (event) => {
    const dialogForm = event.target.closest('[data-dialog-form]');
    if (dialogForm) {
      event.preventDefault();
      submitActiveForm(dialogForm);
      return;
    }
    const settingsForm = event.target.closest('[data-settings-form]');
    if (settingsForm) {
      // Enter in a settings field saves like the "Lưu cài đặt" button.
      event.preventDefault();
      handleAction('save-settings', settingsForm);
      return;
    }
    const filterForm = event.target.closest('[data-filter-form]');
    if (filterForm) {
      event.preventDefault();
      const query = Object.fromEntries(new FormData(filterForm).entries());
      namespace.navigate(filterForm.dataset.filterRoute, { replace: true, query });
    }
  });

  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-dialog-close]')) close();
  });

  document.addEventListener('keydown', (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (!tab || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const tabs = [...tab.closest('[role="tablist"]').querySelectorAll('[role="tab"]')];
    const current = tabs.indexOf(tab);
    const next = event.key === 'Home' ? 0 : (event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length);
    event.preventDefault();
    tabs[next].focus();
    switchTab(tabs[next]);
  });

  getDialog()?.addEventListener('cancel', (event) => {
    event.preventDefault();
    close();
  });

  namespace.forms = Object.freeze({
    open,
    close,
    confirm,
    defaultCycleId,
    handleAction,
    formNames,
    helpers: Object.freeze({ escapeHtml, escapeAttr, buildDefinition, field, textarea, select, selectValues, hidden, idByName, fold }),
  });
})(globalThis);
