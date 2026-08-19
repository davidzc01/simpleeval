/**
 * 评测集用例 CRUD（B-5）前端组件测试
 *
 * 通过 node 提取 index.html 中的 JS，在沙箱中执行 renderEvalsetDetail，
 * 验证按钮出现、previewField 按 eval_type 取字段、空态、XSS 转义等。
 *
 * 运行：node tests/test_evalset_ui.js
 */

const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, '..', 'app', 'static', 'index.html');

function extractJs(html) {
    const m = html.match(/<script>([\s\S]*?)<\/script>/);
    if (!m) throw new Error('no <script> tag found');
    return m[1];
}

function loadRenderingFunctions(jsSource) {
    const sandbox = {
        document: { getElementById: () => null, querySelectorAll: () => [], addEventListener: () => {} },
        window: { addEventListener: () => {}, scrollTo: () => {} },
        console,
        setTimeout: () => {},
        setInterval: () => 0,
        location: { hash: '' },
        escapeHtml: (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
    };
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        jsSource + '; return { renderEvalsetDetail };'
    );
    return fn(
        sandbox.document, sandbox.window, sandbox.console,
        sandbox.setTimeout, sandbox.setInterval, sandbox.location, sandbox.escapeHtml
    );
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

// ============== 测试 ==============

const html = fs.readFileSync(HTML_PATH, 'utf8');
const js = extractJs(html);
const { renderEvalsetDetail } = loadRenderingFunctions(js);

const PID = 'proj-1';
const EID = 'es-1';

describe('renderEvalsetDetail — 操作按钮齐全', () => {
    const evalset = {
        id: EID, project_id: PID, name: '测试集',
        cases: [{
            id: 'c1', case_name: 'case1', input: '你好', eval_type: 'exact',
            expected_output: '你好！', eval_params: {}, enabled: true
        }]
    };
    const out = renderEvalsetDetail(evalset, PID);
    assertIncludes(out, '+ 新增用例', 'header 包含 + 新增用例 按钮');
    assertIncludes(out, 'showCaseEditorModal', '行内编辑按钮调用 showCaseEditorModal');
    assertIncludes(out, 'showDeleteCaseModal', '行内删除按钮调用 showDeleteCaseModal');
    assertIncludes(out, '永久删除', '删除按钮 title=永久删除');  // 实际是 title attr
    // 编辑按钮单独可见
    assertIncludes(out, '>编辑<', '编辑按钮文本');
});

describe('renderEvalsetDetail — previewField 按 eval_type 取字段', () => {
    const cases = [
        { id: 'e', case_name: 'e', input: 'x', eval_type: 'exact', expected_output: '期望A', eval_params: {}, enabled: true },
        { id: 'c', case_name: 'c', input: 'x', eval_type: 'contains', expected_output: null, eval_params: { substring: '子串B' }, enabled: true },
        { id: 'n', case_name: 'n', input: 'x', eval_type: 'not_contains', expected_output: null, eval_params: { substring: '禁词C' }, enabled: true },
        { id: 'l', case_name: 'l', input: 'x', eval_type: 'length', expected_output: null, eval_params: { min: 3, max: 10 }, enabled: true },
        { id: 'j', case_name: 'j', input: 'x', eval_type: 'llm_judge', expected_output: null, output_requirement: '判据D', eval_params: {}, enabled: true },
    ];
    const out = renderEvalsetDetail({ id: EID, project_id: PID, name: 'all', cases }, PID);
    assertIncludes(out, '期望A', 'exact → 显示 expected_output');
    assertIncludes(out, '子串B', 'contains → 显示 eval_params.substring');
    assertIncludes(out, '禁词C', 'not_contains → 显示 eval_params.substring');
    assertIncludes(out, '[3, 10]', 'length → 显示 [min, max]');
    assertIncludes(out, '判据D', 'llm_judge → 显示 output_requirement');
});

describe('renderEvalsetDetail — length 缺省边界', () => {
    // min/max 缺省：min→0, max→∞
    const onlyMin = { id: 'l1', case_name: 'l1', input: 'x', eval_type: 'length', eval_params: { min: 5 }, enabled: true };
    const onlyMax = { id: 'l2', case_name: 'l2', input: 'x', eval_type: 'length', eval_params: { max: 12 }, enabled: true };
    const none = { id: 'l3', case_name: 'l3', input: 'x', eval_type: 'length', eval_params: {}, enabled: true };
    const out1 = renderEvalsetDetail({ id: EID, project_id: PID, name: 's', cases: [onlyMin] }, PID);
    assertIncludes(out1, '[5, ∞]', 'length 缺 max → 显示 ∞');
    const out2 = renderEvalsetDetail({ id: EID, project_id: PID, name: 's', cases: [onlyMax] }, PID);
    assertIncludes(out2, '[0, 12]', 'length 缺 min → 显示 0');
    const out3 = renderEvalsetDetail({ id: EID, project_id: PID, name: 's', cases: [none] }, PID);
    assertIncludes(out3, '[0, ∞]', 'length min/max 都缺 → [0, ∞]');
});

describe('renderEvalsetDetail — 空态', () => {
    const evalset = { id: EID, project_id: PID, name: '空集', cases: [] };
    const out = renderEvalsetDetail(evalset, PID);
    assertIncludes(out, '暂无用例', '空态文案');
    assertIncludes(out, '新增用例', '引导用户使用新增按钮');
    assertNotIncludes(out, 'showDeleteCaseModal', '空态不应渲染删除按钮');
});

describe('renderEvalsetDetail — 启用/禁用样式', () => {
    const evalset = {
        id: EID, project_id: PID, name: 's',
        cases: [
            { id: 'a', case_name: 'a', input: 'x', eval_type: 'exact', expected_output: 'y', eval_params: {}, enabled: true },
            { id: 'b', case_name: 'b', input: 'x', eval_type: 'exact', expected_output: 'y', eval_params: {}, enabled: false },
        ]
    };
    const out = renderEvalsetDetail(evalset, PID);
    assertIncludes(out, 'opacity: 0.5', '禁用行置灰');
    assertIncludes(out, '共 2 条 · 启用 1 条', 'header 显示启用计数');
    // 一行启用按钮文本="禁用"，另一行="启用"——两个字符串都应出现
    assertIncludes(out, '禁用', '启用行的按钮显示"禁用"');
    assertIncludes(out, '启用', '禁用行的按钮显示"启用"');
});

describe('renderEvalsetDetail — XSS 转义', () => {
    const evalset = {
        id: EID, project_id: PID, name: '<script>x',
        cases: [{
            id: 'x', case_name: '<img src=x onerror=alert(1)>',
            input: 'a"b',
            eval_type: 'exact', expected_output: 'c<d>', eval_params: {}, enabled: true
        }]
    };
    const out = renderEvalsetDetail(evalset, PID);
    assertNotIncludes(out, '<script>x</span>', 'case_name 中的 <script> 已转义');
    // 关键 XSS 防御：原始 <img> 标签不应出现在 HTML 中（已被转义为 &lt;img）
    assertNotIncludes(out, '<img src=x', '原始 <img> 标签未出现在 HTML 中（已转义）');
    assertIncludes(out, '&lt;img', 'case_name 转义为 &lt;img');
    assertIncludes(out, '&lt;d&gt;', 'expected_output 转义为 &lt;d&gt;');
});

describe('renderEvalsetDetail — 删除按钮调用安全', () => {
    // case_name 含特殊字符不应破坏 onclick 属性
    const evalset = {
        id: EID, project_id: PID, name: 's',
        cases: [{
            id: 'c1', case_name: '含"双引号"和\'单引号\'',
            input: 'x', eval_type: 'exact', expected_output: 'y', eval_params: {}, enabled: true
        }]
    };
    const out = renderEvalsetDetail(evalset, PID);
    assertIncludes(out, 'showDeleteCaseModal', '删除按钮仍可调用');
    // onclick 只传 evalsetId/projectId/caseId（3 个参数），不传 caseName
    assertIncludes(out, `showDeleteCaseModal('${EID}', '${PID}', 'c1')`, 'onclick 只传 3 个 ID 参数');
    // 危险的原始 case_name 不应直接出现在 HTML 属性里
    assertNotIncludes(out, '含"双引号"', '原始未转义 case_name 不应出现在 onclick 中');
});

// ============== 汇总 ==============
console.log(`\n${'='.repeat(40)}`);
console.log(`passed: ${passed}, failed: ${failed}`);
process.exit(failed > 0 ? 1 : 0);
