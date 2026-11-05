#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rag_engine.py —— RAG 引擎（阶段 1 原型的可复用层 + 流式改造）
====================================================================
这个文件是阶段 2 的核心。它把阶段 1 `rag_proto.py` 里"与 Web 无关"的
部分原样抽出，并做两处增强：

  1. LLM 支持流式输出（chat_stream 生成器）——SSE 打字机效果的数据源
  2. 切片带上「来源文件 + 章节标题」元数据——引用区要显示"文件名+片段"

设计原则：本文件**完全不依赖 FastAPI**，可以单独 import 做单元测试，
也可以原样搬到阶段 3 的微服务里。

【核心模块清单】（阶段 2/3 直接复用）
  ErrorCode / RAGError   统一错误契约，与接口层共用
  Config                 环境变量配置，阶段 2 平移 pydantic-settings
  TextChunker            markdown 按标题切节 + 500 字细分
  EmbeddingProvider      向量化抽象（智谱 / 本地 / 降级三实现）
  FaissVectorStore       向量索引（接口对齐 pgvector，换库只改这里）
  BM25Store              关键词检索
  HybridRetriever        BM25 + 向量 RRF 融合
  BaseLLM                LLM 抽象（chat 同步 / chat_stream 流式）
  build_messages         提示词组装（含历史上下文）
  validate_citations     引用完整性自检
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol

from ..utils.paths import DEFAULT_DOC_PATH, INDEX_CACHE_DIR

# ====================================================================
# 【核心模块 · 全栈共用】统一错误契约
# ----------------------------------------------------------------
# 复用说明：main.py 的 HTTP 层和 SSE 层都 import 这里的 code/message，
# 保证"接口文档写的错误码"和"代码里抛的错误码"永远不会不一致。
# 阶段 2 接入监控时，直接按 code 聚合告警即可。
# ====================================================================
class ErrorCode:
    """错误码常量。code 用 5 位数字，前两位区分责任层。"""

    # ---- 4xx：调用方问题 ----
    INVALID_JSON = (40001, "请求体不是合法的 JSON")
    MISSING_QUESTION = (40002, "缺少必填参数 question")
    EMPTY_QUESTION = (40003, "问题不能为空")
    QUESTION_TOO_LONG = (40004, "问题长度不能超过 500 字")
    BAD_SESSION_ID = (40005, "session_id 格式不合法")
    SESSION_NOT_FOUND = (40101, "会话不存在")
    FORBIDDEN_SESSION = (40301, "无权访问该会话")
    RATE_LIMITED = (42901, "请求过于频繁，请稍后再试")

    # ---- 5xx：服务方问题 ----
    INDEX_NOT_READY = (50001, "制度索引未构建，请联系管理员")
    RETRIEVAL_FAILED = (50002, "检索服务不可用")
    LLM_UNAVAILABLE = (50003, "模型服务暂时不可用，请稍后重试")
    LLM_TIMEOUT = (50004, "模型响应超时，请重试")
    INTERNAL_ERROR = (50099, "服务内部错误")


class RAGError(Exception):
    """携带错误码的业务异常。

    用法：raise RAGError(ErrorCode.EMPTY_QUESTION, detail="question 为空串")
    接口层捕获后统一转成 JSON 或 SSE error 事件。
    """

    def __init__(self, code_pair: tuple[int, str], detail: str = "") -> None:
        self.code, self.message = code_pair
        self.detail = detail
        super().__init__(f"[{self.code}] {self.message}")

    def to_dict(self, request_id: str = "") -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "request_id": request_id,
        }


