# ============================================================
# 三层地址校验模块 + 多维评分
#
# 第一层：规则校验  — 格式完整性、字段合法性
# 第二层：知识库校验 — 行政区名称匹配、省市逻辑一致性
# 第三层：联网验证  — 高德地图地理编码（需配置 AMAP_API_KEY）
#
# 最终总分计算公式（权重可调）：
#   total = 0.30*parse + 0.15*translation + 0.15*format
#         + 0.20*knowledge + 0.20*online
# ============================================================

import os
import re
import json
import logging
import httpx

from modules.knowledge_base import (
    lookup_province,
    lookup_city,
    verify_city_province,
)

logger = logging.getLogger(__name__)

# 高德地图地理编码 API 端点
AMAP_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"


# ── 第一层：规则校验 ──────────────────────────────────────

def validate_layer1_rules(parsed: dict) -> dict:
    """
    规则校验：纯本地逻辑，检查字段完整性与格式合法性。

    返回：
        {
          "passed": bool,
          "issues": list[str],
          "score": int  (0-100)
        }
    """
    issues: list[str] = []
    deductions = 0

    # 必填字段缺失检查
    if not parsed.get("city"):
        issues.append("缺少城市字段")
        deductions += 25
    if not parsed.get("province"):
        issues.append("缺少省份字段")
        deductions += 15
    if not parsed.get("street") and not parsed.get("number"):
        issues.append("缺少街道或门牌号信息")
        deductions += 20

    # 邮编格式校验（如果提供了邮编）
    postal_code = parsed.get("postal_code", "")
    if postal_code and not re.fullmatch(r"\d{6}", postal_code):
        issues.append(f"邮编格式有误（应为 6 位数字，当前：{postal_code}）")
        deductions += 10

    # 逻辑冲突检查：国家字段应为 China
    country = parsed.get("country", "")
    if country and country.lower() not in ("china", "中国", "cn"):
        issues.append(f"地址中存在非中国地区国家标识：{country}")
        deductions += 30

    score = max(0, 100 - deductions)
    return {"passed": len(issues) == 0, "issues": issues, "score": score}


# ── 第二层：知识库校验 ────────────────────────────────────

def validate_layer2_knowledge(parsed: dict) -> dict:
    """
    知识库校验：对照内置行政区数据库验证名称映射的准确性。

    返回：
        {
          "passed": bool,
          "issues": list[str],
          "province_verified": bool,
          "city_verified": bool,
          "score": int  (0-100)
        }
    """
    issues: list[str] = []
    deductions = 0
    province_verified = False
    city_verified = False

    province_en = parsed.get("province", "")
    city_en = parsed.get("city", "")

    # 校验省份名是否在已知列表中
    # （LLM 可能已翻译，所以用英文名反向查找）
    known_provinces = set(
        v for v in __import__("modules.knowledge_base", fromlist=["PROVINCE_MAP"])
        .PROVINCE_MAP.values()
    )
    if province_en:
        if province_en in known_provinces:
            province_verified = True
        else:
            issues.append(f"省份名称未能在知识库中确认：{province_en}")
            deductions += 10

    # 校验城市名是否在已知列表中
    known_cities = set(
        v for v in __import__("modules.knowledge_base", fromlist=["CITY_MAP"])
        .CITY_MAP.values()
    )
    if city_en:
        if city_en in known_cities:
            city_verified = True
        else:
            # 小城市不在索引中属正常情况，仅降低评分不报错
            issues.append(f"城市名称未收录于知识库，无法确认：{city_en}")
            deductions += 5

    # 校验城市与省份的归属关系
    if city_verified and province_verified:
        if not verify_city_province(city_en, province_en):
            issues.append(
                f"城市与省份存在逻辑冲突：{city_en} 不属于 {province_en}"
            )
            deductions += 25

    score = max(0, 100 - deductions)
    return {
        "passed": deductions < 20,   # 轻微未收录不算 failed
        "issues": issues,
        "province_verified": province_verified,
        "city_verified": city_verified,
        "score": score,
    }


# ── 第三层：联网验证（高德地图地理编码） ──────────────────


