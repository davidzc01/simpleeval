/**
 * B-13~B-21 前端组件测试
 *
 * 覆盖：
 * - B-13: 变量 kv 行编辑器（kvRowHTML/collectKvRows）+ CSV variables 列（validateImportData）
 * - B-14: 响应解析卡片 unpack 开关 + output_field（collectResponseParsing/toggleRpUnpackField）
 * - B-16: 评测集表格 checkbox 批量操作（renderEvalsetDetail 渲染 + 选择辅助函数）
 * - B-17: run 详情页「返回项目」按钮（源码含 navigateTo('project-detail')）
 * - B-18: token_missing 文案「token 不可得」（renderProjectOverview + updateRunDetailDom）
 *
 * 运行：node tests/test_b13_b21_ui.js
 */

const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, '..', 'app', 'static', 'index.html');

function extractJs(html) {
    const m = html.match(/<script>([\s\S]*?)<\/script>/);
    if (!m) throw new Error('no <script> tag found');
    return m[1];
}

function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ============== 测试框架 ==============

let passed = 0;
let failed = 0;

function assert(cond, msg) {
    if (cond) { passed++; }
    else { failed++; console.error('  ✗ FAIL:', msg); }
}

function assertIncludes(haystack, needle, msg) {
    if (String(haystack).includes(needle)) { passed++; }
    else { failed++; console.error(`  ✗ FAIL: ${msg}\n    expected to include: ${JSON.stringify(needle)}`); }
}

function assertNotIncludes(haystack, needle, msg) {
    if (!String(haystack).includes(needle)) { passed++; }
    else { failed++; console.error(`  ✗ FAIL: ${msg}\n    should NOT include: ${JSON.stringify(needle)}`); }
}

function describe(name, fn) {
    console.log(`\n${name}`);
    fn();
}

// ============== 加载函数 ==============

const html = fs.readFileSync(HTML_PATH, 'utf8');
const js = extractJs(html);

/**
 * 构造一个可控的 fake DOM，支持：
 * - getElementById 返回预置节点（可设 .value / .checked / .style / .children / .innerHTML）
 * - querySelectorAll 返回预置节点列表
 * - createElement 返回空壳节点
 */
function makeFakeDom(overrides = {}) {
    const byId = {};
    const lists = {};
    const fakeDoc = {
        getElementById: (id) => byId[id] || overrides[id] || null,
        querySelector: (sel) => overrides._qs ? overrides._qs(sel) : null,
        querySelectorAll: (sel) => lists[sel] || overrides._qsa ? (overrides._qsa ? overrides._qsa(sel) : []) : [],
        addEventListener: () => {},
        createElement: (tag) => ({
            className: '', id: '',
            classList: { add: () => {}, toggle: () => {}, contains: () => false, remove: () => {} },
            appendChild: () => {}, insertAdjacentHTML: () => {},
            innerHTML: '', textContent: '',
            querySelector: () => null, querySelectorAll: () => [],
            style: {}, dataset: {},
            remove: () => {},
        }),
        activeElement: null,
        body: { appendChild: () => {} },
    };
    function setById(id, node) { byId[id] = node; }
    function setList(sel, nodes) { lists[sel] = nodes; }
    return { fakeDoc, setById, setList, byId };
}

// 加载纯函数 + 可访问 state
function loadFunctions(jsSource) {
    const { fakeDoc } = makeFakeDom();
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        jsSource + '; return {'
        + ' renderEvalsetDetail, renderProjectOverview,'
        + ' kvRowHTML, initCaseVarRows, addCaseVarRow, removeKvRow, collectKvRows,'
        + ' validateImportData,'
        + ' collectResponseParsing, toggleRpUnpackField,'
        + ' getSelectedCaseIds, updateBatchOpsBar, toggleBatchSelectAll,'
        + ' updateRunDetailDom,'
        + ' getState: () => state'
        + ' };'
    );
    return fn(
        fakeDoc, { addEventListener: () => {}, scrollTo: () => {} }, console,
        () => {}, 0, { hash: '' },
        escapeHtml
    );
}

