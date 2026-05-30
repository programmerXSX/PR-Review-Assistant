# AI PR Review Assistant

> 粘贴一个公开 GitHub PR 链接，AI 自动分析变更，输出 **变更总结 + 结构化风险 Findings + 修改建议**。

---

## 功能

| 能力 | 说明 |
|---|---|
| **变更总结** | 一句话概览 + 按文件分条说明 + 架构/接口/依赖变动 + 影响范围（中文 Markdown） |
| **风险识别** | 7 个维度（逻辑/安全/性能/可维护性/边界/风格/测试），结构化 findings |
| **置信度分级** | high / medium / low，低置信度折叠展示，高置信度直接展开 |
| **容错降级** | 非法 JSON 自动重试，网络超时友好提示，PR 不存在给出可读错误 |
| **噪音过滤** | 自动跳过 lock 文件 / 二进制 / 生成产物 / node_modules 等 |

---

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11+ / FastAPI |
| 前端 | Streamlit（胖客户端，直接调 `/api/review`） |
| 大模型 | DeepSeek V4 Pro（1M 上下文，Thinking / Non-Thinking 双模式） |
| 模型 SDK | OpenAI Python SDK（改 `base_url` 指向 DeepSeek） |
| GitHub API | httpx（取 PR 元信息 / 文件列表 / 全文） |

---

## 快速开始

### 1. 安装依赖

```bash
# 先激活虚拟环境（Windows）:
.venv\Scripts\activate

# macOS / Linux:
source .venv/bin/activate
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`，填入必填项：

```
DEEPSEEK_API_KEY=sk-your-key-here
```

其余项有合理默认值，按需调整。建议可选配 `GITHUB_TOKEN` 提升 API 速率限制（匿名 60 次/小时 → 带 token 5000 次/小时）。

### 3. 启动

**第一步：激活虚拟环境**

```bash
# Windows PowerShell / cmd（在项目根目录 E:\LLM\PR_Review_Assistant 执行）:
.venv\Scripts\activate

# macOS / Linux:
source .venv/bin/activate
```

**第二步：同步依赖库，项目由uv管理：**
```bash
# 执行
uv sync
```

> 如果不想每次手动激活，也可以跳过这一步，直接用 venv 里的 python 替代 `uvicorn` / `streamlit` 命令（见下方备选方式）。

**第二步：分别启动后端和前端**

终端 1 — 后端：

```bash
cd ai-pr-review  #进入项目文件夹
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000 # 启动后端
```

终端 2 — 前端：

```bash
cd ai-pr-review  #进入项目文件夹
streamlit run frontend/app.py  # 启动前端
```

打开 <http://localhost:8501>。

> **备选方式（无需手动激活 venv）**：用 venv 内的 python 直接启动，效果相同:
>
> ```bash
> cd ai-pr-review
> ..\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000   # 后端
> ..\.venv\Scripts\python.exe -m streamlit run frontend/app.py                                     # 前端
> ```
>
> **Windows 注意**：所有命令必须在 `ai-pr-review/` 目录下执行，否则 Python 找不到 `backend` 包。

---

## 使用说明

1. 在浏览器打开 <http://localhost:8501>
2. 粘贴一个公开 GitHub PR URL，格式为 `https://github.com/<owner>/<repo>/pull/<n>`
3. 可选展开「⚙️ 高级选项」调整 `max_files`、Thinking 开关、token 预算
4. 点击「🚀 开始评审」，等待 30-120 秒
5. 查看结果：PR 信息 + 变更总结 + 分层的风险 Findings

---

## 推荐演示 PR

| PR | 仓库 | 文件 | 特点 |
|---|---|---|---|
| [#6702](https://github.com/psf/requests/pull/6702) | psf/requests | 5 | 依赖重构，代码变更适中 |
| [#6801](https://github.com/psf/requests/pull/6801) | psf/requests | 1 | 纯文档 PR，观察 findings 是否合理为空 |
| [#33000](https://github.com/facebook/react/pull/33000) | facebook/react | 5 | 前端项目，中等规模 |

---

## 已知限制

- **仅支持公开仓库** — 私有仓库 / GitHub App / webhook 不在 MVP 范围内。
- **上下文 Level 1** — 只看本 PR 的 diff + 文件全文，不做跨文件调用链 / RAG 检索。
- **同步阻塞** — 大型 PR 可能耗时 60-120 秒，前端等待期间无流式输出。
- **GitHub 限流** — 匿名 60 次/小时，频繁测试建议配 `GITHUB_TOKEN`。
- **行号精度** — 依赖 LLM 从全文定位行号，偶有偏移。
- **无持久化** — 结果存内存，刷新页面或重启服务后丢失。
- **仅 GitHub** — 不支持 GitLab / Bitbucket。

---

## 未来方向

- **GitHub App + webhook** — PR 打开/更新自动触发审查，结果回写行内评论。
- **上下文 Level 2/3** — 顺 import 链拉关联文件；大仓库 embedding RAG 检索。
- **模型分层路由** — 便宜模型做总结，强模型做深度 review。
- **多平台** — GitLab / Bitbucket 支持。
- **CI 集成** — 作为流水线步骤，风险超阈值时阻断合并。
- **流式输出** — SSE 流式返回 findings，改善等待体验。
- **团队规范注入** — 把项目风格指南/编码规范作为审查上下文。

---

## 项目结构

```
ai-pr-review/
├── backend/
│   ├── __init__.py          # 包声明
│   ├── main.py              # FastAPI 入口（/api/health, /api/review）
│   ├── config.py            # 环境变量读取（.env + Settings）
│   ├── models.py            # Pydantic v2 数据结构
│   ├── github_client.py     # GitHub REST API 客户端
│   ├── context_builder.py   # 上下文构建（过滤/裁剪/token 预算）
│   ├── llm_client.py        # DeepSeek V4 调用封装（Thinking/Non-Thinking）
│   ├── prompts.py           # 两趟 System Prompt 模板
│   ├── orchestrator.py      # 分析编排（并发两趟 + 重试）
│   ├── aggregator.py        # Findings 解析/校验/去重/排序
├── frontend/
│   └── app.py               # Streamlit 前端
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```