# ====================================================================
# 【核心模块】配置
# ====================================================================
@dataclass
class Config:
    """全部可调参数。阶段 2 换成 pydantic-settings，字段名保持不变。"""

    # ---- 文档与切片 ----
    doc_path: str = "./报销制度.md"
    chunk_size: int = 500
    chunk_overlap: int = 50
    min_chunk_len: int = 30

    # ---- 检索 ----
    top_k: int = 3
    rrf_k: int = 60
    bm25_candidates: int = 20
    vector_candidates: int = 20

    # ---- 上下文历史 ----
    history_rounds: int = 6            # 最近 6 轮 = 12 条消息
    history_char_budget: int = 3000    # 历史总字数硬上限，超出从最早开始裁

    # ---- 输入约束 ----
    max_question_len: int = 500

    # ---- 向量供应商 ----
    embed_provider: str = "zhipu"      # zhipu | fastembed | hash
    embed_model: str = "embedding-2"
    embed_dim: int = 1024

    # ---- LLM ----
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.1
    llm_max_tokens: int = 800
    llm_timeout: int = 60

    # ---- 索引缓存 ----
    # 默认落在项目根 data/.rag_cache，不再依赖进程当前工作目录。
    index_dir: str = str(INDEX_CACHE_DIR)

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量读取。所有供应商切换都不需要改代码。"""
        return cls(
            # 原型期默认是 "./报销制度.md"（相对 CWD，换个目录启动就找不到）。
            # 现在指向项目根 data/，同时保留 DOC_PATH / INDEX_DIR 覆盖能力。
            doc_path=os.getenv("DOC_PATH", str(DEFAULT_DOC_PATH)),
            index_dir=os.getenv("INDEX_DIR", str(INDEX_CACHE_DIR)),
            embed_provider=os.getenv("EMBED_PROVIDER", "zhipu"),
            embed_model=os.getenv("EMBED_MODEL", "embedding-2"),
            embed_dim=int(os.getenv("EMBED_DIM", "1024")),
            chunk_size=int(os.getenv("CHUNK_SIZE", "500")),
            top_k=int(os.getenv("TOP_K", "3")),
            history_rounds=int(os.getenv("HISTORY_ROUNDS", "6")),
            llm_base_url=os.getenv("LLM_BASE_URL", ""),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL", ""),
            llm_timeout=int(os.getenv("LLM_TIMEOUT", "60")),
        )


# ====================================================================
# 【核心模块】文档切片器
# ----------------------------------------------------------------
# 阶段 1 踩坑结论：制度/合同类文档必须按 markdown 标题切节，
# 纯按字数滑窗会把多个章节搅进同一块，导致答案串项。
# ====================================================================
class TextChunker:
    """按标题切「节」，节内再按 chunk_size 细分，绝不跨节合并。"""

    def __init__(self, chunk_size: int = 500, overlap: int = 50,
                 min_len: int = 30) -> None:
        if overlap >= chunk_size:
            raise ValueError("overlap 必须小于 chunk_size")
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.min_len = min_len

    def load_markdown(self, path: str | Path) -> str:
        text = Path(path).read_text(encoding="utf-8")
        lines = [ln.rstrip() for ln in text.splitlines()]
        text = "\n".join(lines)
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text.strip()

    def split(self, text: str) -> list[str]:
        if not text:
            return []

        # ---- 1) 按标题行切节 ----
        sections: list[list[str]] = []
        current: list[str] = []
        for line in text.split("\n"):
            if line.lstrip().startswith("#") and current:
                sections.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append(current)

        # ---- 2) 节内切块 ----
        chunks: list[str] = []
        for sec in sections:
            block = "\n".join(sec).strip()
            if not block:
                continue
            if len(block) <= self.chunk_size:
                chunks.append(block)
                continue

            buffer = ""
            for line in sec:
                if len(line) > self.chunk_size:
                    if buffer:
                        chunks.append(buffer.strip())
                        buffer = ""
                    for i in range(0, len(line), self.chunk_size - self.overlap):
                        chunks.append(line[i : i + self.chunk_size].strip())
                    continue
                if len(buffer) + len(line) + 1 > self.chunk_size:
                    chunks.append(buffer.strip())
                    buffer = buffer[-self.overlap :] if self.overlap else ""
                buffer = (buffer + "\n" + line) if buffer else line
            if buffer.strip():
                chunks.append(buffer.strip())

        return [c for c in chunks if len(c) >= self.min_len]


def extract_section(chunk_text: str) -> str:
    """从切片文本里取出第一个标题行，作为引用区显示的"章节名"。"""
    for line in chunk_text.split("\n"):
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
    return "（无标题片段）"


# ====================================================================
# 【核心模块】向量化抽象
# ====================================================================
class EmbeddingProvider(Protocol):
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]:
        ...


class ZhipuEmbeddingProvider:
    """智谱 embedding API（默认，复用 LLM 的 Key 和网络出口）。"""

    def __init__(self, base_url: str, api_key: str, model: str = "embedding-2",
                 dim: int = 1024, timeout: int = 60, batch_size: int = 64) -> None:
        if not base_url or not api_key:
            raise ValueError("ZhipuEmbeddingProvider 需要 base_url 和 api_key")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout = timeout
        self.batch_size = batch_size

    def encode(self, texts: list[str]) -> list[list[float]]:
        import requests

        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            resp = requests.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model, "input": batch},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            # 必须按 index 排序：API 不保证返回顺序与输入一致，
            # 直接用会让"切片→向量"错位，且极难排查
            items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
            out.extend(item["embedding"] for item in items)

        if len(out) != len(texts):
            raise RAGError(ErrorCode.RETRIEVAL_FAILED,
                           detail=f"向量化条数不匹配：期望 {len(texts)}，实际 {len(out)}")
        return out


class FastEmbedProvider:
    """本地 ONNX embedding（EMBED_PROVIDER=fastembed 时使用）。"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name)
        # fastembed 0.9.x 的属性名是 embedding_size，不是 dim
        self.dim = self._model.embedding_size

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(texts)]


