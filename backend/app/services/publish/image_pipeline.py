"""
Image processing pipeline for Xianyu listing differentiation.
Three-tier source priority → smart processing → 10 candidate images output.
"""
import io
import os
import random
import uuid
from pathlib import Path

import httpx
from loguru import logger
from PIL import Image, ImageEnhance, ImageFilter, ImageDraw, ImageFont

UPLOAD_DIR = Path("/home/devbox/project/storage/images")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ASPECT_RATIOS = [(3, 4), (1, 1), (4, 3)]
MAX_DIMENSION = 1200
JPEG_QUALITY_RANGE = (78, 92)


def strip_exif(img: Image.Image) -> Image.Image:
    """Remove all EXIF metadata by re-creating the image data."""
    data = list(img.getdata())
    clean = Image.new(img.mode, img.size)
    clean.putdata(data)
    return clean


def random_crop(img: Image.Image, ratio: tuple[int, int] | None = None) -> Image.Image:
    """Crop image to a random aspect ratio with slight offset."""
    if ratio is None:
        ratio = random.choice(ASPECT_RATIOS)
    w, h = img.size
    target_r = ratio[0] / ratio[1]
    current_r = w / h

    if current_r > target_r:
        new_w = int(h * target_r)
        max_offset = max(0, w - new_w)
        offset = random.randint(0, max_offset)
        return img.crop((offset, 0, offset + new_w, h))
    else:
        new_h = int(w / target_r)
        max_offset = max(0, h - new_h)
        offset = random.randint(0, max_offset)
        return img.crop((0, offset, w, offset + new_h))


def adjust_brightness_contrast(img: Image.Image) -> Image.Image:
    """Randomly adjust brightness and contrast within ±3~8%."""
    brightness = 1.0 + random.uniform(-0.08, 0.08)
    contrast = 1.0 + random.uniform(-0.08, 0.08)
    saturation = 1.0 + random.uniform(-0.05, 0.05)

    img = ImageEnhance.Brightness(img).enhance(brightness)
    img = ImageEnhance.Contrast(img).enhance(contrast)
    img = ImageEnhance.Color(img).enhance(saturation)
    return img


def slight_rotation(img: Image.Image) -> Image.Image:
    """Apply a very slight rotation to differentiate the image."""
    angle = random.uniform(-1.5, 1.5)
    return img.rotate(angle, expand=False, fillcolor=(255, 255, 255))


def apply_background_blur(img: Image.Image) -> Image.Image:
    """Apply light Gaussian blur to the outer edges."""
    w, h = img.size
    border = int(min(w, h) * 0.15)
    center = img.crop((border, border, w - border, h - border))
    blurred = img.filter(ImageFilter.GaussianBlur(radius=3))
    blurred.paste(center, (border, border))
    return blurred


def add_text_label(img: Image.Image, text: str) -> Image.Image:
    """Add a small text label (selling point or model info) to the image."""
    draw = ImageDraw.Draw(img)
    w, h = img.size
    font_size = max(16, int(min(w, h) * 0.04))

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    padding = 6

    positions = [
        (w - text_w - padding * 3, h - text_h - padding * 3),
        (padding * 2, h - text_h - padding * 3),
        (w - text_w - padding * 3, padding * 2),
    ]
    x, y = random.choice(positions)

    draw.rectangle([x - padding, y - padding, x + text_w + padding, y + text_h + padding],
                    fill=(0, 0, 0, 180))
    draw.text((x, y), text, fill=(255, 255, 255), font=font)
    return img


def create_comparison_collage(images: list[Image.Image], max_cols: int = 2) -> Image.Image | None:
    """Create a collage from multiple images."""
    if len(images) < 2:
        return None
    selected = random.sample(images, min(4, len(images)))
    cols = min(max_cols, len(selected))
    rows = (len(selected) + cols - 1) // cols

    cell_w, cell_h = 400, 400
    collage = Image.new("RGB", (cell_w * cols, cell_h * rows), (255, 255, 255))

    for i, img in enumerate(selected):
        r, c = divmod(i, cols)
        thumb = img.copy()
        thumb.thumbnail((cell_w, cell_h), Image.LANCZOS)
        offset_x = (cell_w - thumb.width) // 2
        offset_y = (cell_h - thumb.height) // 2
        collage.paste(thumb, (c * cell_w + offset_x, r * cell_h + offset_y))

    return collage


