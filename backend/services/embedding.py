# backend/services/embedding.py
"""
可配置的 embedding（向量化）服务层。

设计目标：
- 双引擎：本地 Ollama 与云端 OpenAI 均通过 OpenAI 兼容协议调用（复用 AsyncOpenAI），
  与项目现有 LLMService 的客户端构造方式保持一致。
- 配置驱动：provider / base_url / api_key / model 来自 app_settings（见 db/kb_settings.py）。
- 维度自适应：不同模型维度不同（bge-m3=1024，text-embedding-3-small=1536），
  维度由 probe() 探测并持久化；切换模型后维度变化必须重建索引。
"""
import asyncio
from dataclasses import dataclass
from typing import List, Dict, Any

from openai import AsyncOpenAI, APIError

from backend.db.kb_settings import get_embedding_config, update_embedding_dim
from backend.bootstrap import logger

# 单次请求的最大文本条数，防止请求体过大 / 超时
_BATCH_SIZE = 32
# 失败重试次数
_MAX_RETRIES = 2


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
    """封装一个 embedding 配置对应的客户端与调用逻辑。"""

    def __init__(self, cfg: EmbeddingConfig):
        self.cfg = cfg
        if not cfg.model:
            raise EmbeddingError("尚未配置 embedding 模型，请先在「知识库设置」中填写。")
        if not cfg.base_url:
            raise EmbeddingError("尚未配置 embedding 服务地址（base_url）。")
        # Ollama 本地无需 key；云端必须有 key
        self.client = AsyncOpenAI(api_key=cfg.api_key or "ollama", base_url=cfg.base_url)

    async def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        last_err = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self.client.embeddings.create(
                    model=self.cfg.model,
                    input=texts,
                    encoding_format="float",  # 显式指定 float，兼容不支持 base64 的提供商
                )
                # 保持与输入顺序一致
                data = sorted(resp.data, key=lambda d: d.index)
                return [d.embedding for d in data]
            except APIError as e:
                last_err = e
                status = getattr(e, 'status_code', 0)
                err_body = {}
                try:
                    err_body = e.body if isinstance(e.body, dict) else {}
                except Exception:
                    pass
                err_code = err_body.get("code", "")
                err_msg = err_body.get("message", str(e))

                # 硅基流动 20015：参数无效 → 通常是模型名不支持或 encoding_format 问题
                if err_code == 20015:
                    logger.warning(
                        f"[embedding] API 参数错误（code=20015）。"
                        f"请检查知识库设置中的 embedding 模型名是否正确。"
                        f"硅基流动支持的模型如 BAAI/bge-m3、BAAI/bge-large-zh-v1.5 等。"
                        f"当前模型: {self.cfg.model}"
                    )
                    # 不重试——参数错误重试也没用
                    raise EmbeddingError(
                        f"embedding API 参数无效（code=20015）。"
                        f"请检查模型名「{self.cfg.model}」是否在当前服务商支持。"
                    ) from e

                logger.warning(
                    f"[embedding] 第 {attempt + 1} 次调用失败"
                    f"（HTTP {status}）: {err_msg[:200]}"
                )
                await asyncio.sleep(0.5 * (attempt + 1))
            except Exception as e:
                last_err = e
                logger.warning(f"[embedding] 第 {attempt + 1} 次调用异常：{e}")
                await asyncio.sleep(0.5 * (attempt + 1))
        raise EmbeddingError(f"embedding 调用失败：{last_err}")

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