class HashEmbeddingProvider:
    """无语义的降级实现。只用于验证链路，绝不参与效果评估。"""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            grams = [text[i : i + 2] for i in range(max(len(text) - 1, 1))]
            for g in grams:
                idx = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16) % self.dim
                vec[idx] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def build_embedding_provider(cfg: Config) -> tuple[EmbeddingProvider, bool]:
    """工厂，返回 (provider, vector_enabled)。

    ⭐ 阶段 1 最重要的工程教训：宁缺勿滥。
    embedding 不可用时，第一版会降级成关键词哈希向量继续跑混合检索，
    结果那些毫无语义的向量把 BM25 的正确结果从 Top3 挤了出去——
    向量路不但没帮忙，反而在拖后腿。
    所以现在：一旦向量供应商不可用，就**整条关掉**，退化为纯 BM25，
    并明确告知调用方。
    """
    if cfg.embed_provider == "hash":
        return HashEmbeddingProvider(cfg.embed_dim), False

    if cfg.embed_provider == "zhipu":
        if not (cfg.llm_api_key and cfg.llm_base_url):
            raise RAGError(ErrorCode.RETRIEVAL_FAILED,
                           detail="智谱 embedding 需要 LLM_API_KEY / LLM_BASE_URL")
        try:
            provider = ZhipuEmbeddingProvider(
                base_url=cfg.llm_base_url,
                api_key=cfg.llm_api_key,
                model=cfg.embed_model,
                dim=cfg.embed_dim,
            )
            return provider, True
        except Exception as exc:
            raise RAGError(ErrorCode.RETRIEVAL_FAILED,
                           detail=f"智谱 embedding 初始化失败：{type(exc).__name__}")

    if cfg.embed_provider == "fastembed":
        try:
            return FastEmbedProvider(cfg.embed_model), True
        except Exception as exc:
            # 国内直连 HuggingFace 常被 SSL 拦，可设 HF_ENDPOINT 重试
            raise RAGError(ErrorCode.RETRIEVAL_FAILED,
                           detail=f"本地模型不可用：{type(exc).__name__}；"
                                  f"可设 HF_ENDPOINT=https://hf-mirror.com")

    raise RAGError(ErrorCode.RETRIEVAL_FAILED,
                   detail=f"未知 EMBED_PROVIDER={cfg.embed_provider}")


