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
import time
import asyncio
import logging
import difflib
import httpx

from modules.knowledge_base import (
    lookup_province,
    lookup_city,
    verify_city_province,
    PROVINCE_MAP,
    CITY_MAP,
)

logger = logging.getLogger(__name__)

# 高德地图 Web 服务 API 端点
AMAP_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
AMAP_INPUTTIPS_URL = "https://restapi.amap.com/v3/assistant/inputtips"
AMAP_PLACE_TEXT_URL = "https://restapi.amap.com/v3/place/text"

# 联网校验：一致性打分阈值（满分约 100）
_AMAP_PASS_SCORE = 52
_AMAP_AMBIGUOUS_LOW = 30
_AMAP_TIE_GAP = 6

# 高德 QPS / 并发 / 缓存 / 早停（可通过环境变量覆盖）
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


# 进程级：全局限流 + 并发信号量 + 响应缓存（单 worker 内共享）
_amap_rate_limiter: "_AsyncTokenBucket | None" = None
_amap_concurrency_sem: asyncio.Semaphore | None = None
_amap_cache_lock = asyncio.Lock()
_amap_response_cache: dict[str, tuple[float, dict]] = {}


class _AsyncTokenBucket:
    """异步令牌桶：限制平均请求速率（如 3 req/s），返回本次等待毫秒数。"""

    def __init__(self, rate_per_sec: float, capacity: float | None = None) -> None:
        self.rate = max(0.1, rate_per_sec)
        self.capacity = capacity if capacity is not None else self.rate
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def consume(self, n: float = 1.0) -> float:
        """消耗 n 个令牌，必要时等待；返回等待毫秒数。"""
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= n:
                    self._tokens -= n
                    return waited * 1000.0
                need = n - self._tokens
                sleep_sec = need / self.rate if self.rate > 0 else 0.05
            await asyncio.sleep(sleep_sec)
            waited += sleep_sec


def _get_amap_rate_limiter() -> _AsyncTokenBucket:
    global _amap_rate_limiter
    if _amap_rate_limiter is None:
        rps = _env_float("AMAP_RATE_LIMIT_PER_SEC", 3.0)
        cap = min(rps, 3.0)
        _amap_rate_limiter = _AsyncTokenBucket(rps, capacity=cap)
    return _amap_rate_limiter


def _get_amap_concurrency_sem() -> asyncio.Semaphore:
    global _amap_concurrency_sem
    if _amap_concurrency_sem is None:
        n = max(1, _env_int("AMAP_MAX_CONCURRENCY", 2))
        _amap_concurrency_sem = asyncio.Semaphore(n)
    return _amap_concurrency_sem


def _amap_cache_ttl_sec() -> float:
    return max(0.0, _env_float("AMAP_CACHE_TTL_SEC", 60.0))


def _amap_early_stop_score() -> int:
    return max(_AMAP_PASS_SCORE, _env_int("AMAP_EARLY_STOP_SCORE", 70))


def _cache_key_for_get(url: str, params: dict[str, str]) -> str:
    items = sorted((k, str(v)) for k, v in params.items())
    return url + "?" + json.dumps(items, ensure_ascii=False)


def _source_priority_rank(source: str | None) -> int:
    return {"geocode": 3, "place_text": 2, "inputtips": 1}.get(source or "", 0)


def _completeness_hits(cand: dict) -> int:
    n = 0
    for k in ("province", "city", "district"):
        if (cand.get(k) or "").strip():
            n += 1
    fa = (cand.get("formatted_address") or "").strip()
    if fa:
        n += 1
    if fa and re.search(r"\d", fa):
        n += 1
    return n


def _arbitrate_top_on_tie(
    scored: list[tuple[int, dict, list[str], bool]],
    tie_gap: int,
) -> tuple[int, dict, list[str], bool]:
    """
    当顶分与后续候选分差小于 tie_gap 时，按来源优先级与字段完整度二次择优。
    返回应作为「首选」的一条 (score, cand, reasons, hard_mismatch)。
    """
    if not scored:
        raise ValueError("scored empty")
    best_score, best_cand, best_rs, best_hm = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -999
    if (best_score - second_score) >= tie_gap:
        return scored[0]
    band: list[tuple[int, dict, list[str], bool]] = [
        t for t in scored if t[0] >= best_score - tie_gap
    ]
    if len(band) <= 1:
        return scored[0]

    def sort_key(t: tuple[int, dict, list[str], bool]) -> tuple:
        sc, c, _, hm = t
        # hard_mismatch 置底；其余按分、来源、完整度
        return (sc, 0 if hm else 1, _source_priority_rank(c.get("source")), _completeness_hits(c))

    band.sort(key=sort_key, reverse=True)
    chosen = band[0]
    logger.info(
        "高德并列仲裁: 分差<%d 时在 %d 条带内重排，选中 source=%s addr=%s",
        tie_gap,
        len(band),
        chosen[1].get("source"),
        (chosen[1].get("formatted_address") or "")[:120],
    )
    return chosen


