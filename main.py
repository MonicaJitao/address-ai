# ============================================================
# AI 地址智能标准化工具 — FastAPI 主入口
#
# 启动方式：
#   python -m uvicorn main:app --reload --port 8000
#   或直接运行本文件：python main.py
#
# 接口：
#   GET  /            → 前端页面（自动打开浏览器）
#   POST /api/normalize → 地址标准化核心接口
#   GET  /api/health  → 服务健康检查
# ============================================================

import os
import logging
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

# 在最早期加载 .env，确保所有模块初始化前环境变量已就绪
# 使用绝对路径，避免 uvicorn --reload 子进程工作目录不一致导致找不到 .env
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_env_path, override=True)

# 在环境变量加载后再引入依赖模块（避免 env 读取时序问题）
from modules.address_processor import normalize_address, get_llm_adapter

# ── 日志配置 ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "false").lower() == "true" else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


# ── 应用生命周期：启动时预热 LLM Adapter ──────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时预热，避免首次请求延迟（按供应商分别预热，失败仅记录警告）"""
    logger.info("服务启动中，预热 LLM Adapter（DeepSeek / Claude）...")
    for name in ("deepseek", "claude"):
        try:
            get_llm_adapter(name)
            logger.info("LLM Adapter 预热完成：%s", name)
        except Exception as exc:
            logger.warning("LLM Adapter [%s] 预热跳过：%s", name, exc)
    yield
    logger.info("服务已关闭")


# ── 创建 FastAPI 应用 ─────────────────────────────────────
app = FastAPI(
    title="AI 地址智能标准化工具",
    description="将中国地址解析并转换为 CN_INTL_V1 国际英文标准格式",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS 配置：允许前端直接以 file:// 方式打开 HTML 调用 API（开发用）
# 生产环境应将 allow_origins 收窄到具体域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 请求 / 响应数据模型 ───────────────────────────────────

class NormalizeRequest(BaseModel):
    """地址标准化请求体"""
    address: str
    use_online_verify: bool = True
    provider: str = "deepseek"

    @field_validator("address")
    @classmethod
    def address_must_not_be_empty(cls, v: str) -> str:
        """校验地址不能为空"""
        if not v or not v.strip():
            raise ValueError("address 字段不能为空")
        return v.strip()

    @field_validator("provider")
    @classmethod
    def provider_must_be_allowed(cls, v: str) -> str:
        """仅允许 claude 或 deepseek"""
        key = (v or "deepseek").lower().strip()
        if key not in ("claude", "deepseek"):
            raise ValueError('provider 必须是 "claude" 或 "deepseek"')
        return key


class NormalizeResponse(BaseModel):
    """地址标准化响应体（字段含义见 address_processor.py）"""
    success: bool
    raw_address: str
    parsed: dict
    formatted_address: list[str]
    formatted_text: str
    validation: dict
    scores: dict
    model_used: str
    provider: str
    processing_time_ms: int


# ── 路由定义 ──────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_frontend():
    """
    托管前端页面。
    浏览器访问 http://localhost:8000 即可打开完整 UI。
    """
    html_path = os.path.join(os.path.dirname(__file__), "地址智能标准化_前端.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="前端文件未找到")
    return FileResponse(html_path, media_type="text/html")


@app.post("/api/normalize", response_model=NormalizeResponse)
async def api_normalize(req: NormalizeRequest):
    """
    核心接口：接收原始地址，返回标准化结果。

    请求体：
        address (str):           原始地址（中文/英文/混合）
        use_online_verify (bool): 是否启用联网验证（默认 true）

    返回：
        NormalizeResponse：包含解析字段、格式化地址、校验结果、分数等
    """
    logger.info("收到标准化请求：%s", req.address[:80])
    try:
        result = await normalize_address(
            raw_address=req.address,
            use_online_verify=req.use_online_verify,
            provider=req.provider,
        )
        return result
    except RuntimeError as exc:
        # LLM 调用失败等可预期错误，返回 503
        logger.error("标准化处理失败：%s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        # 未预期错误，返回 500
        logger.exception("未知错误：%s", exc)
        raise HTTPException(status_code=500, detail=f"服务器内部错误：{exc}")


@app.get("/api/health")
async def health_check():
    """
    健康检查接口，返回服务状态和当前配置概要。
    可用于监控系统探活。
    """
    provider = os.getenv("LLM_PROVIDER", "deepseek")
    amap_enabled = bool(os.getenv("AMAP_API_KEY", "").strip())
    return {
        "status": "ok",
        "llm_provider": provider,
        "online_verify_enabled": amap_enabled,
        "version": "1.0.0",
    }


# ── 直接运行入口 ──────────────────────────────────────────
if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    debug = os.getenv("DEBUG", "true").lower() == "true"

    amap_key_loaded = bool(os.getenv("AMAP_API_KEY", "").strip())
    logger.info("启动服务：http://%s:%d  (debug=%s, amap_key=%s)", host, port, debug, "已加载" if amap_key_loaded else "未配置")
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=debug,       # debug 模式开启热重载
        log_level="debug" if debug else "info",
    )