# ====================================================================
# 【核心模块】FAISS 向量库
# ----------------------------------------------------------------
# 阶段 3 换 pgvector 时只改这个类的内部实现，对外三个方法不变。
# ====================================================================
class FaissVectorStore:
    """FAISS 索引 + 切片元数据持久化。

    用 IndexFlatIP + L2 归一化 == 余弦相似度。原型数据量小，
    暴力精确检索完全够用且结果确定；上万切片再换 IVFFlat。

    ⭐ 阶段 3 改造：新增 `use_cache` 开关与 `build_from_vectors`。
      - 多租户场景下每个租户一个内存索引，不落盘缓存
        （落盘按租户分文件会让"删一份文档"要清理多个缓存文件，
          且缓存与数据库双写必然出现不一致）
      - `build_from_vectors` 直接用库里读回的向量建索引，
        **不重新调 embedding API**——实测 500 块重建仅 0.002s，
        而重新调 API 要 18s 且每次删文档都烧钱
    """

    def __init__(self, dim: int, index_dir: str = "", index_tag: str = "default",
                 use_cache: bool = True) -> None:
        import faiss

        self.dim = dim
        self.use_cache = use_cache
        self.index_path = None
        self.meta_path = None

        if use_cache:
            self.index_dir = Path(index_dir)
            self.index_dir.mkdir(parents=True, exist_ok=True)
            # ⚠️ 索引缓存必须按「供应商+模型+维度」分文件。
            # 否则换 embedding 模型后误用旧维度的索引——FAISS 要么报维度错，
            # 要么用旧向量硬跑，后者是静默错误，表现为"检索结果莫名不相关"。
            safe_tag = "".join(c if c.isalnum() or c in "-_." else "_" for c in index_tag)
            self.index_path = self.index_dir / f"index.{safe_tag}.faiss"
            self.meta_path = self.index_dir / f"chunks.{safe_tag}.json"

        self.chunks: list[str] = []
        self.chunk_meta: list[dict[str, Any]] = []
        self.index = faiss.IndexFlatIP(dim)

        if use_cache and self.index_path and self.index_path.exists() \
                and self.meta_path and self.meta_path.exists():
            self.index = faiss.read_index(str(self.index_path))
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            self.chunks = meta["chunks"]
            self.chunk_meta = meta["chunk_meta"]

    @property
    def ready(self) -> bool:
        return self.index.ntotal > 0

    def build(self, chunks: list[str], provider: EmbeddingProvider,
              source_file: str = "") -> None:
        """现场向量化 + 建索引（首次启动 / --rebuild 场景）。"""
        if not chunks:
            raise RAGError(ErrorCode.INDEX_NOT_READY, detail="切片为空")
        vectors = provider.encode(chunks)
        self.build_from_vectors(
            chunks,
            [{"source_file": source_file, "section": extract_section(c)}
             for c in chunks],
            vectors,
        )

    def build_from_vectors(self, chunks: list[str],
                           chunk_meta: list[dict[str, Any]],
                           vectors: list[list[float]]) -> None:
        """⭐ 用已有的向量直接建索引，不调 embedding API。

        这是"删除文档后重建索引"能做到秒级的关键：
        向量早就随切片一起存在 SQLite 的 BLOB 字段里了，
        重建只是把 BLOB 读回来重新喂给 FAISS。
        """
        import faiss
        import numpy as np

        if not chunks:
            self.index = faiss.IndexFlatIP(self.dim)
            self.chunks = []
            self.chunk_meta = []
            return
        if len(chunks) != len(vectors) or len(chunks) != len(chunk_meta):
            raise RAGError(ErrorCode.RETRIEVAL_FAILED,
                           detail=f"切片/元数据/向量数量不一致："
                                  f"{len(chunks)}/{len(chunk_meta)}/{len(vectors)}")
        matrix = np.asarray(vectors, dtype="float32")
        if matrix.shape[1] != self.dim:
            # 换过 embedding 模型但库里还是旧维度的向量——
            # 必须显式报错，绝不静默截断/补零（那样检索结果全错）
            raise RAGError(ErrorCode.RETRIEVAL_FAILED,
                           detail=f"向量维度不匹配：库里 {matrix.shape[1]}，"
                                  f"当前模型 {self.dim}，需重新向量化")
        # 必须归一化，否则 IP 检索不等于余弦
        faiss.normalize_L2(matrix)
        self.index = faiss.IndexFlatIP(self.dim)
        self.index.add(matrix)
        self.chunks = chunks
        self.chunk_meta = chunk_meta

    def search(self, query: str, provider: EmbeddingProvider,
               top_k: int) -> list[tuple[int, float]]:
        import faiss
        import numpy as np

        if self.index.ntotal == 0:
            return []
        qv = np.asarray(provider.encode([query]), dtype="float32")
        faiss.normalize_L2(qv)
        scores, ids = self.index.search(qv, min(top_k, self.index.ntotal))
        return [(int(i), float(s)) for s, i in zip(scores[0], ids[0]) if i != -1]

    def save(self) -> None:
        """落盘缓存。

        ⭐ 内存索引（use_cache=False）不落盘——多租户场景下
        每租户一个索引文件，删除文档后必须同步清掉对应缓存，
        与数据库双写必然出现不一致。多租户模式下索引是**派生视图**，
        随时可以从库里重建，不值得维护一致性。
        """
        if not self.use_cache:
            return
        import faiss

        faiss.write_index(self.index, str(self.index_path))
        self.meta_path.write_text(
            json.dumps(
                {"chunks": self.chunks, "chunk_meta": self.chunk_meta},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def vector_to_blob(vector: list[float]) -> bytes:
        """向量 → SQLite BLOB。

        用 float32 而不是 float64：1024 维下每块 4KB 而不是 8KB，
        500 块就是 2MB 而不是 4MB。精度损失对余弦检索无影响
        （归一化后 IP 分数本来就在 0~1，float32 有 7 位有效数字）。
        """
        import numpy as np

        return np.asarray(vector, dtype="float32").tobytes()

    @staticmethod
    def blob_to_vector(blob: bytes) -> list[float]:
        """BLOB → 向量。"""
        import numpy as np

        return np.frombuffer(blob, dtype="float32").tolist()


# ====================================================================
# 【核心模块】BM25 关键词检索
# ====================================================================
class BM25Store:
    """BM25 检索器，支持两种切分模式。

    为什么需要两路（word + char）：

    ⭐ 实测踩坑：问"报销款多久到账？"，正确条文在
    "报销**款项**在审批通过后 10 个工作日内支付至员工工资卡"。
    jieba 把查询切成 [报销, 款, 多久, 到账]，而文档里"款"从不单独成词
    （它总是"款项"），于是 BM25 **一个判别词都匹配不上**，
    只剩"报销"这个在 11 个块里遍地都是的低 IDF 词在撑分数——
    结果 BM25 按词频乱排，把正确块压到第 7 名。

    这是中文检索的经典问题：**分词不一致**。查询和文档的分词结果
    稍有偏差，词级匹配就整体失效。

    字符二元组是标准解法：把文本切成 2 字一组的滑窗，"销款"这种
    罕见二元组会成为强判别信号，且完全不依赖分词器是否一致。

    所以最终是**三路 RRF**：词级 BM25 + 字符二元组 BM25 + 向量。
    """

    def __init__(self, chunks: list[str], mode: str = "word") -> None:
        import jieba
        from rank_bm25 import BM25Okapi

        if mode not in ("word", "char"):
            raise ValueError(f"不支持的 BM25 模式：{mode}")
        self.mode = mode
        self._jieba = jieba
        self.tokenized = [self._tokenize(c) for c in chunks]
        self.bm25 = BM25Okapi(self.tokenized)

    def _tokenize(self, text: str) -> list[str]:
        if self.mode == "word":
            return self._jieba.lcut(text)
        return _char_bigrams(text)

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        tokens = self._tokenize(query)
        if not tokens:
            return []
        scores = self.bm25.get_scores(tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return [(i, float(s)) for i, s in ranked[:top_k] if s > 0]


def _char_bigrams(text: str) -> list[str]:
    """字符二元组切分。

    先剥掉 markdown 标记和标点——它们不携带语义，
    留着会让大量文档共享 `**`、`##` 这类"词"，反而稀释判别力。
    """
    cleaned = re.sub(
        r"[\s，。、；：？！…—\-\*\#\(\)（）《》\[\]【】\"'`]+", "", text
    )
    if len(cleaned) < 2:
        return [cleaned] if cleaned else []
    return [cleaned[i : i + 2] for i in range(len(cleaned) - 1)]


# ====================================================================
# 【核心模块】混合检索（RRF 融合）
# ====================================================================
class HybridRetriever:
    """三路召回 + RRF 融合：词级 BM25 + 字符二元组 BM25 + 向量。

    为什么用 RRF 而不是加权求和：三路分数（BM25 0~几十、向量 0~1）
    量纲完全不同，直接加权等于让某一一路说了算。RRF 只用排名不用分数，
    天然免疫量纲差异。公式：score(d) = Σ 1/(k + rank(d))，k 常取 60。
    """

    def __init__(self, bm25_word: BM25Store, bm25_char: BM25Store,
                 store: FaissVectorStore, vector_enabled: bool = True) -> None:
        self.bm25_word = bm25_word
        self.bm25_char = bm25_char
        self.store = store
        self.vector_enabled = vector_enabled

    def retrieve(self, query: str, provider: EmbeddingProvider, cfg: Config
                 ) -> list[dict[str, Any]]:
        word_hits = self.bm25_word.search(query, cfg.bm25_candidates)
        char_hits = self.bm25_char.search(query, cfg.bm25_candidates)
        if self.vector_enabled:
            try:
                vec_hits = self.store.search(query, provider, cfg.vector_candidates)
            except Exception as exc:
                raise RAGError(ErrorCode.RETRIEVAL_FAILED,
                               detail=f"向量检索失败：{type(exc).__name__}: {exc}")
        else:
            vec_hits = []

        fused: dict[int, float] = {}
        for hits in (word_hits, char_hits, vec_hits):
            for rank, (idx, _s) in enumerate(hits, start=1):
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (cfg.rrf_k + rank)

        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)[: cfg.top_k]

        results: list[dict[str, Any]] = []
        for idx, score in ranked:
            meta = self.store.chunk_meta[idx] if idx < len(self.store.chunk_meta) else {}
            results.append({
                "index": len(results) + 1,          # 引用编号，与提示词里的 [n] 对应
                "chunk_index": idx,
                "source_file": meta.get("source_file", ""),
                "section": meta.get("section", ""),
                "snippet": self.store.chunks[idx],
                "score": round(score, 6),
                "hit_by": {
                    "bm25_word": any(i == idx for i, _ in word_hits),
                    "bm25_char": any(i == idx for i, _ in char_hits),
                    "vector": any(i == idx for i, _ in vec_hits),
                },
            })
        return results


# ====================================================================
# 【核心模块】LLM 抽象（同步 + 流式）
# ====================================================================
@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class BaseLLM(Protocol):
    name: str

    def chat(self, messages: list[dict[str, str]], **kw: Any) -> LLMResponse:
        ...

    def chat_stream(self, messages: list[dict[str, str]], **kw: Any
                    ) -> Iterator[tuple[str, Any]]:
        """流式接口，yield ("delta", 文本片段) ... 最后 yield ("done", usage)。"""
        ...


class OpenAICompatibleLLM:
    """OpenAI 兼容协议实现。

    DeepSeek / 通义 / 智谱 / Kimi / 本地 vLLM 几乎都提供
    /chat/completions 兼容端点，改 base_url + model 即可切换，
    无需为每家写适配器。
    """

    name = "openai-compatible"

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 60) -> None:
        if not base_url or not api_key:
            raise RAGError(ErrorCode.LLM_UNAVAILABLE,
                           detail="OpenAICompatibleLLM 需要 base_url 和 api_key")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    # ---- 同步 ----
    def chat(self, messages: list[dict[str, str]], **kw: Any) -> LLMResponse:
        import requests

        t0 = time.time()
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": kw.get("temperature", 0.1),
                    "max_tokens": kw.get("max_tokens", 800),
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.Timeout:
            raise RAGError(ErrorCode.LLM_TIMEOUT, detail=f"{self.timeout}s 未响应")
        except requests.HTTPError as exc:
            raise RAGError(ErrorCode.LLM_UNAVAILABLE,
                           detail=f"HTTP {exc.response.status_code}")
        except requests.RequestException as exc:
            raise RAGError(ErrorCode.LLM_UNAVAILABLE,
                           detail=f"{type(exc).__name__}: {exc}")

        data = resp.json()
        usage = data.get("usage", {})
        return LLMResponse(
            text=data["choices"][0]["message"].get("content", ""),
            model=data.get("model", self.model),
            provider=self.name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=int((time.time() - t0) * 1000),
        )

    # ---- 流式 ----
    def chat_stream(self, messages: list[dict[str, str]], **kw: Any
                    ) -> Iterator[tuple[str, Any]]:
        """流式调用，逐段 yield 文本。

        注意两点：
        1. 必须显式传 stream_options.include_usage，否则流式响应拿不到
           token 用量，计费和对账会缺数据（部分供应商不支持，做了兜底）。
        2. 这是**同步生成器**，会被 main.py 丢进线程池执行，
           避免阻塞 FastAPI 的事件循环。
        """
        import requests

        t0 = time.time()
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": kw.get("temperature", 0.1),
                    "max_tokens": kw.get("max_tokens", 800),
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
                stream=True,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.Timeout:
            raise RAGError(ErrorCode.LLM_TIMEOUT, detail=f"{self.timeout}s 未建立连接")
        except requests.HTTPError as exc:
            raise RAGError(ErrorCode.LLM_UNAVAILABLE,
                           detail=f"HTTP {exc.response.status_code}")
        except requests.RequestException as exc:
            raise RAGError(ErrorCode.LLM_UNAVAILABLE,
                           detail=f"{type(exc).__name__}: {exc}")

        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                payload = raw[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue  # 心跳行或格式异常行，跳过不影响主流程

                # usage 通常在最后一个 chunk 里单独下发
                if chunk.get("usage"):
                    usage = {
                        "prompt_tokens": chunk["usage"].get("prompt_tokens", 0),
                        "completion_tokens": chunk["usage"].get("completion_tokens", 0),
                    }

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    yield ("delta", text)
        except requests.RequestException as exc:
            raise RAGError(ErrorCode.LLM_UNAVAILABLE,
                           detail=f"流式连接中断：{type(exc).__name__}: {exc}")
        finally:
            resp.close()

        usage["latency_ms"] = int((time.time() - t0) * 1000)
        yield ("done", usage)


class MockLLM:
    """离线 Mock，无 Key 时验证链路。"""

    name = "mock"

    def __init__(self, model: str = "mock-echo") -> None:
        self.model = model

    def chat(self, messages: list[dict[str, str]], **kw: Any) -> LLMResponse:
        prompt = messages[-1]["content"] if messages else ""
        return LLMResponse(text=prompt[:600], model=self.model, provider=self.name)

    def chat_stream(self, messages: list[dict[str, str]], **kw: Any
                    ) -> Iterator[tuple[str, Any]]:
        prompt = messages[-1]["content"] if messages else ""
        text = f"【MockLLM】提示词长度 {len(prompt)} 字符，前 300 字：{prompt[:300]}"
        # 切成小片，模拟真实流式
        for i in range(0, len(text), 20):
            yield ("delta", text[i : i + 20])
        yield ("done", {"prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0})


def build_llm(cfg: Config) -> BaseLLM:
    if cfg.llm_base_url and cfg.llm_api_key and cfg.llm_model:
        return OpenAICompatibleLLM(cfg.llm_base_url, cfg.llm_api_key,
                                   cfg.llm_model, cfg.llm_timeout)
    return MockLLM()


# ====================================================================
# 【核心模块】提示词组装 + 引用自检
# ----------------------------------------------------------------
# ⭐ 提示词分两段（阶段 3 起管理员可在后台编辑）：
#
#   MANDATORY_RULES —— 强制段，**代码写死，管理员不可编辑**。
#       它承载的是"可追溯性"的技术前提：引用编号格式、拒答规则、
#       数字原文引用。放开编辑等于允许管理员把系统改成无引用的
#       自由发挥模型，审计链就断了。
#
#   DEFAULT_PROMPT_BODY —— 正文段，管理员可编辑，存 prompt_templates 表。
#       承载业务口径：语气、垂询对象、附加约束、常见问题导流。
#       每次保存产生新版本，旧版本保留可回滚（审计要求）。
# ====================================================================

MANDATORY_RULES = """你是企业报销制度的答疑助手。你的唯一职责是依据下面给定的「制度原文」回答员工问题。

硬性规则（违反任何一条都算失败）：
1. 只能依据「制度原文」作答，禁止使用你自己的常识、记忆或推测。
2. 回答中每一个结论，都必须标注引用编号，格式为 [1]、[2]、[3]。
3. 引用编号必须与你实际依据的那段原文的编号严格一致：
   内容来自编号 [1] 的原文，就必须写 [1]，禁止写错号或张冠李戴。
4. 如果「制度原文」中没有覆盖该问题，必须明确回答"制度中未作规定，请联系财务确认"，
   绝不编造金额、比例、流程或时限。
5. 涉及具体数字（金额上限、比例、天数）时，必须原文引用，不得四舍五入或改写。
6. 语气简洁、直接，面向企业员工，不写客套话。"""

# 管理员可编辑的正文段默认值（首次进入后台时的初始内容）
DEFAULT_PROMPT_BODY = """补充说明（由管理员维护）：
1. 回答时请先给结论，再给依据，最后给操作建议，便于员工快速取用。
2. 若员工描述的情形涉及多个费用项或特殊身份（实习生、外派、新入职），
   请分别说明适用口径，并提示以用工单位制度为准。
3. 若制度原文只规定了上限而未说明例外，请明确提示"需主管审批"，
   不要替审批人做决定。
4. 涉及金额、比例、天数等数字时，除引用编号外再补一句自然语言摘要，
   避免员工漏看 [n] 标注。"""


def build_system_prompt(custom_body: str = "") -> str:
    """拼装最终 system 提示词 = 强制段 + 管理员正文段。

    ⭐ 强制段永远在最前面且不可被覆盖——即使管理员把正文写成
    "忽略上面的规则"，强制段依然完整保留。这是刻意的：
    后台配置失误不应该能悄悄关掉引用编号校验。
    """
    body = (custom_body or "").strip()
    if not body:
        body = DEFAULT_PROMPT_BODY.strip()
    return f"{MANDATORY_RULES}\n\n{body}"


# 拒答话术白名单：命中这些就不要求引用编号（见 validate_citations 注释）
REFUSAL_PATTERNS = [
    "制度中未作规定",
    "未作规定",
    "请联系财务确认",
    "制度未涵盖",
    "无法从制度中",
]


def build_messages(question: str, contexts: list[dict[str, Any]],
                   history: list[dict[str, str]] | None = None,
                   custom_body: str = ""
                   ) -> list[dict[str, str]]:
    """拼装消息列表：system + 历史 + 当前 user。

    history 是已经按轮次截断好的 [{"role":"user"/"assistant","content":...}]。
    引用编号只在当前这轮生效——历史轮次的 [n] 指的是当时那批检索结果，
    与新一批不一致，所以 system 提示词限定"本轮"。

    custom_body 是管理员在后台保存的正文段（prompt_templates.body_text）；
    为空时用内置默认正文。强制段无论如何都会拼在前面。
    """
    if not contexts:
        context_block = "（未检索到任何相关制度原文）"
    else:
        parts = [
            f"[{c['index']}] 来源：{c['source_file']} · {c['section']}\n{c['snippet']}"
            for c in contexts
        ]
        context_block = "\n\n".join(parts)

    user_prompt = f"""【制度原文】（本轮）
{context_block}

【员工问题】
{question}

请依据上述制度原文回答问题，并标注引用编号。"""

    messages: list[dict[str, str]] = [
        {"role": "system", "content": build_system_prompt(custom_body)}
    ]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_prompt})
    return messages


def validate_citations(answer: str, num_contexts: int) -> dict[str, Any]:
    """引用完整性自检（原型级，零成本）。

    实测：即使检索正确、答案内容正确，GLM-4-flash 仍会把引用编号标错
    （内容出自 [1] 却写成 [3]）。对"可追溯"这个核心诉求来说，
    指错出处和答错几乎是同等失败。

    这里只做**机械可验**的部分：
      - 答案里到底有没有引用编号
      - 引用编号是否越界（比如只有 3 段原文却写 [5]）
    抓不住"编号没越界但指错块"——那需要语义判定，
    阶段 2 用轻量 NLI 或二次 LLM 调用来做，架构里必须留出这个位置。

    ⚠️ 一个必须区分的场景：**拒答**。
    "制度中未作规定，请联系财务确认" 是提示词要求的正确行为，
    它天然没有引用编号。如果不加区分，每次正常拒答都会被判成
    "答案不可溯源"，前端弹警示——那是误报，用户很快就会学会忽略警示。
    所以拒答单独走一支，ok=True，并标记 is_refusal 让前端换个友好提示。
    """
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)})

    if any(p in answer for p in REFUSAL_PATTERNS):
        return {
            "cited": [], "out_of_range": [], "has_citation": False,
            "is_refusal": True, "ok": True,
        }

    out_of_range = [n for n in cited if n < 1 or n > num_contexts]
    return {
        "cited": cited,
        "out_of_range": out_of_range,
        "has_citation": bool(cited),
        "is_refusal": False,
        "ok": bool(cited) and not out_of_range,
    }


