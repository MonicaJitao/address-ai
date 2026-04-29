# 标准化英文地址 AI 工具

一个基于 FastAPI 的中国地址智能标准化服务，用于将中文/英文/拼音/混合地址解析为结构化字段，并输出符合 `CN_INTL_V1` 规范的国际英文地址。

支持两类大模型供应商：
- DeepSeek（OpenAI 兼容接口）
- Claude（Anthropic Messages API 中转）

并内置三层校验（规则、知识库、联网）与综合评分，适合跨境寄递、KYC、表单标准化、地址质量治理等场景。

## 功能特性

- 地址解析：把原始输入拆成 `省/市/区/街道/门牌/楼栋/房间` 等字段
- 英文标准化：输出 `CN_INTL_V1` 多行地址格式
- 三层校验：
  - 第一层：规则校验（字段完整性、邮编格式、基础逻辑）
  - 第二层：知识库校验（行政区命名与省市归属关系）
  - 第三层：联网校验（高德地理编码，可选）
- 多维评分：解析、翻译、格式、知识库、联网校验融合为总分
- Provider 可切换：请求级别指定 `claude` 或 `deepseek`

## 项目结构

```text
.
├─ main.py                         # FastAPI 入口与 API 路由
├─ 地址智能标准化_前端.html         # 本地前端页面（由 / 路由托管）
├─ requirements.txt                # Python 依赖
├─ .env                            # 本地环境配置（已在 .gitignore 中忽略）
├─ .gitignore
└─ modules/
   ├─ address_processor.py         # 主流程编排（解析/格式化/校验/评分）
   ├─ llm_adapter.py               # LLM 适配层（DeepSeek / Claude）
   ├─ address_formatter.py         # CN_INTL_V1 格式化
   ├─ address_validator.py         # 三层校验与总分计算
   └─ knowledge_base.py            # 行政区映射与知识库
```

## 运行环境

- Python 3.10+
- Windows / macOS / Linux 均可

## 快速开始

### 1) 安装依赖

```bash
pip install -r requirements.txt
```

### 2) 配置环境变量

在项目根目录创建 `.env`（或复制你现有模板），至少配置以下键：

```env
# 供应商选择：claude 或 deepseek
LLM_PROVIDER=deepseek

# DeepSeek
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_MODEL=deepseek-chat

# Claude 中转（当 LLM_PROVIDER=claude 时使用）
CLAUDE_PROXY_BASE_URL=https://your-proxy-host
CLAUDE_PROXY_API_KEY=your_claude_proxy_key
CLAUDE_PROXY_MODEL=claude-sonnet-4-6

# 第三层联网验证（可选）
AMAP_API_KEY=your_amap_api_key
# 高德 Web 服务 QPS 与并发（默认 3 次/秒、最多 2 路并发，留余量防 10021）
# AMAP_RATE_LIMIT_PER_SEC=3
# AMAP_MAX_CONCURRENCY=2
# AMAP_CACHE_TTL_SEC=60
# AMAP_EARLY_STOP_SCORE=70

# 服务参数
HOST=0.0.0.0
PORT=8000
DEBUG=true
```

### 3) 启动服务

方式一：

```bash
python main.py
```

方式二：

```bash
python -m uvicorn main:app --reload --port 8000
```

启动后访问：
- 前端页面：`http://localhost:8000/`
- 健康检查：`http://localhost:8000/api/health`
- API 文档：`http://localhost:8000/docs`

## API 说明

### `POST /api/normalize`

请求体：

```json
{
  "address": "广东省深圳市南山区科技园科苑路15号",
  "use_online_verify": true,
  "provider": "deepseek"
}
```

字段说明：
- `address`: 原始地址（必填）
- `use_online_verify`: 是否启用第三层联网校验（默认 `true`）
- `provider`: 本次请求使用的模型供应商（`claude` 或 `deepseek`）

响应（节选）：

```json
{
  "success": true,
  "raw_address": "广东省深圳市南山区科技园科苑路15号",
  "parsed": {
    "country": "China",
    "province": "Guangdong",
    "city": "Shenzhen",
    "district": "Nanshan",
    "street": "Keyuan Rd.",
    "number": "15",
    "confidence": 0.95
  },
  "formatted_address": [
    "Science Park",
    "No. 15 Keyuan Rd.",
    "Nanshan District",
    "Shenzhen, Guangdong",
    "CHINA"
  ],
  "validation": {
    "layer1_rules": {},
    "layer2_knowledge": {},
    "layer3_online": {}
  },
  "scores": {
    "total_score": 90.5
  },
  "model_used": "deepseek-chat",
  "provider": "deepseek",
  "processing_time_ms": 348
}
```

