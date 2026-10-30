#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 1 · 裸问答原型（无 Web，单脚本）
====================================================================
目标：验证「RAG 混合检索 + LLM 制度问答」这条链路能不能跑通。

链路：
    报销制度.md
        → ① 加载并按 ~500 字切片（带重叠）
        → ② 向量化写入 FAISS
        → ③ 命令行提问
        → ④ BM25 + 向量 混合检索 Top3（RRF 融合）
        → ⑤ 拼装提示词（带引用编号）
        → ⑥ 调用 LLM（供应商可配置）生成回答

运行：
    # 1) 准备一份制度 markdown
    # 2) 配置供应商（默认走智谱，LLM 与 embedding 复用同一套 Key）
    export LLM_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
    export LLM_API_KEY="你的智谱Key"
    export LLM_MODEL="glm-4-flash"
    export EMBED_PROVIDER="zhipu"        # zhipu | fastembed | hash
    export EMBED_MODEL="embedding-2"
    # 3) 运行
    python rag_proto.py --doc ./报销制度.md

依赖：jieba / rank_bm25 / faiss-cpu / fastembed(仅 EMBED_PROVIDER=fastembed 时需要)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# ====================================================================
# 【核心模块 · 后续阶段直接复用】之一：全局配置
# ----------------------------------------------------------------
# 复用说明：进入阶段 2（FastAPI 服务化）时，这个 Config 会原样搬进
# core/config.py，由 pydantic-settings 接管并从环境变量注入。
# 现在先保持"零框架、纯 dataclass"，避免原型过早引入依赖。
# ====================================================================
@dataclass
class Config:
    """原型全部可调参数集中在这里，方便阶段 2 平移到 pydantic-settings。"""

    # ---- 切片参数 ----
    chunk_size: int = 500          # 目标切片字数（中文字符近似）
    chunk_overlap: int = 50        # 相邻切片重叠字数，避免语义被切断

    # ---- 检索参数 ----
    top_k: int = 3                 # 混合检索最终返回条数
    rrf_k: int = 60                # RRF 融合常数，业界常用 60
    bm25_candidates: int = 20      # BM25 侧候选数
    vector_candidates: int = 20    # 向量侧候选数

    # ---- 向量模型 ----
    # 供应商三选一：
    #   zhipu     调智谱 embedding API（默认，复用 LLM 的 Key，免本地模型下载）
    #   fastembed 本地 ONNX 模型（需联网下载；国内可设 HF_ENDPOINT=https://hf-mirror.com）
    #   hash      无语义的关键词哈希，仅用于"验证链路能通"，不参与真实效果评估
    embed_provider: str = "zhipu"
    embed_model: str = "embedding-2"       # 智谱 embedding-2，维度 1024
    embed_dim: int = 1024                  # ⚠️ 换模型必须同步改；fastembed 会自动探测故忽略此值

    # ---- LLM 供应商（可配置）----
    llm_base_url: str = ""         # 例：https://api.deepseek.com/v1
    llm_api_key: str = ""          # 例：sk-xxxx
    llm_model: str = ""            # 例：deepseek-chat / qwen-plus / glm-4
    llm_temperature: float = 0.1   # 制度问答要"照本宣科"，低温抑制发挥
    llm_max_tokens: int = 800
    llm_timeout: int = 60

    # ---- 索引缓存 ----
    index_dir: str = "./.rag_cache"  # FAISS 索引 + 切片文本的落盘目录

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量读取，方便切换供应商而不改代码。"""
        return cls(
            llm_base_url=os.getenv("LLM_BASE_URL", ""),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL", ""),
            embed_provider=os.getenv("EMBED_PROVIDER", "zhipu"),
            embed_model=os.getenv("EMBED_MODEL", "embedding-2"),
            embed_dim=int(os.getenv("EMBED_DIM", "1024")),
            chunk_size=int(os.getenv("CHUNK_SIZE", "500")),
            top_k=int(os.getenv("TOP_K", "3")),
        )


# ====================================================================
# 【核心模块 · 后续阶段直接复用】之二：文档加载 + 切片器
# ----------------------------------------------------------------
# 复用说明：阶段 2 的知识库域（services/knowledge/）会直接复用这里。
# 需要扩展的是：支持 PDF/Word 输入、按 markdown 标题层级切片。
# 现在只做"按字数滑窗 + 尽量在句号处收尾"。
# ====================================================================
class TextChunker:
    """把一篇 markdown 切成带重叠的小块。

    设计取舍：
    - 为什么按"字"而不是按 token？中文场景下按字更直观，且原型无需
      精确对齐 tokenizer；阶段 2 换 bge 的分词器后可改为按 token。
    - 为什么保留重叠？防止一句话被拦腰切断，导致检索到半句话。
    """

    def __init__(self, chunk_size: int = 500, overlap: int = 50) -> None:
        if overlap >= chunk_size:
            raise ValueError("overlap 必须小于 chunk_size")
        self.chunk_size = chunk_size
        self.overlap = overlap

    def load_markdown(self, path: str | Path) -> str:
        """读取 markdown 原文。

        注意：这里只做最基础的清洗（去首尾空白、压缩连续空行）。
        markdown 标记符号（#、-、|）先保留——它们对 LLM 理解表格/标题
        结构有帮助，且不影响切片。
        """
        text = Path(path).read_text(encoding="utf-8")
        lines = [ln.rstrip() for ln in text.splitlines()]
        text = "\n".join(lines)
        # 压缩 3 个以上连续换行为 2 个，避免空行撑大块体积
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text.strip()

    def split(self, text: str) -> list[str]:
        """按 markdown 标题切「节」，节内再按 chunk_size 细分。

        ⭐ 这是原型迭代中改动最关键的一处。第一版是纯按字数滑窗，
        实测暴露严重问题：一份 8 节的制度被切成 5 块，每块都横跨
        好几个章节。后果是——
           问"超过多少钱需要副总裁审批"
           命中一块同时含「发票要求 + 审批流程 + 业务招待」的杂块
           LLM 拿着错块答题，把"招待费 3000 元"和"报销审批 50000 元"
           两条规定混成一句，答案直接错了。
        而且"伙食补助"这种短块因为含"出差""报销"等高频词，
        在四个测试问题里全部挤进 Top3，纯属噪声。

        制度 / 合同 / SOP 类文档的标题就是天然语义边界，
        切过边界等于把两件不相关的事绑进同一个检索单元。

        切块规则：
          1. 按标题行把文档切成若干「节」（标题 + 其下正文）
          2. 节 <= chunk_size → 单独成块
          3. 节 >  chunk_size → 节内按行贪心累积（带重叠）
          4. 绝不跨节合并
        """
        # ---- 1) 按标题切节 ----
        sections: list[list[str]] = []
        current: list[str] = []
        for line in text.split("\n"):
            is_heading = line.lstrip().startswith("#")
            if is_heading and current:
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

            # 节本身不长 → 整节成块（理想情况，语义最完整）
            if len(block) <= self.chunk_size:
                chunks.append(block)
                continue

            # 节超长 → 节内按行贪心累积，注意 buffer 不跨节
            buffer = ""
            for line in sec:
                # 单行就超长（少见，如长表格行）→ 强制按字硬切
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

        # 过滤掉切太小的碎块（通常来自文档末尾）
        return [c for c in chunks if len(c) >= 30]


# ====================================================================
# 【核心模块 · 后续阶段直接复用】之三：Embedding 抽象
# ----------------------------------------------------------------
# 复用说明：阶段 2 这一层会长成 services/knowledge/embedding.py，
# 增加"按租户缓存模型实例""批量向量化"等能力。
# 关键点：Protocol 定义接口，实现可插拔，换模型不动上层代码。
# ====================================================================
class EmbeddingProvider(Protocol):
    """向量化提供者接口。任何实现只要满足这个签名就能被替换。"""

    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]:
        """把一批文本编码为向量。返回 list[list[float]]，长度与输入一致。"""
        ...


class FastEmbedProvider:
    """本地 embedding 实现（默认）。

    用 fastembed（ONNX 推理）而不是 sentence-transformers（PyTorch），
    原因：原型不想拖一个 2GB 的 torch 依赖，ONNX 体积小、启动快。
    模型首次使用时会自动从 HuggingFace 下载（约 100MB）。
    """

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        from fastembed import TextEmbedding  # 延迟导入：没装也能看代码结构

        print(f"[embedding] 加载模型 {model_name}（首次运行需下载）...", flush=True)
        t0 = time.time()
        self._model = TextEmbedding(model_name=model_name)
        # ⚠️ 注意：fastembed 0.9.x 的属性名是 embedding_size，不是 dim。
        # 早期版本没有公开维度属性，手写死维度一旦换模型就会静默错乱，
        # 所以这里坚持从模型自身读。
        self.dim = self._model.embedding_size
        print(f"[embedding] 模型就绪，维度={self.dim}，耗时 {time.time() - t0:.1f}s", flush=True)

    def encode(self, texts: list[str]) -> list[list[float]]:
        # fastembed 返回的是生成器，这里物化成 list
        vectors = list(self._model.embed(texts))
        return [v.tolist() for v in vectors]


class HashEmbeddingProvider:
    """零依赖的降级 embedding（仅用于无网络/无模型时验证链路）。

    ⚠️ 质量很差：它只统计字符 n-gram 的哈希频次，无语义理解能力。
    用途只有一个——在没有网络、下不动模型的环境里，先把
    "切片→向量→FAISS→混合检索→LLM" 这条管线跑通。
    真实效果一律以 FastEmbedProvider 为准。
    """

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        import hashlib
        import math

        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            # 取字符 2-gram，把每个 gram 哈希到一个维度上累加
            grams = [text[i : i + 2] for i in range(max(len(text) - 1, 1))]
            for g in grams:
                idx = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16) % self.dim
                vec[idx] += 1.0
            # L2 归一化，让 FAISS 用内积近似余弦相似度
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class ZhipuEmbeddingProvider:
    """智谱 GLM embedding API 实现（OpenAI 兼容 /embeddings 端点）。

    为什么默认用它而不是本地模型：
    原型本来就要调智谱做生成，向量化复用同一个 Key 和网络出口，
    省掉一次模型下载——国内直连 HuggingFace 经常被 SSL 拦掉（实测会）。
    阶段 2 若客户要求"向量不出网"，再切 FastEmbedProvider 或本地部署，
    上层代码零改动（同一套 Protocol）。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "embedding-2",
        dim: int = 1024,
        timeout: int = 60,
        batch_size: int = 64,
    ) -> None:
        if not base_url or not api_key:
            raise ValueError("ZhipuEmbeddingProvider 需要 base_url 和 api_key")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout = timeout
        self.batch_size = batch_size

    def encode(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。

        两个关键处理：
        1. 分批发送——embedding 接口对单次 input 条数有上限，切片入库时
           可能一次性几百条，不分批会被直接拒掉。
        2. 按 index 排序——API 不保证返回顺序与输入一致，直接用会让
           "切片→向量"错位，且这种错位在检索时表现为"结果莫名不相关"，
           极难排查。
        """
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
            items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
            out.extend(item["embedding"] for item in items)

        if len(out) != len(texts):
            raise RuntimeError(f"向量化条数不匹配：期望 {len(texts)}，实际 {len(out)}")
        return out


def build_embedding_provider(cfg: Config) -> tuple[EmbeddingProvider, bool]:
    """工厂：按 EMBED_PROVIDER 选择向量供应商。

    返回 (provider, vector_enabled)：
      provider      实际使用的向量化实现
      vector_enabled False = "这条路不可信，请关闭向量检索"

    ⭐ 这是原型里最重要的一个工程判断：宁缺勿滥。
    之前第一版在 embedding 失败时会降级成关键词哈希向量继续跑，
    结果那些毫无语义的向量把 BM25 的正确结果从 Top3 里挤了出去——
    向量路不但没帮忙，反而在拖后腿。所以现在的规则是：
    一旦向量供应商不可用，就**整条关掉**，退化为纯 BM25，
    并在命令行明确告知用户"本次未使用向量检索"。

    阶段 2 这里会改成"按租户选择供应商"（有的客户要求向量走内网 API），
    工厂函数的结构不变。
    """
    if cfg.embed_provider == "hash":
        print("[embedding] 按配置使用 HashEmbeddingProvider（无语义，仅保链路）", flush=True)
        return HashEmbeddingProvider(cfg.embed_dim), False

    if cfg.embed_provider == "zhipu":
        if not (cfg.llm_api_key and cfg.llm_base_url):
            print("[embedding] ⚠️ 智谱 embedding 需要 LLM_API_KEY / LLM_BASE_URL", flush=True)
            return HashEmbeddingProvider(cfg.embed_dim), False
        try:
            provider = ZhipuEmbeddingProvider(
                base_url=cfg.llm_base_url,
                api_key=cfg.llm_api_key,
                model=cfg.embed_model,
                dim=cfg.embed_dim,
            )
            print(f"[embedding] 使用智谱 embedding：{cfg.embed_model}（维度 {provider.dim}）", flush=True)
            return provider, True
        except Exception as exc:
            print(f"[embedding] ⚠️ 智谱 embedding 不可用（{type(exc).__name__}: {exc}）", flush=True)
            return HashEmbeddingProvider(cfg.embed_dim), False

    if cfg.embed_provider == "fastembed":
        try:
            return FastEmbedProvider(cfg.embed_model), True
        except Exception as exc:
            print(f"[embedding] ⚠️ 本地模型不可用（{type(exc).__name__}: {exc}）", flush=True)
            print("[embedding] 提示：国内网络设 HF_ENDPOINT=https://hf-mirror.com 可重试下载", flush=True)
            return HashEmbeddingProvider(cfg.embed_dim), False

    print(f"[embedding] ⚠️ 未知 EMBED_PROVIDER={cfg.embed_provider}，关闭向量路", flush=True)
    return HashEmbeddingProvider(cfg.embed_dim), False


# ====================================================================
# 【核心模块 · 后续阶段直接复用】之四：向量库（FAISS）
# ----------------------------------------------------------------
# 复用说明：阶段 2 会替换为 pgvector（同一套接口），这里保留
# add/search/持久化三个方法就是为了让那次替换只改实现不改调用方。
# ====================================================================
class FaissVectorStore:
    """FAISS 向量索引封装。

    为什么用 IndexFlatIP：原型数据量小（几百个切片），暴力精确检索
    完全够用且结果确定；等切片量上到万级再换 IndexIVFFlat。
    用内积（IP）+ 向量已 L2 归一化 == 余弦相似度。
    """

    def __init__(self, dim: int, index_dir: str, index_tag: str = "default") -> None:
        import faiss

        self.dim = dim
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        # ⚠️ 索引缓存按「模型身份」分文件，不能只用一个 index.faiss。
        # 否则先用 A 模型（512 维）建了索引，换 B 模型（1024 维）后会命中
        # 旧缓存，FAISS 要么报维度错、要么用旧向量硬跑——后者是静默错误，
        # 表现为"检索结果莫名不相关"，最难排查的那类 bug。
        safe_tag = "".join(c if c.isalnum() or c in "-_." else "_" for c in index_tag)
        self.index_path = self.index_dir / f"index.{safe_tag}.faiss"
        self.meta_path = self.index_dir / f"chunks.{safe_tag}.json"
        self.chunks: list[str] = []

        if self.index_path.exists() and self.meta_path.exists():
            # 命中缓存：直接加载，避免每次提问都重新向量化
            print(f"[faiss] 命中缓存，加载索引 {self.index_path}", flush=True)
            self.index = faiss.read_index(str(self.index_path))
            self.chunks = json.loads(self.meta_path.read_text(encoding="utf-8"))
            print(f"[faiss] 已加载 {self.index.ntotal} 个切片", flush=True)
        else:
            self.index = faiss.IndexFlatIP(dim)

    def build(self, chunks: list[str], provider: EmbeddingProvider) -> None:
        """全量构建索引（原型一次性用法）。"""
        import faiss
        import numpy as np

        if not chunks:
            raise ValueError("切片为空，无法构建索引")
        print(f"[faiss] 向量化 {len(chunks)} 个切片...", flush=True)
        t0 = time.time()
        vectors = provider.encode(chunks)
        matrix = np.asarray(vectors, dtype="float32")
        # 防御：确保归一化，否则 IP 检索不等于余弦
        faiss.normalize_L2(matrix)
        self.index = faiss.IndexFlatIP(self.dim)
        self.index.add(matrix)
        self.chunks = chunks
        print(f"[faiss] 构建完成，耗时 {time.time() - t0:.1f}s", flush=True)

    def search(self, query: str, provider: EmbeddingProvider, top_k: int) -> list[tuple[int, float]]:
        """返回 [(chunk_index, score), ...]，按分数降序。"""
        import faiss
        import numpy as np

        if self.index.ntotal == 0:
            return []
        qv = np.asarray(provider.encode([query]), dtype="float32")
        faiss.normalize_L2(qv)
        scores, ids = self.index.search(qv, min(top_k, self.index.ntotal))
        return [(int(i), float(s)) for s, i in zip(scores[0], ids[0]) if i != -1]

    def save(self) -> None:
        """索引落盘，下次启动直接复用。"""
        import faiss

        faiss.write_index(self.index, str(self.index_path))
        self.meta_path.write_text(
            json.dumps(self.chunks, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[faiss] 索引已保存到 {self.index_dir}", flush=True)


# ====================================================================
# 【核心模块 · 后续阶段直接复用】之五：BM25 关键词检索
# ----------------------------------------------------------------
# 复用说明：阶段 2 混检管线会与向量检索并列放在 services/knowledge/retrieval.py。
# ====================================================================
class BM25Store:
    """基于 jieba 分词的 BM25 检索器。

    为什么需要 BM25：向量检索对"专有名词 / 金额数字 / 条款编号"不敏感，
    比如问"差旅费上限是多少"，BM25 能靠"差旅费""上限"这些词直接命中，
    而向量可能被语义相近但无关的条款带偏。两者互补。
    """

    def __init__(self, chunks: list[str]) -> None:
        import jieba
        from rank_bm25 import BM25Okapi

        self._jieba = jieba
        print(f"[bm25] 对 {len(chunks)} 个切片分词...", flush=True)
        # jieba 需要分词后的 token 列表；这里用 lcut 直接拿 list
        self.tokenized = [jieba.lcut(c) for c in chunks]
        self.bm25 = BM25Okapi(self.tokenized)
        print("[bm25] 索引就绪", flush=True)

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        """返回 [(chunk_index, score), ...]，按分数降序。"""
        tokens = self._jieba.lcut(query)
        scores = self.bm25.get_scores(tokens)
        # 取 top_k：用 sorted 而非 argpartition，原型可读性优先
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return [(i, float(s)) for i, s in ranked[:top_k] if s > 0]


# ====================================================================
# 【核心模块 · 后续阶段直接复用】之六：混合检索（RRF 融合）
# ----------------------------------------------------------------
# 复用说明：这是阶段 2 RAG 管线的核心算法，直接搬到
# services/knowledge/rag/ 下。需要扩展的是"重排（rerank）"一步。
# ====================================================================
class HybridRetriever:
    """BM25 + 向量 双路召回，用 RRF（倒数排名融合）合并。

    为什么用 RRF 而不是加权求和分数：
    BM25 分数（0~几十）和向量分数（0~1）量纲完全不同，直接加权
    等于让向量那一路说了算。RRF 只用"排名"不用"分数"，天然免疫量纲差异，
    公式：score(d) = Σ 1 / (k + rank(d))，k 通常取 60。
    """

    def __init__(
        self,
        bm25: BM25Store,
        vector_store: FaissVectorStore,
        vector_enabled: bool = True,
    ) -> None:
        self.bm25 = bm25
        self.vector_store = vector_store
        # 向量路开关：False 时完全不走向量检索，退化为纯 BM25。
        # 由 build_embedding_provider 的结果决定——见那里的"宁缺勿滥"说明。
        self.vector_enabled = vector_enabled

    def retrieve(
        self,
        query: str,
        provider: EmbeddingProvider,
        top_k: int = 3,
        rrf_k: int = 60,
        bm25_candidates: int = 20,
        vector_candidates: int = 20,
    ) -> list[dict[str, Any]]:
        """混合检索主入口。返回带来源信息的字典列表。"""
        # 双路召回；向量路不可用时自动降级为纯 BM25
        bm25_hits = self.bm25.search(query, bm25_candidates)
        if self.vector_enabled:
            vec_hits = self.vector_store.search(query, provider, vector_candidates)
        else:
            vec_hits = []
            print("[retrieve] ⚠️ 向量路已禁用，本次仅使用 BM25 关键词检索", flush=True)

        # RRF 融合：按 chunk_index 累加排名分
        fused: dict[int, float] = {}
        for rank, (idx, _score) in enumerate(bm25_hits, start=1):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (rrf_k + rank)
        for rank, (idx, _score) in enumerate(vec_hits, start=1):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (rrf_k + rank)

        # 取融合后 top_k
        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:top_k]

        results: list[dict[str, Any]] = []
        for idx, fused_score in ranked:
            results.append(
                {
                    "chunk_index": idx,
                    "score": round(fused_score, 6),
                    "text": self.vector_store.chunks[idx],
                    # 标注该块是被哪一路召回的，方便调试"为什么检索到它"
                    "hit_by": {
                        "bm25": any(i == idx for i, _ in bm25_hits),
                        "vector": any(i == idx for i, _ in vec_hits),
                    },
                }
            )
        return results


# ====================================================================
# 【核心模块 · 后续阶段直接复用】之七：LLM 供应商抽象
# ----------------------------------------------------------------
# 复用说明：阶段 2 会长成 agent/llm/ 包（provider.py + adapters.py），
# 增加流式 SSE、token 计量、多供应商故障转移。接口不变。
# 这是整个原型里"最应该被复用"的一块。
# ====================================================================
@dataclass
class LLMResponse:
    """统一的 LLM 返回结构。上层只认这个，不认具体供应商的响应格式。"""

    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class BaseLLM(Protocol):
    """LLM 提供者接口。换供应商 = 新写一个实现类，其他代码零改动。"""

    name: str

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        ...


class OpenAICompatibleLLM:
    """OpenAI 兼容协议的 LLM 实现。

    为什么只写这一个实现：DeepSeek / 通义千问 / 智谱 / Kimi / 本地 vLLM
    几乎都提供 /chat/completions 兼容端点，改 base_url + model 即可切换，
    无需为每家写适配器。真正不兼容的供应商阶段 2 再加实现类。
    """

    name = "openai-compatible"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60) -> None:
        if not base_url or not api_key:
            raise ValueError("OpenAICompatibleLLM 需要 base_url 和 api_key")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        """同步调用 /chat/completions。

        注意：原型用 requests 同步调用即可；阶段 2 服务化时这个函数体
        改成 httpx.AsyncClient 的 await 版本，签名保持不变。
        """
        import requests

        t0 = time.time()
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "temperature": kwargs.get("temperature", 0.1),
                "max_tokens": kwargs.get("max_tokens", 800),
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]["message"]
        usage = data.get("usage", {})
        return LLMResponse(
            text=choice.get("content", ""),
            model=data.get("model", self.model),
            provider=self.name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=int((time.time() - t0) * 1000),
        )


class MockLLM:
    """离线 Mock：不调用任何外部服务，把检索到的原文回显成"回答"。

    用途：没有 API Key 时验证整条链路（检索→拼提示词→出回答）。
    它输出的内容刻意暴露"命中了哪些引用"，方便人工核对检索质量。
    """

    name = "mock"

    def __init__(self, model: str = "mock-echo") -> None:
        self.model = model

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        # 从最后一条 user 消息里提取提示词内容
        prompt = messages[-1]["content"] if messages else ""
        # 简化处理：把参考资料段落原样回显
        echo = (
            "【MockLLM 未调用真实模型，以下为链路自检输出】\n"
            "提示词长度：%d 字符\n"
            "---- 提示词预览（前 600 字）----\n%s\n"
            "---- 提示词预览结束 ----\n"
            "若以上内容包含你检索到的制度原文，说明 RAG 链路已通。"
        ) % (len(prompt), prompt[:600])
        return LLMResponse(text=echo, model=self.model, provider=self.name)


def build_llm(cfg: Config) -> BaseLLM:
    """工厂：有 Key 走真实供应商，没有就走 Mock。

    阶段 2 这里会变成"按配置路由到不同适配器 + 故障转移"。
    """
    if cfg.llm_base_url and cfg.llm_api_key and cfg.llm_model:
        print(f"[llm] 供应商：{cfg.llm_base_url} 模型：{cfg.llm_model}", flush=True)
        return OpenAICompatibleLLM(
            base_url=cfg.llm_base_url,
            api_key=cfg.llm_api_key,
            model=cfg.llm_model,
            timeout=cfg.llm_timeout,
        )
    print("[llm] ⚠️ 未配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，使用 MockLLM", flush=True)
    return MockLLM()


# ====================================================================
# 【核心模块 · 后续阶段直接复用】之八：提示词组装
# ----------------------------------------------------------------
# 复用说明：阶段 2 会长成 agent/prompts/ 目录（按场景分模板），
# 并从数据库读取客户定制的制度口径。这里的 system + user 骨架不变。
# ====================================================================
SYSTEM_PROMPT = """你是企业报销制度的答疑助手。你的唯一职责是依据下面给定的「制度原文」回答员工问题。

硬性规则（违反任何一条都算失败）：
1. 只能依据「制度原文」作答，禁止使用你自己的常识、记忆或推测。
2. 回答中每一个结论，都必须标注引用编号，格式为 [1]、[2]、[3]。
3. 引用编号必须与你实际依据的那段原文的编号**严格一致**：
   内容来自编号 [1] 的原文，就必须写 [1]，禁止写错号或张冠李戴。
4. 如果「制度原文」中没有覆盖该问题，必须明确回答"制度中未作规定，请联系财务确认"，
   绝不编造金额、比例、流程或时限。
5. 涉及具体数字（金额上限、比例、天数）时，必须原文引用，不得四舍五入或改写。
6. 语气简洁、直接，面向企业员工，不写客套话。
"""


def validate_citations(answer: str, num_contexts: int) -> dict[str, Any]:
    """引用完整性自检（原型级，零成本）。

    ⭐ 为什么需要它：实测发现即使检索正确、答案内容正确，
    GLM-4-flash 仍会把引用编号标错（内容出自 [1] 却写成 [3]）。
    对"可追溯"这个核心诉求来说，指错出处和答错几乎是同等的失败。

    这里只做**机械可验**的部分：
      - 答案里到底有没有引用编号
      - 引用编号是否越界（比如只有 3 段原文却写 [5]）
    它抓不住"编号没越界但指错了块"——那需要语义判定，
    阶段 2 用一次轻量 NLI/entailment 校验或二次 LLM 调用来做，
    原型先不做，但必须在架构里留出这个位置。
    """
    import re

    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)})
    out_of_range = [n for n in cited if n < 1 or n > num_contexts]
    return {
        "cited": cited,
        "out_of_range": out_of_range,
        "has_citation": bool(cited),
        # ok 只代表"格式与范围合规"，不代表"指向正确"
        "ok": bool(cited) and not out_of_range,
    }


def build_messages(question: str, contexts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把检索结果拼进提示词。

    关键设计：给每个切片编号 [1][2][3]，并要求模型在回答里引用编号。
    这是"可溯源"的最低成本实现——阶段 2 会把这个编号映射回
    kb_chunks.id，前端就能做点击跳转。
    """
    if not contexts:
        context_block = "（未检索到任何相关制度原文）"
    else:
        parts = []
        for i, ctx in enumerate(contexts, start=1):
            parts.append(f"[{i}] 来源：制度切片 #{ctx['chunk_index']}\n{ctx['text']}")
        context_block = "\n\n".join(parts)

    user_prompt = f"""【制度原文】
{context_block}

【员工问题】
{question}

请依据上述制度原文回答问题，并标注引用编号。"""

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


# ====================================================================
# 原型装配层（阶段 2 会被 FastAPI 依赖注入取代）
# ----------------------------------------------------------------
# 复用说明：这一层是"胶水"，阶段 2 服务化时会被拆成
# services/knowledge/ingest.py + api/v1/chat.py，逻辑本身不变。
# ====================================================================
class RAGPipeline:
    """把切片、索引、检索、LLM 串成一条可复用的管线。"""

    def __init__(self, cfg: Config, doc_path: str, rebuild: bool = False) -> None:
        self.cfg = cfg
        # 拿到 (provider, vector_enabled)：embedding 不可用时关闭向量路，
        # 而不是用噪声向量污染混合检索（详见 build_embedding_provider 注释）
        self.provider, vector_enabled = build_embedding_provider(cfg)

        # 索引缓存 tag：把供应商+模型+维度编进文件名，防止换模型后误用旧索引
        index_tag = f"{cfg.embed_provider}-{cfg.embed_model}-{self.provider.dim}"
        store = FaissVectorStore(
            dim=self.provider.dim, index_dir=cfg.index_dir, index_tag=index_tag
        )
        need_build = rebuild or store.index.ntotal == 0

        if need_build:
            print(f"[pipeline] 读取制度文件：{doc_path}", flush=True)
            chunker = TextChunker(cfg.chunk_size, cfg.chunk_overlap)
            text = chunker.load_markdown(doc_path)
            chunks = chunker.split(text)
            print(f"[pipeline] 切片完成：{len(chunks)} 块"
                  f"（目标 {cfg.chunk_size} 字 / 重叠 {cfg.chunk_overlap} 字）", flush=True)
            for i, c in enumerate(chunks[:3]):
                print(f"  └ 切片#{i}（{len(c)}字）：{c[:60].replace(chr(10), ' ')}...", flush=True)

            store.build(chunks, self.provider)
            store.save()
        else:
            print("[pipeline] 使用已缓存的索引（--rebuild 可强制重建）", flush=True)

        print("[pipeline] 构建 BM25 索引...", flush=True)
        self.bm25 = BM25Store(store.chunks)
        self.vector_store = store
        self.retriever = HybridRetriever(self.bm25, store, vector_enabled=vector_enabled)
        self.llm = build_llm(cfg)

        if not vector_enabled:
            print("[pipeline] ⚠️ 当前为纯 BM25 检索模式，语义召回缺失，"
                  "结果质量不代表最终效果", flush=True)

    def ask(self, question: str) -> dict[str, Any]:
        """一次完整问答：检索 → 拼提示词 → 调 LLM。"""
        # ① 混合检索
        hits = self.retriever.retrieve(
            question,
            provider=self.provider,
            top_k=self.cfg.top_k,
            rrf_k=self.cfg.rrf_k,
            bm25_candidates=self.cfg.bm25_candidates,
            vector_candidates=self.cfg.vector_candidates,
        )

        # ② 拼提示词
        messages = build_messages(question, hits)

        # ③ 调 LLM
        resp = self.llm.chat(
            messages,
            temperature=self.cfg.llm_temperature,
            max_tokens=self.cfg.llm_max_tokens,
        )

        return {
            "question": question,
            "answer": resp.text,
            "citations": hits,
            "citation_check": validate_citations(resp.text, len(hits)),
            "llm": {
                "provider": resp.provider,
                "model": resp.model,
                "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens,
                "latency_ms": resp.latency_ms,
            },
        }


def print_result(result: dict[str, Any]) -> None:
    """把一次问答结果格式化打印到命令行。"""
    print("\n" + "=" * 68)
    print(f"问：{result['question']}")
    print("-" * 68)
    print(result["answer"])
    print("-" * 68)

    # 引用自检：把问题直接暴露在命令行，不藏在日志里
    check = result.get("citation_check", {})
    if check:
        if check["ok"]:
            print(f"引用自检：通过（编号 {check['cited']}）")
        elif not check["has_citation"]:
            print("引用自检：⚠️ 未检出任何引用编号——答案不可溯源，"
                  "按硬性规则应判为不合格")
        else:
            print(f"引用自检：⚠️ 引用编号越界 {check['out_of_range']}"
                  f"（原文仅 {len(result['citations'])} 段）")

    print("命中引用：")
    for i, c in enumerate(result["citations"], start=1):
        via = "/".join(k for k, v in c["hit_by"].items() if v)
        preview = c["text"][:70].replace("\n", " ")
        print(f"  [{i}] 切片#{c['chunk_index']}  RRF={c['score']:.5f}  "
              f"命中方式={via}  「{preview}...」")
    llm = result["llm"]
    print(f"LLM：{llm['provider']}/{llm['model']}  "
          f"tokens={llm['prompt_tokens']}+{llm['completion_tokens']}  "
          f"耗时={llm['latency_ms']}ms")
    print("=" * 68 + "\n")


# ====================================================================
# 命令行入口
# ====================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="阶段1 裸问答原型：markdown 制度 → 混合检索 → LLM 回答"
    )
    parser.add_argument("--doc", required=True, help="报销制度 markdown 文件路径")
    parser.add_argument("--rebuild", action="store_true", help="强制重建索引（忽略缓存）")
    parser.add_argument("--top-k", type=int, default=3, help="检索返回条数，默认 3")
    args = parser.parse_args()

    cfg = Config.from_env()
    cfg.top_k = args.top_k

    if not Path(args.doc).exists():
        print(f"错误：找不到文件 {args.doc}", file=sys.stderr)
        sys.exit(1)

    pipeline = RAGPipeline(cfg, args.doc, rebuild=args.rebuild)

    print("\n原型就绪。输入问题后回车，输入 q / quit / exit 退出。\n")
    while True:
        try:
            question = input("提问> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not question:
            continue
        if question.lower() in {"q", "quit", "exit"}:
            print("再见。")
            break
        try:
            result = pipeline.ask(question)
            print_result(result)
        except Exception as exc:
            # 原型阶段不吞异常：让错误直接暴露，便于定位
            print(f"[错误] {type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