const F = loadFunctions(js);

// ============== B-13: 变量 kv 行编辑器 ==============

describe('B-13 kvRowHTML — 渲染键值输入行', () => {
    const out = F.kvRowHTML('leader', '张三');
    assertIncludes(out, 'class="form-input mono kv-key"', '含 key 输入框');
    assertIncludes(out, 'class="form-input mono kv-val"', '含 value 输入框');
    assertIncludes(out, 'value="张三"', 'value 填入张三');
    assertIncludes(out, 'value="leader"', 'key 填入 leader');
    assertIncludes(out, 'kv-del', '含删除按钮');
    assertIncludes(out, 'removeKvRow', '删除按钮调用 removeKvRow');
});

describe('B-13 kvRowHTML — XSS 转义', () => {
    const out = F.kvRowHTML('<script>', '"injection"');
    assertNotIncludes(out, '<script>', '原始 <script> 已转义');
    assertIncludes(out, '&lt;script&gt;', 'key 转义为 &lt;script&gt;');
    assertNotIncludes(out, '"injection"', '原始双引号已转义（不应破坏属性）');
});

describe('B-13 kvRowHTML — 空值容错', () => {
    const out = F.kvRowHTML(null, undefined);
    assertIncludes(out, 'value=""', 'null/undefined → 空字符串');
});

describe('B-13 collectKvRows — 从 DOM 收集成 dict', () => {
    // 构造 fake DOM：3 行 kv，其中 1 行 key 为空（应跳过）
    const { fakeDoc, setById, setList } = makeFakeDom();
    const listEl = { querySelectorAll: () => [] };
    setById('testList', listEl);
    // 用真实的 querySelectorAll 模拟 kv-row
    const rows = [
        { querySelector: (sel) => sel === '.kv-key' ? { value: 'leader' } : { value: '张三' } },
        { querySelector: (sel) => sel === '.kv-key' ? { value: 'enterprise' } : { value: 'ACME' } },
        { querySelector: (sel) => sel === '.kv-key' ? { value: '' } : { value: '空键应跳过' } },
    ];
    // 给 listEl.querySelectorAll 返回 rows
    listEl.querySelectorAll = () => rows;
    // 重新加载函数绑定到此 fakeDoc
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { collectKvRows };'
    );
    const { collectKvRows } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    const result = collectKvRows('testList');
    assert(result !== null, '有有效键 → 非 null');
    assert(result.leader === '张三', 'leader = 张三');
    assert(result.enterprise === 'ACME', 'enterprise = ACME');
    assert(!('' in result), '空键行被跳过');
});

describe('B-13 collectKvRows — 全空返回 null', () => {
    const { fakeDoc, setById } = makeFakeDom();
    const listEl = { querySelectorAll: () => [{ querySelector: () => ({ value: '' }) }] };
    setById('emptyList', listEl);
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { collectKvRows };'
    );
    const { collectKvRows } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    const result = collectKvRows('emptyList');
    assert(result === null, '全部键为空 → null');
});

describe('B-13 collectKvRows — list 不存在返回 null', () => {
    const { fakeDoc } = makeFakeDom();
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { collectKvRows };'
    );
    const { collectKvRows } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    assert(collectKvRows('nonExistent') === null, '不存在的 list → null');
});

// ============== B-13: CSV variables 列（validateImportData） ==============

describe('B-13 validateImportData — variables 为 JSON 字符串（CSV 列）', () => {
    const raw = [{
        case_name: 'c1', input: 'x', eval_type: 'exact',
        variables: '{"leader":"张三","enterprise":"ACME"}'
    }];
    const result = F.validateImportData(raw);
    assert(result.errors.length === 0, '无错误');
    assert(result.cases[0].variables.leader === '张三', 'leader 解析正确');
    assert(result.cases[0].variables.enterprise === 'ACME', 'enterprise 解析正确');
});

