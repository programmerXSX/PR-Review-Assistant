"""分析编排器 —— 串联「抓取 → 构建上下文 → 两趟 LLM 调用 → 解析 → 组装响应」。

提供:
    run_review(req: ReviewRequest) -> ReviewResponse   (async)
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

# 确保 from backend.xxx 导入在任何运行方式下都可用
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.config import get_settings
from backend.context_builder import build_context
from backend.github_client import GitHubClient, GitHubAPIError, parse_pr_url
from backend.llm_client import call_model, LLMError
from backend.models import Finding, ReviewRequest, ReviewResponse
from backend.prompts import FINDINGS_SYSTEM, SUMMARY_SYSTEM


# ======================================================================
# 轻量 findings JSON 解析（P8 aggregator 会完全接管此逻辑）
# ======================================================================

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _parse_findings_raw(raw: str) -> list[Finding]:
    """从 LLM 原始输出中提取并校验 findings 列表。

    容错策略:
        1. 先尝试直接 JSON.parse 整个 raw。
        2. 失败则尝试匹配 Markdown 代码块内容。
        3. 再失败则尝试在 raw 中定位首个 '{' 到最后一个 '}' 的子串。
        4. 仍失败则降级为空列表 + warning 交由上层记录。

    Returns:
        校验通过并排序后的 Finding 列表。
    """
    candidates: list[str] = []

    # 策略 1: 直接解析
    candidates.append(raw.strip())

    # 策略 2: 匹配 ```json ... ``` 或 ``` ... ```
    m = _JSON_BLOCK_RE.search(raw)
    if m:
        candidates.append(m.group(1).strip())

    # 策略 3: 寻找首尾花括号
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw[start : end + 1].strip())

    findings_raw_list: list[dict] = []
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict) and "findings" in obj:
                findings_raw_list = obj["findings"]
                break
            # 也可能直接是 findings 数组
            if isinstance(obj, list):
                findings_raw_list = obj
                break
        except json.JSONDecodeError:
            continue

    # 逐条校验
    findings: list[Finding] = []
    for item in findings_raw_list:
        if not isinstance(item, dict):
            continue
        try:
            f = Finding(**item)
            findings.append(f)
        except Exception:
            # 单条解析失败则跳过
            continue

    # 简单去重: 同 (file, category, title) 只保留首条
    seen: set[tuple[str, str, str]] = set()
    deduped: list[Finding] = []
    for f in findings:
        key = (f.file, f.category, f.title)
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    # 排序: severity 降序 → confidence 降序
    deduped.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(f.severity, 3),
            _CONFIDENCE_RANK.get(f.confidence, 3),
        )
    )

    return deduped


# ======================================================================
# 主入口
# ======================================================================

async def run_review(req: ReviewRequest) -> ReviewResponse:
    """对给定 PR 执行完整的两趟审查流程。

    Args:
        req: 前端提交的审查请求（含 pr_url / thinking_findings / max_files 等）。

    Returns:
        ReviewResponse，包含 summary、findings、文件元信息与统计。

    Raises:
        ValueError:      PR URL 格式非法。
        GitHubAPIError:  GitHub API 调用失败。
        LLMError:        LLM 调用失败。
    """
    t_start = time.monotonic()
    settings = get_settings()
    warnings: list[str] = []

    # ---- Step 1: 解析 URL + 抓取数据 ----
    owner, repo, pull_number = parse_pr_url(req.pr_url)

    with GitHubClient() as gh:
        pr_meta = gh.get_pr_meta(owner, repo, pull_number)
        changed_files = gh.get_changed_files(
            owner, repo, pull_number, max_files=req.max_files
        )

        # ---- Step 2: 构建上下文 ----
        context_text, file_metas, ctx_warnings = build_context(
            pr_meta, changed_files, gh, settings,
        )
        warnings.extend(ctx_warnings)

    # ---- Step 3: 并发两趟 LLM 调用 ----
    loop = asyncio.get_running_loop()

    # 趟一: 变更总结 (Non-Thinking)
    # 趟二: 风险 findings (Thinking 可选 + JSON 模式)
    def _run_summary() -> str:
        return call_model(SUMMARY_SYSTEM, context_text, thinking=False, json_mode=False)

    def _run_findings() -> str:
        return call_model(
            FINDINGS_SYSTEM, context_text,
            thinking=req.thinking_findings, json_mode=True,
        )

    task_summary = loop.run_in_executor(None, _run_summary)
    task_findings = loop.run_in_executor(None, _run_findings)

    results = await asyncio.gather(task_summary, task_findings, return_exceptions=True)

    # 处理趟一结果
    if isinstance(results[0], Exception):
        raise LLMError(f"变更总结调用失败: {results[0]}") from results[0]
    summary: str = results[0]

    # 处理趟二结果
    findings: list[Finding] = []
    if isinstance(results[1], Exception):
        warnings.append(f"风险识别调用失败: {results[1]}，已降级为空 findings")
    else:
        try:
            findings = _parse_findings_raw(results[1])
        except Exception as e:
            warnings.append(f"findings JSON 解析失败: {e}，已降级为空 findings")

    # ---- Step 4: 组装统计 ----
    analyzed = sum(1 for fm in file_metas if fm.included_full_content)
    total = len(file_metas)
    elapsed = round(time.monotonic() - t_start, 1)

    stats = {
        "total_files": len(changed_files),
        "analyzed_files": analyzed,
        "skipped_files": total - analyzed,
        "est_input_tokens": len(context_text) // 4,
        "model": settings.deepseek_model,
        "elapsed_sec": elapsed,
    }

    # ---- Step 5: 组装响应 ----
    return ReviewResponse(
        pr_title=pr_meta.get("title", "未知"),
        pr_author=pr_meta.get("author", "unknown"),
        summary=summary,
        findings=findings,
        files=file_metas,
        stats=stats,
        warnings=warnings,
    )


# ======================================================================
# 自测
# ======================================================================
if __name__ == "__main__":
    TEST_URL = "https://github.com/psf/requests/pull/6702"

    print("=" * 70)
    print("Orchestrator 自测")
    print("=" * 70)
    print(f"PR: {TEST_URL}")
    print()

    async def _main():
        req = ReviewRequest(
            pr_url=TEST_URL,
            thinking_findings=True,
            max_files=10,
        )

        try:
            resp = await run_review(req)
        except ValueError as e:
            print(f"[FAIL] URL 解析: {e}")
            return
        except GitHubAPIError as e:
            print(f"[FAIL] GitHub API: {e}")
            return
        except LLMError as e:
            print(f"[FAIL] LLM: {e}")
            return

        print(f"--- 结果 ---")
        print(f"  PR 标题:   {resp.pr_title}")
        print(f"  PR 作者:   {resp.pr_author}")
        print(f"  Findings:  {len(resp.findings)} 条")
        print(f"  Files:     {len(resp.files)} 个")
        print(f"  Warnings:  {len(resp.warnings)} 条")

        print(f"\n--- Stats ---")
        for k, v in resp.stats.items():
            print(f"  {k}: {v}")

        print(f"\n--- Summary (前 400 字符) ---")
        print(resp.summary[:400])

        if resp.findings:
            print(f"\n--- Findings ({len(resp.findings)} 条) ---")
            for i, f in enumerate(resp.findings):
                sev = {"high": "🔴", "medium": "🟠", "low": "⚪"}.get(f.severity, "?")
                conf = {"high": "H", "medium": "M", "low": "L"}.get(f.confidence, "?")
                print(f"\n  [{i+1}] {sev} {conf} [{f.category}] {f.title}")
                print(f"       {f.file}")
                if f.line_start:
                    print(f"       行 {f.line_start}" + (f"~{f.line_end}" if f.line_end else ""))
                print(f"       {f.description[:120]}")

        if resp.warnings:
            print(f"\n--- Warnings ---")
            for w in resp.warnings:
                print(f"  ⚠ {w}")

        print("\n" + "=" * 70)
        print("自测完成 ✓")
        print("=" * 70)

    asyncio.run(_main())
