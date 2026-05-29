"""分析编排器 —— 串联「抓取 → 构建上下文 → 两趟 LLM 调用 → 解析 → 组装响应」。

提供:
    run_review(req: ReviewRequest) -> ReviewResponse   (async)

重试逻辑:
    - 趟二 findings 解析失败（返回空 + 解析 warning）时，自动重试一次趟二 LLM 调用。
    - 重试仍失败则降级为空 findings + warning，不阻塞整次请求。
"""

from __future__ import annotations

import asyncio
import time

from backend.aggregator import parse_findings
from backend.config import get_settings
from backend.context_builder import build_context
from backend.github_client import GitHubClient, GitHubAPIError, parse_pr_url
from backend.llm_client import call_model, LLMError
from backend.models import Finding, ReviewRequest, ReviewResponse
from backend.prompts import FINDINGS_SYSTEM, SUMMARY_SYSTEM


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

    # 处理趟二结果（含解析失败重试逻辑）
    MAX_FINDINGS_RETRIES = 1  # 首次 + 1 次重试 = 共 2 次
    findings: list[Finding] = []
    findings_raw: str | None = None

    if isinstance(results[1], Exception):
        warnings.append(
            f"风险识别调用失败: {results[1]}，已降级为空 findings"
        )
    else:
        findings_raw = results[1]
        findings, parse_warnings = parse_findings(findings_raw)
        warnings.extend(parse_warnings)

        # 如果解析完全失败（返回空 + 有解析失败 warning），重试一次
        _has_parse_failure = any("解析失败" in w for w in parse_warnings)
        if len(findings) == 0 and _has_parse_failure and MAX_FINDINGS_RETRIES > 0:
            warnings.append("findings 首次解析失败，正在重试趟二 LLM 调用…")

            def _retry_findings() -> str:
                return call_model(
                    FINDINGS_SYSTEM, context_text,
                    thinking=req.thinking_findings, json_mode=True,
                )

            try:
                findings_raw2 = await loop.run_in_executor(None, _retry_findings)
                findings2, parse_warnings2 = parse_findings(findings_raw2)
                findings = findings2

                _has_parse_failure2 = any("解析失败" in w for w in parse_warnings2)
                if len(findings) == 0 and _has_parse_failure2:
                    warnings.append("findings 重试后仍解析失败，已降级为空 findings")
                elif len(findings) > 0:
                    # 重试成功 — 移除首次解析失败的 warning，只追加二次的校验 warning
                    warnings = [w for w in warnings if "解析失败" not in w]
                    warnings.extend(parse_warnings2)
            except Exception as e:
                warnings.append(f"findings 重试调用失败: {e}，已降级为空 findings")

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