describe('B-13 validateImportData — variables 为对象（JSON 文件导入）', () => {
    const raw = [{
        case_name: 'c1', input: 'x', eval_type: 'exact',
        variables: { leader: '李四', content: '报告' }
    }];
    const result = F.validateImportData(raw);
    assert(result.errors.length === 0, '无错误');
    assert(result.cases[0].variables.leader === '李四', 'leader 直传');
    assert(result.cases[0].variables.content === '报告', 'content 直传');
});

describe('B-13 validateImportData — variables 非法 JSON 报错', () => {
    const raw = [{
        case_name: 'c1', input: 'x', eval_type: 'exact',
        variables: '{not valid json'
    }];
    const result = F.validateImportData(raw);
    assert(result.errors.length === 1, '有 1 条错误');
    assertIncludes(result.errors[0].errors.join(','), 'variables 不是合法 JSON', '错误信息含 variables');
});

describe('B-13 validateImportData — 无 variables 时为 null', () => {
    const raw = [{ case_name: 'c1', input: 'x', eval_type: 'exact' }];
    const result = F.validateImportData(raw);
    assert(result.cases[0].variables === null, '无 variables → null');
});

// ============== B-14: 响应解析卡片 unpack 开关 + output_field ==============

describe('B-14 collectResponseParsing — 含 output_unpack_json + output_field', () => {
    // 构造 fake DOM：checkbox 勾选 + input 有值
    const { fakeDoc, setById } = makeFakeDom();
    // readList 需要 output_paths 列表
    setById('rpOutputPathsList', { querySelectorAll: () => [{ value: '$.choices[0].message.content' }] });
    setById('rpTokenPathsList', { querySelectorAll: () => [] });
    setById('rpTokenFieldsList', { querySelectorAll: () => [] });
    setById('rpTokenScope', { value: '' });
    fakeDoc.querySelector = () => ({ value: 'none' }); // radio: none
    setById('rpOutputUnpackJson', { checked: true });
    setById('rpOutputField', { value: 'result' });

    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { collectResponseParsing };'
    );
    const { collectResponseParsing } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    const rp = collectResponseParsing();
    assert(rp.output_unpack_json === true, 'output_unpack_json = true');
    assert(rp.output_field === 'result', 'output_field = result');
    assertIncludes(JSON.stringify(rp.output_paths), '$.choices', 'output_paths 保留');
});

describe('B-14 collectResponseParsing — 未勾选 + 空字段', () => {
    const { fakeDoc, setById } = makeFakeDom();
    setById('rpOutputPathsList', { querySelectorAll: () => [{ value: '' }] });
    setById('rpTokenPathsList', { querySelectorAll: () => [] });
    setById('rpTokenFieldsList', { querySelectorAll: () => [] });
    setById('rpTokenScope', { value: '' });
    fakeDoc.querySelector = () => ({ value: 'none' });
    setById('rpOutputUnpackJson', { checked: false });
    setById('rpOutputField', { value: '  ' });

    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { collectResponseParsing };'
    );
    const { collectResponseParsing } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    const rp = collectResponseParsing();
    assert(rp.output_unpack_json === false, '未勾选 → false');
    assert(rp.output_field === null, '空字段 → null');
});

describe('B-14 toggleRpUnpackField — 联动显示', () => {
    const { fakeDoc, setById } = makeFakeDom();
    let groupDisplay = 'block';
    setById('rpOutputUnpackJson', { checked: true });
    setById('rpOutputFieldGroup', { style: { display: groupDisplay }, get display() { return groupDisplay; }, set display(v) { groupDisplay = v; } });
    // 简化：直接测 style.display 被设为 ''
    const grp = { style: {} };
    setById('rpOutputFieldGroup', grp);
    setById('rpOutputUnpackJson', { checked: true });

    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { toggleRpUnpackField };'
    );
    const { toggleRpUnpackField } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    // checked = true → display = ''
    toggleRpUnpackField();
    assert(grp.style.display === '', '勾选时 output_field 组显示');

    // checked = false → display = 'none'
    setById('rpOutputUnpackJson', { checked: false });
    toggleRpUnpackField();
    assert(grp.style.display === 'none', '未勾选时 output_field 组隐藏');
});

