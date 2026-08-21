/**
 * 批次 3 前端组件测试
 *
 * 覆盖：
 * - T3-1: 项目配置 max_concurrency 输入 + 发起评测弹窗 samples/concurrency + saveProjectConfig/collectConfigSnapshot 含字段
 * - T3-1: run 详情指标卡 tooltip 标注「延迟在 N 并发下采集」
 * - T3-1: confirmRunEval 提交 body 含 samples/concurrency
 * - T3-2: replace 导入 toast 追加「采样统计已重置」+ 评测集 tab 重置空态提示
 *
 * 运行：node tests/test_t3_ui.js
 */

const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, '..', 'app', 'static', 'index.html');

function extractJs(html) {
    const m = html.match(/<script>([\s\S]*?)<\/script>/);
    if (!m) throw new Error('no <script> tag found');
    return m[1];
}

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

const html = fs.readFileSync(HTML_PATH, 'utf8');
const js = extractJs(html);

// ============== T3-1: 项目配置 max_concurrency 输入 ==============

describe('T3-1 项目配置 — max_concurrency 输入', () => {
    // 项目配置页含「采样与并发」卡片 + max_concurrency 输入框
    assertIncludes(js, '采样与并发', '配置页含「采样与并发」卡片标题');
    assertIncludes(js, 'id="maxConcurrency"', '含 maxConcurrency 输入框');
    assertIncludes(js, 'max_concurrency', '含 max_concurrency 字段名');
    assertIncludes(js, '1 = 串行', '提示 1 = 串行');
    // min=1 防止非法 0/负数
    assertIncludes(js, 'min="1"', 'maxConcurrency 最小值 1');
    // 提示并发会引入延迟噪声
    assertIncludes(js, '延迟噪声', '提示并发会引入延迟噪声');
    // 值从 project.max_concurrency 读取
    assertIncludes(js, 'project.max_concurrency', '值从 project.max_concurrency 读取');
});

// ============== T3-1: 发起评测弹窗 samples/concurrency ==============

describe('T3-1 发起评测弹窗 — samples/concurrency 输入', () => {
    // 弹窗含采样次数 k 输入
    assertIncludes(js, 'id="runSamples"', '含 runSamples 输入框');
    assertIncludes(js, '采样次数 k', '含采样次数 k 标签');
    assertIncludes(js, '每 case 跑几次', '含每 case 跑几次说明');
    // 弹窗含并发数输入
    assertIncludes(js, 'id="runConcurrency"', '含 runConcurrency 输入框');
    assertIncludes(js, '并发数', '含并发数标签');
    // 项目最大并发从 project 读取
    assertIncludes(js, 'proj?.max_concurrency', '从 project 读取 max_concurrency 作为上限');
    // maxConc <= 1 时 disabled
    assertIncludes(js, "maxConc <= 1 ? 'disabled'", '项目最大并发=1 时并发输入置灰');
});

// ============== T3-1: saveProjectConfig 含 max_concurrency ==============

describe('T3-1 saveProjectConfig — 提交含 max_concurrency', () => {
    // saveProjectConfig 函数内 config 对象含 max_concurrency
    const saveFnMatch = js.match(/async function saveProjectConfig[\s\S]*?^        \}/m);
    assert(saveFnMatch !== null, '找到 saveProjectConfig 函数');
    if (saveFnMatch) {
        const fnBody = saveFnMatch[0];
        assertIncludes(fnBody, 'max_concurrency:', 'config 对象含 max_concurrency 字段');
        assertIncludes(fnBody, "getElementById('maxConcurrency')", '从 maxConcurrency 输入框取值');
        // parseInt 兜底，空值 → 1
        assertIncludes(fnBody, "parseInt", '用 parseInt 解析');
        assertIncludes(fnBody, "|| 1", '空值兜底为 1');
    }
});

// ============== T3-1: collectConfigSnapshot 含 max_concurrency ==============

describe('T3-1 collectConfigSnapshot — dirty 检测含 max_concurrency', () => {
    // collectConfigSnapshot 返回对象含 max_concurrency
    const snapFnMatch = js.match(/function collectConfigSnapshot[\s\S]*?^        \}/m);
    assert(snapFnMatch !== null, '找到 collectConfigSnapshot 函数');
    if (snapFnMatch) {
        const fnBody = snapFnMatch[0];
        assertIncludes(fnBody, 'max_concurrency:', '快照对象含 max_concurrency 字段');
        assertIncludes(fnBody, "getElementById('maxConcurrency')", '从 maxConcurrency 输入框取值');
    }
});

// ============== T3-1: confirmRunEval 提交 body 含 samples/concurrency ==============