def resize_for_platform(img: Image.Image) -> Image.Image:
    """Resize image to fit platform requirements."""
    w, h = img.size
    if max(w, h) > MAX_DIMENSION:
        ratio = MAX_DIMENSION / max(w, h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
    return img


def save_processed(img: Image.Image, product_id: str, suffix: str = "") -> str:
    """Save processed image and return the file path."""
    product_dir = UPLOAD_DIR / product_id
    product_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex[:12]}{suffix}.jpg"
    filepath = product_dir / filename
    quality = random.randint(*JPEG_QUALITY_RANGE)
    img.save(str(filepath), "JPEG", quality=quality, optimize=True)
    return str(filepath)


async def download_images(urls: list[str]) -> list[tuple[str, Image.Image]]:
    """Download images from URLs, return list of (url, PIL.Image)."""
    results = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and len(resp.content) > 1024:
                    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                    if min(img.size) >= 200:
                        results.append((url, img))
            except Exception as e:
                logger.debug(f"Image download failed: {url} - {e}")
    return results


def process_single_image(
    img: Image.Image,
    product_id: str,
    label_text: str | None = None,
    style: str = "xianyu",
) -> list[str]:
    """
    Apply differentiation to a single source image.
    Returns paths to 2-3 processed variants.
    """
    variants = []

    # Variant 1: basic clean + random crop + color adjust
    v1 = strip_exif(img.copy())
    v1 = random_crop(v1)
    v1 = adjust_brightness_contrast(v1)
    v1 = resize_for_platform(v1)
    variants.append(save_processed(v1, product_id, "_v1"))

    # Variant 2: rotation + different crop
    v2 = strip_exif(img.copy())
    v2 = random_crop(v2, random.choice(ASPECT_RATIOS))
    v2 = slight_rotation(v2)
    v2 = adjust_brightness_contrast(v2)
    v2 = resize_for_platform(v2)
    variants.append(save_processed(v2, product_id, "_v2"))

    # Variant 3 (optional): with label or blur
    if random.random() > 0.4:
        v3 = strip_exif(img.copy())
        v3 = random_crop(v3)
        if label_text and random.random() > 0.5:
            v3 = add_text_label(v3, label_text)
        else:
            v3 = apply_background_blur(v3)
        v3 = resize_for_platform(v3)
        variants.append(save_processed(v3, product_id, "_v3"))

    return variants


async def generate_candidate_images(
    product_id: str,
    source_image_urls: list[str],
    user_upload_paths: list[str] | None = None,
    label_text: str | None = None,
    target_count: int = 10,
) -> list[str]:
    """
    Full image pipeline: download → process → generate 10 candidate images.

    Priority:
    1. User uploads (highest)
    2. Source platform review images
    3. Store detail images
    """
    all_images: list[Image.Image] = []

    # Priority 1: User uploads
    if user_upload_paths:
        for path in user_upload_paths:
            try:
                img = Image.open(path).convert("RGB")
                all_images.append(img)
            except Exception as e:
                logger.warning(f"Failed to open user upload: {path} - {e}")

    # Priority 2 & 3: Download from URLs
    if source_image_urls:
        downloaded = await download_images(source_image_urls)
        all_images.extend([img for _, img in downloaded])

    if not all_images:
        logger.warning(f"No images available for product {product_id}")
        return []

    candidates: list[str] = []

    # Generate processed variants from each source image
    for img in all_images:
        paths = process_single_image(img, product_id, label_text)
        candidates.extend(paths)
        if len(candidates) >= target_count:
            break

    # Fill remaining with collages or additional variants
    if len(candidates) < target_count and len(all_images) >= 2:
        collage = create_comparison_collage(all_images)
        if collage:
            collage = resize_for_platform(collage)
            candidates.append(save_processed(collage, product_id, "_collage"))

    # Ensure exactly target_count by creating more variants if needed
    idx = 0
    while len(candidates) < target_count and all_images:
        img = all_images[idx % len(all_images)]
        extra = process_single_image(img, product_id, label_text)
        candidates.extend(extra[:1])
        idx += 1

    return candidates[:target_count]
