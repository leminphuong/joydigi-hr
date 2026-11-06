// W4 report export: the pure CSV builder (JoyOKR.app.buildReportCsv), the
// ASCII file name, the "Xuất CSV" / "Xuất PDF" wiring in app.js, and an
// opt-in sample CSV built from the rendered report (OKR_W4_SAMPLE=1).
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { load, makeConfig, makeDocument, makeLocation, objectiveSummary, page, source } from './harness.mjs';

const BOM = '\uFEFF';
const EXPORTED_AT = new Date(2026, 10, 5, 9, 30); // 05/11/2026 09:30 local time
const SAMPLE_PATH = 'C:\\Users\\phuc\\AppData\\Local\\Temp\\claude\\f8fc951c1557c1b1\\7e9d5198-24b4-4fa9-8b3d-51568fe912ff\\scratchpad\\w4_sample_report.csv';

function exporter() {
  // No [data-joyokr-app] in this document: app.js only registers JoyOKR.app.
  const { JoyOKR } = load(['app.js']);
  assert.equal(typeof JoyOKR.app?.buildReportCsv, 'function');
  return JoyOKR.app;
}

/** The CSV split into records (no field in these fixtures holds a line break). */
function records(csv) {
  assert.ok(csv.endsWith('\r\n'));
  return csv.slice(BOM.length, -2).split('\r\n');
}

test('buildReportCsv: UTF-8 BOM, CRLF, context lines first, then each table row prefixed with its name', () => {
  const { buildReportCsv } = exporter();
  const csv = buildReportCsv(
    [
      { name: 'Theo phòng ban', rows: [['Phòng ban', 'Objective', 'Tiến độ'], ['Phòng Kinh doanh', '2', '45%']] },
      { name: 'Lịch sử check-in', rows: [['Thời gian', 'Owner'], ['05/11/2026 09:30', 'Nguyễn Văn An'], []] },
    ],
    { cycle: 'Q4 2026 · 1/10/2026 – 31/12/2026', cycleName: 'Q4 2026', filters: [['Phòng ban', 'Phòng Kinh doanh'], ['Trạng thái', 'Có rủi ro']], exportedAt: EXPORTED_AT },
  );
  assert.ok(csv.startsWith(BOM), 'UTF-8 BOM for Excel');
  assert.ok(!/[^\r]\n/.test(csv), 'every line break is CRLF');
  assert.deepEqual(records(csv), [
    '"Báo cáo OKR"',
    '"Chu kỳ","Q4 2026 · 1/10/2026 – 31/12/2026"',
    '"Bộ lọc","Phòng ban: Phòng Kinh doanh; Trạng thái: Có rủi ro"',
    '"Xuất lúc","05/11/2026 09:30"',
    '',
    '"Theo phòng ban","Phòng ban","Objective","Tiến độ"',
    '"Theo phòng ban","Phòng Kinh doanh","2","45%"',
    '"Lịch sử check-in","Thời gian","Owner"',
    '"Lịch sử check-in","05/11/2026 09:30","Nguyễn Văn An"',
  ]);
});

test('buildReportCsv: no filters, a string filter line, and a missing cycle', () => {
  const { buildReportCsv } = exporter();
  const lines = records(buildReportCsv([], { filters: [], exportedAt: EXPORTED_AT }));
  assert.deepEqual(lines.slice(0, 4), ['"Báo cáo OKR"', '"Chu kỳ","Chưa có chu kỳ"', '"Bộ lọc","Không có"', '"Xuất lúc","05/11/2026 09:30"']);
  assert.equal(records(buildReportCsv([], { filters: 'Team: Hạ tầng', exportedAt: EXPORTED_AT }))[2], '"Bộ lọc","Team: Hạ tầng"');
});

test('buildReportCsv: RFC 4180 quoting of quotes, commas and line breaks', () => {
  const { buildReportCsv } = exporter();
  const csv = buildReportCsv([{ name: 'KR', rows: [['Tên "đặc biệt", có dấu phẩy', 'dòng 1\r\ndòng 2', null, undefined, 0]] }], { exportedAt: EXPORTED_AT });
  assert.ok(csv.endsWith('"KR","Tên ""đặc biệt"", có dấu phẩy","dòng 1\r\ndòng 2","","","0"\r\n'));
});

