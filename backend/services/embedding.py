# backend/services/embedding.py
"""
可配置的 embedding（向量化）服务层。

设计目标：
- 双引擎：本地 Ollama 与云端（硅基流动/OpenAI 等）均通过 httpx 直调 OpenAI 兼容 API。
  不再依赖 openai SDK，避免 SDK 自动注入不兼容参数（如 dimensions/modalities 等）
  导致硅基流动等提供商返回 code=20015。
- 配置驱动：provider / base_url / api_key / model 来自 app_settings（见 db/kb_settings.py）。
- 维度自适应：不同模型维度不同（bge-m3=1024，text-embedding-3-small=1536），
  维度由 probe() 探测并持久化；切换模型后维度变化必须重建索引。
"""
import asyncio
import json as _json
from dataclasses import dataclass
from typing import List, Dict, Any

import httpx

from backend.db.kb_settings import get_embedding_config, update_embedding_dim
from backend.bootstrap import logger

# 单次请求的最大文本条数，防止请求体过大 / 超时
_BATCH_SIZE = 32
# 失败重试次数
_MAX_RETRIES = 2
# 可重试的 HTTP 状态码
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# httpx 超时
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)


class EmbeddingError(Exception):
    """embedding 调用失败。"""
    pass


@dataclass
class EmbeddingConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    dim: int

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EmbeddingConfig":
        return cls(
            provider=d.get("provider", "ollama"),
            base_url=d.get("base_url", ""),
            api_key=d.get("api_key", ""),
            model=d.get("model", ""),
            dim=int(d.get("dim", 0) or 0),
        )


class Embedder:
    """
    封装 embedding 配置与调用逻辑。
    使用 httpx 直调（不依赖 openai SDK），精确控制请求参数。
    """

    def __init__(self, cfg: EmbeddingConfig):
        self.cfg = cfg
        if not cfg.model:
            raise EmbeddingError("尚未配置 embedding 模型，请先在「知识库设置」中填写。")
        if not cfg.base_url:
            raise EmbeddingError("尚未配置 embedding 服务地址（base_url）。")
        self._base_url = cfg.base_url.rstrip("/")
        self._api_key = cfg.api_key or "ollama"
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """惰性创建 httpx 客户端（复用连接池）。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=_TIMEOUT,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        """调用 /embeddings 端点，只发送 model + input + encoding_format 三个参数。"""
        import logging as _logging
        _log = _logging.getLogger("My Workbench")
        _log.info(
            f"[embedding] 调用 {self._base_url}/embeddings "
            f"model={self.cfg.model} texts={len(texts)}"
        )

        body = {
            "model": self.cfg.model,
            "input": texts,
            "encoding_format": "float",
        }

        last_err = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                client = self._get_client()
                resp = await client.post("/embeddings", json=body)

                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", [])
                    # 按 index 排序，保证与输入顺序一致
                    items.sort(key=lambda d: d.get("index", 0))
                    return [d["embedding"] for d in items]

                # 处理错误
                err_body = {}
                try:
                    err_body = resp.json()
                except Exception:
                    pass
                err_code = err_body.get("code", "")
                err_msg = err_body.get("message", "")

                # 硅基流动 20015：参数无效
                if err_code == 20015 or "20015" in str(err_code):
                    raise EmbeddingError(
                        f"embedding API 参数无效（code=20015）。"
                        f"当前模型: {self.cfg.model}，请确认该模型在当前服务商可用。"
                        f"硅基流动可用: BAAI/bge-m3 / BAAI/bge-large-zh-v1.5 等"
                    )

                # 可重试的状态码
                if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                    last_err = EmbeddingError(
                        f"HTTP {resp.status_code}: {err_msg or resp.text[:200]}"
                    )
                    logger.warning(
                        f"[embedding] 第 {attempt + 1} 次调用失败"
                        f"（HTTP {resp.status_code}），{0.5 * (attempt + 1):.1f}s 后重试..."
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

                # 不可重试的错误
                raise EmbeddingError(
                    f"embedding API 返回 HTTP {resp.status_code}: "
                    f"{err_msg or resp.text[:200]}"
                )

            except EmbeddingError:
                raise  # 不包装
            except httpx.TimeoutException as e:
                last_err = e
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        f"[embedding] 第 {attempt + 1} 次调用超时，"
                        f"{0.5 * (attempt + 1):.1f}s 后重试..."
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    raise EmbeddingError(f"embedding 调用超时（已重试 {_MAX_RETRIES} 次）") from e
            except httpx.HTTPError as e:
                last_err = e
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        f"[embedding] 第 {attempt + 1} 次网络异常: {e}，"
                        f"{0.5 * (attempt + 1):.1f}s 后重试..."
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    raise EmbeddingError(f"embedding 网络异常（已重试 {_MAX_RETRIES} 次）: {e}") from e
            except Exception as e:
                last_err = e
                logger.warning(f"[embedding] 第 {attempt + 1} 次调用异常：{e}")
                await asyncio.sleep(0.5 * (attempt + 1))

        raise EmbeddingError(f"embedding 调用失败（已重试 {_MAX_RETRIES} 次）: {last_err}")

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """对一组文本做向量化，自动分批。返回与输入等长的向量列表。"""
        if not texts:
            return []
        results: List[List[float]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i:i + _BATCH_SIZE]
            results.extend(await self._embed_batch(batch))
        return results

    async def embed_one(self, text: str) -> List[float]:
        vecs = await self.embed([text])
        return vecs[0] if vecs else []


async def get_embedder() -> Embedder:
    """依据已保存的配置构建 Embedder。"""
    cfg = EmbeddingConfig.from_dict(await get_embedding_config())
    return Embedder(cfg)


async def probe_config(cfg_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    用给定配置发一条测试文本，验证连通性并返回实际向量维度。
    成功时将维度持久化到 app_settings（若与当前保存的配置为同一模型）。

    返回: {"success": bool, "dim": int, "error": str}
    """
    embedder = None
    try:
        cfg = EmbeddingConfig.from_dict(cfg_dict)
        embedder = Embedder(cfg)
        vec = await embedder.embed_one("知识库连接测试 knowledge base connectivity test")
        dim = len(vec)
        if dim == 0:
            return {"success": False, "dim": 0, "error": "服务返回了空向量。"}
        # 仅当探测配置与已保存配置的模型一致时才写回维度
        try:
            await update_embedding_dim(dim)
        except Exception:
            pass
        return {"success": True, "dim": dim, "error": ""}
    except EmbeddingError as e:
        return {"success": False, "dim": 0, "error": str(e)}
    except Exception as e:
        return {"success": False, "dim": 0, "error": f"连接失败：{e}"}
    finally:
        if embedder:
            await embedder.close()