def trim_history(rows: list[dict[str, Any]], rounds: int,
                 char_budget: int) -> list[dict[str, str]]:
    """把数据库消息行转成 LLM 可用的历史，并按"轮"和"字数"双重截断。

    双重上限的原因：
      - 按轮截断（默认 6 轮 = 12 条）保证语义完整
      - 按字数截断（默认 3000 字）防止用户粘贴超长内容把上下文和费用撑爆
    超出预算时从**最早**的开始丢——越近的对话对当前问题越有参考价值。
    """
    usable = [r for r in rows if r.get("role") in ("user", "assistant")
              and r.get("content")]
    # 取最近 rounds*2 条
    usable = usable[-(rounds * 2):]

    history = [{"role": r["role"], "content": r["content"]} for r in usable]

    # 字数预算：从最早开始裁，直到不超预算
    while history and sum(len(h["content"]) for h in history) > char_budget:
        history.pop(0)

    return history


# ====================================================================
# 引擎封装：把上面所有模块串起来，供接口层调用
# ====================================================================
class RAGEngine:
    """接口层唯一需要打交道的对象。

    生命周期：FastAPI 启动时构造一次，索引常驻内存。
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.provider, self.vector_enabled = build_embedding_provider(cfg)

        index_tag = f"{cfg.embed_provider}-{cfg.embed_model}-{self.provider.dim}"
        self.store = FaissVectorStore(self.provider.dim, cfg.index_dir, index_tag)

        if not self.store.ready:
            # 索引不存在 → 现场构建（仅在首次启动或 --rebuild 时发生）
            doc = Path(cfg.doc_path)
            if not doc.exists():
                raise RAGError(ErrorCode.INDEX_NOT_READY,
                               detail=f"制度文件不存在：{cfg.doc_path}")
            chunker = TextChunker(cfg.chunk_size, cfg.chunk_overlap, cfg.min_chunk_len)
            chunks = chunker.split(chunker.load_markdown(doc))
            self.store.build(chunks, self.provider, source_file=doc.name)
            self.store.save()

        # 两路 BM25：词级 + 字符二元组。字符那路专门治中文分词不一致
        self.bm25_word = BM25Store(self.store.chunks, mode="word")
        self.bm25_char = BM25Store(self.store.chunks, mode="char")
        self.retriever = HybridRetriever(self.bm25_word, self.bm25_char,
                                         self.store, self.vector_enabled)
        self.llm = build_llm(cfg)

    # ---- 检索 ----
    def retrieve(self, question: str) -> list[dict[str, Any]]:
        return self.retriever.retrieve(question, self.provider, self.cfg)

    # ---- 流式问答：yield 统一的 (event, data) 元组 ----
    def stream_chat(self, question: str, history: list[dict[str, str]]):
        """生成器，产出接口层直接可用的 (event, data)。

        产出顺序固定：
          citations → delta* → done
        接口层只需在前面补 meta/status，末尾补 error 处理。
        """
        hits = self.retrieve(question)
        yield ("citations", {"items": hits})

        messages = build_messages(question, hits, history)
        parts: list[str] = []
        usage: dict[str, Any] = {}

        for kind, payload in self.llm.chat_stream(
            messages,
            temperature=self.cfg.llm_temperature,
            max_tokens=self.cfg.llm_max_tokens,
        ):
            if kind == "delta":
                parts.append(payload)
                yield ("delta", {"text": payload})
            elif kind == "done":
                usage = payload

        answer = "".join(parts)
        yield ("done", {
            "answer": answer,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
            "latency_ms": usage.get("latency_ms", 0),
            "citation_check": validate_citations(answer, len(hits)),
            "citations": hits,
        })