test('buildReportCsv: formula injection guard, with pure numbers left alone', () => {
  const { buildReportCsv } = exporter();
  const dangerous = ['=HYPERLINK("http://evil.example","x")', '+cmd|calc', '-2+3', '@SUM(A1)', '\t=1', '\rx', '  =1+1', '-', '- Đã ký 2 hợp đồng'];
  const numbers = ['-5', '-5%', '+1,5', '1.234,5', '-0,25%', '45%', '12'];
  const csv = buildReportCsv([{ name: 'T', rows: [dangerous, numbers, [-5, 3.5], ['Tăng 5%', '—', 'a=b', 'x@y.vn']] }], { exportedAt: EXPORTED_AT });
  const [rowDangerous, rowNumbers, rowTyped, rowText] = records(csv).slice(-4);
  assert.equal(rowDangerous, [
    '"T"', '"\'=HYPERLINK(""http://evil.example"",""x"")"', '"\'+cmd|calc"', '"\'-2+3"', '"\'@SUM(A1)"', '"\'\t=1"', '"\'\rx"', '"\'  =1+1"', '"\'-"', '"\'- Đã ký 2 hợp đồng"',
  ].join(','));
  assert.equal(rowNumbers, '"T","-5","-5%","+1,5","1.234,5","-0,25%","45%","12"');
  assert.equal(rowTyped, '"T","-5","3.5"');
  assert.equal(rowText, '"T","Tăng 5%","—","a=b","x@y.vn"', 'only a leading formula character matters');
  // Context lines are guarded too (a filter or cycle name is server text).
  const guarded = records(buildReportCsv([], { cycle: '=1+1', filters: [['Team', '@team']], exportedAt: EXPORTED_AT }));
  assert.equal(guarded[1], '"Chu kỳ","\'=1+1"');
  assert.equal(guarded[2], '"Bộ lọc","Team: @team"', 'a label prefix keeps the filter line safe');
});

test('buildReportCsv keeps Vietnamese text byte for byte (NFC, đ, tone marks)', () => {
  const { buildReportCsv } = exporter();
  const text = 'Tăng trưởng doanh thu Đồng Nai – ưu tiên “khách hàng mới”: Nguyễn Thị Hương, Lê Hoàng Đức';
  const csv = buildReportCsv([{ name: 'Lịch sử check-in', rows: [[text]] }], { cycle: 'Quý IV/2026', exportedAt: EXPORTED_AT });
  assert.ok(csv.includes(`"Lịch sử check-in","${text.replaceAll('"', '""')}"`));
  assert.ok(Buffer.from(csv, 'utf8').toString('utf8').includes('Đồng Nai'));
});

test('reportCsvFilename: bao-cao-okr_<cycle>[_<filters>]_<YYYY-MM-DD>.csv, ASCII only', () => {
  const { reportCsvFilename, slugify } = exporter();
  assert.equal(reportCsvFilename({ cycleName: 'Q4 2026', filters: [], exportedAt: EXPORTED_AT }), 'bao-cao-okr_q4-2026_2026-11-05.csv');
  const name = reportCsvFilename({
    cycleName: 'Quý 4/2026 – Đợt 1',
    filters: [['Phòng ban', 'Kinh doanh Đà Nẵng'], ['Trạng thái', 'Có rủi ro']],
    exportedAt: EXPORTED_AT,
  });
  assert.equal(name, 'bao-cao-okr_quy-4-2026-dot-1_kinh-doanh-da-nang-co-rui-ro_2026-11-05.csv');
  assert.match(name, /^[\x20-\x7e]+$/);
  assert.equal(reportCsvFilename({ cycle: '', filters: [], exportedAt: EXPORTED_AT }), 'bao-cao-okr_chu-ky_2026-11-05.csv');
  assert.equal(reportCsvFilename({ cycleName: '"><img src=x>/../../etc', exportedAt: EXPORTED_AT }), 'bao-cao-okr_img-src-x-etc_2026-11-05.csv');
  assert.equal(slugify('Trần Thị Bích Ngọc – ĐƯỜNG Ưu tiên'), 'tran-thi-bich-ngoc-duong-uu-tien');
  assert.ok(slugify('a'.repeat(200)).length <= 48);
  const long = reportCsvFilename({ cycleName: 'Chu kỳ '.repeat(30), filters: [['Owner', 'Nguyễn '.repeat(30)]], exportedAt: EXPORTED_AT });
  assert.ok(long.length < 140, 'file name stays short');
});