def _build_geocode_query_from_parsed(parsed: dict, formatted_text: str) -> str:
    """用 LLM 结构化字段拼接查询串（多为英文），作为原文 geocode 失败时的回退。"""
    city_zh = parsed.get("city", "")
    query_parts = [
        parsed.get("province", ""),
        city_zh,
        parsed.get("district", ""),
        parsed.get("building", ""),
        f"{parsed.get('number', '')}号" if parsed.get("number") else "",
        parsed.get("street", ""),
    ]
    query = "".join(p for p in query_parts if p).strip()
    if not query:
        query = (formatted_text or "").strip()
    return query


def _geocode_query_candidates(
    raw_address: str,
    parsed: dict,
    formatted_text: str,
) -> list[str]:
    """
    生成按顺序尝试的高德 address 列表（去重、去空）。

    策略说明（输入可能是中文 / 英文 / 拼音 / 混合）：
    1. 用户原文优先：保留用户书写习惯，交给高德解析（对拼音、混写通常比本地硬拼更稳）。
    2. 结构化拼接：原文无结果时，用 LLM 字段再试（适合原文过于口语或缺省行政区划时）。
    3. 标准英文块：最后使用 CN_INTL_V1 多行合并为单行，与前面两者去重后再试。
    """
    raw = (raw_address or "").strip()
    parsed_q = _build_geocode_query_from_parsed(parsed, formatted_text)
    fmt = (formatted_text or "").strip()
    fmt_single = " ".join(line.strip() for line in fmt.splitlines() if line.strip()).strip()

    ordered: list[str] = []
    for q in (raw, parsed_q, fmt_single):
        q = (q or "").strip()
        if not q:
            continue
        if q not in ordered:
            ordered.append(q)
    return ordered


def _log_amap_geocode_response(query: str, data: dict, attempt: int) -> None:
    """打印单次高德地理编码结果（INFO 摘要 + DEBUG 全文）。"""
    try:
        gc = data.get("geocodes") or []
        logger.info("高德地理编码尝试#%d 请求串: %s", attempt, query)
        logger.info(
            "高德地理编码摘要#%d: status=%s info=%s infocode=%s count=%s 候选条数=%d",
            attempt,
            data.get("status"),
            data.get("info"),
            data.get("infocode"),
            data.get("count"),
            len(gc),
        )
        if gc:
            top0 = gc[0]
            logger.info(
                "高德首条命中#%d: level=%s formatted_address=%s location=%s",
                attempt,
                top0.get("level"),
                top0.get("formatted_address"),
                top0.get("location"),
            )
        logger.debug(
            "高德地理编码完整 JSON#%d: %s",
            attempt,
            json.dumps(data, ensure_ascii=False),
        )
    except Exception as exc:
        logger.warning("高德响应日志输出失败（不影响校验）：%s", exc)


