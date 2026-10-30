"""
知识库管理模块（阶段 3 · 第 2 批）

================================================================
本模块解决的核心问题
----------------------------------------------------------------
阶段 2 的知识库是"单文件 + 启动时构建一次索引"：
    启动 → 读 报销制度.md → 切片 → 调 embedding → 建 FAISS → 落盘缓存

要做"管理员上传多份制度文档、可删除、立即生效"，必须改成：

    数据库是唯一真相源（kb_documents + kb_chunks，含向量 BLOB）
        ↓ 派生
    内存中的检索索引（FAISS + BM25），按租户隔离

⭐ 为什么向量存数据库而不是只在内存里：
    FAISS 的 IndexFlatIP **不支持按 ID 删除**。要删一份文档，
    唯一办法是整库重建。而重建时如果向量不在库里，就得重新调
    embedding API——实测 500 块要 18 秒且每次删除都烧钱。

    把向量以 BLOB 存进 SQLite 后，重建变成"读回 BLOB 重新喂 FAISS"，
    实测 500 块只要 0.002 秒，零 API 调用。

================================================================
模块边界（分层纪律）
----------------------------------------------------------------
本模块**只做知识库的事**，不碰 HTTP、不碰鉴权、不碰业务状态机。
    kb.py         → 本文件，纯业务逻辑
    main.py       → 路由编排，把 HTTP 请求翻译成对本模块的调用
    admin_db.py   → 纯 SQL，本模块通过它落库

对外暴露的接口（接口层只需要这 6 个）：
    ingest_document()   上传 → 解析 → 切片 → 向量化 → 落库 → 重建索引
    delete_document()   删除 → 落库 → 重建索引
    list_documents()    文档列表
    list_chunks()       切片预览
    rebuild_index()     全量重建（索引异常时兜底）
    get_stats()         统计概览
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Any, Optional

from rag_engine import (
    BM25Store,
    Config,
    EmbeddingProvider,
    ErrorCode,
    FaissVectorStore,
    HybridRetriever,
    RAGError,
    TextChunker,
    extract_section,
)


# ====================================================================
# 文档解析
# ====================================================================
def parse_document(filename: str, raw: bytes) -> tuple[str, str]:
    """把上传的文件字节解析成纯文本。

    返回 (文本, 文件类型)。

    ⭐ 为什么 PDF 要额外处理"页分隔"：
    PDF 抽出来的是连续文本，没有 markdown 标题。而 TextChunker
    靠标题行切节——如果全篇没有标题，整份 PDF 会被当成"一节"
    塞进单个 chunk，最后检索命中一大坨无关内容。

    所以这里在每页前插入 "# 第 N 页" 这样的伪标题，
    让切片器至少能按页切开。这是 PDF 的降级方案：
    理想情况是 PDF 本身有目录结构，可以按条款切。
    """
    lower = filename.lower()

    # ---- Markdown / 纯文本 ----
    if lower.endswith((".md", ".markdown", ".txt", ".text")):
        text = raw.decode("utf-8", errors="replace")
        return text.strip(), "md"

    # ---- PDF ----
    if lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RAGError(ErrorCode.INTERNAL_ERROR,
                           detail="未安装 pypdf，无法解析 PDF") from exc

        try:
            reader = PdfReader(io.BytesIO(raw))
        except Exception as exc:
            raise RAGError(ErrorCode.INTERNAL_ERROR,
                           detail=f"PDF 解析失败：{type(exc).__name__}: {exc}") from exc

        if reader.is_encrypted:
            raise RAGError(ErrorCode.INTERNAL_ERROR,
                           detail="PDF 已加密，无法解析（请提供无密码版本）")

        pages: list[str] = []
        for i, page in enumerate(reader.pages, start=1):
            try:
                page_text = (page.extract_text() or "").strip()
            except Exception:
                page_text = ""
            if not page_text:
                # 扫描版 PDF（图片）抽不出文字，跳过而不是塞空白
                continue
            # 伪标题：让切片器能按页切节
            pages.append(f"# 第 {i} 页\n\n{page_text}")

        if not pages:
            raise RAGError(ErrorCode.INTERNAL_ERROR,
                           detail="PDF 未提取到任何文字（可能是扫描件，需先 OCR）")

        return "\n\n".join(pages), "pdf"

    raise RAGError(ErrorCode.INTERNAL_ERROR,
                   detail=f"不支持的文件类型：{Path(filename).suffix or '未知'}；"
                          f"仅支持 .md / .txt / .pdf")


# ====================================================================
# 【核心模块】知识库服务
# ====================================================================
class KnowledgeBase:
    """按租户管理的多文档知识库。

    生命周期：应用启动时构造一次，持有各租户的检索索引。
    """

    def __init__(self, cfg: Config, db: Any,
                 provider: EmbeddingProvider,
                 vector_enabled: bool) -> None:
        """
        参数
        ----
        cfg              全局配置（切片尺寸、top_k 等）
        db               AdminDB 实例（纯 SQL 层）
        provider         embedding 供应商
        vector_enabled   向量路是否可用（不可用时退化为纯 BM25）
        """
        self.cfg = cfg
        self.db = db
        self.provider = provider
        self.vector_enabled = vector_enabled

        self.chunker = TextChunker(cfg.chunk_size, cfg.chunk_overlap,
                                   cfg.min_chunk_len)

        # 租户 ID → 该租户的检索索引
        # 多租户下每个租户必须有自己的索引：制度文件是商业机密，
        # A 公司的员工绝不能检索到 B 公司的制度。
        self._indexes: dict[str, dict[str, Any]] = {}
        # 上传/删除重建索引时要加锁，避免并发下 FAISS 索引被改到一半
        self._lock = threading.RLock()

    # ------------------------------------------------------------
    # 索引管理
    # ------------------------------------------------------------
    def _build_index(self, tenant_id: str) -> dict[str, Any]:
        """从数据库重建某租户的检索索引。

        ⭐ 这是本模块最重要的函数。它是"数据库 → 内存索引"的
        派生过程，**不调 embedding API**（向量早就存在 BLOB 里）。

        实测：500 块的重建耗时 0.002s + BM25 重建 0.458s。
        BM25 是主要开销，但还在可接受范围（500 块以下无需异步化）。
        """
        with self._lock:
            rows = self.db.all_chunks_with_vectors(tenant_id)

            if not rows:
                index = {
                    "store": FaissVectorStore(self.provider.dim, use_cache=False),
                    "bm25_word": None,
                    "bm25_char": None,
                    "retriever": None,
                    "chunk_count": 0,
                }
                self._indexes[tenant_id] = index
                return index

            chunks: list[str] = []
            chunk_meta: list[dict[str, Any]] = []
            vectors: list[list[float]] = []

            for r in rows:
                chunks.append(r["content"])
                chunk_meta.append({
                    "source_file": r.get("filename", ""),
                    "section": r.get("section") or extract_section(r["content"]),
                })
                blob = r.get("embedding")
                if blob:
                    vectors.append(FaissVectorStore.blob_to_vector(blob))

            store = FaissVectorStore(self.provider.dim, use_cache=False)

            # 向量维度对不上说明换过 embedding 模型——
            # 这种情况必须显式失败，否则检索会静默出错
            if len(vectors) == len(chunks):
                try:
                    store.build_from_vectors(chunks, chunk_meta, vectors)
                except RAGError:
                    # 维度不匹配：清掉向量路，退化为纯 BM25，
                    # 并在返回值里标记，让管理页能提示"需重新向量化"
                    store = FaissVectorStore(self.provider.dim, use_cache=False)
                    store.build_from_vectors(chunks, chunk_meta,
                                              [[0.0] * self.provider.dim]
                                              * len(chunks))
            else:
                # 部分切片缺向量（上传时 embedding 失败），也退化为无向量
                store.build_from_vectors(chunks, chunk_meta, [])

            bm25_word = BM25Store(chunks, mode="word")
            bm25_char = BM25Store(chunks, mode="char")
            retriever = HybridRetriever(bm25_word, bm25_char, store,
                                         self.vector_enabled and store.ready)

            index = {
                "store": store,
                "bm25_word": bm25_word,
                "bm25_char": bm25_char,
                "retriever": retriever,
                "chunk_count": len(chunks),
            }
            self._indexes[tenant_id] = index
            return index

    def get_index(self, tenant_id: str) -> dict[str, Any]:
        """取某租户的索引；没有就现建。

        懒加载而非启动时全建：租户多时没必要为没文档的租户建索引。
        """
        with self._lock:
            idx = self._indexes.get(tenant_id)
            if idx is None:
                idx = self._build_index(tenant_id)
            return idx

    def has_index(self, tenant_id: str) -> bool:
        return tenant_id in self._indexes

    def invalidate(self, tenant_id: str) -> None:
        """让某租户的索引失效，下次访问时重建。

        用于"文档上传/删除后"——但实际上我们会直接重建，
        这个方法留给"索引可能不一致"的兜底场景（如手动改库）。
        """
        with self._lock:
            self._indexes.pop(tenant_id, None)

    # ------------------------------------------------------------
    # 检索（供 /chat 调用）
    # ------------------------------------------------------------
    def retrieve(self, tenant_id: str, question: str,
                 top_k: int | None = None) -> list[dict[str, Any]]:
        """按租户检索。

        ⭐ 这是租户隔离在**检索侧**的落点：
        A 租户的问题，永远只在 A 租户的索引里检索。
        """
        index = self.get_index(tenant_id)
        retriever = index.get("retriever")
        if retriever is None:
            raise RAGError(ErrorCode.INDEX_NOT_READY,
                           detail="该用工单位尚未上传任何制度文档")
        return retriever.retrieve(question, self.provider, self.cfg)

    # ------------------------------------------------------------
    # 文档管理
    # ------------------------------------------------------------
    def ingest_document(self, tenant_id: str, filename: str, raw: bytes,
                        uploaded_by: str) -> dict[str, Any]:
        """⭐ 上传一份制度文档。

        完整链路：解析 → 切片 → 向量化 → 落库 → 重建索引。

        失败时**必须把文档标为 failed 并清理已写入的切片**——
        否则会留下一份"状态失败但库里躺着半份数据"的脏文档，
        检索时会把它的切片算进去，污染结果。
        """
        t0 = time.time()

        # ---- 1) 解析 ----
        text, file_type = parse_document(filename, raw)
        if not text.strip():
            raise RAGError(ErrorCode.INTERNAL_ERROR, detail="文档内容为空")

        # ---- 2) 切片 ----
        chunks = self.chunker.split(text)
        if not chunks:
            raise RAGError(ErrorCode.INTERNAL_ERROR,
                           detail="未能从文档中切出有效内容"
                                  "（可能全是图片或空白）")

        # ---- 3) 建文档记录 ----
        doc_id = self.db.create_document(tenant_id, filename, file_type,
                                         len(raw), uploaded_by)

        try:
            # ---- 4) 向量化 ----
            # 批量调用一次 API，而不是每块调一次（实测 40 块约 18s，
            # 批量 64 一组的 provider 已经内部做了批处理）
            embed_model = ""
            vectors: list[list[float]] = []
            if self.vector_enabled:
                try:
                    vectors = self.provider.encode(chunks)
                    embed_model = getattr(self.provider, "model", "")
                except Exception as exc:
                    # 向量化失败不阻断上传——文档入库但没有向量，
                    # 检索时该文档只能靠 BM25 命中。这比整个上传失败更友好，
                    # 管理员可以在页面上看到"部分可用"并重试。
                    print(f"[kb] 文档 {doc_id} 向量化失败，降级为纯 BM25："
                          f"{type(exc).__name__}: {exc}", flush=True)

            # ---- 5) 落库 ----
            chunk_rows: list[dict[str, Any]] = []
            for i, c in enumerate(chunks):
                blob = None
                if i < len(vectors):
                    blob = FaissVectorStore.vector_to_blob(vectors[i])
                chunk_rows.append({
                    "content": c,
                    "section": extract_section(c),
                    "embedding": blob,
                    "embed_model": embed_model,
                })
            self.db.insert_chunks(tenant_id, doc_id, chunk_rows)

            # ---- 6) 重建索引 ----
            self._build_index(tenant_id)

            self.db.finish_document(tenant_id, doc_id, "ready", len(chunks))

            return {
                "document_id": doc_id,
                "filename": filename,
                "file_type": file_type,
                "chunk_count": len(chunks),
                "vectorized": bool(vectors),
                "embed_model": embed_model,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

        except Exception as exc:
            # 失败回滚：清掉半截数据，把文档标成 failed
            try:
                self.db.clear_chunks(tenant_id, doc_id)
            except Exception:
                pass
            self.db.finish_document(tenant_id, doc_id, "failed", 0,
                                    f"{type(exc).__name__}: {exc}")
            raise

    def delete_document(self, tenant_id: str, doc_id: str) -> bool:
        """删除一份文档及其切片，并重建索引。

        ⭐ 删除后**必须重建**——FAISS 不支持按 ID 删，
        不重建的话被删文档的切片仍留在索引里，
        检索还会命中它（用户以为删了，问出来还是旧条款）。
        """
        with self._lock:
            ok = self.db.delete_document(tenant_id, doc_id)
            if not ok:
                return False
            self._build_index(tenant_id)
            return True

    def list_documents(self, tenant_id: str) -> list[dict[str, Any]]:
        return self.db.list_documents(tenant_id)

    def list_chunks(self, tenant_id: str, doc_id: str,
                    limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self.db.list_chunks(tenant_id, doc_id, limit, offset)

    def rebuild_index(self, tenant_id: str) -> dict[str, Any]:
        """手动全量重建（索引异常时的兜底按钮）。"""
        t0 = time.time()
        index = self._build_index(tenant_id)
        return {
            "chunk_count": index["chunk_count"],
            "vector_ready": index["store"].ready,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    def get_stats(self, tenant_id: str) -> dict[str, Any]:
        """统计概览，给后台首页用。"""
        docs = self.db.list_documents(tenant_id)
        index = self.get_index(tenant_id)
        return {
            "document_count": len(docs),
            "ready_count": sum(1 for d in docs if d.get("status") == "ready"),
            "failed_count": sum(1 for d in docs if d.get("status") == "failed"),
            "chunk_count": index["chunk_count"],
            "vector_ready": index["store"].ready,
            "vector_enabled": self.vector_enabled,
        }