// -- app.js wiring: "Xuất CSV" and "Xuất PDF" ------------------------------------------------

function fakeCell(parts) {
  return { childNodes: parts.map((textContent) => ({ textContent })), textContent: parts.join('') };
}

function fakeTable(name, rows) {
  return {
    dataset: { reportExportName: name },
    querySelectorAll: () => rows.map((cells) => ({ querySelectorAll: () => cells.map(fakeCell) })),
  };
}

function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function bootReportPage() {
  const listeners = {};
  const blobs = [];
  const links = [];
  const printed = [];
  const bodyClasses = new Set();
  const stamp = { textContent: '—' };
  const outlet = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [], setAttribute() {}, removeAttribute() {} };
  const tables = [
    fakeTable('KR quá hạn hoàn thành', [[['Owner'], ['Key Result']], [['Trần Thị Bích Ngọc'], ['Doanh thu bán lẻ', 'Tăng doanh thu']]]),
    fakeTable('Lịch sử check-in', [[['Nội dung']], [['=cmd|calc']]]),
  ];
  const meta = { dataset: { reportTitle: 'Báo cáo OKR', reportCycle: 'Q4 2026 · 1/10/2026 – 31/12/2026', reportCycleName: 'Q4 2026' } };
  const filters = [{ dataset: { label: 'Phòng ban', value: 'Phòng Kinh doanh' } }];
  const document = makeDocument({
    title: 'Mục tiêu & OKR · JOYDIGI',
    querySelector(selector) {
      if (selector === '[data-joyokr-app]') return { classList: { add() {}, remove() {}, toggle() {} } };
      if (selector === '#joyokr-view') return outlet;
      if (selector === '[data-report-meta]') return meta;
      if (selector === '[data-report-exported-at]') return stamp;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[data-report-export]') return tables;
      if (selector === '[data-report-filter]') return filters;
      return [];
    },
    addEventListener(type, handler) { listeners[type] = handler; },
    createElement(tag) {
      const link = { tag, click() { this.clicked = true; }, remove() {} };
      links.push(link);
      return link;
    },
    body: {
      classList: { add: (name) => bodyClasses.add(name), remove: (name) => bodyClasses.delete(name), contains: (name) => bodyClasses.has(name) },
      append() {},
    },
  });
  class TestURL extends URL {}
  TestURL.createObjectURL = (blob) => { blobs.push(blob); return 'blob:test'; };
  TestURL.revokeObjectURL = () => {};
  const { sandbox } = load([], {
    location: makeLocation('/okr/reports', ''),
    document,
    config: makeConfig({ initialRoute: 'reports' }),
    extra: {
      URL: TestURL,
      history: { pushState() {}, replaceState() {} },
      addEventListener() {},
      scrollTo() {},
      matchMedia: () => ({ matches: false }),
      print() { printed.push({ classes: [...bodyClasses], stamp: stamp.textContent, title: document.title }); },
      JoyOKR: {
        store: { setState() {}, getState: () => ({}) },
        views: { async render(name, context) { context.outlet.innerHTML = `<h1>${name}</h1>`; return true; } },
      },
    },
  });
  vm.runInContext(source('app.js'), sandbox, { filename: 'app.js' });
  await tick();
  const click = (action) => listeners.click({
    target: { closest: (selector) => (selector === '[data-action]' ? { dataset: { action } } : null) },
    preventDefault() {},
  });
  return { click, blobs, links, printed, bodyClasses, stamp, document };
}

