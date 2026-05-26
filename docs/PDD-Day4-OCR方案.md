# PDD APP Day 4：OCR 兜底方案

> 背景：Day 3 实测发现 PDD「百亿补贴」卡片的价格用 Canvas/Drawable 自绘，
> uiautomator2 dump_hierarchy 完全看不到。这恰恰是转卖比价最重要的数据
> （平台补贴价、跨平台最低价对标）。必须用 OCR 把图像里的价格读出来。
>
> 详细背景见 `docs/PDD-自建采集-roadmap.md` 第 7 条踩坑记录。

## 目标

- 价格覆盖率 ≥ 90%（无论卡片是否带百亿补贴）
- 单任务耗时增量 < 5s（vs Day 3 baseline 30s）
- OCR 在 worker 本地跑，不依赖云端 API（避免外网延迟 + 隐私 + 成本）

## 方案选型

### 候选 1：PaddleOCR ⭐ 推荐

| 维度 | 评分 |
|---|---|
| 中文识别精度 | ⭐⭐⭐⭐⭐ 百度自研，中文场景 SOTA |
| 数字 + 货币符号识别 | ⭐⭐⭐⭐⭐ 训练数据覆盖 |
| Windows 安装 | ⭐⭐⭐ 需要 ~500MB（模型 + paddle 框架）|
| CPU 推理速度 | 单张小图 ~50-100ms（足够）|
| Python 3.14 兼容 | ⚠️ 待验证，目前官方支持到 Python 3.12 |
| 维护活跃度 | ⭐⭐⭐⭐⭐ 百度主推 |
| License | Apache 2.0，商用友好 |

**装法**：

```cmd
pip install paddlepaddle paddleocr
```

如果 Python 3.14 跑不起来，**降级到 Python 3.12 跑 worker**（这个我们之前
就讨论过，Python 3.14 太新，多数 ML 库都没适配）。

### 候选 2：EasyOCR

| 维度 | 评分 |
|---|---|
| 中文识别精度 | ⭐⭐⭐⭐ 略低于 PaddleOCR |
| 安装大小 | 类似 ~500MB |
| Python 3.14 兼容 | ⚠️ 同样未验证 |
| 上手友好度 | ⭐⭐⭐⭐⭐ API 极简 |

**装法**：`pip install easyocr`

### 候选 3：Tesseract + 中文语言包

| 维度 | 评分 |
|---|---|
| 中文识别精度 | ⭐⭐ 普通图够用、广告字体打折 |
| 安装大小 | ~50MB（轻量）|
| 速度 | 最快（C++ 实现）|
| 上手 | 要装 Windows 二进制 + 配 PATH |

适合"价格只有数字"的简单场景，但 PDD 价格周围有"¥"+"百亿补贴"+slogan 干扰，
精度不够。**不推荐**。

### 候选 4：云 API（阿里云/百度云/华为云 OCR）

| 维度 | 评分 |
|---|---|
| 精度 | ⭐⭐⭐⭐⭐ 商业级 |
| 速度 | 100-300ms/张（含网络）|
| 成本 | 约 ¥1.5/千次（百度通用文字 OCR）|
| 隐私 | ⚠️ 图片上云 |
| 依赖外网 | 是 |

只在本地 OCR 不达标时作为兜底。

## 推荐落地：PaddleOCR + Python 3.12 venv

为什么选这个组合：
- PDD 价格识别是典型「短文本 + 中文 + 数字」场景，PaddleOCR 最稳
- Python 3.12 是 PaddleOCR + uiautomator2 + httpx 共同的甜点版本
- 全本地，不依赖云
- 一次性 500MB 模型下载，之后离线工作

## 实施步骤（Day 4 工作清单）

### Step 1: 给 worker 装 Python 3.12（如果 3.14 装不上 PaddleOCR）

```cmd
:: 1. 下载 Python 3.12.8（已在 Day 0 文档里讨论过）
::    https://www.python.org/downloads/release/python-3128/
:: 2. 安装时勾选 "Add python.exe to PATH" + "Install for all users"

:: 3. 重建 venv
cd /d C:\resell\worker
rmdir /s /q pdd_app_worker\.venv
"C:\Program Files\Python312\python.exe" -m venv pdd_app_worker\.venv
pdd_app_worker\.venv\Scripts\activate

:: 4. 装基础依赖 + paddleocr
pip install -r pdd_app_worker\requirements.txt
pip install paddlepaddle paddleocr
```

