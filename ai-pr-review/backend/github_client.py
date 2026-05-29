"""GitHub PR Fetcher —— 从公开 GitHub 仓库抓取 PR 变更数据。

提供:
    - parse_pr_url(url)          → (owner, repo, pull_number)
    - GitHubClient                → 封装 GitHub REST API 调用
      ├── get_pr_meta()           → PR 元信息
      ├── get_changed_files()     → 变更文件列表 (含 diff/patch)
      └── get_file_content()      → 单个文件全文 (base64 → str)
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path
from typing import Tuple

# 优先将项目根目录加入 sys.path，确保 `from backend.xxx` 导入在任何运行方式下都可用
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import httpx

from backend.config import get_settings
from backend.models import ChangedFile

# ---------------------------------------------------------------------------
# URL 解析
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", re.IGNORECASE)

# 可识别为文本/代码的常见扩展名（用于 contents API 返回的 base64 内容判断）
_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".r",
    ".html", ".css", ".scss", ".sass", ".less", ".xml", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".md", ".rst", ".txt",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmake", ".makefile",
    ".dockerfile", ".sql", ".graphql", ".proto", ".vue", ".svelte",
})


def parse_pr_url(url: str) -> Tuple[str, str, int]:
    """将 GitHub PR URL 解析为 (owner, repo, pull_number)。

    Raises:
        ValueError: URL 格式不匹配时给出可读提示。
    """
    m = _PR_URL_RE.search(url)
    if not m:
        raise ValueError(
            f"无法解析 PR URL: {url!r}\n"
            f"期望格式: https://github.com/<owner>/<repo>/pull/<number>"
        )
    return m.group(1), m.group(2), int(m.group(3))


# ---------------------------------------------------------------------------
# GitHub API 错误
# ---------------------------------------------------------------------------

class GitHubAPIError(Exception):
    """GitHub API 调用失败时抛出，附带可读提示。"""
    pass


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class GitHubClient:
    """同步 GitHub REST API 客户端（基于 httpx.Client）。

    自动从 config.get_settings() 读取 GITHUB_TOKEN 与 REQUEST_TIMEOUT_SEC。
    """

    def __init__(self) -> None:
        settings = get_settings()
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-pr-review/0.1.0",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"

        self._client = httpx.Client(
            base_url="https://api.github.com",
            headers=headers,
            timeout=float(settings.request_timeout_sec),
        )

    # ------------------------------------------------------------------
    # PR 元信息
    # ------------------------------------------------------------------
    def get_pr_meta(self, owner: str, repo: str, pull_number: int) -> dict:
        """获取 PR 标题、作者、base/head SHA、变更文件数等。

        Returns:
            dict with keys: title, body, author, base_sha, head_sha, changed_files
        """
        path = f"/repos/{owner}/{repo}/pulls/{pull_number}"
        resp = self._request("GET", path)
        data = resp.json()
        return {
            "owner": owner,
            "repo": repo,
            "title": data.get("title", ""),
            "body": data.get("body", "") or "",
            "author": (data.get("user") or {}).get("login", "unknown"),
            "base_sha": (data.get("base") or {}).get("sha", ""),
            "head_sha": (data.get("head") or {}).get("sha", ""),
            "changed_files": data.get("changed_files", 0),
        }

    # ------------------------------------------------------------------
    # 变更文件列表（含 diff / patch）
    # ------------------------------------------------------------------
    def get_changed_files(
        self, owner: str, repo: str, pull_number: int, max_files: int = 80
    ) -> list[ChangedFile]:
        """分页拉取 PR 变更文件列表，每个文件含 patch（可能为 None）。

        翻页逻辑：?per_page=100&page=1 → page=2 → … 直到取完或达 max_files。
        此阶段 **不拉取全文**（full_content=None），后续由 context_builder 决定。
        """
        results: list[ChangedFile] = []
        page = 1
        while len(results) < max_files:
            path = (
                f"/repos/{owner}/{repo}/pulls/{pull_number}/files"
                f"?per_page=100&page={page}"
            )
            resp = self._request("GET", path)
            items: list[dict] = resp.json()

            if not items:
                break  # 没有更多文件了

            for item in items:
                if len(results) >= max_files:
                    break
                cf = ChangedFile(
                    filename=item.get("filename", ""),
                    status=item.get("status", "modified"),
                    additions=item.get("additions", 0),
                    deletions=item.get("deletions", 0),
                    patch=item.get("patch"),          # None 表示超大文件无 patch
                    full_content=None,                 # 此阶段不取
                    sha=item.get("sha", ""),
                )
                results.append(cf)

            # GitHub 返回少于 100 表示到最后一页
            if len(items) < 100:
                break
            page += 1

        return results

    # ------------------------------------------------------------------
    # 文件全文
    # ------------------------------------------------------------------
    def get_file_content(
        self, owner: str, repo: str, path: str, ref: str
    ) -> str | None:
        """获取指定文件在 ref 处的完整内容（base64 解码后返回 utf-8 字符串）。

        Returns:
            文件文本内容；404/二进制/过大/非文本时返回 None。
        """
        try:
            resp = self._request(
                "GET",
                f"/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
            )
        except GitHubAPIError:
            return None

        data = resp.json()

        # GitHub 可能返回数组（目录）或单文件对象
        if isinstance(data, list):
            return None

        encoding = data.get("encoding", "")
        if encoding != "base64":
            # 非 base64 编码（如上传的二进制），不处理
            return None

        raw = data.get("content", "")
        if not raw:
            return None

        # 检查扩展名是否为可识别的文本类型
        ext = _get_ext(path)
        if ext and ext not in _TEXT_EXTENSIONS:
            return None

        try:
            decoded = base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

        return decoded

    # ------------------------------------------------------------------
    # 内部：统一 HTTP 请求 + 错误处理
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """发起 HTTP 请求并统一处理错误状态码。"""
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise GitHubAPIError(
                f"GitHub API 请求超时: {method} {path}\n"
                f"请检查网络或增大 .env 中 REQUEST_TIMEOUT_SEC 的值。"
            )
        except httpx.RequestError as exc:
            raise GitHubAPIError(
                f"GitHub API 网络错误: {method} {path}\n{exc}"
            )

        if resp.status_code == 404:
            raise GitHubAPIError(
                f"PR 不存在或为私有仓库（{resp.url}）。\n"
                f"请确认:\n"
                f"  1) PR URL 是否正确\n"
                f"  2) 仓库是否为公开仓库（暂不支持私有仓库）"
            )
        if resp.status_code == 403:
            # 403 可能是限流，也可能是因为被 banned
            remaining = resp.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                raise GitHubAPIError(
                    "GitHub API 限流: 本小时配额已耗尽。\n"
                    "  解决方案: 在 .env 中配置 GITHUB_TOKEN 可提升至 5000 次/小时。"
                )
            raise GitHubAPIError(
                f"GitHub API 403 Forbidden: {method} {path}\n"
                f"响应: {resp.text[:500]}"
            )

        if resp.status_code >= 400:
            raise GitHubAPIError(
                f"GitHub API 错误 {resp.status_code}: {method} {path}\n"
                f"响应: {resp.text[:500]}"
            )

        return resp

    def close(self) -> None:
        """关闭底层 HTTP 客户端。"""
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _get_ext(path: str) -> str:
    """返回文件扩展名（小写，含点号）；如无扩展名返回空字符串。"""
    dot = path.rfind(".")
    if dot == -1:
        return ""
    return path[dot:].lower()


# ---------------------------------------------------------------------------
# 自测（仅在本文件直接运行时执行）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import textwrap

    # ---- 配置项 ----
    # 可修改为任意公开 PR URL
    TEST_URL = "https://github.com/psf/requests/pull/6702"

    print("=" * 70)
    print("GitHubClient 自测")
    print("=" * 70)

    # 1) URL 解析
    print("\n--- 1) parse_pr_url ---")
    try:
        owner, repo, num = parse_pr_url(TEST_URL)
        print(f"owner={owner}, repo={repo}, pull_number={num}")
    except ValueError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

    # 负面测试
    for bad in ["not-a-url", "https://github.com/a", "https://github.com/a/b/issues/5"]:
        try:
            parse_pr_url(bad)
            print(f"[FAIL] 应拒绝: {bad}")
        except ValueError:
            print(f"[PASS] 正确拒绝: {bad}")

    # 2) 抓取 PR 元信息
    print("\n--- 2) get_pr_meta ---")
    with GitHubClient() as client:
        try:
            meta = client.get_pr_meta(owner, repo, num)
            print(f"  title:         {meta['title']}")
            print(f"  author:        {meta['author']}")
            print(f"  changed_files: {meta['changed_files']}")
            print(f"  base_sha:      {meta['base_sha'][:12]}...")
            print(f"  head_sha:      {meta['head_sha'][:12]}...")
            body_preview = (meta["body"] or "")[:120].replace("\n", "\\n")
            print(f"  body_preview:  {body_preview}")
        except GitHubAPIError as e:
            print(f"[FAIL] {e}")
            sys.exit(1)

        # 3) 抓取变更文件列表
        print("\n--- 3) get_changed_files ---")
        max_f = min(meta["changed_files"], 10)  # 自测只取前 10 个
        files = client.get_changed_files(owner, repo, num, max_files=max_f)
        print(f"  实际拿到 {len(files)} 个文件:\n")
        for i, cf in enumerate(files):
            patch_preview = (
                textwrap.shorten((cf.patch or ""), width=80, placeholder="...")
                if cf.patch
                else "(无 patch - 可能为超大/二进制文件)"
            )
            print(f"  [{i+1}] {cf.filename}")
            print(f"       status={cf.status}  +{cf.additions}/-{cf.deletions}  sha={cf.sha[:8]}")
            print(f"       patch: {patch_preview}")
            print()

        if not files:
            print("[WARN] 未取到任何文件，跳过后续测试。")
            sys.exit(0)

        # 4) 取首个文件的全文
        print("--- 4) get_file_content (首个文件) ---")
        first = files[0]
        content = client.get_file_content(
            owner, repo, first.filename, meta["head_sha"]
        )
        if content is not None:
            lines = content.splitlines()
            print(f"  文件: {first.filename}")
            print(f"  全文 {len(lines)} 行, {len(content)} 字符")
            print(f"  前 5 行:")
            for line in lines[:5]:
                print(f"    | {line}")
        else:
            print(f"  文件: {first.filename}")
            print(f"  [SKIP] 无法取全文（可能为二进制、超大或非文本文件）")

        # 5) 抓取第二个文件的全文（如果存在）
        if len(files) > 1:
            second = files[1]
            content2 = client.get_file_content(
                owner, repo, second.filename, meta["head_sha"]
            )
            if content2 is not None:
                lines2 = content2.splitlines()
                print(f"\n  文件2: {second.filename}")
                print(f"  全文 {len(lines2)} 行, {len(content2)} 字符")
            else:
                print(f"\n  文件2: {second.filename}")
                print(f"  [SKIP] 无法取全文")

    print("\n" + "=" * 70)
    print("自测完成 ✓")
    print("=" * 70)
