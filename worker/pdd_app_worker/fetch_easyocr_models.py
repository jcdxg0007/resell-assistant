"""通过 ghfast.top 镜像把 EasyOCR 需要的模型下到 %USERPROFILE%\\.EasyOCR\\model\\。

EasyOCR 默认从 github.com 下载，国内网络经常 TimeoutError（2026-05-27 实测过）。
本脚本：

  1. 读出 easyocr 内部 config 里登记的 ch_sim + en 模型 URL（detection + recognition）
  2. 把每个 URL 的 github.com 部分替换成 ghfast.top/https://github.com
  3. 下载到 ~/.EasyOCR/model/，解压 zip，删 zip
  4. 跑一次 ``easyocr.Reader(['ch_sim','en'])`` 看模型识别（应该没有任何网络请求）

如果某些 URL 镜像也连不通，会尝试 gh-proxy.com 作为第 2 个镜像。

用法（venv 激活后）::

    python -m pdd_app_worker.fetch_easyocr_models

成功后会打印 ``EasyOCR ready`` 并退出码 0。
"""
from __future__ import annotations

import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


GITHUB_MIRRORS = [
    "https://ghfast.top/",          # 你之前 git pull 一直在用的镜像
    "https://gh-proxy.com/",        # 备选 1
    "https://mirror.ghproxy.com/",  # 备选 2
]


def _mirror_url(url: str, mirror_prefix: str) -> str:
    """https://github.com/... → https://ghfast.top/https://github.com/...

    部分镜像（ghfast）要求保留原 https:// 前缀；
    部分镜像（gh-proxy 老风格）只要 github.com 之后的 path。这里统一用第一种格式，
    实测 ghfast / gh-proxy / mirror.ghproxy 都兼容。
    """
    return mirror_prefix.rstrip("/") + "/" + url


def _download_with_fallback(url: str, dst: Path, *, max_tries: int = 2) -> None:
    """先直连，失败再依次走镜像。每次最多 ``max_tries`` 个连接。"""
    candidates = [url] + [_mirror_url(url, m) for m in GITHUB_MIRRORS]
    last_err: Exception | None = None
    for cand in candidates:
        for attempt in range(max_tries):
            try:
                print(f"  → 尝试 {cand} (attempt {attempt + 1}/{max_tries})")
                urllib.request.urlretrieve(cand, dst)
                size_mb = dst.stat().st_size / 1024 / 1024
                print(f"     ✅ {size_mb:.1f} MB 已下载到 {dst}")
                return
            except (urllib.error.URLError, TimeoutError, ConnectionResetError) as exc:
                last_err = exc
                print(f"     ❌ {type(exc).__name__}: {exc}")
                continue
    raise RuntimeError(
        f"所有镜像都连不通  原 URL={url}  最后错误={last_err!r}"
    )


def _unpack_zip(zip_path: Path, target_dir: Path) -> None:
    print(f"  → 解压 {zip_path.name} 到 {target_dir}")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target_dir)
    zip_path.unlink()


def _collect_required_models(langs: list[str]) -> list[tuple[str, str, str]]:
    """读 easyocr 内部 config，返回 [(model_kind, filename, url), ...]。

    依赖 easyocr 内部数据结构——不同版本字段名稍有差异，做兼容处理。
    """
    import easyocr  # noqa: F401  确保 import 成功
    from easyocr.config import (  # type: ignore[attr-defined]
        detection_models, recognition_models,
    )

    needed: list[tuple[str, str, str]] = []

    # detection model（craft 是默认）
    det = detection_models.get("craft") or next(iter(detection_models.values()))
    needed.append(("detection", det["filename"], det["url"]))

    # recognition models（lang → 模型）
    # 当用户传 ['ch_sim', 'en'] 时，EasyOCR 内部通常会选 'zh_sim_g2'（包含 en）。
    # 我们简单地把所有相关候选都拉一遍——多下一两个不大，避免缺货。
    candidates: set[str] = set()
    if "ch_sim" in langs:
        candidates.update(["zh_sim_g2", "chinese_sim", "cyrillic_g2"])  # 兼容老版本字段名
    if "en" in langs:
        candidates.update(["english_g2", "english"])

    gen2 = recognition_models.get("gen2") or {}
    gen1 = recognition_models.get("gen1") or {}

    seen_filenames: set[str] = set()
    for table in (gen2, gen1, recognition_models):
        if not isinstance(table, dict):
            continue
        for key, info in table.items():
            if not isinstance(info, dict) or "filename" not in info or "url" not in info:
                continue
            if key in candidates or info["filename"] in candidates:
                if info["filename"] in seen_filenames:
                    continue
                seen_filenames.add(info["filename"])
                needed.append(("recognition", info["filename"], info["url"]))
    return needed


def main() -> int:
    langs = ["ch_sim", "en"]
    model_dir = Path.home() / ".EasyOCR" / "model"
    model_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== EasyOCR 模型预下载 ===")
    print(f"目标目录: {model_dir}")
    print(f"语言:     {langs}\n")

    try:
        models = _collect_required_models(langs)
    except Exception as exc:
        print(f"❌ 读取 easyocr.config 失败: {exc!r}")
        print("   可能 easyocr 版本 ≠ 1.7.2；请把 easyocr 版本告诉我。")
        return 2

    print(f"需要下载 {len(models)} 个模型：")
    for kind, fname, _url in models:
        print(f"  [{kind}] {fname}")
    print()

    success = 0
    for kind, fname, url in models:
        # filename 通常带 .pth 后缀；EasyOCR 下载的是 .zip 然后解压出 .pth。
        # 这里我们按 EasyOCR 原逻辑：下载到 model_dir/<basename>.zip，解压。
        if (model_dir / fname).exists():
            print(f"[skip] {fname} 已存在")
            success += 1
            continue
        zip_name = url.rstrip("/").rsplit("/", 1)[-1]
        zip_path = model_dir / zip_name
        try:
            print(f"[fetch] {kind}: {fname}")
            _download_with_fallback(url, zip_path)
            if zip_path.suffix.lower() == ".zip":
                _unpack_zip(zip_path, model_dir)
            success += 1
        except Exception as exc:
            print(f"  ❌ 跳过 {fname}: {exc}")
            if zip_path.exists():
                zip_path.unlink()

    print(f"\n下载完成: {success}/{len(models)} 个成功")

    # 验证：让 EasyOCR 在 download_enabled=False 模式下加载，看本地模型够不够
    print("\n=== 验证模型本地加载 ===")
    try:
        import easyocr
        reader = easyocr.Reader(langs, gpu=False, download_enabled=False, verbose=False)
        print("✅ EasyOCR ready (本地模型够用)")
        # 跑一个极简识别确认推理路径通了（创建一张白图，应该返回空 list）
        import numpy as np
        blank = np.full((100, 200, 3), 255, dtype=np.uint8)
        result = reader.readtext(blank)
        print(f"✅ 推理路径通过 (空白图返回 {len(result)} 个候选，合理)")
        return 0
    except Exception as exc:
        print(f"❌ 验证加载失败: {type(exc).__name__}: {exc}")
        print("   说明还有模型没下到位。检查 ~/.EasyOCR/model/ 缺什么文件。")
        return 3


if __name__ == "__main__":
    sys.exit(main())