async def validate_layer3_online(
    parsed: dict,
    formatted_text: str,
    raw_address: str = "",
) -> dict:
    """
    联网验证：调用高德地图地理编码 API，核验地址是否可被识别。
    若未配置 AMAP_API_KEY，则跳过并返回 enabled=False。

    raw_address：用户原始输入。地理编码按「原文 → 结构化字段 → 标准英文块」
    依次尝试（见 _geocode_query_candidates），适配中文 / 英文 / 拼音 / 混合输入。

    高德 API 返回的 match_type 含义：
    - 1：门址级（最精确）
    - 2：道路级
    - 3：道路交叉口
    - 0：无匹配

    返回：
        {
          "enabled": bool,
          "passed": bool,
          "match_status": "strong_match|partial_match|weak_match|no_match",
          "provider": "amap",
          "provider_confidence": float,
          "amap_address": str,
          "issues": list[str]
        }
    """
    amap_key = os.getenv("AMAP_API_KEY", "").strip()
    if not amap_key:
        # 未配置 API Key，跳过第三层
        return {
            "enabled": False,
            "passed": True,     # 不可用时不计入失败
            "match_status": "disabled",
            "provider": "amap",
            "provider_confidence": 0.0,
            "amap_address": "",
            "issues": [],
            "score": 0,         # 联网分不参与计算
        }

    candidates = _geocode_query_candidates(raw_address, parsed, formatted_text)
    if not candidates:
        return {
            "enabled": True,
            "passed": False,
            "match_status": "no_match",
            "provider": "amap",
            "provider_confidence": 0.0,
            "amap_address": "",
            "issues": ["缺少可用于地理编码的地址文本"],
            "score": 20,
        }

    data: dict = {}
    query_used = ""
    async with httpx.AsyncClient(timeout=5.0) as client:
        for attempt, query in enumerate(candidates, start=1):
            try:
                resp = await client.get(
                    AMAP_GEOCODE_URL,
                    params={"address": query, "key": amap_key, "output": "json"},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning(
                    "高德第 %d/%d 次请求异常（将尝试下一候选查询串）：%s",
                    attempt,
                    len(candidates),
                    exc,
                )
                if attempt >= len(candidates):
                    return {
                        "enabled": True,
                        "passed": True,
                        "match_status": "api_error",
                        "provider": "amap",
                        "provider_confidence": 0.0,
                        "amap_address": "",
                        "issues": [f"联网验证请求失败：{exc}"],
                        "score": 50,
                    }
                continue

            _log_amap_geocode_response(query, data, attempt)

            geocodes_try = data.get("geocodes") or []
            if data.get("status") == "1" and geocodes_try:
                query_used = query
                break
        else:
            query_used = candidates[-1]

    geocodes = data.get("geocodes", [])
    if not geocodes or data.get("status") != "1":
        return {
            "enabled": True,
            "passed": False,
            "match_status": "no_match",
            "provider": "amap",
            "provider_confidence": 0.0,
            "amap_address": "",
            "issues": [
                "高德地图无法识别该地址（已依次尝试多种查询写法：用户原文、"
                "结构化字段拼接、标准英文块），请核实是否真实存在",
            ],
            "score": 20,
        }

    if query_used:
        logger.info("高德地理编码本次命中所采用的查询串: %s", query_used)

    top = geocodes[0]
    level = top.get("level", "")    # 地址精度级别
    amap_address = top.get("formatted_address", "")
    candidate_count = len(geocodes)

    # 根据精度级别判断匹配强度
    if level in ("门牌号", "兴趣点", "单元"):
        match_status = "strong_match"
        confidence = 0.92
        score = 95
    elif level in ("道路", "道路交叉口", "街道"):
        match_status = "partial_match"
        confidence = 0.70
        score = 75
    elif level in ("区县", "城市"):
        match_status = "weak_match"
        confidence = 0.40
        score = 45
    else:
        match_status = "partial_match"
        confidence = 0.60
        score = 65

    # 多候选且级别不精确时降级评分
    if candidate_count > 3 and match_status != "strong_match":
        match_status = "partial_match"
        score = min(score, 65)

    issues = []
    if match_status == "weak_match":
        issues.append("地址匹配精度较低，仅命中区县/城市级别，建议补充详细街道信息")
    if candidate_count > 3:
        issues.append(f"发现 {candidate_count} 个候选结果，地址可能存在歧义")

    return {
        "enabled": True,
        "passed": match_status in ("strong_match", "partial_match"),
        "match_status": match_status,
        "provider": "amap",
        "provider_confidence": confidence,
        "amap_address": amap_address,
        "issues": issues,
        "score": score,
    }


# ── 综合评分计算 ──────────────────────────────────────────

def calculate_total_score(
    llm_confidence: float,
    translation_score: int,
    format_score: int,
    knowledge_score: int,
    online_score: int,
    online_enabled: bool,
) -> dict:
    """
    按加权公式合成最终总分，并返回各维度明细。

    权重分配（online 不可用时，其权重平摊给其他维度）：
    - parse_score:        0.30
    - translation_score:  0.15
    - format_score:       0.15
    - knowledge_score:    0.20
    - online_score:       0.20（不可用时归零，其他维度等比放大）
    """
    parse_score = int(llm_confidence * 100)

    if online_enabled:
        total = (
            0.30 * parse_score
            + 0.15 * translation_score
            + 0.15 * format_score
            + 0.20 * knowledge_score
            + 0.20 * online_score
        )
    else:
        # 第三层未启用，权重平摊到其余四个维度
        total = (
            0.375 * parse_score
            + 0.1875 * translation_score
            + 0.1875 * format_score
            + 0.25 * knowledge_score
        )

    return {
        "parse_score": parse_score,
        "translation_score": translation_score,
        "format_score": format_score,
        "knowledge_score": knowledge_score,
        "online_verify_score": online_score if online_enabled else 0,
        "total_score": round(total, 1),
    }