// ============== B-16: 评测集表格 checkbox 批量操作 ==============

const PID16 = 'proj-16';
const EID16 = 'es-16';

describe('B-16 renderEvalsetDetail — checkbox 列 + 批量操作条', () => {
    const evalset = {
        id: EID16, project_id: PID16, name: '批量测试集',
        cases: [
            { id: 'c1', case_name: 'a', input: 'x', eval_type: 'exact', expected_output: 'y', eval_params: {}, enabled: true },
            { id: 'c2', case_name: 'b', input: 'x', eval_type: 'exact', expected_output: 'y', eval_params: {}, enabled: false },
        ]
    };
    const out = F.renderEvalsetDetail(evalset, PID16);
    assertIncludes(out, 'batchSelectAll', '表头含全选 checkbox');
    assertIncludes(out, 'batch-case-cb', '行含 checkbox');
    assertIncludes(out, 'toggleBatchSelectAll', '全选 onchange 调用 toggleBatchSelectAll');
    assertIncludes(out, 'updateBatchOpsBar', '行 onchange 调用 updateBatchOpsBar');
    assertIncludes(out, 'batchOpsBar', '含批量操作条');
    assertIncludes(out, '批量启用', '含批量启用按钮');
    assertIncludes(out, '批量禁用', '含批量禁用按钮');
    assertIncludes(out, '批量删除', '含批量删除按钮');
    assertIncludes(out, 'batchToggleCases', '批量启禁用调用 batchToggleCases');
    assertIncludes(out, 'showBatchDeleteModal', '批量删除调用 showBatchDeleteModal');
});

describe('B-16 renderEvalsetDetail — colspan 改为 7', () => {
    const evalset = { id: EID16, project_id: PID16, name: '空', cases: [] };
    const out = F.renderEvalsetDetail(evalset, PID16);
    assertIncludes(out, 'colspan="7"', '空态 colspan=7（含新增的 checkbox 列）');
});

describe('B-16 renderEvalsetDetail — checkbox value 为 case id（转义）', () => {
    const evalset = {
        id: EID16, project_id: PID16, name: 's',
        cases: [{ id: 'c"x', case_name: 'a', input: 'x', eval_type: 'exact', expected_output: 'y', eval_params: {}, enabled: true }]
    };
    const out = F.renderEvalsetDetail(evalset, PID16);
    assertNotIncludes(out, 'value="c"x"', '含双引号的 id 已转义');
    assertIncludes(out, 'value="c&quot;x"', 'id 转义为 c&quot;x');
});

describe('B-16 getSelectedCaseIds — 收集勾选的 case id', () => {
    const { fakeDoc } = makeFakeDom();
    fakeDoc.querySelectorAll = (sel) => {
        if (sel === '.batch-case-cb:checked') return [{ value: 'c1' }, { value: 'c2' }];
        return [];
    };
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { getSelectedCaseIds };'
    );
    const { getSelectedCaseIds } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    const ids = getSelectedCaseIds();
    assert(ids.length === 2, '返回 2 个 id');
    assert(ids[0] === 'c1' && ids[1] === 'c2', 'id 正确');
});

describe('B-16 updateBatchOpsBar — 选中时显示操作条', () => {
    const { fakeDoc, setById } = makeFakeDom();
    const bar = { style: { display: 'none' } };
    const countEl = { textContent: '' };
    const selectAll = { checked: false };
    setById('batchOpsBar', bar);
    setById('batchSelectedCount', countEl);
    setById('batchSelectAll', selectAll);
    fakeDoc.querySelectorAll = (sel) => {
        if (sel === '.batch-case-cb:checked') return [{ value: 'c1' }];
        if (sel === '.batch-case-cb') return [{ checked: true }, { checked: false }];
        return [];
    };
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { updateBatchOpsBar };'
    );
    const { updateBatchOpsBar } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    updateBatchOpsBar();
    assert(bar.style.display === 'flex', '有选中 → display=flex');
    assertIncludes(countEl.textContent, '1', '计数显示 1');
});