test('"Xuất CSV" exports the page tables with the report context and the slug file name', async () => {
  const page = await bootReportPage();
  page.click('export-report-csv');
  assert.equal(page.blobs.length, 1);
  assert.equal(page.blobs[0].type, 'text/csv;charset=utf-8');
  // Blob.text() would drop the BOM while decoding: check the bytes.
  const bytes = new Uint8Array(await page.blobs[0].arrayBuffer());
  assert.deepEqual([...bytes.slice(0, 3)], [0xef, 0xbb, 0xbf], 'UTF-8 BOM bytes');
  const csv = new TextDecoder('utf-8', { ignoreBOM: true }).decode(bytes);
  const lines = csv.split('\r\n');
  assert.equal(lines[0], '\uFEFF"Báo cáo OKR"');
  assert.equal(lines[1], '"Chu kỳ","Q4 2026 · 1/10/2026 – 31/12/2026"');
  assert.equal(lines[2], '"Bộ lọc","Phòng ban: Phòng Kinh doanh"');
  assert.match(lines[3], /^"Xuất lúc","\d{2}\/\d{2}\/\d{4} \d{2}:\d{2}"$/);
  assert.ok(lines.includes('"KR quá hạn hoàn thành","Trần Thị Bích Ngọc","Doanh thu bán lẻ Tăng doanh thu"'), 'child elements stay separate words');
  assert.ok(lines.includes('"Lịch sử check-in","\'=cmd|calc"'), 'formula guard applies to exported cells');
  const link = page.links.find((item) => item.tag === 'a');
  assert.ok(link.clicked);
  assert.match(link.download, /^bao-cao-okr_q4-2026_phong-kinh-doanh_\d{4}-\d{2}-\d{2}\.csv$/);
});

test('"Xuất PDF" stamps the print header, prints with joyokr-print-report and restores the page', async () => {
  const page = await bootReportPage();
  page.click('print-report');
  assert.equal(page.printed.length, 1);
  const [printed] = page.printed;
  assert.deepEqual(printed.classes, ['joyokr-print-report']);
  assert.match(printed.stamp, /^\d{2}\/\d{2}\/\d{4} \d{2}:\d{2}$/, 'Xuất lúc filled before printing');
  assert.match(printed.title, /^bao-cao-okr_q4-2026_phong-kinh-doanh_\d{4}-\d{2}-\d{2}$/, 'PDF file name suggestion');
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(page.bodyClasses.size, 0);
  assert.equal(page.document.title, 'Mục tiêu & OKR · JOYDIGI');
});

// -- sample CSV from the rendered report (realistic Vietnamese data) --------------------------

const HTML_ENTITIES = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'", '&#96;': '`' };

function unescapeHtml(value) {
  return value.replace(/&(?:amp|lt|gt|quot|#39|#96);/g, (entity) => HTML_ENTITIES[entity]);
}

/** What app.js reads from the DOM, taken from the rendered HTML (cells hold no nested tables). */
function exportTablesFromHtml(html) {
  return [...html.matchAll(/<table[^>]*data-report-export-name="([^"]*)"[^>]*>([\s\S]*?)<\/table>/g)].map(([, name, body]) => ({
    name: unescapeHtml(name),
    rows: [...body.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/g)].map(([, row]) => [...row.matchAll(/<t[hd][^>]*>([\s\S]*?)<\/t[hd]>/g)]
      .map(([, cell]) => unescapeHtml(cell.replace(/<[^>]*>/g, ' ')).replace(/\s+/g, ' ').trim())),
  }));
}

function attribute(html, name) {
  const match = html.match(new RegExp(`${name}="([^"]*)"`));
  return match ? unescapeHtml(match[1]) : '';
}

