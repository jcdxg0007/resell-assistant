"""
Cross-platform product matching pipeline.
Step 1: Text similarity (keyword + title)
Step 2: Perceptual hash (pHash) for image filtering
Step 3: CLIP vector similarity for semantic matching
"""
import hashlib
import re
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any

import httpx
from loguru import logger

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import open_clip
    import torch
    HAS_CLIP = False  # Disabled until model is downloaded
except ImportError:
    HAS_CLIP = False


def clean_title(title: str) -> str:
    """Remove noise from product titles for comparison."""
    noise = r'[【】\[\]{}()（）❤️🔥✅💥☆★~！!。，,、/\\\-—_|·]'
    title = re.sub(noise, ' ', title)
    stopwords = {'包邮', '现货', '全新', '正品', '特价', '促销', '限时', '秒杀', '热卖', '爆款'}
    words = title.split()
    words = [w for w in words if w not in stopwords]
    return ' '.join(words).strip()


def text_similarity(title_a: str, title_b: str) -> float:
    """Calculate title similarity using SequenceMatcher."""
    a = clean_title(title_a).lower()
    b = clean_title(title_b).lower()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def price_in_range(price_a: float, price_b: float, tolerance: float = 0.5) -> bool:
    """Check if two prices are within a tolerance ratio of each other."""
    if price_a <= 0 or price_b <= 0:
        return False
    ratio = max(price_a, price_b) / min(price_a, price_b)
    return ratio <= (1 + tolerance)


def compute_phash(image_bytes: bytes, hash_size: int = 8) -> str | None:
    """Compute perceptual hash (pHash) of an image."""
    if not HAS_PIL:
        return None
    try:
        img = Image.open(BytesIO(image_bytes)).convert('L')
        img = img.resize((hash_size * 4, hash_size * 4), Image.LANCZOS)

        import numpy as np
        pixels = np.array(img, dtype=float)

        from scipy.fft import dct
        dct_result = dct(dct(pixels, axis=0), axis=1)
        dct_low = dct_result[:hash_size, :hash_size]

        median = np.median(dct_low)
        diff = dct_low > median
        return ''.join(['1' if v else '0' for v in diff.flatten()])
    except ImportError:
        img = Image.open(BytesIO(image_bytes)).convert('L').resize((hash_size, hash_size), Image.LANCZOS)
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        return ''.join(['1' if p > avg else '0' for p in pixels])
    except Exception as e:
        logger.debug(f"pHash computation failed: {e}")
        return None


def phash_similarity(hash_a: str, hash_b: str) -> float:
    """Calculate similarity between two perceptual hashes (0.0 to 1.0)."""
    if not hash_a or not hash_b or len(hash_a) != len(hash_b):
        return 0.0
    matching = sum(a == b for a, b in zip(hash_a, hash_b))
    return matching / len(hash_a)


async def download_image(url: str) -> bytes | None:
    """Download image from URL."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.content
    except Exception as e:
        logger.debug(f"Image download failed: {url} - {e}")
    return None


class ProductMatcher:
    """
    Multi-step product matching pipeline.

    Pipeline:
    1. Text + Price filter -> candidates (20-50)
    2. pHash image filter -> narrowed (5-15)
    3. CLIP semantic match -> final matches (scored)
    """

    def __init__(self):
        self.text_threshold = 0.3
        self.phash_threshold = 0.75
        self.clip_threshold_match = 0.85
        self.clip_threshold_review = 0.70

    async def find_matches(
        self,
        source_product: dict,
        target_products: list[dict],
    ) -> list[dict]:
        """Run the full matching pipeline."""
        # Step 1: Text + Price filter
        text_candidates = []
        for target in target_products:
            sim = text_similarity(source_product.get("title", ""), target.get("title", ""))
            in_range = price_in_range(
                source_product.get("price", 0),
                target.get("price", 0),
                tolerance=3.0,  # Xianyu prices can be 3x source
            )
            if sim >= self.text_threshold or in_range:
                target["_text_sim"] = sim
                text_candidates.append(target)

        logger.info(f"Step 1 (text+price): {len(target_products)} -> {len(text_candidates)} candidates")

        if not text_candidates:
            return []

        # Step 2: pHash filter (if images available)
        source_img_url = (source_product.get("image_urls") or [None])[0] or source_product.get("image_url")
        if source_img_url and HAS_PIL:
            source_img = await download_image(source_img_url)
            source_hash = compute_phash(source_img) if source_img else None

            if source_hash:
                phash_candidates = []
                for target in text_candidates:
                    target_img_url = (target.get("image_urls") or [None])[0] or target.get("image_url")
                    if target_img_url:
                        target_img = await download_image(target_img_url)
                        target_hash = compute_phash(target_img) if target_img else None
                        p_sim = phash_similarity(source_hash, target_hash) if target_hash else 0
                        target["_phash_sim"] = p_sim
                        if p_sim >= self.phash_threshold:
                            phash_candidates.append(target)
                        elif target.get("_text_sim", 0) >= 0.6:
                            phash_candidates.append(target)
                    else:
                        phash_candidates.append(target)

                logger.info(f"Step 2 (pHash): {len(text_candidates)} -> {len(phash_candidates)} candidates")
                text_candidates = phash_candidates

        # Step 3: Compute overall score
        results = []
        for target in text_candidates:
            text_sim = target.get("_text_sim", 0)
            phash_sim = target.get("_phash_sim", 0)
            clip_sim = target.get("_clip_sim", 0)

            overall = text_sim * 0.3 + phash_sim * 0.4 + clip_sim * 0.3
            if not phash_sim and not clip_sim:
                overall = text_sim

            results.append({
                "target": target,
                "text_similarity": round(text_sim, 3),
                "phash_similarity": round(phash_sim, 3),
                "clip_similarity": round(clip_sim, 3),
                "overall_score": round(overall, 3),
                "is_match": overall >= 0.6,
                "needs_review": 0.4 <= overall < 0.6,
            })

        results.sort(key=lambda x: x["overall_score"], reverse=True)
        return results


product_matcher = ProductMatcher()