describe('B-16 updateBatchOpsBar — 无选中时隐藏', () => {
    const { fakeDoc, setById } = makeFakeDom();
    const bar = { style: { display: 'flex' } };
    setById('batchOpsBar', bar);
    setById('batchSelectedCount', { textContent: '' });
    setById('batchSelectAll', { checked: false });
    fakeDoc.querySelectorAll = () => [];
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { updateBatchOpsBar };'
    );
    const { updateBatchOpsBar } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    updateBatchOpsBar();
    assert(bar.style.display === 'none', '无选中 → display=none');
});

describe('B-16 toggleBatchSelectAll — 全选联动', () => {
    const checkedCbs = [];
    const { fakeDoc, setById } = makeFakeDom();
    setById('batchSelectAll', { checked: true });
    setById('batchOpsBar', { style: {} });
    setById('batchSelectedCount', { textContent: '' });
    const cbs = [
        { checked: false, value: 'c1' },
        { checked: false, value: 'c2' },
    ];
    fakeDoc.querySelectorAll = (sel) => {
        if (sel === '.batch-case-cb') return cbs;
        if (sel === '.batch-case-cb:checked') return cbs.filter(c => c.checked);
        return [];
    };
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { toggleBatchSelectAll };'
    );
    const { toggleBatchSelectAll } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    toggleBatchSelectAll();
    assert(cbs.every(c => c.checked === true), '全选勾选后所有行 checked=true');
});

// ============== B-17: run 详情页「返回项目」按钮 ==============

describe('B-17 源码含「返回项目」按钮', () => {
    // renderRunDetailPage 是 async + 复杂 DOM，这里验证源码包含关键调用
    assertIncludes(js, "‹ 返回项目", '源码含「‹ 返回项目」按钮文本');
    assertIncludes(js, "navigateTo('project-detail'", '源码含 navigateTo(project-detail) 调用');
    assertIncludes(js, '返回项目</button>', '按钮闭合标签正确');
});

// ============== B-18: token_missing 文案「token 不可得」 ==============

describe('B-18 renderProjectOverview — lastRun.token_missing 时显示「token 不可得」', () => {
    const project = {
        name: 'p', last_run: {
            pass_rate: 0.5, total_token: 0, token_per_pass: 0,
            latency_p50: 100, latency_p95: 200, failed_count: 1,
            token_missing: true
        }, trend: []
    };
    const out = F.renderProjectOverview(project);
    assertIncludes(out, 'token 不可得', 'token_missing=true → 显示「token 不可得」');
    assertIncludes(out, 'title="响应未配置 token 统计路径', '含 tooltip 说明原因');
});

describe('B-18 renderProjectOverview — token 正常时显示数值', () => {
    const project = {
        name: 'p', last_run: {
            pass_rate: 0.8, total_token: 12345, token_per_pass: 5.5,
            latency_p50: 100, latency_p95: 200, failed_count: 0,
            token_missing: false
        }, trend: []
    };
    const out = F.renderProjectOverview(project);
    assertNotIncludes(out, 'token 不可得', 'token_missing=false → 不显示「token 不可得」');
    assertIncludes(out, '12.3K', '显示正常 token 数值（formatNumber 12345 → 12.3K）');
});

describe('B-18 renderProjectOverview — 无 lastRun 显示 -', () => {
    const out = F.renderProjectOverview({ name: 'p', last_run: null, trend: [] });
    assertIncludes(out, '>-<', '无 lastRun → 显示 -');
    assertNotIncludes(out, 'token 不可得', '无 lastRun 不应显示 token 不可得');
});

