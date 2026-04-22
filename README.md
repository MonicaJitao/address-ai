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