def _suggested_zh_confidence_reason(
    passed: bool,
    match_status: str,
    best_score: int,
    second_score: int,
    near_top: int,
    pick_source: str | None,
) -> tuple[str, str]:
    """
    返回 (confidence, reason)。confidence: high | medium | low。
    调用方仅在 passed 且非 mismatch 等场景下附带 suggested_zh_address。

    口径（C）：strong_match 一律高置信；partial_match 再受分差/近分候选影响；weak_match 低置信。
    """
    if not passed or match_status in ("mismatch", "no_match", "api_error", "disabled", "skipped"):
        return "", ""
    gap = best_score - second_score
    tie_like = gap < _AMAP_TIE_GAP
    if match_status == "weak_match":
        return "low", "仅区县/城市级命中，不建议作为强参考"
    # 强命中：业务上优先信任，不因近分候选降级
    if match_status == "strong_match":
        return "high", "兴趣点/门牌级强命中（strong_match），中文参考按高置信展示"
    # 道路级等部分命中：保留保守判断
    if match_status == "partial_match":
        if near_top >= 4:
            return "low", "多条候选得分接近，建议人工核对"
        if tie_like and best_score < 60:
            return "low", "顶分与备选分差过小且分数偏低"
        if tie_like:
            return "medium", "存在得分接近的备选（partial_match）"
        if pick_source == "geocode":
            return "high", "地理编码直命中（道路级）"
        return "medium", "已通过道路级/部分结构化匹配（partial_match）"
    return "medium", "已通过联网校验阈值"

# 常见「城市英文名 → 区县英文名关键词 → 高德 district 中应包含的汉字」
# 用于在 LLM 仅输出英文区县时做弱约束（可按业务扩展）
_DISTRICT_EN_HINT: dict[tuple[str, str], str] = {
    ("shenzhen", "nanshan"): "南山",
    ("shenzhen", "futian"): "福田",
    ("shenzhen", "luohu"): "罗湖",
    ("shenzhen", "longhua"): "龙华",
    ("shenzhen", "baoan"): "宝安",
    ("shenzhen", "longgang"): "龙岗",
    ("shenzhen", "yantian"): "盐田",
    ("shenzhen", "pingshan"): "坪山",
    ("shenzhen", "guangming"): "光明",
    ("guangzhou", "tianhe"): "天河",
    ("guangzhou", "yuexiu"): "越秀",
    ("guangzhou", "haizhu"): "海珠",
    ("guangzhou", "liwan"): "荔湾",
    ("guangzhou", "baiyun"): "白云",
    ("guangzhou", "huangpu"): "黄埔",
    ("guangzhou", "panyu"): "番禺",
}

# 常见混输错拼修正（拼音连续串 / 行政后缀）
_PINYIN_CORRECTIONS: dict[str, str] = {
    "guandongsheng": "guangdongsheng",
    "shenzhengshi": "shenzhenshi",
    "nanshengqu": "nanshanqu",
    "jingangjiedao": "jingangjiedao",
}

_PINYIN_TO_ZH: dict[str, str] = {
    "guangdongsheng": "广东省",
    "shenzhenshi": "深圳市",
    "nanshanqu": "南山区",
    "jingangjiedao": "金港街道",
    "jingangjie": "金港街",
    "webankdasha": "微众银行大厦",
}


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


def _reverse_lookup_zh_name(mapping: dict[str, str], en_value: str, prefer_suffix: str = "") -> str:
    """英文行政区名称反查中文（找不到则返回空）。"""
    en = (en_value or "").strip()
    if not en:
        return ""
    for zh, mapped in mapping.items():
        if mapped == en:
            if prefer_suffix and not zh.endswith(prefer_suffix):
                continue
            return zh
    return ""


def _normalize_pinyin_token(token: str) -> str:
    """对拼音 token 做轻量纠错（覆盖常见错拼 + 近似匹配）。"""
    t = re.sub(r"[^a-z]", "", token.lower())
    if not t:
        return token
    if t in _PINYIN_CORRECTIONS:
        return _PINYIN_CORRECTIONS[t]
    candidates = list(_PINYIN_TO_ZH.keys()) + list(_PINYIN_CORRECTIONS.values())
    m = difflib.get_close_matches(t, candidates, n=1, cutoff=0.86)
    return m[0] if m else t