describe('T3-1 confirmRunEval — 提交 body 含 samples/concurrency', () => {
    const fnMatch = js.match(/async function confirmRunEval[\s\S]*?^        \}/m);
    assert(fnMatch !== null, '找到 confirmRunEval 函数');
    if (fnMatch) {
        const fnBody = fnMatch[0];
        // 从 runSamples 输入取值
        assertIncludes(fnBody, "getElementById('runSamples')", '从 runSamples 输入取采样次数');
        assertIncludes(fnBody, "getElementById('runConcurrency')", '从 runConcurrency 输入取并发数');
        // body 含 samples
        assertIncludes(fnBody, 'samples', 'body 含 samples 字段');
        // concurrency 有值时才加入 body（null = 串行）
        assertIncludes(fnBody, 'concurrency', 'body 含 concurrency 字段');
        // samples 兜底 1
        assertIncludes(fnBody, '|| 1', 'samples 空值兜底 1');
    }
});

// ============== T3-1: run 详情指标卡 concurrency tooltip ==============

describe('T3-1 run 详情 — 指标卡 concurrency 标注', () => {
    // P50 指标卡 tooltip 含并发采集说明
    assertIncludes(js, '延迟在 ${s.concurrency} 并发下采集', 'P50 指标卡 tooltip 含并发采集说明');
    // P95 指标卡同样标注
    const p95TooltipCount = (js.match(/延迟在 \$\{s\.concurrency\} 并发下采集/g) || []).length;
    assert(p95TooltipCount >= 2, `P50 和 P95 两处 tooltip 均含并发标注，实际 ${p95TooltipCount} 处`);
    // 指标卡 label 含 [N 并发] 角标
    assertIncludes(js, '[${s.concurrency} 并发]', '指标卡 label 含 [N 并发] 角标');
    // concurrency=1 时不标注（条件判断 s.concurrency > 1）
    assertIncludes(js, 's.concurrency > 1', 'concurrency > 1 才标注');
});

// ============== T3-2: replace 导入 toast ==============

describe('T3-2 replace 导入 — 采样重置 toast', () => {
    // confirmImport 函数内 mode === 'replace' 时追加 toast
    const fnMatch = js.match(/async function confirmImport[\s\S]*?^        \}/m);
    assert(fnMatch !== null, '找到 confirmImport 函数');
    if (fnMatch) {
        const fnBody = fnMatch[0];
        assertIncludes(fnBody, "mode === 'replace'", '检测 replace 模式分支');
        assertIncludes(fnBody, '采样统计已按新评测集重置', 'toast 含「采样统计已按新评测集重置」');
    }
});

// ============== T3-2: 评测集 tab 重置空态提示 ==============

describe('T3-2 评测集 tab — 重置后空态提示', () => {
    // renderEvalsetDetail 中检测 content_updated_at + total_runs === 0
    assertIncludes(js, 'evalset.content_updated_at', '检测 evalset.content_updated_at');
    assertIncludes(js, 'state._evalsetSampling', '检测 state._evalsetSampling');
    assertIncludes(js, 'total_runs === 0', '检测 total_runs === 0');
    assertIncludes(js, '评测集内容已更新，旧采样统计已清零', '含重置空态提示文案');
});

// ============== T3-3: 版本 tab + 时间线 + 对比 ==============

describe('T3-3 版本 tab — tab 按钮与 switchTab', () => {
    assertIncludes(html, 'data-tab="versions"', '含版本 tab 按钮');
    assertIncludes(js, "switchTab('versions')", 'tab 点击 switchTab versions');
    assertIncludes(js, "case 'versions':", 'switchTab 含 versions case');
    assertIncludes(js, 'function renderVersionsTab', '定义 renderVersionsTab 函数');
});

describe('T3-3 版本时间线 — 开新版本 + 删除', () => {
    assertIncludes(js, 'function showCreateVersionModal', '定义 showCreateVersionModal');
    assertIncludes(js, 'function confirmCreateVersion', '定义 confirmCreateVersion');
    assertIncludes(js, 'function deleteVersion', '定义 deleteVersion');
    assertIncludes(js, '版本时间线', '含版本时间线卡片标题');
    assertIncludes(js, '开新版本', '含开新版本按钮');
    // 弹窗含名称输入 + 必填阻断
    assertIncludes(js, 'id="newVersionName"', '含版本名称输入框');
    assertIncludes(js, 'createVersionBtn', 'disabled', '空名阻断提交');
    // POST /versions 端点
    assertIncludes(js, "/projects/${projectId}/versions", "method: 'POST'");
    assertIncludes(js, '版本已创建', '含创建成功 toast');
});