### Step 2: 改 `pdd_app_client.py` 加 OCR 后备路径

伪代码：

```python
class PddAppClient:
    def __init__(self, serial: str):
        ...
        self._ocr = None  # 懒加载，第一次用到才初始化
    
    def _get_ocr(self):
        if self._ocr is None:
            from paddleocr import PaddleOCR
            self._ocr = PaddleOCR(
                use_angle_cls=False,
                lang='ch',
                show_log=False,
            )
        return self._ocr
    
    async def _dump_visible_cards(self):
        # ... 原有逻辑 ...
        
        # 对每个 missing price 的卡片，截图 + OCR
        for item in items:
            if item["price"] == 0:
                ocr_price = await self._ocr_card_price(item["bounds"])
                if ocr_price:
                    item["price"] = ocr_price
                    item["price_source"] = "ocr"
                else:
                    item["price_source"] = "missing"
            else:
                item["price_source"] = "xml"
        
        return items
    
    async def _ocr_card_price(self, title_bounds):
        """从标题 bounds 推导价格区域，裁图，OCR。"""
        # 价格通常在标题正下方 50-200px
        x1, y1, x2, y2 = title_bounds
        price_region = (x1, y2 + 50, x2, y2 + 200)
        
        def _do_ocr():
            screenshot = self._d.screenshot(format='opencv')
            # 裁剪
            x1, y1, x2, y2 = price_region
            cropped = screenshot[y1:y2, x1:x2]
            # OCR
            result = self._get_ocr().ocr(cropped, cls=False)
            for line in result[0] if result else []:
                text = line[1][0]
                from pdd_app_worker.pdd_app_client import parse_price
                p = parse_price(text)
                if p and 0.1 < p < 100000:
                    return p
            return None
        
        return await asyncio.to_thread(_do_ocr)
```

### Step 3: 加 `price_source` 字段到 result schema

让 backend 知道这个价格是从 XML 拿到还是 OCR 拿到的，方便做置信度评估。

```python
# pdd_app_queue.py 里 PddAppResult.items 字段已经是 list[dict]，
# 直接在 dict 里加 "price_source": "xml" | "ocr" | "missing"
```

### Step 4: 准备 OCR 容错策略

| 失败场景 | 处理 |
|---|---|
| OCR 识别为空 | price_source="missing"，不入库 |
| OCR 识别成 "¥" 没数字 | 同上 |
| OCR 识别成离谱数字（如 ¥0.01 或 ¥99999） | 标记 price_source="ocr_low_confidence"，需要 backend 二次校验 |
| OCR 进程崩溃 | 整个 task 不挂；该商品 price_source="ocr_error"，继续下一个 |

### Step 5: 验收测试

```bash
# 跑同一个关键词 10 次
for i in 1..10:
    派任务 keyword="机械键盘"
    统计 price_source 分布

# 期望分布：
# xml: 30-50%（非补贴卡片）
# ocr: 40-60%（百亿补贴卡片）
# missing: < 10%（OCR 也救不回的疑难杂症）
# 总有价格覆盖率：90%+ ✅
```

## 反对意见 / 风险

- **OCR 假阳性**：PDD 卡片图里的促销文案（"¥99 拼"、"立减¥10"）可能被 OCR 误抓
  → 解决：限制 OCR 区域只在标题正下方 ~150px 高度的窄带，避开图片主体
- **截图分辨率**：1080x2400 截图 + 多卡片 OCR 单次可能要 1-2s
  → 缓解：只对 missing 卡片 OCR，不全屏；fast 模式预期 ≤ 5s 增量
- **PaddleOCR 冷启动**：第一次 init 模型加载 3-5s
  → 缓解：worker 启动时预热（main.py 启动阶段加 `_ocr.ocr(blank_img)`）

## 备选：如果 PaddleOCR 在 worker 上跑不起来

按降级顺序：
1. 试 EasyOCR
2. 切百度云 OCR API（需要申请 access_key，月费约 ¥50-100 看用量）
3. 改方案 C（点详情页，价格 100% 准但慢，fast 模式不可用）

## 时间预算

- Step 1（装环境）：30-60 分钟
- Step 2（加 OCR 路径）：60-90 分钟
- Step 3-4（schema + 容错）：30 分钟
- Step 5（验收测试）：30 分钟

**合计：2.5-4 小时**，加缓冲一个工作日内 Day 4 能完成。