def _normalize_mixed_raw_query(raw_address: str) -> str:
    """
    对中英拼音混输做轻量规范化：
    - 英文/拼音块统一小写并纠错
    - 可映射为中文的拼音行政词替换为中文
    """
    raw = (raw_address or "").strip()
    if not raw:
        return ""
    tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z]+|\d+|[^\s]", raw)
    out: list[str] = []
    for tok in tokens:
        if re.fullmatch(r"[A-Za-z]+", tok):
            fixed = _normalize_pinyin_token(tok)
            zh = _PINYIN_TO_ZH.get(fixed, "")
            out.append(zh if zh else fixed)
        else:
            out.append(tok)
    s = "".join(out)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_structured_zh_query(parsed: dict, raw_address: str) -> str:
    """
    构造标准中文检索串（省市区 + 路/街 + 门牌 + 建筑），用于提升高德召回。
    """
    province_zh = _reverse_lookup_zh_name(PROVINCE_MAP, parsed.get("province", ""), "省")
    city_zh = _reverse_lookup_zh_name(CITY_MAP, parsed.get("city", ""), "市")
    dist_core = _district_expectation_zh(parsed, raw_address)
    district_zh = f"{dist_core}区" if dist_core else ""

    # 尽量使用原文中的中文道路词，避免英文 St. 误导
    street_zh = ""
    m_street = re.search(r"([\u4e00-\u9fff]{2,16}(?:路|街|大道|街道))", raw_address or "")
    if m_street:
        street_zh = m_street.group(1)

    building_zh = ""
    m_building = re.search(r"([\u4e00-\u9fff]{2,24}(?:大厦|大楼|中心|广场|园区|银行))", raw_address or "")
    if m_building:
        building_zh = m_building.group(1)

    number = (parsed.get("number") or "").strip()
    number_zh = f"{number}号" if number else ""
    return "".join(p for p in [province_zh, city_zh, district_zh, street_zh, number_zh, building_zh] if p).strip()


def _normalized_query_candidates(raw_address: str, parsed: dict, formatted_text: str) -> list[str]:
    """
    生成面向混输场景的有序查询列表（3-5 条）：
    1) 原文
    2) 纠错后的混输串
    3) 结构化中文串
    4) 结构化英文拼接
    5) 标准英文格式化单行
    """
    raw = (raw_address or "").strip()
    mixed_fixed = _normalize_mixed_raw_query(raw)
    zh_structured = _build_structured_zh_query(parsed, raw)
    legacy = _geocode_query_candidates(raw_address, parsed, formatted_text)
    ordered: list[str] = []
    for q in [raw, mixed_fixed, zh_structured, *legacy]:
        q = (q or "").strip()
        if not q:
            continue
        if q not in ordered:
            ordered.append(q)
    return ordered[:5]


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


def _en_province_to_zh_keyword(province_en: str) -> str | None:
    """英文名省份 → 用于匹配的短中文片段（如 广东）。"""
    en = (province_en or "").strip()
    if not en:
        return None
    for zh, mapped in PROVINCE_MAP.items():
        if mapped == en:
            z = zh.replace("省", "").replace("市", "").replace("自治区", "")
            z = z.replace("壮族", "").replace("回族", "").replace("维吾尔", "")
            return z[:6] if z else None
    return None


def _city_limit_for_amap(parsed: dict) -> str | None:
    """高德 geocode / inputtips 的 city 限定：优先中文城市名。"""
    city_en = (parsed.get("city") or "").strip()
    if not city_en:
        return None
    for zh, mapped in CITY_MAP.items():
        if mapped == city_en:
            return zh if zh.endswith("市") else f"{zh}市"
    return city_en


def _extract_district_zh_from_raw(raw_address: str) -> str | None:
    """从原文中提取「xx区」片段，用于混输场景下的区县强约束。"""
    m = re.search(r"([\u4e00-\u9fff]{2,8}区)", (raw_address or ""))
    return m.group(1) if m else None


def _district_expectation_zh(parsed: dict, raw_address: str) -> str | None:
    """期望的区县中文关键词：优先原文 xx区，其次英文区县 + 城市查表。"""
    from_raw = _extract_district_zh_from_raw(raw_address)
    if from_raw:
        return from_raw.replace("区", "")
    dist_en = (parsed.get("district") or "").replace("District", "").strip().lower()
    dist_en = re.sub(r"[^a-z]+", "", dist_en)
    city_en = (parsed.get("city") or "").strip().lower()
    if not dist_en:
        return None
    hint = _DISTRICT_EN_HINT.get((city_en, dist_en))
    return hint


def _tip_city_name(tip: dict) -> str:
    c = tip.get("city")
    if isinstance(c, list):
        return "".join(str(x) for x in c if x) or ""
    return str(c or "")


def _candidate_from_input_tip(tip: dict, keywords_used: str) -> dict:
    """将 InputTips 单条结果规范为统一候选结构。"""
    name = tip.get("name") or ""
    addr = tip.get("address") or ""
    district = tip.get("district") or ""
    city = _tip_city_name(tip)
    province = str(tip.get("province") or "")
    loc = tip.get("location") or ""
    formatted = f"{province}{city}{district}{addr}{name}".strip() or name
    level = "兴趣点" if loc else "区县"
    return {
        "source": "inputtips",
        "query_used": keywords_used,
        "formatted_address": formatted,
        "province": province,
        "city": city,
        "district": district,
        "level": level,
        "location": str(loc),
        "name": name,
        "raw": tip,
    }


def _candidate_from_geocode_row(gc: dict, query: str) -> dict:
    return {
        "source": "geocode",
        "query_used": query,
        "formatted_address": gc.get("formatted_address") or "",
        "province": gc.get("province") or "",
        "city": gc.get("city") or "",
        "district": gc.get("district") or "",
        "level": gc.get("level") or "",
        "location": gc.get("location") or "",
        "name": "",
        "raw": gc,
    }


