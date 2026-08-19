/**
 * 采样稳定性卡片前端组件测试
 *
 * 通过 node 提取 index.html 中的 JS，在沙箱中执行渲染函数，
 * 验证 SVG 输出结构、空态、低 coverage 标灰、数据点坐标等。
 *
 * 运行：node tests/test_sampling_ui.js
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
 * 在隔离的沙箱中只暴露渲染函数所需的依赖，
 * 避免 document/window/api 等全局未定义导致 Function() 报错。
 */
function loadRenderingFunctions(jsSource) {
    // 提供桩：document/window/api 在函数定义时不会被访问，
    // 只有调用 loadSamplingCard/renderProjectOverview 才会，我们不调用这些。
    const sandbox = {
        document: { getElementById: () => null, querySelectorAll: () => [], addEventListener: () => {} },
        window: { addEventListener: () => {}, scrollTo: () => {} },
        console,
        setTimeout: () => {},
        setInterval: () => 0,
        location: { hash: '' },
        escapeHtml: (s) => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
    };
    // 把整个 JS 体的函数定义塞进沙箱
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        jsSource + '; return { renderSamplingCard, renderSamplingChart };'
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

function assertEqual(actual, expected, msg) {
    if (actual === expected) { passed++; }
    else { failed++; console.error(`  ✗ FAIL: ${msg}\n    expected: ${JSON.stringify(expected)}\n    actual:   ${JSON.stringify(actual)}`); }
}

function describe(name, fn) {
    console.log(`\n${name}`);
    fn();
}

// ============== 测试 ==============

const html = fs.readFileSync(HTML_PATH, 'utf8');
const js = extractJs(html);
const { renderSamplingCard, renderSamplingChart } = loadRenderingFunctions(js);

describe('renderSamplingCard — 空态', () => {
    const html = renderSamplingCard({ total_runs: 0, total_cases: 0, k_values: [1,2,3], pass_at_k: [], pass_pow_k: [] });
    assert(html.includes('跑两次评测后这里会显示采样稳定性'), '空态文案');
    assert(html.includes('基于 case_name 跨 run 对齐'), '注脚');
    assert(!html.includes('<svg'), '空态不渲染 SVG');
});

describe('renderSamplingCard — 数据不足（所有 value=null）', () => {
    const data = {
        total_runs: 1, total_cases: 2, k_values: [1,2,3],
        pass_at_k: [{k:1, value:0.5, coverage:2}, {k:2, value:null, coverage:0}, {k:3, value:null, coverage:0}],
        pass_pow_k: [{k:1, value:0.5, coverage:2}, {k:2, value:null, coverage:0}, {k:3, value:null, coverage:0}],
    };
    // 至少 k=1 有值，所以不会走 allNull 分支
    const html = renderSamplingCard(data);
    assert(html.includes('<svg'), 'k=1 有值时渲染 SVG');
});

describe('renderSamplingCard — 正常数据', () => {
    const data = {
        total_runs: 5, total_cases: 4, k_values: [1,2,3],
        pass_at_k: [
            {k:1, value:0.85, coverage:4},
            {k:2, value:0.92, coverage:4},
            {k:3, value:0.95, coverage:3},
        ],
        pass_pow_k: [
            {k:1, value:0.85, coverage:4},
            {k:2, value:0.70, coverage:4},
            {k:3, value:0.55, coverage:3},
        ],
    };
    const html = renderSamplingCard(data);
    assert(html.includes('<svg'), '渲染 SVG');
    assert(html.includes('pass@k'), '图例 pass@k');
    assert(html.includes('pass^k'), '图例 pass^k');
    assert(html.includes('不确定性区间'), '图例 不确定性');
    assert(html.includes('共 5 次 run'), '总数显示');
    assert(html.includes('4 个 case'), 'case 数显示');
    // k=3 coverage=3/4=0.75 > 0.6，不应触发低 coverage 提示
    assert(!html.includes('采样不足'), 'coverage 充足时不提示');
});

describe('renderSamplingCard — 低 coverage 提示', () => {
    const data = {
        total_runs: 2, total_cases: 10, k_values: [1,2,3],
        pass_at_k: [
            {k:1, value:0.8, coverage:10},  // 10/10 = 100% 充足
            {k:2, value:0.85, coverage:2},   // 2/10 = 20% 低
            {k:3, value:null, coverage:0},    // 0/10 = 0% 低
        ],
        pass_pow_k: [
            {k:1, value:0.8, coverage:10},
            {k:2, value:0.5, coverage:2},
            {k:3, value:null, coverage:0},
        ],
    };
    const html = renderSamplingCard(data);
    assert(html.includes('k=2：采样不足'), 'k=2 低 coverage 提示');
    assert(html.includes('k=3：需至少 3 次 run'), 'k=3 完全无 coverage 提示');
});

describe('renderSamplingChart — SVG 结构', () => {
    const atK = [
        {k:1, value:0.85, coverage:4},
        {k:2, value:0.92, coverage:4},
        {k:3, value:0.95, coverage:3},
    ];
    const powK = [
        {k:1, value:0.85, coverage:4},
        {k:2, value:0.70, coverage:4},
        {k:3, value:0.55, coverage:3},
    ];
    const svg = renderSamplingChart(atK, powK, 4);

    assert(svg.trim().startsWith('<svg'), '以 <svg 开头');
    assert(svg.includes('</svg>'), '以 </svg> 结尾');
    assert(svg.includes('viewBox='), '有 viewBox');
    assert(svg.includes('k=1') && svg.includes('k=2') && svg.includes('k=3'), 'x 轴标签');
    // 网格线 y 刻度
    assert(svg.includes('0.00') && svg.includes('1.00'), 'y 轴 0/1 刻度');
    // 折线路径
    assert(svg.includes('<path'), '有 path 元素');
    // 数据点
    const circleCount = (svg.match(/<circle/g) || []).length;
    assertEqual(circleCount, 6, '6 个数据点（3 atK + 3 powK）');
    // 不确定性区域
    assert(svg.includes('accent-pale'), '不确定性区域填充');
});

describe('renderSamplingChart — pass@k 点位置', () => {
    // k=1 value=1.0 应在图顶部，k=2 value=0.0 应在图底部
    const atK = [{k:1, value:1.0, coverage:2}, {k:2, value:0.0, coverage:2}];
    const powK = [{k:1, value:1.0, coverage:2}, {k:2, value:0.0, coverage:2}];
    const svg = renderSamplingChart(atK, powK, 2);
    // 顶部 y 应小于底部 y
    const circles = [...svg.matchAll(/cy="([\d.]+)"/g)].map(m => parseFloat(m[1]));
    const maxY = Math.max(...circles);
    const minY = Math.min(...circles);
    assert(maxY > minY, '不同 value 对应不同 y 坐标');
});

describe('renderSamplingChart — 低 coverage 标灰', () => {
    const atK = [
        {k:1, value:0.8, coverage:10},  // 充足
        {k:3, value:0.9, coverage:1},   // 1/10=10% 低
    ];
    const powK = [
        {k:1, value:0.8, coverage:10},
        {k:3, value:0.5, coverage:1},
    ];
    const svg = renderSamplingChart(atK, powK, 10);
    // 低 coverage 点用 ink-5，正常点用 accent
    assert(svg.includes('var(--ink-5)'), '低 coverage 点标灰');
    assert(svg.includes('var(--accent)'), '正常点用主色');
});

describe('renderSamplingChart — null value 跳过', () => {
    const atK = [{k:1, value:0.5, coverage:2}, {k:2, value:null, coverage:0}, {k:3, value:0.7, coverage:2}];
    const powK = [{k:1, value:0.5, coverage:2}, {k:2, value:null, coverage:0}, {k:3, value:0.7, coverage:2}];
    const svg = renderSamplingChart(atK, powK, 2);
    // 应有 4 个 circle（2 个 atK + 2 个 powK，跳过 k=2 的 null）
    const circleCount = (svg.match(/<circle/g) || []).length;
    assertEqual(circleCount, 4, 'null value 的点不渲染');
    // 路径应包含 M（中断后重新起笔）
    assert(svg.includes('M '), '路径在 null 处断开重新起笔');
});

// ============== 结果 ==============

console.log(`\n===============================`);
console.log(`UI 组件测试：${passed} passed, ${failed} failed`);
if (failed > 0) {
    process.exit(1);
}
