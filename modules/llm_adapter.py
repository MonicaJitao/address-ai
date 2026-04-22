# ============================================================
# LLM Provider Adapter（大模型供应商适配层）
#
# 设计原则：
# - 业务逻辑只依赖本模块的 LLMAdapter，不直接绑定任何具体模型 SDK
# - DeepSeek：OpenAI 兼容 chat.completions
# - Claude 中转：Anthropic Messages API（POST /v1/messages），使用 httpx，不复用 OpenAI 响应解析
# ============================================================

import os
import json
import re
import logging
from typing import Any

import httpx
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# ── 地址解析 Prompt ────────────────────────────────────────
PARSE_SYSTEM_PROMPT = """你是一个专业的中国地址解析与英文标准化专家。

你的任务是将用户输入的地址（可能是中文、英文、拼音或混合格式）解析为结构化 JSON，
并将每个字段翻译为符合国际通用规范的英文表达。

请严格按照以下 JSON Schema 返回结果，不要输出任何其他文字或 Markdown 代码块标记：
{
  "language": "zh|en|pinyin|mixed",  
  "country": "China",
  "province": "省份英文名，如 Guangdong",
  "city": "城市英文名，如 Shenzhen",
  "district": "区县英文名（不含 District 后缀），如 Nanshan",
  "subdistrict": "街道办/镇英文名（可选），如 Xili Subdistrict",
  "street": "街道名英文（含道路类型缩写，如 Keyuan Rd.），不含门牌号",
  "number": "门牌号数字，如 15",
  "building": "楼宇/园区/POI 英文名，如 Science Park",
  "tower": "塔/座号，如 Tower A",
  "floor": "楼层数字，如 12",
  "room": "房间号，如 1203",
  "postal_code": "6 位邮政编码（仅当输入中明确包含时填写，否则留空）",
  "confidence": 0.95,
  "issues": ["发现的问题描述，如 '门牌号缺失'、'区县信息不明确'"]
}

翻译规范：
- 道路后缀统一缩写：路→Rd. 大道→Ave. 街→St. 大街→Blvd.
- 区县不加 District 后缀（格式化模块会补充）
- 专有楼宇若有官方英文名则优先使用，否则按约定译名翻译
- 若字段无法从输入中提取，请留空字符串，不要猜测或编造
- confidence 表示整体解析可信度（0.0~1.0）

低信号 / 非地址输入：
- 若输入仅为纯数字、随机片段、明显测试串或非地理文本（如 "111"、"test"），应视为低置信度解析：
  - 未知字段一律留空，不要为凑字段而编造 street、city、district 等
  - 将 confidence 明显压低（例如 ≤0.25）
  - 在 issues 中说明原因（如「输入缺乏有效地址要素」）

连续拼音 / 大写拼音（如道路或街道办拼音连写）：
- 识别常见行政与道路后缀：jiedao、lu、road、rd、st、ave、dadao、zhen 等，合理切分语义单元
- 不要将整串无空格拼音不加分析地原样塞进 street；若能判断为街道办/片区，优先填入 subdistrict（或相应字段）并保留合理英文形式
- 若语义仍模糊，宁可留空相关字段并在 issues 中注明「拼音连续串语义不明确」
- 不要为单一样本硬编码映射；以通用分类规则为准"""


def _claude_messages_url(base: str) -> str:
    """根据 CLAUDE_PROXY_BASE_URL 拼出 Messages 端点。"""
    b = base.rstrip("/")
    if b.endswith("/v1/messages"):
        return b
    if b.endswith("/messages") and "/v1/" in b:
        return b
    if b.endswith("/v1"):
        return f"{b}/messages"
    return f"{b}/v1/messages"