启用高德时，`validation.layer3_online` 除 `amap_address`、`match_status`、`score` 外，还可能包含：
- `suggested_zh_address` / `suggested_zh_confidence` / `suggested_zh_reason`：通过第三层校验时的「可能更准确的中文表述」及置信说明
- `amap_calls`、`queued_ms`、`early_stop_hit`、`rate_limited_count`、`cache_hits`：单次请求内高德调用与限流/缓存摘要

### `GET /api/health`

返回服务状态、当前 LLM 供应商、联网校验开关等信息，便于探活和监控。

## CN_INTL_V1 格式说明

输出遵循“由细到粗”的行序（空行自动省略）：
1. 房间 / 楼层 / 座号
2. 楼宇 / 园区
3. 门牌号 + 街道
4. 区县（自动补 `District`）
5. 城市, 省份 邮编
6. `CHINA`

## 高德联网校验说明

### 验证流程

第三层联网校验通过三轮 API 调用收集候选地址，再打分择优。

**第一步：生成多种查询变体（3~5 条）**

同一个地址会被转换成多种问法，例如原始输入、拼音纠错版、结构化中文串、标准英文格式等，目的是用不同的钥匙去敲高德的门，提高召回率。

**第二步：依次调用三个 API**

```
第一轮：地理编码 API（最权威，优先）
    ↓ 若找到高分结果（≥70分 且精度到道路级）→ 早停，跳过后两轮
第二轮：POI 地点搜索 API（补充楼宇、商场等兴趣点）
    ↓
第三轮：输入提示 API（类似搜索框自动补全，适合拼音/英文混合输入）
```

**第三步：一致性打分**

对每个候选地址按省/市/区/街道/门牌逐项比对，加减分后选出最高分。城市或区县若明确对不上，触发"硬不匹配"，直接判定不通过，不受其他分项影响。

**第四步：最终判决**

| 情况 | 结论 | 对总分的影响 |
|------|------|------------|
| 硬不匹配（城市/区县对不上） | 不通过 | 总分强制压到 ≤75 |
| 一致性分 < 30 | 不通过 | 总分强制压到 ≤75 |
| 一致性分 30~52 | 模糊匹配，不通过 | 总分强制压到 ≤82 |
| 一致性分 ≥ 52，精度到门牌号 | 强匹配，通过 | 联网分最高 95 |
| 一致性分 ≥ 52，精度到道路 | 部分匹配，通过 | 联网分最高 75 |

### 每个地址大约调用几次高德 API？

| 场景 | 调用次数 | 说明 |
|------|---------|------|
| 最好情况（早停） | 1~2 次 | 第一或第二个地理编码查询即得高分，后两轮全跳过 |
| 中间情况 | 最多 5 次 | 地理编码 5 个变体全部跑完，但未触发早停 |
| 最坏情况（三轮全跑） | 最多 12 次 | 地理编码 5 次 + POI 搜索 4 次 + 输入提示 3 次 |

**实际经验值：**
- 标准中文地址 → 约 **1~2 次**（几乎必然早停）
- 英文/拼音混合地址 → 约 **5~8 次**（高德对英文支持较弱，早停概率低）
- 估算 API 成本时，**保守按 8 次/地址**，清晰地址可按 2 次计算

> 注：并发控制（`AMAP_MAX_CONCURRENCY=2`）会将多个查询分批并发发出，实际耗时低于串行调用次数。

## 常见问题

- 未配置 `AMAP_API_KEY` 会怎样？
  - 第三层联网校验自动跳过，不影响前两层与整体流程。

- 为什么要同时有 `provider` 参数和 `.env` 的 `LLM_PROVIDER`？
  - `.env` 提供默认供应商；接口中的 `provider` 允许单次请求覆盖默认值。

- 支持非中国地址吗？
  - 当前规则和知识库围绕中国地址设计，`country` 也默认为 `China`。

## 安全建议

- `.env` 中的 Key 不要提交到仓库（项目已通过 `.gitignore` 忽略）
- 若 Key 曾在聊天、日志或仓库中暴露，建议立即到对应平台重置
- 生产环境请收紧 CORS，不要使用 `allow_origins=["*"]`

## 后续可扩展方向

- 增强区县/乡镇知识库覆盖（长尾城市）
- 增加地址批处理接口（CSV/Excel）
- 引入结果缓存与并发控制
- 增加自动化测试（单元测试 + 接口测试）
