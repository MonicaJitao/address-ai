# ============================================================
# CN_INTL_V1 格式化模块
# 将 LLM 解析出的结构化字段，按中国地址国际英文格式规范
# 重新组装为 6 行标准输出
#
# 行序（由细到粗，适合跨境表单/KYC/面单）：
#   第1行：房间 / 楼层 / 单元 / 座号（可选）
#   第2行：楼宇 / 园区 / POI（可选）
#   第3行：门牌号 + 街道名（核心行）
#   第4行：区县（可选，若与城市不同）
#   第5行：城市, 省份 邮编
#   第6行：CHINA
# ============================================================

import re


def _ensure_suffix(value: str, suffix: str) -> str:
    """若字符串末尾没有 suffix，则自动追加"""
    if value and not value.endswith(suffix):
        return f"{value} {suffix}"
    return value


def _normalize_room(room: str, tower: str, floor: str) -> str | None:
    """
    拼接房间/楼层/座号为第1行。
    示例：Rm 1203, Tower A, F12
    """
    parts = []
    if room:
        # 若已含 Rm/Room 等前缀则保留，否则加上 Rm
        if not re.match(r"^(Rm|Room|Unit|Suite)\b", room, re.IGNORECASE):
            parts.append(f"Rm {room}")
        else:
            parts.append(room)
    if tower:
        # 若已含 Tower/Bldg 等前缀则保留
        if not re.match(r"^(Tower|Bldg|Block)\b", tower, re.IGNORECASE):
            parts.append(f"Tower {tower}")
        else:
            parts.append(tower)
    if floor:
        parts.append(f"F{floor}" if not str(floor).startswith("F") else floor)
    return ", ".join(parts) if parts else None


def _normalize_street(number: str, street: str) -> str | None:
    """
    拼接门牌号和街道名为第3行。
    示例：No. 15 Keyuan Rd.
    规则：
    - 门牌号统一加 "No. " 前缀
    - 若街道名末尾无道路类型缩写，不强行追加（以 LLM 翻译结果为准）
    """
    if number and street:
        # 去掉街道名中多余的 No./no. 前缀（避免重复）
        clean_street = re.sub(r"^No\.\s*\d+\s*", "", street, flags=re.IGNORECASE).strip()
        return f"No. {number} {clean_street}"
    if street:
        return street
    if number:
        return f"No. {number}"
    return None


def _normalize_district(district: str) -> str | None:
    """
    标准化区县名称。
    若末尾没有 District，自动补充。
    """
    if not district:
        return None
    dist = district.strip()
    if not dist.lower().endswith("district"):
        dist = f"{dist} District"
    return dist


def format_cn_intl_v1(parsed: dict) -> tuple[list[str], str]:
    """
    主格式化函数：将解析字段组装为 CN_INTL_V1 格式。

    参数：
        parsed (dict): LLM 解析后的结构化地址字段

    返回：
        formatted_lines (list[str]): 每行地址的列表（已过滤空行）
        formatted_text  (str):       用换行符拼接的完整地址字符串
    """
    lines: list[str] = []

    # 第1行：房间 / 楼层 / 单元（最细粒度，选填）
    line1 = _normalize_room(
        parsed.get("room", ""),
        parsed.get("tower", ""),
        parsed.get("floor", ""),
    )
    if line1:
        lines.append(line1)

    # 第2行：楼宇 / 园区 / POI（选填）
    building = parsed.get("building", "").strip()
    if building:
        lines.append(building)

    # 第3行：门牌号 + 街道名（核心行，缺失时输出警告但不伪造）
    line3 = _normalize_street(
        parsed.get("number", ""),
        parsed.get("street", ""),
    )
    if line3:
        lines.append(line3)

    # 第4行：区县（若与城市不同，选填）
    district = _normalize_district(parsed.get("district", ""))
    city_en = parsed.get("city", "")
    # 避免区县与城市同名时重复输出（如直辖市场景）
    if district and district.replace(" District", "") != city_en:
        lines.append(district)

    # 第5行：城市, 省份 邮编
    city_line_parts = []
    if city_en:
        province_en = parsed.get("province", "")
        # 直辖市省份与城市相同，不重复输出
        if province_en and province_en != city_en:
            city_line_parts.append(f"{city_en}, {province_en}")
        else:
            city_line_parts.append(city_en)
    postal_code = parsed.get("postal_code", "").strip()
    if postal_code and re.fullmatch(r"\d{6}", postal_code):
        # 邮编 6 位校验通过后追加
        if city_line_parts:
            city_line_parts[0] += f" {postal_code}"
        else:
            city_line_parts.append(postal_code)
    if city_line_parts:
        lines.append(city_line_parts[0])

    # 第6行：国家（固定大写）
    lines.append("CHINA")

    formatted_text = "\n".join(lines)
    return lines, formatted_text


def evaluate_format_score(parsed: dict, formatted_lines: list[str]) -> int:
    """
    评估格式化质量，返回 0-100 分。
    扣分规则：
    - 缺少街道行（-30）
    - 缺少城市行（-25）
    - 缺少门牌号（-15）
    - 格式行数少于 3 行（-20）
    """
    score = 100
    if not parsed.get("street"):
        score -= 30
    if not parsed.get("city"):
        score -= 25
    if not parsed.get("number"):
        score -= 15
    if len(formatted_lines) < 3:
        score -= 20
    return max(0, score)
