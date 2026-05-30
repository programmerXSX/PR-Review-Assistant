"""AI PR Review Assistant — Streamlit 前端。

启动方式（先激活 venv）:

    # 在项目根目录执行:
    .venv/Scripts/activate
    cd ai-pr-review
    streamlit run frontend/app.py

    # 或不用 activate，直接用 venv 里的 python:
    ../.venv/Scripts/python.exe -m streamlit run frontend/app.py

依赖:
    - 后端需先在另一个终端启动:
      cd ai-pr-review
      uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
    - 配置见 ../.env（BACKEND_URL、REQUEST_TIMEOUT_SEC）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# 加载 .env
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    _env_vars: dict[str, str] = {}
    with open(_ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            _env_vars[key.strip()] = value.strip().strip('"').strip("'")
    for k, v in _env_vars.items():
        if k not in os.environ:
            os.environ[k] = v

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SEC", "180"))

# ---------------------------------------------------------------------------
# 页面配置
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="AI PR Review Assistant",
    page_icon="🔍",
    layout="wide",
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SEVERITY_EMOJI = {
    "high": "🔴",
    "medium": "🟠",
    "low": "⚪",
}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}

CATEGORY_LABELS: dict[str, str] = {
    "logic": "逻辑错误",
    "security": "安全问题",
    "performance": "性能问题",
    "maintainability": "可维护性",
    "edge_case": "边界/异常",
    "style": "风格规范",
    "test": "测试缺失",
}

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _validate_pr_url(url: str) -> bool:
    """简单校验 PR URL 格式，避免明显非法的请求打到后端。"""
    import re
    return bool(re.match(r"^https?://github\.com/[^/]+/[^/]+/pull/\d+", url))


def _sort_findings(findings: list[dict]) -> list[dict]:
    """按 severity 降序 → confidence 降序排序。"""
    return sorted(
        findings,
        key=lambda f: (
            SEVERITY_ORDER.get(f.get("severity", "low"), 3),
            CONFIDENCE_ORDER.get(f.get("confidence", "low"), 3),
        ),
    )


def _group_by_severity(findings: list[dict]) -> dict[str, list[dict]]:
    """将 findings 按 severity 分组，各组内部已排序。"""
    groups: dict[str, list[dict]] = {"high": [], "medium": [], "low": []}
    for f in _sort_findings(findings):
        sev = f.get("severity", "low")
        groups.setdefault(sev, []).append(f)
    return groups


# ---------------------------------------------------------------------------
# 渲染函数
# ---------------------------------------------------------------------------

def _render_pr_info(resp: dict) -> None:
    """渲染 PR 基本信息区。"""
    stats = resp.get("stats", {})
    files = resp.get("files", [])

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("📝 PR 标题", resp.get("pr_title", "—"))
    col2.metric("👤 作者", resp.get("pr_author", "—"))
    col3.metric("📁 文件数", f"{stats.get('total_files', len(files))} (含全文 {stats.get('analyzed_files', '?')})")
    col4.metric("⏱ 耗时", f"{stats.get('elapsed_sec', '?')}s")

    col_a, col_b = st.columns(2)
    col_a.caption(f"模型: {stats.get('model', '—')}  |  估算 tokens: {stats.get('est_input_tokens', '—')}")
    col_b.caption(f"Skipped: {stats.get('skipped_files', 0)} 个文件仅保留 diff")

    # Warnings — 重要警告始终展示，非关键信息折叠
    warnings = resp.get("warnings", [])
    if warnings:
        # 关键类警告（截断/预算/解析失败/重试）在顶部直接展示
        critical_keywords = ["文件数超上限", "token 接近上限", "解析失败", "重试", "调用失败"]
        critical_warns = [w for w in warnings if any(k in w for k in critical_keywords)]
        info_warns = [w for w in warnings if w not in critical_warns]

        for w in critical_warns:
            st.warning(f"⚠️ {w}")
        if info_warns:
            with st.expander(f"ℹ️ 处理信息 ({len(info_warns)} 条)", expanded=False):
                for w in info_warns:
                    st.info(w)

    # 变更文件清单
    if files:
        with st.expander(f"📋 变更文件清单 ({len(files)} 个)", expanded=False):
            for fm in files:
                full_tag = "📄 全文" if fm.get("included_full_content") else "✂️ 仅 diff"
                st.text(
                    f"{full_tag}  {fm['filename']}  "
                    f"({fm.get('status', '?')}, +{fm.get('additions', 0)}/-{fm.get('deletions', 0)})"
                )


def _render_summary(summary: str) -> None:
    """渲染变更总结（Markdown）。"""
    st.markdown("## 📄 变更总结")
    st.markdown(summary)


def _render_findings(findings: list[dict]) -> None:
    """渲染风险 findings，按 severity 分组 + confidence 折叠。"""
    if not findings:
        st.success("✅ 未发现需要关注的问题。")
        return

    st.markdown("## 🔍 风险 Findings")
    st.caption(f"共 {len(findings)} 条，按严重度 × 置信度排列。低置信度条目已折叠，仅供参考。")

    groups = _group_by_severity(findings)
    severity_labels = {
        "high": f"{SEVERITY_EMOJI['high']} 高风险 ({len(groups['high'])} 条)",
        "medium": f"{SEVERITY_EMOJI['medium']} 中风险 ({len(groups['medium'])} 条)",
        "low": f"{SEVERITY_EMOJI['low']} 低风险 ({len(groups['low'])} 条)",
    }

    for sev in ["high", "medium", "low"]:
        items = groups.get(sev, [])
        if not items:
            continue

        st.markdown(f"### {severity_labels[sev]}")
        for idx, f in enumerate(items):
            confidence = f.get("confidence", "low")
            expanded = confidence != "low"  # low confidence 默认折叠

            with st.expander(
                f"{SEVERITY_EMOJI.get(sev, '?')} "
                f"[{f.get('category', '?')}] "
                f"[置信度: {confidence}] "
                f"{f.get('title', '无标题')}",
                expanded=expanded,
            ):
                # 位置信息
                file = f.get("file", "?")
                ls = f.get("line_start")
                le = f.get("line_end")
                location = file
                if ls is not None:
                    location += f":{ls}"
                    if le is not None:
                        location += f"~{le}"
                st.caption(f"📍 {location}")

                # 描述与建议
                col_desc, col_sug = st.columns(2)
                with col_desc:
                    st.markdown("**问题说明**")
                    st.text(f.get("description", "—"))
                with col_sug:
                    st.markdown("**修改建议**")
                    st.text(f.get("suggestion", "—"))

                # 代码片段（可选）
                snippet = f.get("code_snippet")
                if snippet:
                    st.markdown("**相关代码**")
                    st.code(snippet, language="python")


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------

def main() -> None:
    st.title("🔍 AI PR Review Assistant")
    st.caption(
        "粘贴一个公开 GitHub PR 链接，AI 将自动分析变更并生成 "
        "**变更总结 + 结构化风险 Findings**。"
    )
    st.divider()

    # ---- 输入区 ----
    col_input, col_btn = st.columns([5, 1])
    with col_input:
        pr_url = st.text_input(
            "GitHub PR URL",
            placeholder="https://github.com/owner/repo/pull/42",
            label_visibility="collapsed",
        )
    with col_btn:
        submit = st.button("🚀 开始评审", type="primary", use_container_width=True)

    # ---- 高级选项（折叠） ----
    with st.expander("⚙️ 高级选项", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            max_files = st.number_input(
                "最大文件数",
                min_value=1,
                max_value=200,
                value=80,
                help="超过此数后只保留 diff，不拉全文",
            )
        with col2:
            thinking = st.checkbox(
                "启用 Thinking 模式",
                value=True,
                help="对 Findings 开启 DeepSeek 深度推理（更准但更慢）",
            )
        with col3:
            max_tokens = st.number_input(
                "输入 Token 上限",
                min_value=10_000,
                max_value=1_000_000,
                value=300_000,
                step=10_000,
                help="超过后仅保留 diff",
            )

    st.divider()

    # ---- 结果区 ----
    if not submit:
        st.info("👆 请输入 GitHub PR URL 并点击「开始评审」")
        return

    # 前端校验
    if not pr_url.strip():
        st.error("❌ 请输入 PR URL")
        return
    if not _validate_pr_url(pr_url.strip()):
        st.error(
            "❌ PR URL 格式不正确。期望格式:\n\n"
            "`https://github.com/<owner>/<repo>/pull/<number>`"
        )
        return

    # ---- 调用后端 ----
    api_url = f"{BACKEND_URL.rstrip('/')}/api/review"
    payload = {
        "pr_url": pr_url.strip(),
        "thinking_findings": thinking,
        "max_files": max_files,
        "max_input_tokens": max_tokens,
    }

    with st.spinner("🔍 正在分析 PR…\n\n"
                    "• 抓取变更文件与 diff\n"
                    "• 构建上下文\n"
                    "• AI 生成变更总结\n"
                    "• AI 识别风险 Findings\n\n"
                    "大型 PR 可能需要 30-120 秒，请耐心等待…"):
        try:
            resp = requests.post(
                api_url,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.ConnectionError:
            st.error(
                f"❌ 无法连接到后端服务 ({BACKEND_URL})。\n\n"
                f"请确认:\n"
                f"1. venv 已激活: `.venv/Scripts/activate` (Windows)\n"
                f"2. 已在 ai-pr-review/ 目录下启动后端:\n"
                f"   `uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000`\n"
                f"3. BACKEND_URL 配置正确（当前: {BACKEND_URL}）"
            )
            return
        except requests.exceptions.Timeout:
            st.error(
                f"❌ 请求超时（{REQUEST_TIMEOUT}s）。\n\n"
                f"PR 可能过大或模型响应较慢，可尝试:\n"
                f"1. 减小 max_files\n"
                f"2. 增大 .env 中 REQUEST_TIMEOUT_SEC"
            )
            return
        except requests.exceptions.RequestException as e:
            st.error(f"❌ 网络请求失败: {e}")
            return

    # ---- 处理响应 ----
    if resp.status_code == 200:
        data = resp.json()
        _render_pr_info(data)
        st.divider()
        _render_summary(data.get("summary", ""))
        st.divider()
        _render_findings(data.get("findings", []))
    else:
        detail = ""
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        st.error(f"❌ 后端返回错误 ({resp.status_code}):\n\n{detail}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()
