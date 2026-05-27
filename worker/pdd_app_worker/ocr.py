"""OCR fallback for PDD price extraction.

背景：
PDD「百亿补贴」/ 部分活动卡片把价格用 Canvas / Drawable 自绘，
``uiautomator2.dump_hierarchy()`` 在 ViewHolder 树里看不到价格 TextView。
这恰恰是转卖比价最重要的字段（补贴价 vs 原价对标）。本模块用 EasyOCR
做兜底：截图 + 裁剪卡片底部价格区域 + OCR。

设计：
- ``EasyOCR.Reader`` 是单例，懒加载（首次用到才 init，~2-5s）
- 模型必须预先下到 ``~/.EasyOCR/model/``（``fetch_easyocr_models``）。
  ``download_enabled=False`` 让在线下不行就显式报错而不是默默卡住
- 每次价格提取只 OCR 一块小 crop（卡片标题下方 ~200px 高的窄带），
  不全屏识别，单卡 60-150ms
- ``extract_price_async`` 是 ``asyncio.to_thread`` wrapper，把 CPU 工作
  推到工作线程，不阻塞 worker 的 asyncio 事件循环
- 候选筛选：confidence ≥ 0.35 + 价格范围 0.1-100000 + 含 ¥ 的 token 优先

不做的事：
- 不做"先试 PaddleOCR"——PaddleOCR 在 Python 3.14 上没 wheel，单独养
  一个 3.12 venv 性价比不高（见 ``docs/PDD-Day4-OCR方案.md`` §候选 2）
- 不做"识别失败 fallback 到云 API"——保持全本地、零成本、零隐私出网
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# 价格 token 正则：可能带 ¥/￥ 前缀，数字可以是整数或小数（保留逗号是因为
# OCR 偶尔把"."识别成","，等下做归一）
_PRICE_TOKEN_RE = re.compile(r"[¥￥]?\s*([0-9]+(?:[.,][0-9]+)?)")

_MIN_PRICE = 0.1       # 拼多多最便宜的薅羊毛商品也不会低于 0.1 元
_MAX_PRICE = 100_000.0
_MIN_CONFIDENCE = 0.35  # EasyOCR 对清晰大字基本 ≥ 0.6，0.35 是兜底阈值

_reader_lock = threading.Lock()
_reader: Any = None  # easyocr.Reader 实例，懒加载


def _get_reader() -> Any:
    """懒加载 EasyOCR Reader。模型必须已经下到 ``~/.EasyOCR/model/``。"""
    global _reader
    with _reader_lock:
        if _reader is None:
            try:
                import easyocr  # type: ignore[import-untyped]
            except ImportError as exc:
                raise RuntimeError(
                    "easyocr 没装——先跑 `pip install easyocr`，详见 "
                    "docs/PDD-Day4-OCR方案.md §Step 1"
                ) from exc
            _reader = easyocr.Reader(
                ["ch_sim", "en"],
                gpu=False,
                download_enabled=False,  # 必须预下，避免运行时卡 GitHub
                verbose=False,
            )
            logger.info("EasyOCR reader initialized (ch_sim+en, CPU)")
    return _reader


def preload_reader() -> None:
    """worker 启动时显式预热——把模型一次性加载进内存。

    冷启动一次 ~2-5s，预热掉之后第一条真实任务不用等。失败不抛——
    OCR 是 best-effort 兜底，没它 worker 也能跑（只是补贴价取不到）。
    """
    try:
        _get_reader()
    except Exception as exc:
        logger.warning(f"OCR preload failed (will retry lazily): {type(exc).__name__}: {exc}")


@dataclass
class _Candidate:
    value: float
    confidence: float
    raw_text: str
    bbox: list


def _normalize_value(num_str: str) -> float | None:
    """OCR 偶尔把'.'识别成','，统一成 float。失败返回 None。"""
    s = num_str.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def extract_price_from_image(
    image_bgr: Any,
    region: tuple[int, int, int, int],
) -> tuple[float | None, dict[str, Any]]:
    """从截图中指定矩形区域 OCR 出价格。

    :param image_bgr: numpy ndarray, BGR 全屏截图（``d.screenshot(format='opencv')``）
    :param region: (x1, y1, x2, y2) 像素坐标，对应价格扫描窗口
    :return: (识别到的价格 or None, 元数据 dict 用于 debug)

    元数据字段：
    - ``reason``: ok / empty_region / empty_crop / ocr_error / no_price_candidates
    - ``raw_text``: 命中候选的原始 OCR 文本
    - ``confidence``: 命中候选的 EasyOCR 置信度 + bonus
    - ``n_candidates``: 总候选数（debug 用）
    - ``raw_results``: no_price_candidates 时附 OCR 原始返回前 8 条（debug 用）
    """
    try:
        h, w = image_bgr.shape[:2]
    except AttributeError:
        return None, {"reason": "bad_image", "detail": "image_bgr is not numpy array"}

    x1, y1, x2, y2 = region
    x1 = max(0, min(int(x1), w))
    y1 = max(0, min(int(y1), h))
    x2 = max(0, min(int(x2), w))
    y2 = max(0, min(int(y2), h))
    if x2 <= x1 or y2 <= y1:
        return None, {"reason": "empty_region", "region": region}

    crop = image_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return None, {"reason": "empty_crop"}

    try:
        reader = _get_reader()
    except Exception as exc:
        return None, {"reason": "ocr_init_error", "error": repr(exc)}

    try:
        # detail=1 返回 [(bbox, text, confidence), ...]
        # paragraph=False 不合并相邻行（价格通常是单独一行）
        results = reader.readtext(crop, detail=1, paragraph=False)
    except Exception as exc:
        logger.debug(f"OCR readtext failed on region {region}: {exc}")
        return None, {"reason": "ocr_error", "error": repr(exc)}

    candidates: list[_Candidate] = []
    for bbox, text, conf in results:
        if conf < _MIN_CONFIDENCE:
            continue
        for m in _PRICE_TOKEN_RE.finditer(text):
            val = _normalize_value(m.group(1))
            if val is None:
                continue
            if not (_MIN_PRICE <= val <= _MAX_PRICE):
                continue
            # 加分项：
            # 1. token 带 ¥/￥ 前缀 → +0.15（强信号是价格）
            # 2. token 在 crop 上半部分 → +0.05（价格通常在标题正下方，subtitle/促销在底部）
            bonus = 0.0
            if "¥" in text or "￥" in text:
                bonus += 0.15
            try:
                top_y = min(p[1] for p in bbox)
                crop_h = y2 - y1
                if crop_h > 0 and top_y < crop_h * 0.4:
                    bonus += 0.05
            except Exception:
                pass
            candidates.append(_Candidate(
                value=val,
                confidence=conf + bonus,
                raw_text=text,
                bbox=bbox,
            ))

    if not candidates:
        return None, {
            "reason": "no_price_candidates",
            "raw_results": [
                {"text": t, "conf": round(float(c), 3)}
                for _, t, c in results[:8]
            ],
        }

    # 取置信度最高的；同分时选价格更低的（活动价 < 原价是常态，PDD 大字
    # 显示的也是低价那个）
    candidates.sort(key=lambda c: (-c.confidence, c.value))
    best = candidates[0]
    return best.value, {
        "reason": "ok",
        "raw_text": best.raw_text,
        "confidence": round(best.confidence, 3),
        "n_candidates": len(candidates),
    }


async def extract_price_async(
    image_bgr: Any,
    region: tuple[int, int, int, int],
) -> tuple[float | None, dict[str, Any]]:
    """``extract_price_from_image`` 的 async 包装。CPU-bound 工作放到线程。"""
    return await asyncio.to_thread(extract_price_from_image, image_bgr, region)
