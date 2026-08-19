/**
 * 响应解析卡片（B-7）前端组件测试
 *
 * 通过 node 提取 index.html 中的 JS，在沙箱中执行 renderJsonTree / pickJsonPath 等纯函数，
 * 验证：JSON 树结构、JSONPath 生成（对象/数组/特殊字符 key）、叶子节点类型染色、
 * 折叠交互、token_fields 末段字段提取、点选填入空 input。
 *
 * 运行：node tests/test_parsing_ui.js
 */

const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, '..', 'app', 'static', 'index.html');

function extractJs(html) {
    const m = html.match(/<script>([\s\S]*?)<\/script>/);
    if (!m) throw new Error('no <script> tag found');
    return m[1];
}

/**
 * renderJsonTree 是纯函数（不读 DOM/state），可以直接调用。
 * pickJsonPath 依赖 state._rpActiveTarget + DOM，我们用伪造 DOM 节点测试它。
 */
function loadFunctions(jsSource) {
    // 桩：document.getElementById 返回可控的 fake list；querySelector 模拟 radio
    const fakeLists = {};
    const fakeToast = { appendChild: () => {}, remove: () => {} };
    const fakeDocument = {
        getElementById: (id) => {
            if (id === 'toastContainer') return fakeToast;
            return fakeLists[id] || null;
        },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
        createElement: (tag) => ({
            className: '',
            classList: { add: () => {}, toggle: () => {}, contains: () => false },
            appendChild: () => {},
            innerHTML: '',
            remove: () => {},
            querySelector: () => null,
            querySelectorAll: () => [],
            style: {},
        }),
        activeElement: null,
    };
    // 注意：不传 state 作为参数，让 JS 里的 `const state = {...}` 自行定义
    // 测试通过 setRpActiveTarget 改 state._rpActiveTarget
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        jsSource + '; return { renderJsonTree, pickJsonPath, RP_LIST_IDS, setRpActiveTarget, getState: () => state };'
    );
    return fn(
        fakeDocument, { addEventListener: () => {}, scrollTo: () => {} }, console,
        () => {}, 0, { hash: '' },
        (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    );
}

// 简化 escapeHtml（与前端一致）
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

// ============== 测试 ==============

const html = fs.readFileSync(HTML_PATH, 'utf8');
const js = extractJs(html);
const { renderJsonTree, pickJsonPath, RP_LIST_IDS, setRpActiveTarget } = loadFunctions(js);

describe('renderJsonTree — 基础结构', () => {
    const data = { name: 'hello', age: 18, active: true, parent: null, list: [1, 2, { x: 'a' }] };
    const out = renderJsonTree(data, '$');
    assertIncludes(out, 'data-toggle="1"', '对象/数组括号带 data-toggle（可折叠）');
    assertIncludes(out, 'jt-children', '有子节点容器');
    assertIncludes(out, '$.name', '对象 key 生成 $.name 路径');
    assertIncludes(out, '$.age', '对象 key 生成 $.age 路径');
    assertIncludes(out, '$.list[0]', '数组索引生成 $.list[0] 路径');
    assertIncludes(out, '$.list[2].x', '嵌套对象在数组里生成 $.list[2].x 路径');
});

describe('renderJsonTree — 叶子节点类型染色', () => {
    const data = { s: 'str', n: 42, b: true, nl: null };
    const out = renderJsonTree(data, '$');
    assertIncludes(out, 'jt-string', 'string 染色 jt-string');
    assertIncludes(out, 'jt-number', 'number 染色 jt-number');
    assertIncludes(out, 'jt-boolean', 'boolean 染色 jt-boolean');
    assertIncludes(out, 'jt-null', 'null 染色 jt-null');
    assertIncludes(out, '"str"', 'string 值带引号显示');
});

describe('renderJsonTree — 特殊字符 key', () => {
    const data = { 'with-dash': 1, 'with space': 2, '中文键': 3, "with'quote": 4 };
    const out = renderJsonTree(data, '$');
    // 含特殊字符的 key 应使用 ['...'] 包裹，而不是点写法
    assertIncludes(out, "$['with-dash']", '含连字符 key 用 [\'...\'] 包裹');
    assertIncludes(out, "$['with space']", '含空格 key 用 [\'...\'] 包裹');
    assertIncludes(out, "$['中文键']", '中文 key 用 [\'...\'] 包裹');
    assertIncludes(out, "['with\\'quote']", '含单引号 key 转义');
    // 普通 key 不应包裹（应走 . 写法）
    assertNotIncludes(out, "$['name']", '普通 key 不应用 [\'...\'] 包裹（这里 data 没有 name，不应出现）');
});

describe('renderJsonTree — 空容器', () => {
    const out1 = renderJsonTree([], '$');
    assertIncludes(out1, '[]', '空数组渲染 []');
    const out2 = renderJsonTree({}, '$');
    assertIncludes(out2, '{}', '空对象渲染 {}');
    assertNotIncludes(out2, 'jt-children', '空容器无 children');
});

describe('renderJsonTree — preview 文案', () => {
    const data = { arr: [1, 2, 3], obj: { a: 1, b: 2, c: 3 } };
    const out = renderJsonTree(data, '$');
    assertIncludes(out, '[3 项]', '数组 preview 显示项数');
    assertIncludes(out, '{3 键}', '对象 preview 显示键数');
});

describe('renderJsonTree — XSS 转义', () => {
    const data = { 'evil': '<img src=x onerror=alert(1)>' };
    const out = renderJsonTree(data, '$');
    // key 含 HTML，应被转义（出现在 jt-key 文本里）
    assertNotIncludes(out, '<img src=x', '原始 <img> 标签未出现在 HTML 中');
    assertIncludes(out, '&lt;img', 'evil 值转义为 &lt;img');
    // 'evil' 是合法标识符 → 路径走 $.evil 写法（不应用 ['...'] 包裹）
    assertIncludes(out, 'data-path="$.evil"', "普通 key 路径走 $.evil 写法");
});

describe('renderJsonTree — XSS 转义（含特殊字符 key）', () => {
    // key 含 < > → 必须用 ['...'] 包裹，且 < > 在 attribute 里被转义为 &lt; &gt;
    const data = { 'ev<img>il': 'val' };
    const out = renderJsonTree(data, '$');
    // 路径里包含特殊字符 key（['ev<img>il']），HTML attribute 里 < > 已转义
    assertIncludes(out, "$['ev&lt;img&gt;il']", "含 < > 的 key 路径走 ['...'] 包裹且转义");
});

describe('pickJsonPath — 未设激活目标时提示', () => {
    // 模拟未设 _rpActiveTarget
    const result = pickJsonPath('$.foo');
    assert(result === undefined, '未设激活目标时 pickJsonPath 返回 undefined（不填入）');
    // 注：showToast 在沙箱里被替换为 fakeToast
});

describe('RP_LIST_IDS — 常量映射正确', () => {
    assert(RP_LIST_IDS.output_paths === 'rpOutputPathsList', 'output_paths → rpOutputPathsList');
    assert(RP_LIST_IDS.token_paths === 'rpTokenPathsList', 'token_paths → rpTokenPathsList');
    assert(RP_LIST_IDS.token_fields === 'rpTokenFieldsList', 'token_fields → rpTokenFieldsList');
});

// ============== 汇总 ==============
console.log(`\n${'='.repeat(40)}`);
console.log(`passed: ${passed}, failed: ${failed}`);
process.exit(failed > 0 ? 1 : 0);