describe('B-18 updateRunDetailDom — case 表格 token_missing 显示「token 不可得」', () => {
    const { fakeDoc, setById } = makeFakeDom();
    // 构造 updateRunDetailDom 需要的 DOM 节点
    setById('runStatusRow', { innerHTML: '' });
    setById('runError', { innerHTML: '' });
    setById('metricsGrid', { innerHTML: '' });
    setById('caseTableBody', { innerHTML: '' });
    // state.currentRunEvalset 需要存在
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; state.currentRunEvalset = { cases: [{ case_name: "c1", enabled: true}] };'
        + '; return { updateRunDetailDom, getState: () => state };'
    );
    const { updateRunDetailDom, getState } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    const run = {
        status: 'completed',
        results: [{ case_name: 'c1', actual_output: 'out', passed: true, score: 1, latency_ms: 50, token_used: 0, token_missing: true }],
        summary: { pass_rate: 1, total_token: 0, token_per_pass: 0, latency_p50: 50, latency_p95: 50 }
    };
    updateRunDetailDom(run, { c1: { eval_type: 'exact' } });
    const tbody = fakeDoc.getElementById('caseTableBody');
    assertIncludes(tbody.innerHTML, 'token 不可得', 'case 行 token_missing → 显示「token 不可得」');
    assertIncludes(tbody.innerHTML, 'title="响应未配置', 'case 行含 tooltip');
    // 指标卡：allMissing=true → 显示 token 不可得
    const metrics = fakeDoc.getElementById('metricsGrid');
    assertIncludes(metrics.innerHTML, 'token 不可得', '指标卡 allMissing → 显示「token 不可得」');
});

describe('B-18 updateRunDetailDom — token 正常时 case 行显示数值', () => {
    const { fakeDoc, setById } = makeFakeDom();
    setById('runStatusRow', { innerHTML: '' });
    setById('runError', { innerHTML: '' });
    setIdMetricsGrid(setById, '');
    setById('caseTableBody', { innerHTML: '' });
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; state.currentRunEvalset = { cases: [{ case_name: "c1", enabled: true}] };'
        + '; return { updateRunDetailDom };'
    );
    const { updateRunDetailDom } = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml
    );
    const run = {
        status: 'completed',
        results: [{ case_name: 'c1', actual_output: 'out', passed: true, score: 1, latency_ms: 50, token_used: 120, token_missing: false }],
        summary: { pass_rate: 1, total_token: 120, token_per_pass: 5, latency_p50: 50, latency_p95: 50 }
    };
    updateRunDetailDom(run, { c1: { eval_type: 'exact' } });
    const tbody = fakeDoc.getElementById('caseTableBody');
    assertNotIncludes(tbody.innerHTML, 'token 不可得', 'token_missing=false → 不显示「token 不可得」');
    assertIncludes(tbody.innerHTML, '120', '显示 token 数值 120');
});

function setIdMetricsGrid(setById, _) {
    setById('metricsGrid', { innerHTML: '' });
}

// ============== B-15: 项目详情附 last_run（间接验证） ==============

describe('B-15 renderProjectOverview — last_run 摘要字段齐全', () => {
    const project = {
        name: 'p', last_run: {
            pass_rate: 0.75, total_token: 5000, token_per_pass: 3.2,
            latency_p50: 200, latency_p95: 800, failed_count: 2,
            token_missing: false
        }, trend: []
    };
    const out = F.renderProjectOverview(project);
    assertIncludes(out, '75.0%', 'pass_rate 渲染');
    assertIncludes(out, '5.0K', 'total_token 渲染（formatNumber 5000 → 5.0K）');
    assertIncludes(out, '3.2', 'token_per_pass 渲染');
    assertIncludes(out, '0.2s', 'P50 延迟');
    assertIncludes(out, '0.8s', 'P95 延迟');
    assertIncludes(out, '>2<', 'failed_count 渲染');
});

// ============== 汇总 ==============

console.log(`\n${'='.repeat(50)}`);
console.log(`B-13~B-21 UI 组件测试：${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