describe('T3-3 跨版本对比 — 表格 + delta 高亮', () => {
    assertIncludes(js, '跨版本对比', '含对比卡片标题');
    assertIncludes(js, '/projects/${projectId}/versions/compare', '调用对比端点');
    assertIncludes(js, 'delta_pass_rate', '含 delta_pass_rate 字段');
    assertIncludes(js, 'delta_total_token', '含 delta_total_token 字段');
    assertIncludes(js, 'Δ pass rate', '含 Δ pass rate 表头');
    assertIncludes(js, 'Δ token', '含 Δ token 表头');
    // delta 正绿负红
    assertIncludes(js, "var(--green)", 'delta 正值绿色');
    assertIncludes(js, "var(--red)", 'delta 负值红色');
    // 空态
    assertIncludes(js, '开版本并跑评测后', '对比空态文案');
});

describe('T3-3 历史列表 — 版本标注', () => {
    assertIncludes(js, 'r.version_id', '历史列表读取 run.version_id');
    assertIncludes(js, 'state._versionNames', '版本 id → name 映射缓存');
    assertIncludes(js, '<th>版本</th>', '历史列表含版本列表头');
    // version_id 为空时显示 -
    assertIncludes(js, "r.version_id ?", 'version_id 存在性判断');
});

// ============== T3-4: 定时回归 ==============

describe('T3-4 配置页 — 定时回归区', () => {
    assertIncludes(js, '定时回归', '配置页含定时回归卡片');
    assertIncludes(js, 'id="scheduleEnabled"', '含 schedule 开关 checkbox');
    assertIncludes(js, 'id="scheduleCron"', '含 cron 表达式输入');
    assertIncludes(js, 'id="scheduleTags"', '含标签筛选输入');
    assertIncludes(js, 'id="scheduleThreshold"', '含阈值输入');
    assertIncludes(js, 'id="scheduleFields"', '含可折叠字段区');
    assertIncludes(js, 'function updateScheduleToggle', '定义 updateScheduleToggle 开关切换');
});

describe('T3-4 saveProjectConfig — 含 schedule', () => {
    assertIncludes(js, 'function collectSchedule', '定义 collectSchedule 函数');
    assertIncludes(js, 'schedule: collectSchedule()', 'saveProjectConfig 含 schedule 字段');
    // collectSchedule 从表单收集 enabled/cron/tags/threshold
    const fnMatch = js.match(/function collectSchedule[\s\S]*?^        \}/m);
    assert(fnMatch !== null, '找到 collectSchedule 函数体');
    if (fnMatch) {
        const fnBody = fnMatch[0];
        assertIncludes(fnBody, 'scheduleEnabled', '读取 scheduleEnabled 开关');
        assertIncludes(fnBody, 'scheduleCron', '读取 scheduleCron 输入');
        assertIncludes(fnBody, 'scheduleTags', '读取 scheduleTags 输入');
        assertIncludes(fnBody, 'scheduleThreshold', '读取 scheduleThreshold 输入');
    }
});

describe('T3-4 collectConfigSnapshot — dirty 检测含 schedule', () => {
    assertIncludes(js, 'schedule_enabled:', '快照含 schedule_enabled');
    assertIncludes(js, 'schedule_cron:', '快照含 schedule_cron');
    assertIncludes(js, 'schedule_threshold:', '快照含 schedule_threshold');
});

describe('T3-4 回归告警横幅', () => {
    assertIncludes(js, 'id="regressionAlertBanner"', '项目详情页含回归告警横幅容器');
    assertIncludes(js, 'function loadRegressionAlertBanner', '定义 loadRegressionAlertBanner');
    assertIncludes(js, '/regression-alerts', '调用回归告警端点');
    assertIncludes(js, '回归告警', '含回归告警文案');
    assertIncludes(js, 'baseline', '告警含 baseline 对比');
    assertIncludes(js, 'loadRegressionAlertBanner(project.id)', '项目详情页加载时调用');
});

describe('T3-4 历史 tab — run 列表标红', () => {
    // renderHistoryTab 内并行拉取告警
    const fnMatch = js.match(/async function renderHistoryTab[\s\S]*?^        \}/m);
    assert(fnMatch !== null, '找到 renderHistoryTab 函数');
    if (fnMatch) {
        const fnBody = fnMatch[0];
        assertIncludes(fnBody, '/regression-alerts', 'renderHistoryTab 拉取告警端点');
        assertIncludes(fnBody, 'Promise.all', '并行拉取 runs 与 alerts');
        assertIncludes(fnBody, 'regressionRunIds', '构造告警 run_id 集合');
        assertIncludes(fnBody, 'regressionRunIds.has', '按 run_id 匹配告警');
        // 标红行：背景色
        assertIncludes(fnBody, 'background: #fef2f2', '回归行背景浅红');
        // pass rate 单元格含 ⚠ 红字
        assertIncludes(fnBody, 'color: var(--red)', '回归 pass rate 红色');
        assertIncludes(fnBody, '⚠', '回归 pass rate 含 ⚠ 图标');
    }
});

// ============== 汇总 ==============

console.log(`\n========================================`);
console.log(`批次 3 UI 组件测试：${passed} passed, ${failed} failed`);
if (failed > 0) {
    process.exit(1);
}