class LLMAdapter:
    """
    LLM 供应商适配层。
    - deepseek：OpenAI 兼容接口
    - claude：Anthropic Messages API（中转站）
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider.lower().strip()
        if self.provider not in ("claude", "deepseek"):
            raise ValueError(f"不支持的 LLM 供应商: {provider!r}，仅支持 claude、deepseek")

        self.client: AsyncOpenAI | None = None
        self._claude_messages_url: str | None = None
        self._claude_api_key: str | None = None

        if self.provider == "claude":
            base_url = os.getenv("CLAUDE_PROXY_BASE_URL", "").strip().rstrip("/")
            api_key = os.getenv("CLAUDE_PROXY_API_KEY", "")
            self.model = os.getenv("CLAUDE_PROXY_MODEL", "claude-3-5-sonnet-20241022")
            if not base_url or not api_key:
                raise ValueError(
                    "使用 Claude 中转站时，请在 .env 中设置 "
                    "CLAUDE_PROXY_BASE_URL 和 CLAUDE_PROXY_API_KEY"
                )
            self._claude_messages_url = _claude_messages_url(base_url)
            self._claude_api_key = api_key
        else:
            base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
            api_key = os.getenv("DEEPSEEK_API_KEY", "")
            self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
            if not api_key:
                raise ValueError("使用 DeepSeek 时，请在 .env 中设置 DEEPSEEK_API_KEY")
            self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)

        logger.info("LLM Adapter 初始化完成：provider=%s, model=%s", self.provider, self.model)

    async def parse_address(self, raw_address: str) -> dict:
        logger.debug("LLM 请求地址解析：%s", raw_address)
        if self.provider == "claude":
            raw_text = await self._call_claude_messages(raw_address)
        else:
            raw_text = await self._call_deepseek_chat(raw_address)
        logger.debug("LLM 原始响应：%s", raw_text[:500] if raw_text else "")
        return self._extract_json(raw_text)

    async def _call_deepseek_chat(self, raw_address: str) -> str:
        if self.client is None:
            raise RuntimeError("DeepSeek 客户端未初始化")
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": PARSE_SYSTEM_PROMPT},
                {"role": "user", "content": f"请解析以下地址：\n{raw_address}"},
            ],
            temperature=0.1,
            max_tokens=1024,
            response_format={"type": "json_object"},
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("DeepSeek returned no choices")
        msg = getattr(choices[0], "message", None)
        if msg is None:
            raise RuntimeError("DeepSeek returned no message on first choice")
        content = getattr(msg, "content", None)
        if content is None or not str(content).strip():
            raise RuntimeError("DeepSeek returned empty content")
        return str(content).strip()

    async def _call_claude_messages(self, raw_address: str) -> str:
        if not self._claude_messages_url or not self._claude_api_key:
            raise RuntimeError("Claude 中转配置不完整")
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1024,
            "system": PARSE_SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": f"请解析以下地址：\n{raw_address}"},
            ],
            "temperature": 0.1,
        }
        headers = {
            "x-api-key": self._claude_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(120.0, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(self._claude_messages_url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Claude proxy HTTP {resp.status_code}: {resp.text[:800]}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError("Claude proxy returned non-JSON body") from exc

        content_blocks = data.get("content") or []
        text_parts = [
            str(block.get("text", ""))
            for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        raw_text = "".join(text_parts).strip()
        if not raw_text:
            raise RuntimeError("Claude proxy returned empty content")
        return raw_text

    @staticmethod
    def _extract_json(text: str) -> dict:
        text = re.sub(r"```(?:json)?", "", text).strip()
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise RuntimeError("LLM returned non-object JSON root")
            return parsed
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]+\}", text)
            if match:
                try:
                    parsed = json.loads(match.group())
                    if not isinstance(parsed, dict):
                        raise RuntimeError("LLM returned non-object JSON root")
                    return parsed
                except json.JSONDecodeError:
                    pass
            logger.warning("LLM 响应 JSON 解析失败，返回空结构：%s", text[:200])
            return {
                "language": "unknown",
                "country": "China",
                "province": "",
                "city": "",
                "district": "",
                "subdistrict": "",
                "street": "",
                "number": "",
                "building": "",
                "tower": "",
                "floor": "",
                "room": "",
                "postal_code": "",
                "confidence": 0.0,
                "issues": ["LLM 响应格式异常，解析失败"],
            }
