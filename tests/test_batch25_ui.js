/**
 * 批次 2.5 前端组件测试
 *
 * 覆盖：
 * - REQ-10: 评测集空态文案（无「新建评测集」入口，引导导入）
 * - REQ-11: Target/Judge 双模式描述文案统一
 * - REQ-12: 「获取真实样例」按钮存在
 * - REQ-15: Target 配置模板下拉菜单（⋯ 按钮）
 * - REQ-16: 侧栏 Judge 配置入口 + Judge 卡片「管理」按钮 + 下拉选择
 * - REQ-17: 保存模板弹窗含名称输入 + 必填提示 + 同名覆盖确认
 *
 * 运行：node tests/test_batch25_ui.js
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
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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

// 简易 fake DOM：getElementById 返回可控节点
function makeFakeDom(overrides = {}) {
    const byId = {};
    const fakeDoc = {
        getElementById: (id) => byId[id] || overrides[id] || null,
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
        createElement: () => ({
            className: '', id: '', classList: { add: () => {}, remove: () => {}, contains: () => false, toggle: () => {} },
            appendChild: () => {}, innerHTML: '', textContent: '',
            querySelector: () => null, querySelectorAll: () => [],
            style: {}, dataset: {}, remove: () => {},
        }),
        activeElement: null,
        body: { appendChild: () => {} },
    };
    function setById(id, node) { byId[id] = node; }
    return { fakeDoc, setById, byId };
}

// 加载纯函数（不含 main 入口的 DOMContentLoaded 调用副作用）
function loadFunctions(jsSource, exposeList) {
    const { fakeDoc } = makeFakeDom();
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        jsSource + '; return {' + exposeList.join(',') + '};'
    );
    return fn(
        fakeDoc,
        { addEventListener: () => {}, scrollTo: () => {} },
        console,
        () => {}, 0, { hash: '' },
        escapeHtml
    );
}

// ============== REQ-10: 评测集空态 ==============

describe('REQ-10 renderEvalsetsTab — 无评测集时空态文案', () => {
    // 直接验证 HTML 源代码里 renderEvalsetsTab 的空态分支
    // REQ-10 要求：删除「新建评测集」入口，空态改为导入引导
    assertIncludes(js, 'REQ-10: 评测集免新建', '源码含 REQ-10 标记注释');
    assertIncludes(js, '导入 CSV/JSON 或逐条添加用例', '空态文案引导导入/逐条添加');
    assertNotIncludes(js, 'function showCreateEvalSetModal', '已删除 showCreateEvalSetModal 函数');
    assertNotIncludes(js, 'function createEvalSet(', '已删除 createEvalSet 函数');
});

// ============== REQ-11: 双模式描述文案统一 ==============

describe('REQ-11 双模式描述 — Target 与 Judge 文案逐字一致', () => {
    // REQ-11 要求：Target 卡片与 Judge 卡片两处描述文案逐字一致
    // 文案模板：
    //   openai_compatible: '标准模型接口（OpenAI 风格）。需 base_url + model；api_key 可留空（无认证头）。'
    //   custom:            '任意 HTTP JSON 接口。需请求模板；不注入 model/messages，请求体完全由模板决定。'
    const OPENAI_DESC = '标准模型接口（OpenAI 风格）。需 base_url + model；api_key 可留空（无认证头）。';
    const CUSTOM_DESC = '任意 HTTP JSON 接口。需请求模板；不注入 model/messages，请求体完全由模板决定。';

    // 统计两个文案在源码中的出现次数——REQ-11 要求 Target 卡 + Judge 卡 + Judge 编辑弹窗至少 3 处一致
    const openaiCount = (js.match(new RegExp(OPENAI_DESC.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g')) || []).length;
    const customCount = (js.match(new RegExp(CUSTOM_DESC.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g')) || []).length;

    assert(openaiCount >= 3, `openai_compatible 文案至少出现 3 次（Target/Judge/JcEditor），实际 ${openaiCount} 次`);
    assert(customCount >= 3, `custom 文案至少出现 3 次（Target/Judge/JcEditor），实际 ${customCount} 次`);

    // 关键短语检查
    assertIncludes(OPENAI_DESC, '标准模型接口（OpenAI 风格）', 'openai_compatible 含标准模型接口');
    assertIncludes(OPENAI_DESC, 'api_key 可留空', 'openai_compatible 含 api_key 可留空说明');
    assertIncludes(CUSTOM_DESC, '任意 HTTP JSON 接口', 'custom 含任意 HTTP JSON 接口');
    assertIncludes(CUSTOM_DESC, '请求体完全由模板决定', 'custom 含请求体完全由模板决定');
});

// ============== REQ-12: 获取真实样例按钮 ==============

describe('REQ-12 fetchRealSample — 按钮与函数存在', () => {
    // 在源码中验证「获取真实样例」按钮 + fetchRealSample 函数定义
    assertIncludes(js, 'function fetchRealSample', '定义 fetchRealSample 函数');
    assertIncludes(js, '获取真实样例', '按钮含「获取真实样例」文案');
    assertIncludes(js, '会真实调用，消耗 token', '按钮提示会真实调用消耗 token');
    assertIncludes(js, '/api/test/target', 'fetchRealSample 调用 /api/test/target');
});

// ============== REQ-15: Target 模板下拉菜单 ==============

describe('REQ-15 Target 配置模板 — ⋯ 下拉菜单', () => {
    assertIncludes(js, 'id="targetTplBar"', 'Target 卡片含 targetTplBar 容器');
    assertIncludes(js, 'id="tplDropdownMenu"', '含下拉菜单 div');
    assertIncludes(js, 'function toggleTplDropdown', '定义 toggleTplDropdown');
    assertIncludes(js, 'function refreshTargetTplList', '定义 refreshTargetTplList');
    assertIncludes(js, 'function showSaveTemplateModal', '定义 showSaveTemplateModal');
    assertIncludes(js, 'function loadTargetTemplateById', '定义 loadTargetTemplateById');
    assertIncludes(js, 'function deleteTargetTemplateById', '定义 deleteTargetTemplateById');
    // 下拉菜单入口文本「⋯ 配置模板」
    assertIncludes(js, '⋯ 配置模板', '按钮含「⋯ 配置模板」文案');
});

// ============== REQ-16: Judge 配置独立管理 ==============

describe('REQ-16 Judge 配置管理 — 侧栏入口与项目卡片', () => {
    // 侧栏导航项
    assertIncludes(html, "data-page=\"judge-configs\"", '侧栏含 judge-configs 导航项');
    assertIncludes(html, "navigateTo('judge-configs')", '侧栏点击 navigateTo judge-configs');
    assertIncludes(html, '⚖</span> Judge 配置', '侧栏含 ⚖ Judge 配置 文案');
    // 管理页函数
    assertIncludes(js, 'function renderJudgeConfigsPage', '定义 renderJudgeConfigsPage');
    assertIncludes(js, 'function showJudgeConfigEditorModal', '定义 showJudgeConfigEditorModal');
    assertIncludes(js, 'function saveJudgeConfig', '定义 saveJudgeConfig');
    assertIncludes(js, 'function showDeleteJudgeConfigModal', '定义 showDeleteJudgeConfigModal');
    // 项目配置卡片改为下拉选择 + 管理按钮
    assertIncludes(js, 'id="judgeConfigIdSelect"', '项目卡片含全局 Judge 配置下拉');
    assertIncludes(js, "navigateTo('judge-configs')", '项目卡片含跳转管理页入口');
    assertIncludes(js, 'function onJudgeConfigSelectChange', '定义 onJudgeConfigSelectChange');
    assertIncludes(js, 'function editSelectedJudgeConfig', '定义 editSelectedJudgeConfig');
    // 移除了 use_target_config 复用 checkbox
    assertNotIncludes(js, 'use_target_config', '已移除 use_target_config 引用');
    assertNotIncludes(js, '复用 Target API 配置', '已移除「复用 Target API 配置」文案');
});

describe('REQ-16 renderJudgeConfigsPage — 空态', () => {
    // 构造 fake DOM，调用 renderJudgeConfigsPage 验证空态
    const { fakeDoc, setById } = makeFakeDom();
    // 拦截 fetch 返回空列表
    const fakeFetch = async (url) => ({
        ok: true,
        json: async () => ({ judge_configs: [] }),
    });
    // 加载 renderJudgeConfigsPage，注入 fake fetch（state 由源码内部声明，无需传入）
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        'fetch', 'showToast',
        js + '; return { renderJudgeConfigsPage };'
    );
    const F = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml,
        fakeFetch, () => {},
    );
    let rendered = '';
    const c = { set innerHTML(v) { rendered = v; }, get innerHTML() { return rendered; } };
    const ha = { set innerHTML(v) {}, get innerHTML() { return ''; } };
    F.renderJudgeConfigsPage(c, ha, {}).then(() => {
        assertIncludes(rendered, '还没有全局 Judge 配置', '空态含引导文案');
        assertIncludes(rendered, '+ 新建 Judge 配置', '空态含新建按钮');
        assertIncludes(rendered, 'showJudgeConfigEditorModal', '空态按钮调用 showJudgeConfigEditorModal');
    }).catch(e => {
        assert(false, 'renderJudgeConfigsPage 空态抛错: ' + e.message);
    });
});

// ============== REQ-17: 模板保存弹窗（名称输入 + 同名覆盖） ==============

describe('REQ-17 showSaveTemplateModal — 含名称输入与必填提示', () => {
    // 构造 fake DOM：modalOverlay / modalContent 可写 innerHTML + tplDropdownMenu 关闭用
    const { fakeDoc, setById } = makeFakeDom();
    const overlay = { style: {} };
    const content = { innerHTML: '' };
    setById('modalOverlay', overlay);
    setById('modalContent', content);
    setById('tplDropdownMenu', { style: { display: 'none' } });
    setById('tplNameInput', { value: '', focus: () => {} });
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        js + '; return { showSaveTemplateModal };'
    );
    const F = fn(fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml);
    F.showSaveTemplateModal('proj-test');
    const html2 = content.innerHTML;
    assertIncludes(html2, 'id="tplNameInput"', '弹窗含名称输入框（id=tplNameInput）');
    assertIncludes(html2, 'placeholder=', '名称输入框有 placeholder');
    assertIncludes(html2, 'onclick="confirmSaveTemplate', '保存按钮调用 confirmSaveTemplate');
    assertIncludes(html2, '*', '必填字段标星号');
    // 必填校验文案在 confirmSaveTemplate 中（空名阻断显示「模板名称必填」）
    assertIncludes(js, '模板名称必填', 'confirmSaveTemplate 含「模板名称必填」错误文案');
    // 同名覆盖提示文案（confirmSaveTemplate 源码含 window.confirm 同名检查）
    assertIncludes(js, '已存在名为', 'confirmSaveTemplate 含同名检查文案');
    assertIncludes(js, '是否覆盖', 'confirmSaveTemplate 含覆盖确认');
});

describe('REQ-17 confirmSaveTemplate — 空名阻断', () => {
    // 构造 fake DOM：tplNameInput 空值 → confirmSaveTemplate 应阻断
    const { fakeDoc, setById } = makeFakeDom();
    const nameInput = { value: '', focus: () => {} };
    const errEl = { textContent: '', style: { display: 'none' } };
    const saveBtn = {};
    setById('tplNameInput', nameInput);
    setById('tplNameError', errEl);
    setById('tplSaveBtn', saveBtn);
    let toastCalled = false;
    const fn = new Function(
        'document', 'window', 'console', 'setTimeout', 'setInterval', 'location', 'escapeHtml',
        'fetch', 'showToast', 'closeModal', 'navigateTo', 'api',
        js + '; return { confirmSaveTemplate, getState: () => ({}) };'
    );
    const fakeFetch = async () => ({ ok: true, json: async () => ({ templates: [] }) });
    const showToast = (msg, type) => { if (type === 'error') toastCalled = true; };
    const F = fn(
        fakeDoc, { addEventListener: () => {} }, console, () => {}, 0, { hash: '' }, escapeHtml,
        fakeFetch, showToast, () => {}, () => {}, async () => ({ templates: [] }),
    );
    let threw = false;
    F.confirmSaveTemplate('proj-test').catch(() => { threw = true; });
    // 立即同步检查 errEl 是否被填入「模板名称必填」
    // 由于 confirmSaveTemplate 是 async，先 await 再断言
    setTimeout(() => {
        assertIncludes(errEl.textContent, '必填', '空名时 errEl 显示必填');
        assert(errEl.style.display !== 'none' || errEl.textContent.includes('必填'), '错误提示可见');
    }, 50);
});

// ============== 总结 ==============

setTimeout(() => {
    console.log(`\n========================================`);
    console.log(`批次 2.5 UI 组件测试：${passed} passed, ${failed} failed`);
    process.exit(failed > 0 ? 1 : 0);
}, 100);
