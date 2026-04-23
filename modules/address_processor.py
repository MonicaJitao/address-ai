# ============================================================
# 地址处理主流程编排
#
# Pipeline 顺序：
#   1. LLM 解析  → 结构化字段 + 置信度
#   2. 格式化    → CN_INTL_V1 标准英文地址行
#   3. 三层校验  → 规则 / 知识库 / 联网（可选）
#   4. 综合评分  → 多维度置信度分数
# ============================================================

import os
import time
import logging

from modules.llm_adapter import LLMAdapter
from modules.address_formatter import format_cn_intl_v1, evaluate_format_score
from modules.address_validator import (
    validate_layer1_rules,
    validate_layer2_knowledge,
    validate_layer3_online,
    calculate_total_score,
)

logger = logging.getLogger(__name__)

# 按 provider 分别缓存适配器，避免并发请求间切换供应商导致状态串扰
_llm_adapters: dict[str, LLMAdapter] = {}


def _default_provider_from_env() -> str:
    p = os.getenv("LLM_PROVIDER", "deepseek").lower().strip()
    return p if p in ("claude", "deepseek") else "deepseek"


def get_llm_adapter(provider: str | None = None) -> LLMAdapter:
    """懒加载并缓存 LLMAdapter（每个 provider 一个实例）。"""
    key = (provider or _default_provider_from_env()).lower().strip()
    if key not in ("claude", "deepseek"):
        raise ValueError(f'不支持的 LLM 供应商: {key!r}，仅允许 "claude" 或 "deepseek"')
    if key not in _llm_adapters:
        _llm_adapters[key] = LLMAdapter(provider=key)
    return _llm_adapters[key]


async def normalize_address(
    raw_address: str,
    use_online_verify: bool = True,
    provider: str = "deepseek",
) -> dict:
    """
    地址标准化主函数，对外暴露的核心接口。

    参数：
        raw_address (str):       用户输入的原始地址（中文/英文/混合均可）
        use_online_verify (bool): 是否启用第三层联网验证（默认开启）
        provider (str): 大模型供应商，claude 或 deepseek（默认 deepseek）

    返回：
        完整的标准化结果字典，包含：
        - raw_address:       原始输入
        - parsed:            LLM 解析后的结构化字段
        - formatted_address: 标准英文地址行列表（CN_INTL_V1）
        - formatted_text:    换行拼接的完整地址字符串
        - validation:        三层校验详情
        - scores:            各维度评分和总分
        - model_used:        实际使用的模型名称
        - provider:          LLM 供应商名称
        - processing_time_ms: 处理耗时（毫秒）
    """
    start_time = time.monotonic()

    # ── 步骤 1：LLM 解析 ──────────────────────────────────
    adapter = get_llm_adapter(provider)
    try:
        parsed = await adapter.parse_address(raw_address)
        logger.info("LLM 解析完成：city=%s, province=%s", parsed.get("city"), parsed.get("province"))
    except Exception as exc:
        # LLM 调用失败时，返回错误结构而不是抛出 500
        logger.error("LLM 调用失败：%s", exc)
        raise RuntimeError(f"大模型调用失败，请检查 API Key 和网络连接：{exc}") from exc

    # ── 步骤 2：格式化（CN_INTL_V1） ──────────────────────
    formatted_lines, formatted_text = format_cn_intl_v1(parsed)
    format_score = evaluate_format_score(parsed, formatted_lines)

    # ── 步骤 3a：规则校验（第一层） ───────────────────────
    layer1 = validate_layer1_rules(parsed)

    # ── 步骤 3b：知识库校验（第二层） ─────────────────────
    layer2 = validate_layer2_knowledge(parsed)

    # ── 步骤 3c：联网验证（第三层，可选） ─────────────────
    if use_online_verify:
        layer3 = await validate_layer3_online(parsed, formatted_text, raw_address)
    else:
        # 手动跳过联网验证
        layer3 = {
            "enabled": False,
            "passed": True,
            "match_status": "skipped",
            "provider": "amap",
            "provider_confidence": 0.0,
            "amap_address": "",
            "issues": [],
            "score": 0,
        }

    # 联动收紧：联网层出现歧义/冲突时，限制 online_score，避免总分虚高
    l3_status = layer3.get("match_status", "")
    if l3_status == "mismatch":
        layer3["score"] = min(int(layer3.get("score", 0)), 25)
    elif l3_status == "ambiguous_match":
        layer3["score"] = min(int(layer3.get("score", 0)), 35)

    # ── 步骤 4：综合评分 ────────────────────────────────
    # translation_score：知识库校验通过说明翻译质量高，反映翻译置信度
    translation_score = layer2["score"]
    llm_confidence = float(parsed.get("confidence", 0.8))
    online_enabled = layer3.get("enabled", False)

    scores = calculate_total_score(
        llm_confidence=llm_confidence,
        translation_score=translation_score,
        format_score=format_score,
        knowledge_score=layer2["score"],
        online_score=layer3.get("score", 0),
        online_enabled=online_enabled,
    )

    # 二次兜底：联网冲突时封顶总分，避免“明显错匹配却高分”
    if l3_status == "mismatch":
        scores["total_score"] = min(float(scores.get("total_score", 0)), 75.0)
    elif l3_status == "ambiguous_match":
        scores["total_score"] = min(float(scores.get("total_score", 0)), 82.0)

    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    logger.info("地址标准化完成：total_score=%.1f, elapsed=%dms", scores["total_score"], elapsed_ms)

    return {
        "success": True,
        "raw_address": raw_address,
        "parsed": parsed,
        "formatted_address": formatted_lines,
        "formatted_text": formatted_text,
        "validation": {
            "layer1_rules": layer1,
            "layer2_knowledge": layer2,
            "layer3_online": layer3,
        },
        "scores": scores,
        "model_used": adapter.model,
        "provider": adapter.provider,
        "processing_time_ms": elapsed_ms,
    }
