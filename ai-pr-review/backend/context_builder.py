"""上下文构建器 —— 过滤 / 裁剪 / token 预算控制 / 组装模型输入文本。

提供:
    build_context(pr_meta, changed_files, github_client, settings)
        -> (context_text: str, file_metas: list[FileMeta], warnings: list[str])
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from backend.config import Settings
from backend.models import FileMeta

if TYPE_CHECKING:
    from backend.github_client import GitHubClient
    from backend.models import ChangedFile


# ======================================================================
# 噪声文件识别 —— 仅保留 diff，不拉全文
# ======================================================================

# 锁文件（精确匹配文件名）
_LOCK_FILES: frozenset[str] = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "go.sum", "Cargo.lock", "composer.lock", "Gemfile.lock", "Pipfile.lock",
})

# 噪声目录前缀（匹配路径中任意一段）
_NOISE_DIRS: tuple[str, ...] = (
    "dist/", "build/", "vendor/", "node_modules/",
    ".venv/", "venv/", "__pycache__/", ".git/",
)

# 生成/压缩产物的扩展名
_GENERATED_EXTS: frozenset[str] = frozenset({
    ".min.js", ".min.css", ".map",
})

# 二进制文件扩展名
_BINARY_EXTS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm", ".flv",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".db", ".sqlite", ".sqlite3",
    ".class", ".jar", ".war", ".ear",
    ".pyc", ".pyo", ".o", ".a", ".lib", ".obj",
    ".ipynb",        # Jupyter notebook — 含大量 base64 输出，不做文本 diff
})


def _is_noise_file(filename: str) -> bool:
    """判断是否为噪音文件（只保留 diff、不取全文）。"""
    basename = filename.rsplit("/", 1)[-1]

    # 1) 锁文件
    if basename in _LOCK_FILES:
        return True

    # 2) 噪声目录
    for nd in _NOISE_DIRS:
        if nd in filename:
            return True

    # 3) 生成产物扩展名
    for gen_ext in _GENERATED_EXTS:
        if basename.endswith(gen_ext):
            return True

    # 4) 二进制扩展名
    ext = _file_ext(basename)
    if ext in _BINARY_EXTS:
        return True

    return False


# ======================================================================
# 扩展名 → 语言标记（用于 Markdown 代码块）
# ======================================================================

_LANG_MAP: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".java": "java", ".go": "go", ".rs": "rust",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".kt": "kotlin", ".scala": "scala",
    ".r": "r", ".m": "matlab",
    ".html": "html", ".css": "css", ".scss": "scss", ".less": "less",
    ".xml": "xml", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".md": "markdown", ".rst": "rst", ".txt": "text",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".ps1": "powershell", ".bat": "batch",
    ".sql": "sql", ".graphql": "graphql",
    ".dockerfile": "dockerfile", ".makefile": "makefile", ".cmake": "cmake",
    ".vue": "html", ".svelte": "html",
    ".proto": "protobuf",
}


def _lang_tag(filename: str) -> str:
    """根据文件扩展名返回 Markdown 代码块语言标记。"""
    ext = _file_ext(filename)
    return _LANG_MAP.get(ext, "")


# ======================================================================
# 大文件截断 —— 保留 diff hunk 命中区域 ±40 行上下文
# ======================================================================

_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")


def _truncate_file(full_content: str, patch: str | None, context_lines: int = 40) -> str:
    """将超大文件截断为 diff 命中区域 ± context_lines 的合并视图。

    Args:
        full_content: 文件全文。
        patch: Unified diff 文本（可为 None）。
        context_lines: 每个 hunk 上下各保留的行数。

    Returns:
        截断后的文本，包含「… 省略 N 行 …」标记。
    """
    lines = full_content.splitlines()
    total = len(lines)

    if patch is None:
        # 无 diff 信息，只保留头部
        keep = min(context_lines * 3, total)
        head = "\n".join(lines[:keep])
        return f"[文件过长（{total} 行），仅展示前 {keep} 行]\n\n{head}"

    # 解析 diff hunk，收集需要保留的行号区间（1-indexed）
    ranges: list[tuple[int, int]] = []
    for m in _HUNK_RE.finditer(patch):
        new_start = int(m.group(3))          # 新文件起始行（1-indexed）
        new_count = int(m.group(4) or "1")   # hunk 涉及的新文件行数
        lo = max(1, new_start - context_lines)
        hi = min(total, new_start + new_count + context_lines)
        ranges.append((lo, hi))

    if not ranges:
        head = "\n".join(lines[: context_lines * 3])
        return f"[文件过长（{total} 行），仅展示前 {context_lines * 3} 行]\n\n{head}"

    # 合并重叠区间
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for lo, hi in ranges:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))

    # 拼接截断结果
    parts: list[str] = [f"[文件过长（{total} 行），仅展示 diff 命中区域 ±{context_lines} 行]"]
    prev_end = 0
    for lo, hi in merged:
        if lo > prev_end + 1:
            skipped = lo - prev_end - 1
            parts.append(f"\n… 省略 {skipped} 行 …\n")
        parts.append("\n".join(lines[lo - 1 : hi]))
        prev_end = hi
    if prev_end < total:
        parts.append(f"\n… 省略 {total - prev_end} 行 …")

    return "\n".join(parts)


# ======================================================================
# 核心入口
# ======================================================================

def build_context(
    pr_meta: dict,
    changed_files: list[ChangedFile],
    github_client: GitHubClient,
    settings: Settings,
) -> tuple[str, list[FileMeta], list[str]]:
    """组装送入 LLM 的完整上下文文本。

    严格按顺序应用过滤 / 裁剪 / token 预算规则（见 §7.2）。

    Args:
        pr_meta:       PR 元信息 dict（含 title, body, author, head_sha 及 owner, repo）。
        changed_files: 变更文件列表（patch 已填充，full_content 为空）。
        github_client: GitHubClient 实例，用于拉取全文。
        settings:      全局配置。

    Returns:
        (context_text, file_metas, warnings)
    """
    warnings: list[str] = []
    file_metas: list[FileMeta] = []

    owner = pr_meta.get("owner", "")
    repo = pr_meta.get("repo", "")
    head_sha = pr_meta.get("head_sha", "")

    max_files = settings.max_files
    max_file_lines = settings.max_file_lines
    max_input_tokens = settings.max_input_tokens

    # ---- 组装头部 ----
    parts: list[str] = []
    parts.append(f"# PR 标题: {pr_meta.get('title', '未知')}")
    body = pr_meta.get("body", "") or ""
    parts.append(f"\n# PR 描述:\n{body}" if body.strip() else "")

    # 粗估当前 token 数
    current_text = "".join(parts)
    est_tokens = _est_tokens(current_text)

    # 追踪是否仍在 token 预算内（只控制追加全文的行为）
    budget_exceeded = est_tokens >= max_input_tokens
    file_limit_reached = False

    total_files = len(changed_files)

    for idx, cf in enumerate(changed_files):
        # ---- Rule 4: 文件数上限 ----
        if idx >= max_files:
            if not file_limit_reached:
                warnings.append(
                    f"文件数超上限（max_files={max_files}），"
                    f"第 {max_files + 1} 个及之后的文件仅保留 diff，不取全文"
                )
                file_limit_reached = True
            # 仍然写入 diff（若存在），但不取全文
            _append_file_section(parts, cf, full_content=None)
            file_metas.append(_make_meta(cf, included_full_content=False))
            continue

        # ---- Rule 1: 噪音文件 ----
        if _is_noise_file(cf.filename):
            _append_file_section(parts, cf, full_content=None)
            file_metas.append(_make_meta(cf, included_full_content=False))
            continue

        # ---- Rule 2: 已删除文件 ----
        if cf.status == "removed":
            _append_file_section(parts, cf, full_content=None)
            file_metas.append(_make_meta(cf, included_full_content=False))
            continue

        # ---- 拉取全文 ----
        full = None
        included_full = False

        if not budget_exceeded and not file_limit_reached:
            full = github_client.get_file_content(owner, repo, cf.filename, head_sha)

        if full is None:
            # 取不到全文（二进制 / 404 / 过大），仅用 diff
            _append_file_section(parts, cf, full_content=None)
            file_metas.append(_make_meta(cf, included_full_content=False))
            continue

        full_lines = len(full.splitlines())

        # ---- Rule 3: 超大文件裁剪 ----
        if full_lines > max_file_lines:
            full = _truncate_file(full, cf.patch)
            included_full = False
        else:
            included_full = True

        # ---- Rule 5: Token 软预算（追加全文前检查） ----
        # 构造该文件段落以估 token 增量
        section_parts = _build_file_section(cf, full)
        section_text = "".join(section_parts)
        section_tokens = _est_tokens(section_text)

        if est_tokens + section_tokens > max_input_tokens:
            if not budget_exceeded:
                warnings.append(
                    f"输入 token 接近上限（max_input_tokens={max_input_tokens}），"
                    f"自文件 #{idx + 1} '{cf.filename}' 起不再追加全文"
                )
                budget_exceeded = True
            # 降级为仅 diff
            _append_file_section(parts, cf, full_content=None)
            file_metas.append(_make_meta(cf, included_full_content=False))
            continue

        # 通过所有检查，写入完整段落
        _append_file_section(parts, cf, full_content=full)
        est_tokens += section_tokens
        file_metas.append(_make_meta(cf, included_full_content=included_full))

    context_text = "\n".join(parts)

    # 如果没有任何文件有全文，给出 warning
    if total_files > 0 and not any(fm.included_full_content for fm in file_metas):
        warnings.append("所有文件均未包含全文（可能均为噪音文件或超出 token 预算）")

    return context_text, file_metas, warnings


# ======================================================================
# 内部辅助
# ======================================================================

def _est_tokens(text: str) -> int:
    """粗估 token 数：字符数 ÷ 4（适用于中英混合文本）。"""
    return len(text) // 4


def _file_ext(filename: str) -> str:
    """返回文件扩展名（小写含点号），无扩展名则空字符串。"""
    dot = filename.rfind(".")
    if dot == -1:
        return ""
    return filename[dot:].lower()


def _make_meta(cf: ChangedFile, *, included_full_content: bool) -> FileMeta:
    """从 ChangedFile 生成 FileMeta。"""
    return FileMeta(
        filename=cf.filename,
        status=cf.status,
        additions=cf.additions,
        deletions=cf.deletions,
        included_full_content=included_full_content,
    )


def _build_file_section(
    cf: ChangedFile, full_content: str | None
) -> list[str]:
    """构建单个文件的 Markdown 段落（返回行列表，供拼接与 token 估算）。"""
    lines: list[str] = []
    lines.append(f"\n## 变更文件: {cf.filename} ({cf.status}, +{cf.additions}/-{cf.deletions})")

    # Diff 段落
    lines.append("\n### Diff")
    lines.append(f"```diff")
    lines.append(cf.patch or "(无 diff — 可能为超大或二进制文件)")
    lines.append("```")

    # 全文段落
    if full_content is not None:
        lang = _lang_tag(cf.filename)
        lines.append(f"\n### 文件完整内容（新版本）")
        lines.append(f"```{lang}")
        lines.append(full_content)
        lines.append("```")

    return lines


def _append_file_section(
    parts: list[str], cf: ChangedFile, full_content: str | None
) -> None:
    """将单个文件的 Markdown 段落追加到 parts 列表。"""
    section = _build_file_section(cf, full_content)
    parts.extend(section)


# ======================================================================
# 自测
# ======================================================================
if __name__ == "__main__":
    import textwrap

    from config import get_settings
    from github_client import GitHubClient, parse_pr_url

    TEST_URL = "https://github.com/psf/requests/pull/6702"

    print("=" * 70)
    print("ContextBuilder 自测")
    print("=" * 70)

    settings = get_settings()
    owner, repo, num = parse_pr_url(TEST_URL)

    with GitHubClient() as client:
        # 复用 get_pr_meta（内部已包含 owner/repo 字段需自行补充）
        raw_meta = client.get_pr_meta(owner, repo, num)
        pr_meta = {**raw_meta, "owner": owner, "repo": repo}

        files = client.get_changed_files(owner, repo, num, max_files=settings.max_files)

        print(f"\nPR: {pr_meta['title']}")
        print(f"  文件数: {len(files)}")

        context_text, file_metas, warnings = build_context(
            pr_meta, files, client, settings,
        )

        print(f"\n--- 结果 ---")
        print(f"  context_text 长度: {len(context_text)} 字符")
        print(f"  估算 tokens:     {len(context_text) // 4}")
        print(f"  file_metas 数量:  {len(file_metas)}")
        print(f"  warnings 数量:    {len(warnings)}")

        print(f"\n--- FileMetas ---")
        for fm in file_metas:
            full_tag = "✓ 全文" if fm.included_full_content else "✗ 仅 diff"
            print(f"  {full_tag}  {fm.filename}  ({fm.status}, +{fm.additions}/-{fm.deletions})")

        if warnings:
            print(f"\n--- Warnings ---")
            for w in warnings:
                print(f"  ⚠ {w}")

        print(f"\n--- context_text 前 500 字符 ---")
        print(context_text[:500])

        print(f"\n--- context_text 尾部 300 字符 ---")
        print(context_text[-300:])

    print("\n" + "=" * 70)
    print("自测完成 ✓")
    print("=" * 70)
