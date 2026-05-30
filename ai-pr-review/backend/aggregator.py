"""Findings 解析器 —— 将模型原始输出清洗为 list[Finding]。

提供:
    parse_findings(raw_text) → (findings: list[Finding], warnings: list[str])

容错与清洗规则:
    1. 健壮 JSON 解析（3 层回退）
    2. 逐条 pydantic 校验，字段非法就丢弃 + warning
    3. file+title 去重
    4. severity → confidence 排序

注意: 本模块只负责「解析 + 清洗」，不负责重试。
      **重试逻辑应放在 orchestrator 层**（调用方拿到空 findings + 解析 warning 时，
      可选择重调一次趟二 LLM，仍失败再降级为空）。
"""

from __future__ import annotations

import json
import re

from models import Finding

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹的代码块
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)

# 排序权重
_SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
_CONFIDENCE_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# 合法的枚举值集合（用于字段校验 warning）
_VALID_SEVERITY: set[str] = set(_SEVERITY_RANK.keys())
_VALID_CONFIDENCE: set[str] = set(_CONFIDENCE_RANK.keys())
_VALID_CATEGORY: set[str] = {
    "logic", "security", "performance", "maintainability",
    "edge_case", "style", "test",
}


# ---------------------------------------------------------------------------
# 核心入口
# ---------------------------------------------------------------------------

def parse_findings(raw_text: str) -> tuple[list[Finding], list[str]]:
    """从 LLM 原始输出文本中解析并清洗 findings 列表。

    解析策略（按序回退）:
        1. 直接 json.loads(raw_text)
        2. 剥离 ```json / ``` 包裹后 parse
        3. 提取首尾花括号子串后 parse
        4. 全部失败 → 返回空列表 + warning（不抛异常）

    清洗规则:
        - 逐条用 Finding 校验；字段越界/缺失的丢弃，生成一条 warning
        - file + title 相同的视为重复，仅保留第一条
        - 按 severity(high→low) 再 confidence(high→low) 排序

    Args:
        raw_text: 模型返回的原始文本。

    Returns:
        (findings: list[Finding], warnings: list[str])
    """
    warnings: list[str] = []

    # ---- Step 1: 多层 JSON 提取 ----
    findings_dicts = _extract_findings_list(raw_text)

    if findings_dicts is None:
        warnings.append(
            "findings JSON 解析失败：模型输出无法解析为合法 JSON。"
            "请检查 prompt 是否要求模型严格输出 JSON。"
        )
        return [], warnings

    # ---- Step 2: 逐条校验 ----
    valid: list[Finding] = []
    for idx, item in enumerate(findings_dicts):
        if not isinstance(item, dict):
            warnings.append(f"findings[{idx}] 不是 JSON 对象，已跳过")
            continue

        # 字段越界预检（生成更明确的 warning）
        cat = item.get("category", "")
        sev = item.get("severity", "")
        conf = item.get("confidence", "")

        if cat and cat not in _VALID_CATEGORY:
            warnings.append(
                f"findings[{idx}] category='{cat}' 不在合法取值 {_VALID_CATEGORY} 中，已丢弃"
            )
            continue
        if sev and sev not in _VALID_SEVERITY:
            warnings.append(
                f"findings[{idx}] severity='{sev}' 不在合法取值 {_VALID_SEVERITY} 中，已丢弃"
            )
            continue
        if conf and conf not in _VALID_CONFIDENCE:
            warnings.append(
                f"findings[{idx}] confidence='{conf}' 不在合法取值 {_VALID_CONFIDENCE} 中，已丢弃"
            )
            continue

        try:
            f = Finding(**item)
            valid.append(f)
        except Exception as e:
            warnings.append(
                f"findings[{idx}] pydantic 校验失败: {e}，已丢弃"
            )
            continue

    # ---- Step 3: 去重 (file + title) ----
    seen: set[tuple[str, str]] = set()
    deduped: list[Finding] = []
    dup_count = 0
    for f in valid:
        key = (f.file, f.title)
        if key not in seen:
            seen.add(key)
            deduped.append(f)
        else:
            dup_count += 1
    if dup_count > 0:
        warnings.append(f"发现 {dup_count} 条重复 finding（file+title 相同），已去重")

    # ---- Step 4: 排序 ----
    deduped.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(f.severity, 3),
            _CONFIDENCE_RANK.get(f.confidence, 3),
        )
    )

    return deduped, warnings


# ---------------------------------------------------------------------------
# 内部：JSON 提取
# ---------------------------------------------------------------------------

