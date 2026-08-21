# simpleEval

一个面向 LLM 工作流与 Agent 的评测工具。给评测集，跑一遍，得到通过率、成本和稳定性——不用再说"观察一下"。

---

## 起点

"观察一下。"

这是我调试 AI 工作流时，最常说给业务方的一句话。直到有一次，对方反问：

> "观察一下……那什么时候不用观察？"

我答不上来。AI 应用的效果好不好，我居然没有一个量化的答案。simpleEval 就是那个答案：当你能说出"通过率 88%、每万 token 完成率 3.4"时，就不用再说"观察一下"了。

---

## Quick Start

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --port 8000
```

打开 http://localhost:8000 ，五步：

1. 新建项目
2. 配置 Target API（OpenAI 兼容 / 自定义 API 两种模式）
3. 配置 Judge（全局配置管理页，项目里引用）
4. 导入 `examples/demo-evalset.json`（15 条客服用例，开箱即用）
5. 发起评测，看结果

完整使用说明见 [docs/usage-guide.md](docs/usage-guide.md)。

---

## 它长什么样

评测集管理：五类评测类型、标签筛选、逐条采样数据。

![评测集管理](docs/screenshots/evalset-tab.png)

评测结果：通过率、成本拆分（含 Judge 消耗）、三栏对比。

![评测结果](docs/screenshots/run-detail.png)

项目总览：趋势与采样稳定性——pass@k 与 pass^k 的夹缝宽度，就是这个项目的"不确定性"。

![项目总览](docs/screenshots/overview.png)

![采样稳定性](docs/screenshots/sampling-card.png)

---

## 能做什么

- **多变量输入**：工作流要 leader/enterprise/content 三个变量？case 里配好，模板里 `{key}` 引用
- **非标返回解析**：路径提取、字段递归统计 token、content 内嵌 JSON 二次解包——不假设返回结构
- **多字段验证**：一次返回多个字段，各自用不同规则验证
- **采样稳定性**：pass@k（会不会）与 pass^k（稳不稳），case 级下钻，最不稳定的排最上面
- **成本视角**：每万 token 完成率；Judge 消耗计入评测成本；token 预算可硬中断
- **标签筛选**：冒烟组日常跑，回归组大改跑，按需组合
- **异步执行**：发起即返回，关页面不打断，任务状态随时找回

## 为什么是这样

- **只依赖输入输出**：不要求埋 SDK、不假设框架——搭在低代码平台上的工作流照样测
- **数据不出你的机器**：本地 JSON 存储，评测集和结果都可以 git diff
- **数字带着条件**：token 缺失显式标记，延迟标注采集条件——诚实比好看重要
- **薄基座**：FastAPI + 单文件前端，无数据库、无 ORM、无前端框架

---

## 一个真实用法

还是这套 demo 客服集：15 条用例，三个标签——`smoke`（冒烟）、`regression`（回归）、`edge_case`（边界）。

每次改客服 prompt 或模型后：

```bash
# 日常小改：只跑冒烟组
发起评测 → 标签选「smoke」→ 5 条快检

# 大改：跑全量
发起评测 → 不筛标签 → 15 条全跑
```

上面的截图就是这么跑出来的：三轮下来 pass rate 从 73.3% 爬到 80.0%，趋势图里那条线就是改动的证据链。

---

## 测试

```bash
python3 -m pytest tests/ -q
```

353 个测试，覆盖评测类型、模板渲染、响应解析、采样统计、API 全链路。

## 文档

- [使用指南](docs/usage-guide.md) —— 从零到第一次评测
- [API 契约](docs/api-contract.md)
- [UI 设计规范](docs/ui-spec.md)

## 版本日志

### v0.2（2026-08-21）

- Judge 全局配置管理；Judge 双模式；Judge token 计入成本
- 一项目一评测集 + case 标签筛选
- case 级采样稳定性；多变量输入 + 二次解包 + 多字段验证
- token 预算硬限制；配置模板；样例响应自动获取

### v0.1（2026-08-19）

- 首个可用版本：五类评测类型、成本分析、异步执行、Web UI
- 采样稳定性（历史聚合）