function sampleRoutes() {
  const people = [
    { id: 7, name: 'Nguyễn Văn An' },
    { id: 8, name: 'Trần Thị Bích Ngọc' },
    { id: 9, name: 'Lê Hoàng Đức' },
  ];
  const cycle = { id: 3, name: 'Quý 4/2026', cycle_type: 'quarter', start_date: '2026-10-01', end_date: '2026-12-31', status: 'active' };
  const objective = (id, title, owner, scope, progress) => objectiveSummary({ id, title, owner, scope, progress, approval_status: 'approved', health: 'at_risk', provisional: false, cycle: { id: 3, name: cycle.name, end_date: cycle.end_date } });
  const history = (id, at, owner, author, kr, unit, values, progress, confidence, text) => ({
    id, created_at: at, author, previous_value: values[0], current_value: values[1], previous_progress: progress[0], current_progress: progress[1],
    confidence, update_text: text, blockers: '', next_steps: '', key_result: { id: kr[0], title: kr[1], unit }, objective_title: kr[2], owner,
  });
  return {
    'GET /api/okr/departments/': { body: page([{ id: 1, name: 'Phòng Kinh doanh', okr_manager: people[0] }, { id: 2, name: 'Phòng Kỹ thuật', okr_manager: people[2] }]) },
    'GET /api/okr/teams/': { body: page([{ id: 4, name: 'Team Bán lẻ miền Nam', description: '', department_id: 1, department_name: 'Phòng Kinh doanh', manager: people[1], status: 'active', member_count: 6 }]) },
    'GET /api/okr/people/': { body: page(people.map((person) => ({ ...person, department_id: 1, department_name: 'Phòng Kinh doanh', manager_id: null }))) },
    'GET /api/okr/reports/': {
      body: {
        cycle,
        cycle_selection: { selected_id: 3, selected_by: 'default', rule: 'Khi chưa chọn chu kỳ: dùng chu kỳ đang hoạt động có ngày hôm nay nằm trong khoảng thời gian.', active_cycles: [{ id: 3, name: cycle.name, start_date: cycle.start_date, end_date: cycle.end_date }] },
        filters: { department_id: 1, health: 'at_risk' },
        progress: { value: '47.30', objective_count: 4, total_weight: '100.00' },
        selection_progress: '41.25',
        expected_progress: '52.00',
        objective_stats: { total: 2, on_track: 0, at_risk: 2, off_track: 0, completed: 0 },
        key_result_count: 5,
        stale_count: 1,
        rows: [
          objective(12, 'Tăng trưởng doanh thu bán lẻ khu vực phía Nam', people[0], 'department', '45.00'),
          objective(14, 'Nâng cao trải nghiệm khách hàng, giảm khiếu nại', people[1], 'team', '37.50'),
        ],
        by_scope: [{ label: 'department', count: 1, progress: '45.00' }, { label: 'team', count: 1, progress: '37.50' }],
        by_owner: [{ label: 'Nguyễn Văn An', count: 1, progress: '45.00' }, { label: 'Trần Thị Bích Ngọc', count: 1, progress: '37.50' }],
        by_department: [{ label: 'Phòng Kinh doanh', count: 2, progress: '41.25' }],
        by_team: [{ label: 'Team Bán lẻ miền Nam', count: 1, progress: '37.50' }, { label: 'Chưa xác định', count: 1, progress: '45.00' }],
        by_rating: [
          { label: 'Chưa đạt', min: '0', max: '0.3', count: 0 },
          { label: 'Cần cải thiện', min: '0.3', max: '0.7', count: 2 },
          { label: 'Đạt', min: '0.7', max: '0.9', count: 0 },
          { label: 'Xuất sắc', min: '0.9', max: '1', count: 0 },
        ],
        overdue_key_results: [
          { id: 21, title: 'Mở 3 điểm bán mới tại Đồng Nai, Bình Dương', objective_id: 12, objective_title: 'Tăng trưởng doanh thu bán lẻ khu vực phía Nam', owner: people[0], due_date: '2026-10-31', days_late: 5, progress: '66.67' },
        ],
        checkin_count: 57,
        checkin_history: [
          history(91, '2026-11-04T16:45:00+07:00', people[1], people[1], [24, 'Giảm thời gian phản hồi khiếu nại xuống dưới 4 giờ', 'Nâng cao trải nghiệm khách hàng, giảm khiếu nại'], 'giờ', ['9', '6.5'], ['20.00', '50.00'], 3, '- Đã áp dụng kịch bản trả lời mẫu; còn thiếu 1 nhân sự ca tối'),
          history(90, '2026-11-03T09:10:00+07:00', people[0], people[0], [22, 'Doanh thu bán lẻ đạt 12 tỷ đồng', 'Tăng trưởng doanh thu bán lẻ khu vực phía Nam'], 'tỷ đồng', ['4.2', '5.4'], ['35.00', '45.00'], 4, 'Ký thêm 2 hợp đồng đại lý, "Siêu thị Hòa Bình" thanh toán đúng hạn'),
          history(88, '2026-11-01T08:00:00+07:00', people[0], people[2], [21, 'Mở 3 điểm bán mới tại Đồng Nai, Bình Dương', 'Tăng trưởng doanh thu bán lẻ khu vực phía Nam'], 'điểm', ['1', '2'], ['33.33', '66.67'], 4, '=HYPERLINK("http://evil.example","bấm vào đây")'),
        ],
        trend: [{ date: '2026-11-01', progress: '38.00' }, { date: '2026-11-04', progress: '41.25' }],
        checkin_attention_by_owner: [{ owner: people[1], overdue_count: 1, off_track_count: 0, items: [{ key_result_id: 25, key_result_title: 'Đạt điểm hài lòng CSAT 4,5/5', objective_title: 'Nâng cao trải nghiệm khách hàng, giảm khiếu nại', reason: 'stale', progress: '20.00', expected_progress: '52.00', days_overdue: 9, last_checkin_at: '2026-10-20T10:00:00+07:00', owner: people[1] }] }],
      },
    },
    'GET /api/okr/notifications/': { body: page([], { unread_count: 0 }) },
  };
}

