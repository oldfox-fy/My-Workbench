# backend/services/reranker.py
"""
可配置的 Reranker（重排序）服务层。

设计目标：
- 与 embedding 共用 provider / base_url / api_key，降低用户配置负担。
  用户只需再指定一个 reranker 模型名即可（大部分平台兼容标准 rerank API）。
- 兼容业界标准的 /rerank 端点（SiliconFlow、Jina、Cohere v2 等均遵循此格式）。
- 用 httpx 直接调用（/rerank 不是 OpenAI 官方端点，AsyncOpenAI 上没有封装）。
- 可独立开关（enabled），不影响已有检索路径。

典型请求/响应格式：
  POST {base_url}/rerank
  → {"model": "...", "query": "...", "documents": ["...", ...], "top_n": N}
  ← {"results": [{"index": 0, "relevance_score": 0.98}, ...]}
"""
import asyncio
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import httpx

from backend.db.kb_settings import get_reranker_config
from backend.bootstrap import logger

# 单次 rerank 最大候选文档数（超量自动截断）
_MAX_CANDIDATES = 64
# 失败重试次数
_MAX_RETRIES = 2


class RerankerError(Exception):
    """rerank 调用失败。"""
    pass


@dataclass
class RerankerConfig:
    enabled: bool
    provider: str
    base_url: str
    api_key: str
    model: str


class Reranker:
    """封装 reranker 客户端与调用逻辑。"""

    def __init__(self, cfg: RerankerConfig):
        self.cfg = cfg
        if not cfg.base_url:
            raise RerankerError("未配置 reranker 服务地址（base_url）。请先配置 embedding 服务地址。")
        if not cfg.model:
            raise RerankerError("尚未配置 reranker 模型，请在「知识库设置」中填写。")
        self._base_url = cfg.base_url.rstrip("/")
        self._api_key = cfg.api_key

    async def rerank(self, query: str, documents: List[str],
                     top_n: int = 8) -> List[Dict[str, Any]]:
        """
        对候选文档列表按与 query 的相关性重新排序。

        Args:
            query: 用户原始查询
            documents: 候选文档文本列表
            top_n: 返回前 N 个结果

        Returns:
            [{"index": int, "relevance_score": float}, ...]，按分数降序
        """
        if not documents:
            return []

        # 截断到最大候选数
        docs = documents[:_MAX_CANDIDATES]
        actual_top_n = min(top_n, len(docs))

        url = f"{self._base_url}/rerank"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key or 'ollama'}",
        }
        body = {
            "model": self.cfg.model,
            "query": query,
            "documents": docs,
            "top_n": actual_top_n,
        }

        last_err = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(url, json=body, headers=headers)
                    if resp.status_code >= 400:
                        raise RerankerError(
                            f"HTTP {resp.status_code}: {resp.text[:500]}"
                        )
                    data = resp.json()
                    results = data.get("results", [])
                    return sorted(
                        results, key=lambda r: r.get("relevance_score", 0), reverse=True
                    )
            except RerankerError:
                raise
            except httpx.TimeoutException:
                last_err = Exception("请求超时")
                logger.warning(f"[reranker] 第 {attempt + 1} 次调用超时")
            except Exception as e:
                last_err = e
                logger.warning(f"[reranker] 第 {attempt + 1} 次调用异常：{e}")

            if attempt < _MAX_RETRIES:
                await asyncio.sleep(0.5 * (attempt + 1))

        raise RerankerError(f"rerank 调用失败：{last_err}")


async def get_reranker() -> Optional[Reranker]:
    """
    依据已保存的配置构建 Reranker。
    若未启用或模型未配置则返回 None。

    reranker 的 base_url / api_key 优先使用自身配置，
    未填写时自动继承 embedding 配置。
    """
    cfg_dict = await get_reranker_config()
    if not cfg_dict.get("enabled"):
        return None
    if not cfg_dict.get("model"):
        return None

    # 尝试从 embedding 配置继承 base_url / api_key
    from backend.db.kb_settings import get_embedding_config
    emb_cfg = await get_embedding_config()

    base_url = cfg_dict.get("base_url", "") or emb_cfg.get("base_url", "")
    api_key = cfg_dict.get("api_key", "") or emb_cfg.get("api_key", "")

    reranker_cfg = RerankerConfig(
        enabled=True,
        provider=cfg_dict.get("provider", "openai"),
        base_url=base_url,
        api_key=api_key,
        model=cfg_dict.get("model", ""),
    )
    return Reranker(reranker_cfg)


async def probe_reranker_config(cfg_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    用给定配置发送一条测试请求，验证连通性。

    返回: {"success": bool, "top_score": float, "error": str}
    """
    try:
        from backend.db.kb_settings import get_embedding_config
        emb_cfg = await get_embedding_config()

        base_url = cfg_dict.get("base_url", "") or emb_cfg.get("base_url", "")
        api_key = cfg_dict.get("api_key", "") or emb_cfg.get("api_key", "")

        cfg = RerankerConfig(
            enabled=True,
            provider=cfg_dict.get("provider", "openai"),
            base_url=base_url,
            api_key=api_key,
            model=cfg_dict.get("model", ""),
        )
        reranker = Reranker(cfg)
        results = await reranker.rerank(
            "知识库测试 query",
            ["这是一段测试文本，用于验证 reranker 连通性。",
             "另一段无关文本，用于测试排序效果。"],
            top_n=2,
        )
        if results:
            return {"success": True, "top_score": results[0].get("relevance_score", 0)}
        return {"success": False, "error": "服务返回了空结果。"}
    except RerankerError as e:
        return {"success": False, "top_score": 0, "error": str(e)}
    except Exception as e:
        return {"success": False, "top_score": 0, "error": f"连接失败：{e}"}
