"""Pydantic v2 数据模型 —— 定义 API 请求/响应结构与内部数据类。

数据类型:
    - ReviewRequest    : 前端提交的 PR 审查请求
    - Finding          : 单条代码审查发现（风险/建议）
    - FileMeta         : 变更文件元信息摘要
    - ReviewResponse   : 审查完成后的完整响应
    - ChangedFile      : 内部用，抓取阶段的原始变更文件数据
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# API 请求
# ---------------------------------------------------------------------------
class ReviewRequest(BaseModel):
    """前端提交的 PR 审查请求。"""

    pr_url: str = Field(
        ...,
        description="GitHub PR URL，格式: https://github.com/<owner>/<repo>/pull/<number>",
        pattern=r"^https?://github\.com/[^/]+/[^/]+/pull/\d+",
    )

    thinking_findings: bool = Field(
        default=True,
        description="是否对 Findings 启用 DeepSeek Thinking 模式",
    )

    max_files: int = Field(
        default=80,
        ge=1,
        le=200,
        description="最多抓取的文件数上限",
    )

    max_input_tokens: int = Field(
        default=300_000,
        ge=10_000,
        le=1_000_000,
        description="送入 LLM 的最大 token 数（近似）",
    )


# ---------------------------------------------------------------------------
# 单条发现
# ---------------------------------------------------------------------------
Severity = Literal["high", "medium", "low"]
Confidence = Literal["high", "medium", "low"]
Category = Literal[
    "logic", "security", "performance", "maintainability", "edge_case", "style", "test"
]


class Finding(BaseModel):
    """LLM 产出的单条代码审查发现。"""

    file: str = Field(..., description="文件路径")
    line_start: int | None = Field(default=None, description="起始行号（1-indexed）")
    line_end: int | None = Field(default=None, description="结束行号（1-indexed）")
    category: Category = Field(..., description="问题分类")
    severity: Severity = Field(..., description="严重程度")
    confidence: Confidence = Field(..., description="置信度")
    title: str = Field(..., description="发现标题（一句话）")
    description: str = Field(..., description="详细说明")
    suggestion: str = Field(..., description="修改建议")
    code_snippet: str | None = Field(default=None, description="涉及代码片段")


# ---------------------------------------------------------------------------
# 文件元信息
# ---------------------------------------------------------------------------
class FileMeta(BaseModel):
    """PR 中单个变更文件的元信息摘要。"""

    filename: str = Field(..., description="文件路径")
    status: str = Field(..., description="变更类型: added / modified / removed / renamed")
    additions: int = Field(default=0, ge=0, description="新增行数")
    deletions: int = Field(default=0, ge=0, description="删除行数")
    included_full_content: bool = Field(
        default=False, description="是否将该文件全文送入了 LLM"
    )


# ---------------------------------------------------------------------------
# API 响应
# ---------------------------------------------------------------------------
class ReviewResponse(BaseModel):
    """PR 审查完整响应。"""

    pr_title: str = Field(..., description="PR 标题")
    pr_author: str = Field(..., description="PR 作者 GitHub 用户名")
    summary: str = Field(..., description="LLM 生成的变更总结")
    findings: list[Finding] = Field(
        default_factory=list, description="结构化发现列表"
    )
    files: list[FileMeta] = Field(
        default_factory=list, description="变更文件元信息列表"
    )
    stats: dict = Field(default_factory=dict, description="PR 统计信息（变更行数等）")
    warnings: list[str] = Field(
        default_factory=list, description="处理过程中的警告信息"
    )


# ---------------------------------------------------------------------------
# 内部数据结构（不暴露给 API）
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ChangedFile:
    """抓取阶段的原始变更文件数据。

    仅供 backend 内部模块之间传递使用，不出现在 API 契约中。
    """

    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    full_content: str | None
    sha: str