def _extract_findings_list(raw_text: str) -> list[dict] | None:
    """从原始文本中提取 findings 列表。

    Returns:
        list[dict] 如果成功；None 如果所有策略都失败。
    """
    # 候选 JSON 字符串列表（按优先级）
    candidates: list[str] = []

    # 策略 1: 直接解析
    candidates.append(raw_text.strip())

    # 策略 2: 剥离 Markdown 代码块
    m = _JSON_BLOCK_RE.search(raw_text)
    if m:
        candidates.append(m.group(1).strip())

    # 策略 3: 首尾花括号提取
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw_text[start : end + 1].strip())

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue

        if isinstance(obj, dict) and "findings" in obj:
            items = obj["findings"]
            if isinstance(items, list):
                return items
        # 兜底: 可能直接是 findings 数组
        if isinstance(obj, list):
            return obj

    return None


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 70)
    print("Aggregator 自测")
    print("=" * 70)

    # ----------------------------------------------------------------
    # 测试 1: 合法 JSON，含两条有效 finding
    # ----------------------------------------------------------------
    print("\n--- 测试 1: 合法 JSON ---")
    raw1 = json.dumps({
        "findings": [
            {
                "file": "src/app.py",
                "line_start": 42,
                "line_end": None,
                "category": "logic",
                "severity": "high",
                "confidence": "high",
                "title": "Potential null dereference",
                "description": "x 可能为 None",
                "suggestion": "加 None guard",
            },
            {
                "file": "src/utils.py",
                "line_start": 10,
                "line_end": 12,
                "category": "style",
                "severity": "low",
                "confidence": "low",
                "title": "Missing type annotation",
                "description": "函数缺少返回类型",
                "suggestion": "加上 -> None",
            },
        ]
    })
    findings1, warns1 = parse_findings(raw1)
    assert len(findings1) == 2, f"Expected 2, got {len(findings1)}"
    # high 应该在 low 前面
    assert findings1[0].severity == "high"
    assert findings1[1].severity == "low"
    assert len(warns1) == 0, f"Expected 0 warns, got {warns1}"
    print(f"  [PASS] {len(findings1)} 条, 0 warnings")

    # ----------------------------------------------------------------
    # 测试 2: Markdown 代码块包裹
    # ----------------------------------------------------------------
    print("\n--- 测试 2: Markdown ```json 包裹 ---")
    raw2 = """```json
    {
        "findings": [
            {
                "file": "b.py",
                "category": "security",
                "severity": "medium",
                "confidence": "high",
                "title": "XSS vulnerability",
                "description": "未转义用户输入",
                "suggestion": "使用 html.escape"
            }
        ]
    }
    ```"""
    findings2, warns2 = parse_findings(raw2)
    assert len(findings2) == 1, f"Expected 1, got {len(findings2)}"
    assert findings2[0].title == "XSS vulnerability"
    assert len(warns2) == 0
    print(f"  [PASS] {len(findings2)} 条, 0 warnings")

    # ----------------------------------------------------------------
    # 测试 3: 前后多余文字（模型经常解释两句再给 JSON）
    # ----------------------------------------------------------------
    print("\n--- 测试 3: 前后多余文字 ---")
    raw3 = """好的，我已经仔细分析了 PR 的变更，以下是发现的问题。

    {
        "findings": [
            {
                "file": "c.py",
                "category": "performance",
                "severity": "medium",
                "confidence": "medium",
                "title": "Unnecessary loop",
                "description": "循环可以优化",
                "suggestion": "使用列表推导"
            }
        ]
    }

    以上是全部发现。"""
    findings3, warns3 = parse_findings(raw3)
    assert len(findings3) == 1, f"Expected 1, got {len(findings3)}"
    assert findings3[0].category == "performance"
    assert len(warns3) == 0
    print(f"  [PASS] {len(findings3)} 条, 0 warnings")

    # ----------------------------------------------------------------
    # 测试 4: 完全非法文本 — 应返回空 + warning（不抛异常）
    # ----------------------------------------------------------------
    print("\n--- 测试 4: 完全非法文本 ---")
    raw4 = "not json at all, just some random text"
    findings4, warns4 = parse_findings(raw4)
    assert len(findings4) == 0
    assert len(warns4) >= 1
    assert "解析失败" in warns4[0]
    print(f"  [PASS] {len(findings4)} 条, {len(warns4)} warning: {warns4[0][:60]}...")

    # ----------------------------------------------------------------
    # 测试 5: 字段越界 — discount 丢弃 + warning
    # ----------------------------------------------------------------
    print("\n--- 测试 5: 字段越界 ---")
    raw5 = json.dumps({
        "findings": [
            {
                "file": "d.py",
                "category": "logic",
                "severity": "high",
                "confidence": "high",
                "title": "Real issue",
                "description": "d",
                "suggestion": "s",
            },
            {
                "file": "e.py",
                "category": "logic",
                "severity": "critical",    # 非法值
                "confidence": "medium",
                "title": "Bad severity",
                "description": "d",
                "suggestion": "s",
            },
            {
                "file": "f.py",
                "category": "design",       # 非法 category
                "severity": "low",
                "confidence": "low",
                "title": "Bad category",
                "description": "d",
                "suggestion": "s",
            },
        ]
    })
    findings5, warns5 = parse_findings(raw5)
    assert len(findings5) == 1, f"Expected 1 valid, got {len(findings5)}"
    assert findings5[0].title == "Real issue"
    assert len(warns5) >= 2, f"Expected >=2 warns for bad fields, got {len(warns5)}"
    assert any("severity='critical'" in w for w in warns5)
    assert any("category='design'" in w for w in warns5)
    print(f"  [PASS] {len(findings5)} 条 valid, {len(warns5)} warnings")
    for w in warns5:
        print(f"    ⚠ {w}")

    # ----------------------------------------------------------------
    # 测试 6: 去重（file + title 相同）
    # ----------------------------------------------------------------
    print("\n--- 测试 6: 去重 ---")
    raw6 = json.dumps({
        "findings": [
            {
                "file": "a.py",
                "category": "logic",
                "severity": "high",
                "confidence": "high",
                "title": "Bug A",
                "description": "d1",
                "suggestion": "s1",
            },
            {
                "file": "a.py",
                "category": "performance",
                "severity": "medium",
                "confidence": "high",
                "title": "Bug A",               # same file+title → dupe
                "description": "d2",
                "suggestion": "s2",
            },
            {
                "file": "b.py",
                "category": "style",
                "severity": "low",
                "confidence": "low",
                "title": "Naming",
                "description": "d3",
                "suggestion": "s3",
            },
        ]
    })
    findings6, warns6 = parse_findings(raw6)
    assert len(findings6) == 2, f"Expected 2 (1 duped), got {len(findings6)}"
    assert any("重复" in w for w in warns6) or any("1 条" in w for w in warns6)
    print(f"  [PASS] {len(findings6)} 条 (去重后), {len(warns6)} warning")

    # ----------------------------------------------------------------
    # 测试 7: 排序 (severity → confidence)
    # ----------------------------------------------------------------
    print("\n--- 测试 7: 排序 ---")
    raw7 = json.dumps({
        "findings": [
            {"file": "a.py", "category": "logic", "severity": "low",    "confidence": "high",   "title": "C_low_high",    "description": "d", "suggestion": "s"},
            {"file": "b.py", "category": "logic", "severity": "high",   "confidence": "low",    "title": "A_high_low",    "description": "d", "suggestion": "s"},
            {"file": "c.py", "category": "logic", "severity": "high",   "confidence": "high",   "title": "B_high_high",   "description": "d", "suggestion": "s"},
            {"file": "d.py", "category": "logic", "severity": "medium", "confidence": "medium", "title": "D_med_med",     "description": "d", "suggestion": "s"},
            {"file": "e.py", "category": "logic", "severity": "medium", "confidence": "low",    "title": "E_med_low",     "description": "d", "suggestion": "s"},
        ]
    })
    findings7, warns7 = parse_findings(raw7)
    titles = [f.title for f in findings7]
    expected_order = ["B_high_high", "A_high_low", "D_med_med", "E_med_low", "C_low_high"]
    assert titles == expected_order, f"Wrong order: {titles}"
    assert len(warns7) == 0
    print(f"  [PASS] Order: {titles}")

    # ----------------------------------------------------------------
    # 测试 8: 空 findings
    # ----------------------------------------------------------------
    print("\n--- 测试 8: 空 findings ---")
    raw8 = '{"findings": []}'
    findings8, warns8 = parse_findings(raw8)
    assert len(findings8) == 0
    assert len(warns8) == 0
    print(f"  [PASS] {len(findings8)} 条, 0 warnings")

    # ----------------------------------------------------------------
    # 测试 9: 缺失必填字段 — pydantic 校验失败
    # ----------------------------------------------------------------
    print("\n--- 测试 9: 缺失必填字段 + 混合 ---")
    raw9 = json.dumps({
        "findings": [
            {
                "file": "a.py",
                "category": "logic",
                "severity": "high",
                "confidence": "high",
                "title": "Valid",
                "description": "d",
                "suggestion": "s",
            },
            {
                # 缺失 file / title 等必填字段
                "category": "style",
                "severity": "low",
            },
        ]
    })
    findings9, warns9 = parse_findings(raw9)
    assert len(findings9) == 1, f"Expected 1 valid, got {len(findings9)}"
    assert findings9[0].title == "Valid"
    assert len(warns9) >= 1
    print(f"  [PASS] {len(findings9)} 条 valid, {len(warns9)} warning")

    print("\n" + "=" * 70)
    print("自测全部通过 ✓")
    print("=" * 70)