def _candidate_from_place_poi(poi: dict, keywords_used: str) -> dict:
    """将 place/text 的 POI 结果规范为统一候选结构。"""
    pname = poi.get("pname") or ""
    city = poi.get("cityname") or ""
    district = poi.get("adname") or ""
    addr = poi.get("address") or ""
    name = poi.get("name") or ""
    location = poi.get("location") or ""
    formatted = f"{pname}{city}{district}{addr}{name}".strip() or name
    return {
        "source": "place_text",
        "query_used": keywords_used,
        "formatted_address": formatted,
        "province": pname,
        "city": city,
        "district": district,
        "level": "兴趣点",
        "location": str(location),
        "name": name,
        "raw": poi,
    }


def _dedupe_amap_candidates(rows: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for r in rows:
        key = (r.get("formatted_address") or "", r.get("location") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _level_rank(level: str) -> int:
    if level in ("门牌号", "兴趣点", "单元"):
        return 3
    if level in ("道路", "道路交叉口", "街道"):
        return 2
    if level in ("区县", "城市"):
        return 1
    return 2


def _haystack_for_match(cand: dict) -> str:
    return (
        (cand.get("formatted_address") or "")
        + (cand.get("district") or "")
        + (cand.get("name") or "")
    ).lower()


def _score_amap_candidate(parsed: dict, raw_address: str, cand: dict) -> tuple[int, list[str], bool]:
    """
    对单条高德候选做一致性打分，返回 (分数, 说明列表)。
    分数用于在「InputTips + 多条 geocode」之间择优，并识别误命中。
    """
    reasons: list[str] = []
    score = 0
    hard_mismatch = False
    hay = _haystack_for_match(cand)
    exp_prov = _en_province_to_zh_keyword(parsed.get("province", ""))
    exp_city = _city_limit_for_amap(parsed)
    exp_dist_core = _district_expectation_zh(parsed, raw_address)

    prov = cand.get("province") or ""
    city = cand.get("city") or ""
    dist = cand.get("district") or ""

    if exp_prov:
        if exp_prov in prov or exp_prov in hay:
            score += 18
            reasons.append(f"省域一致(+18): 期望含「{exp_prov}」")
        elif prov:
            score -= 22
            reasons.append(f"省域不一致(-22): 期望「{exp_prov}」实际「{prov}」")

    if exp_city:
        ec = exp_city.replace("市", "")
        cc = city.replace("市", "")
        if ec and (ec in city or ec in cc or ec in hay):
            score += 22
            reasons.append(f"城市一致(+22): 「{exp_city}」")
        elif city:
            score -= 28
            reasons.append(f"城市不一致(-28): 期望「{exp_city}」实际「{city}」")
            hard_mismatch = True

    if exp_dist_core:
        token = exp_dist_core if exp_dist_core.endswith("区") else f"{exp_dist_core}区"
        if token in dist or exp_dist_core in dist or exp_dist_core in hay:
            score += 26
            reasons.append(f"区县一致(+26): 「{exp_dist_core}」")
        else:
            score -= 18
            reasons.append(f"区县不一致(-18): 期望「{exp_dist_core}」实际「{dist}」")
            hard_mismatch = True

    if cand.get("source") == "inputtips":
        score += 6
        reasons.append("来自 InputTips 联想(+6)")

    # 门牌号 / 建筑物 / 街道一致性（强约束）
    num = str(parsed.get("number") or "").strip()
    hit_core = False
    if num:
        cand_nums = re.findall(r"\d{1,6}", hay)
        if num in cand_nums:
            score += 12
            reasons.append(f"门牌命中(+12): {num}")
            hit_core = True
        elif cand_nums:
            score -= 22
            reasons.append(f"门牌冲突(-22): 期望{num} 实际{','.join(cand_nums[:3])}")

    street = (parsed.get("street") or "").lower()
    street_tokens = [
        t for t in re.findall(r"[a-zA-Z]{4,}", street)
        if t not in {"jing", "gang", "street", "road", "district", "lane", "avenue"}
    ]
    if street_tokens:
        matched_street = any(t in hay for t in street_tokens[:3])
        if matched_street:
            score += 12
            reasons.append(f"街道片段命中(+12): {street_tokens[0]}")
            hit_core = True
        else:
            score -= 16
            reasons.append("街道片段未命中(-16)")

    building = (parsed.get("building") or "").lower()
    b_tokens = [
        t for t in re.findall(r"[a-zA-Z]{4,}", building)
        if t not in {"tower", "building", "plaza", "center", "bank", "office"}
    ]
    if b_tokens:
        matched_building = any(t in hay for t in b_tokens[:3])
        if matched_building:
            score += 10
            reasons.append(f"楼宇片段命中(+10): {b_tokens[0]}")
            hit_core = True
        else:
            score -= 12
            reasons.append("楼宇片段未命中(-12)")

    # 对 ST. 类噪声做惩罚（典型误命中：龙华区 ST.(天虹...)）
    if re.search(r"\bst\.\(", hay):
        score -= 14
        reasons.append("疑似缩写噪声ST.(-14)")

    if not hit_core and any(parsed.get(k) for k in ("number", "street", "building")):
        score -= 12
        reasons.append("街道/门牌/楼宇均未命中(-12)")

    score += _level_rank(cand.get("level", "")) * 5
    reasons.append(f"精度等级加权: level={cand.get('level')}")

    return score, reasons, hard_mismatch


def _online_score_from_consistency(
    passed: bool,
    base_status: str,
    consistency: int,
) -> int:
    """把一致性分数映射回 0–100 的 online_score（供总分公式使用）。"""
    if not passed:
        if base_status == "ambiguous_match":
            return 38
        if base_status == "mismatch":
            return 28
        return 20
    return max(45, min(95, int(consistency * 0.85 + 12)))


def _match_status_from_level(level: str) -> tuple[str, float, int]:
    """由高德 level 得到 (match_status, confidence, base_score)。"""
    if level in ("门牌号", "兴趣点", "单元"):
        return "strong_match", 0.92, 95
    if level in ("道路", "道路交叉口", "街道"):
        return "partial_match", 0.70, 75
    if level in ("区县", "城市"):
        return "weak_match", 0.40, 45
    return "partial_match", 0.60, 65


def _amap_telemetry_payload(stats: dict | None) -> dict:
    st = stats or {}
    return {
        "amap_calls": int(st.get("amap_calls", 0)),
        "queued_ms": round(float(st.get("queued_ms", 0.0)), 2),
        "early_stop_hit": bool(st.get("early_stop_hit", False)),
        "rate_limited_count": int(st.get("rate_limited_count", 0)),
        "cache_hits": int(st.get("cache_hits", 0)),
    }


async def _amap_get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
    stats: dict,
) -> dict:
    """
    统一高德 GET：并发信号量 + 令牌桶限流 + 可选短缓存 + 10021 退避重试。
    """
    ttl = _amap_cache_ttl_sec()
    cache_key = _cache_key_for_get(url, params)
    if ttl > 0:
        async with _amap_cache_lock:
            ent = _amap_response_cache.get(cache_key)
            if ent and ent[0] > time.monotonic():
                stats["cache_hits"] = int(stats.get("cache_hits", 0)) + 1
                return ent[1]

    sem = _get_amap_concurrency_sem()
    limiter = _get_amap_rate_limiter()
    backoffs = (0.35, 0.8, 1.6)
    last_data: dict = {}

    for attempt in range(len(backoffs) + 1):
        async with sem:
            wait_ms = await limiter.consume(1.0)
            stats["queued_ms"] = float(stats.get("queued_ms", 0.0)) + wait_ms
            stats["amap_calls"] = int(stats.get("amap_calls", 0)) + 1
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            last_data = resp.json()

        inf = str(last_data.get("infocode", ""))
        info_txt = str(last_data.get("info", "")).upper()
        if inf == "10021" or "CUQPS" in info_txt:
            stats["rate_limited_count"] = int(stats.get("rate_limited_count", 0)) + 1
            if attempt < len(backoffs):
                await asyncio.sleep(backoffs[attempt])
                continue
        if ttl > 0 and last_data.get("status") == "1":
            async with _amap_cache_lock:
                _amap_response_cache[cache_key] = (time.monotonic() + ttl, last_data)
        return last_data

    return last_data


async def _fetch_input_tips(
    client: httpx.AsyncClient,
    keywords: str,
    city_limit: str | None,
    amap_key: str,
    stats: dict,
) -> list[dict]:
    """调用高德 InputTips，用于拼音 / 英文 / 混合输入的候选召回。"""
    kw = (keywords or "").strip()[:256]
    if not kw:
        return []
    params: dict[str, str] = {
        "keywords": kw,
        "key": amap_key,
        "output": "json",
        "datatype": "poi",
        "citylimit": "true",
    }
    if city_limit:
        params["city"] = city_limit
    try:
        data = await _amap_get_json(client, AMAP_INPUTTIPS_URL, params, stats)
    except Exception as exc:
        logger.warning("高德 InputTips 请求失败：%s", exc)
        return []
    if data.get("status") != "1":
        logger.info(
            "高德 InputTips 无有效结果: status=%s info=%s",
            data.get("status"),
            data.get("info"),
        )
        return []
    tips = data.get("tips")
    if not isinstance(tips, list):
        return []
    return tips


def _extract_place_keywords(raw_address: str, parsed: dict) -> list[str]:
    """提取用于 place/text 的关键词（楼宇优先，其次道路+门牌）。"""
    kws: list[str] = []
    raw = (raw_address or "").strip()
    b = (parsed.get("building") or "").strip()
    s = (parsed.get("street") or "").strip()
    n = (parsed.get("number") or "").strip()

    # 中文楼宇关键词
    m_building = re.findall(r"[\u4e00-\u9fff]{2,24}(?:大厦|大楼|中心|广场|园区|银行)", raw)
    for it in m_building:
        if it not in kws:
            kws.append(it)
    if b and b not in kws:
        kws.append(b)
    if s and n:
        combo = f"{s} {n}"
        if combo not in kws:
            kws.append(combo)
    if raw and raw not in kws:
        kws.append(raw[:80])
    return kws[:4]


async def _fetch_place_text(
    client: httpx.AsyncClient,
    keywords: str,
    city_limit: str | None,
    amap_key: str,
    stats: dict,
) -> list[dict]:
    """调用高德 place/text 做 POI 关键词补召回。"""
    kw = (keywords or "").strip()[:128]
    if not kw:
        return []
    params: dict[str, str] = {
        "keywords": kw,
        "key": amap_key,
        "output": "json",
        "extensions": "base",
        "offset": "10",
        "page": "1",
        "citylimit": "true",
    }
    if city_limit:
        params["city"] = city_limit
    try:
        data = await _amap_get_json(client, AMAP_PLACE_TEXT_URL, params, stats)
    except Exception as exc:
        logger.warning("高德 place/text 请求失败：%s", exc)
        return []
    if data.get("status") != "1":
        logger.info(
            "高德 place/text 无有效结果: status=%s info=%s",
            data.get("status"),
            data.get("info"),
        )
        return []
    pois = data.get("pois")
    if not isinstance(pois, list):
        return []
    return [p for p in pois if isinstance(p, dict)]


async def _collect_amap_candidates(
    client: httpx.AsyncClient,
    raw_address: str,
    parsed: dict,
    formatted_text: str,
    amap_key: str,
    city_limit: str | None,
    stats: dict,
) -> list[dict]:
    """
    汇总 geocode + place/text + InputTips 候选。
    顺序：地理编码优先 → POI 补召回 → InputTips；支持早停、去重、受控并发与全局限流。
    """
    rows: list[dict] = []
    normalized_queries = _normalized_query_candidates(raw_address, parsed, formatted_text)
    kw_primary = normalized_queries[0] if normalized_queries else ""
    geo_queries = list(
        dict.fromkeys(
            normalized_queries
            or _geocode_query_candidates(raw_address, parsed, formatted_text)
        )
    )

    early_thr = _amap_early_stop_score()
    early_stop = False
    max_batch = max(1, _env_int("AMAP_MAX_CONCURRENCY", 2))
    geo_attempt = [0]
    last_geo_exc: Exception | None = None

    async def _run_geo(q_inner: str) -> tuple[list[dict], bool]:
        params_g: dict[str, str] = {
            "address": q_inner,
            "key": amap_key,
            "output": "json",
        }
        if city_limit:
            params_g["city"] = city_limit
        try:
            geo_attempt[0] += 1
            att = geo_attempt[0]
            data = await _amap_get_json(client, AMAP_GEOCODE_URL, params_g, stats)
        except Exception as exc:
            logger.warning(
                "高德 geocode 第 %d/%d 次异常: %s",
                geo_attempt[0],
                len(geo_queries),
                exc,
            )
            return ([], False, exc)
        _log_amap_geocode_response(q_inner, data, att)
        if data.get("status") != "1":
            return ([], False, None)
        acc: list[dict] = []
        st_local = False
        for gc in data.get("geocodes") or []:
            if not isinstance(gc, dict):
                continue
            cand = _candidate_from_geocode_row(gc, q_inner)
            acc.append(cand)
            sc, _, hm = _score_amap_candidate(parsed, raw_address, cand)
            if (
                (not hm)
                and sc >= early_thr
                and _level_rank(cand.get("level", "")) >= 2
            ):
                st_local = True
        return (acc, st_local, None)

    # ── Phase 1：地理编码（优先，小批量 gather）────────────────
    gi = 0
    while gi < len(geo_queries) and not early_stop:
        batch = geo_queries[gi : gi + max_batch]
        gi += len(batch)
        results = await asyncio.gather(*[_run_geo(q) for q in batch])
        for subrows, st_hit, geo_exc in results:
            if geo_exc is not None:
                last_geo_exc = geo_exc
            rows.extend(subrows)
            if st_hit:
                early_stop = True
                stats["early_stop_hit"] = True
                logger.info(
                    "高德早停: 地理编码一致性>=%d 且 level 不低于道路级",
                    early_thr,
                )
                break

    # ── Phase 2：place/text（早停则跳过）──────────────────────
    if not early_stop:
        place_keywords = _extract_place_keywords(kw_primary or raw_address, parsed)
        pi = 0
        while pi < len(place_keywords):
            batch = place_keywords[pi : pi + max_batch]
            pi += len(batch)

            async def _one_place(kw: str) -> list[dict]:
                pois = await _fetch_place_text(client, kw, city_limit, amap_key, stats)
                logger.info(
                    "高德 place/text: keywords=%r city_limit=%r 条数=%d",
                    kw[:80],
                    city_limit,
                    len(pois),
                )
                out_local: list[dict] = []
                for poi in pois[:10]:
                    out_local.append(_candidate_from_place_poi(poi, kw))
                return out_local

            batches = await asyncio.gather(*[_one_place(kw) for kw in batch])
            for bl in batches:
                rows.extend(bl)

    # ── Phase 3：InputTips（早停则跳过）───────────────────────
    if not early_stop:
        tip_queries = normalized_queries[:3]
        ti = 0
        while ti < len(tip_queries):
            batch = tip_queries[ti : ti + max_batch]
            ti += len(batch)

            async def _one_tips(kw: str) -> list[dict]:
                tips = await _fetch_input_tips(client, kw, city_limit, amap_key, stats)
                logger.info(
                    "高德 InputTips: keywords=%r city_limit=%r 条数=%d",
                    kw[:120],
                    city_limit,
                    len(tips),
                )
                out_local: list[dict] = []
                for tip in tips[:15]:
                    out_local.append(_candidate_from_input_tip(tip, kw))
                    logger.debug(
                        "InputTips 候选: name=%s district=%s city=%s",
                        tip.get("name"),
                        tip.get("district"),
                        _tip_city_name(tip),
                    )
                return out_local

            for bl in await asyncio.gather(*[_one_tips(kw) for kw in batch]):
                rows.extend(bl)

    deduped = _dedupe_amap_candidates(rows)
    if not deduped and last_geo_exc is not None:
        raise last_geo_exc
    logger.info(
        "高德请求摘要: calls=%s queued_ms=%.1f early_stop=%s rate_limited=%s cache_hits=%s",
        stats.get("amap_calls", 0),
        float(stats.get("queued_ms", 0.0)),
        stats.get("early_stop_hit", False),
        stats.get("rate_limited_count", 0),
        stats.get("cache_hits", 0),
    )
    return deduped


async def validate_layer3_online(
    parsed: dict,
    formatted_text: str,
    raw_address: str = "",
) -> dict:
    """
    联网验证：InputTips 候选召回 + 地理编码（带 city 限定）+ 一致性打分择优。

    match_status 在 strong_match / partial_match / weak_match / no_match 之外，
    可能为 ambiguous_match（多候选难分）或 mismatch（与解析结构明显矛盾）。
    """
    tel0 = _amap_telemetry_payload({})
    amap_key = os.getenv("AMAP_API_KEY", "").strip()
    if not amap_key:
        return {
            "enabled": False,
            "passed": True,
            "match_status": "disabled",
            "provider": "amap",
            "provider_confidence": 0.0,
            "amap_address": "",
            "issues": [],
            "score": 0,
            **tel0,
        }

    if not _normalized_query_candidates(raw_address, parsed, formatted_text):
        return {
            "enabled": True,
            "passed": False,
            "match_status": "no_match",
            "provider": "amap",
            "provider_confidence": 0.0,
            "amap_address": "",
            "issues": ["缺少可用于地理编码的地址文本"],
            "score": 20,
            **tel0,
        }

    stats: dict = {
        "amap_calls": 0,
        "queued_ms": 0.0,
        "early_stop_hit": False,
        "rate_limited_count": 0,
        "cache_hits": 0,
    }
    city_limit = _city_limit_for_amap(parsed)
    candidates: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            candidates = await _collect_amap_candidates(
                client, raw_address, parsed, formatted_text, amap_key, city_limit, stats
            )
    except Exception as exc:
        logger.warning("高德 API 调用失败：%s", exc)
        return {
            "enabled": True,
            "passed": True,
            "match_status": "api_error",
            "provider": "amap",
            "provider_confidence": 0.0,
            "amap_address": "",
            "issues": [f"联网验证请求失败：{exc}"],
            "score": 50,
            **_amap_telemetry_payload(stats),
        }

    if not candidates:
        return {
            "enabled": True,
            "passed": False,
            "match_status": "no_match",
            "provider": "amap",
            "provider_confidence": 0.0,
            "amap_address": "",
            "issues": [
                "高德未返回可用候选（InputTips 与地理编码均无结果），请检查输入或稍后再试",
            ],
            "score": 20,
            **_amap_telemetry_payload(stats),
        }

    scored: list[tuple[int, dict, list[str], bool]] = []
    for cand in candidates:
        sc, rs, hm = _score_amap_candidate(parsed, raw_address, cand)
        scored.append((sc, cand, rs, hm))
    scored.sort(key=lambda x: x[0], reverse=True)

    debug_enabled = os.getenv("DEBUG", "false").lower() == "true"
    debug_candidates = [
        {
            "score": s,
            "source": c.get("source"),
            "query": c.get("query_used"),
            "address": c.get("formatted_address"),
            "district": c.get("district"),
            "hard_mismatch": hm,
        }
        for s, c, _, hm in scored[:3]
    ]

    chosen = _arbitrate_top_on_tie(scored, _AMAP_TIE_GAP)
    rest = [t for t in scored if t[1] is not chosen[1]]
    rest.sort(key=lambda x: x[0], reverse=True)
    second_score = rest[0][0] if rest else -999

    best_score, best_cand, best_reasons, best_hard_mismatch = chosen

    logger.info(
        "高德候选择优: best_score=%d source=%s query=%r addr=%s",
        best_score,
        best_cand.get("source"),
        (best_cand.get("query_used") or "")[:100],
        (best_cand.get("formatted_address") or "")[:120],
    )
    logger.info("高德候选打分说明: %s", " | ".join(best_reasons[:8]))
    if len(scored) > 1:
        logger.info(
            "高德候选分差: top1=%d top2=%d (候选总数=%d)",
            best_score,
            second_score,
            len(scored),
        )

    tie_like = (best_score - second_score) < _AMAP_TIE_GAP
    tel = _amap_telemetry_payload(stats)
    dbg = {"debug_candidates": debug_candidates} if debug_enabled else {}

    if best_hard_mismatch:
        return {
            "enabled": True,
            "passed": False,
            "match_status": "mismatch",
            "provider": "amap",
            "provider_confidence": 0.22,
            "amap_address": best_cand.get("formatted_address", ""),
            "issues": [
                "候选与期望行政区冲突，已触发硬约束拒绝（hard_mismatch）",
                f"当前最高一致性得分={best_score}",
            ],
            "score": _online_score_from_consistency(False, "mismatch", best_score),
            "consistency_score": best_score,
            "amap_pick_source": best_cand.get("source"),
            "amap_pick_query": best_cand.get("query_used"),
            **tel,
            **dbg,
        }

    if best_score < _AMAP_AMBIGUOUS_LOW:
        return {
            "enabled": True,
            "passed": False,
            "match_status": "mismatch",
            "provider": "amap",
            "provider_confidence": 0.25,
            "amap_address": best_cand.get("formatted_address", ""),
            "issues": [
                "高德返回结果与解析地址一致性不足（省市区或道路信息不匹配），"
                f"最高得分={best_score}",
            ],
            "score": _online_score_from_consistency(False, "mismatch", best_score),
            "consistency_score": best_score,
            "amap_pick_source": best_cand.get("source"),
            "amap_pick_query": best_cand.get("query_used"),
            **tel,
            **dbg,
        }

    # 未达通过阈值：视为模糊或不可信
    if best_score < _AMAP_PASS_SCORE:
        return {
            "enabled": True,
            "passed": False,
            "match_status": "ambiguous_match",
            "provider": "amap",
            "provider_confidence": 0.45,
            "amap_address": best_cand.get("formatted_address", ""),
            "issues": [
                "高德存在多个可能位置或置信偏低，请补充更标准的中文地址或核对区县门牌",
                f"当前最高一致性得分={best_score}（阈值={_AMAP_PASS_SCORE}）",
            ],
            "score": _online_score_from_consistency(False, "ambiguous_match", best_score),
            "consistency_score": best_score,
            "amap_pick_source": best_cand.get("source"),
            "amap_pick_query": best_cand.get("query_used"),
            **tel,
            **dbg,
        }

    # 已达通过阈值，但顶分候选过于接近：仍判歧义，避免误命中
    if tie_like and best_score < 60:
        return {
            "enabled": True,
            "passed": False,
            "match_status": "ambiguous_match",
            "provider": "amap",
            "provider_confidence": 0.48,
            "amap_address": best_cand.get("formatted_address", ""),
            "issues": [
                "高德多条候选得分接近，无法稳定择优，请改用更标准的中文或补充门牌",
                f"当前最高一致性得分={best_score}，与第二名分差 < {_AMAP_TIE_GAP}",
            ],
            "score": _online_score_from_consistency(False, "ambiguous_match", best_score),
            "consistency_score": best_score,
            "amap_pick_source": best_cand.get("source"),
            "amap_pick_query": best_cand.get("query_used"),
            **tel,
            **dbg,
        }

    level = best_cand.get("level", "")
    match_status, confidence, base_score = _match_status_from_level(level)
    near_top = sum(1 for s, _, __, ___ in scored if s >= best_score - _AMAP_TIE_GAP)

    issues: list[str] = []
    if match_status == "weak_match":
        issues.append("地址匹配精度较低，仅命中区县/城市级别，建议补充详细街道信息")
    if near_top >= 4 and match_status != "strong_match":
        issues.append(f"存在 {near_top} 条得分接近的候选，可能存在歧义")

    final_score = _online_score_from_consistency(True, match_status, best_score)
    final_score = min(final_score, base_score)

    passed = match_status in ("strong_match", "partial_match")
    suggested_pack: dict = {}
    if passed:
        zh_addr = (best_cand.get("formatted_address") or "").strip()
        szh_conf, szh_reason = _suggested_zh_confidence_reason(
            True,
            match_status,
            best_score,
            second_score,
            near_top,
            best_cand.get("source"),
        )
        if zh_addr and szh_conf:
            suggested_pack = {
                "suggested_zh_address": zh_addr,
                "suggested_zh_confidence": szh_conf,
                "suggested_zh_reason": szh_reason,
            }
            logger.info(
                "推荐中文参考: addr=%s confidence=%s reason=%s",
                zh_addr[:120],
                szh_conf,
                szh_reason,
            )

    return {
        "enabled": True,
        "passed": passed,
        "match_status": match_status,
        "provider": "amap",
        "provider_confidence": confidence,
        "amap_address": best_cand.get("formatted_address", ""),
        "issues": issues,
        "score": final_score,
        "consistency_score": best_score,
        "amap_pick_source": best_cand.get("source"),
        "amap_pick_query": best_cand.get("query_used"),
        **tel,
        **suggested_pack,
        **dbg,
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