test('sample report CSV from realistic Vietnamese data (written to the scratchpad when OKR_W4_SAMPLE is set)', async () => {
  const { JoyOKR } = load(['domain.js', 'api.js', 'store.js', 'views.js', 'app.js'], {
    routes: sampleRoutes(),
    location: makeLocation('/okr/reports', '?department_id=1&health=at_risk'),
  });
  const outlet = { innerHTML: '' };
  await JoyOKR.views.render('reports', { params: {}, outlet, isCurrent: () => true });
  const html = outlet.innerHTML;
  const tables = exportTablesFromHtml(html);
  assert.deepEqual(tables.map((table) => table.name), [
    'Theo phòng ban', 'Theo team', 'Theo mức độ hoàn thành', 'KR cần chú ý', 'KR quá hạn hoàn thành', 'Lịch sử check-in', 'Objective',
  ]);
  const meta = {
    title: attribute(html, 'data-report-title'),
    cycle: attribute(html, 'data-report-cycle'),
    cycleName: attribute(html, 'data-report-cycle-name'),
    filters: [...html.matchAll(/data-report-filter data-label="([^"]*)" data-value="([^"]*)"/g)].map(([, label, value]) => [unescapeHtml(label), unescapeHtml(value)]),
    exportedAt: EXPORTED_AT,
  };
  const csv = JoyOKR.app.buildReportCsv(tables, meta);
  const filename = JoyOKR.app.reportCsvFilename(meta);
  assert.equal(filename, 'bao-cao-okr_quy-4-2026_phong-kinh-doanh-co-rui-ro_2026-11-05.csv');
  const lines = records(csv);
  assert.equal(lines[1], '"Chu kỳ","Quý 4/2026 · 1/10/2026 – 31/12/2026"');
  assert.equal(lines[2], '"Bộ lọc","Phòng ban: Phòng Kinh doanh; Trạng thái: Có rủi ro"');
  assert.ok(lines.includes('"Theo mức độ hoàn thành","Cần cải thiện","30% – 70%","2"'));
  assert.ok(lines.includes('"KR quá hạn hoàn thành","Nguyễn Văn An","Mở 3 điểm bán mới tại Đồng Nai, Bình Dương","Tăng trưởng doanh thu bán lẻ khu vực phía Nam","31/10/2026","5 ngày","66,67%"'));
  assert.ok(lines.some((line) => line.includes('"\'- Đã áp dụng kịch bản trả lời mẫu; còn thiếu 1 nhân sự ca tối"')), 'leading "-" note is guarded');
  assert.ok(lines.some((line) => line.includes('"\'=HYPERLINK(""http://evil.example"",""bấm vào đây"")"')), 'formula is neutralised');
  assert.ok(lines.some((line) => line.includes('"Ký thêm 2 hợp đồng đại lý, ""Siêu thị Hòa Bình"" thanh toán đúng hạn"')));
  assert.ok(lines.some((line) => line.includes('"4,2 → 5,4 tỷ đồng","35% → 45%","4/5"')), 'value and progress before → after, with unit');

  const target = process.env.OKR_W4_SAMPLE;
  if (target) {
    const file = /\.csv$/i.test(target) ? target : SAMPLE_PATH;
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, csv, 'utf8');
    assert.equal(fs.readFileSync(file, 'utf8'), csv);
  }
});
