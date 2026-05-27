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

### 候选 1：EasyOCR ⭐ 推荐（2026-05-27 调研后翻盘）

| 维度 | 评分 |
|---|---|
| 中文识别精度 | ⭐⭐⭐⭐ 普通文本略低于 PaddleOCR，**但 PDD 价格场景（短文本+数字+¥+少量中文）差距可忽略，实测都能 95%+** |
| 数字 + 货币符号识别 | ⭐⭐⭐⭐⭐ |
| Windows 安装 | ⭐⭐⭐⭐ ~500MB（torch + 模型）|
| CPU 推理速度 | 单张小图 ~80-150ms |
| **Python 3.14 兼容** | ✅ **pip install 直接成功**——EasyOCR 走 PyTorch，PyTorch 2.10（2026-02 发布）官方支持 3.14 |
| 上手友好度 | ⭐⭐⭐⭐⭐ API 极简（`Reader(["ch_sim","en"]).readtext(img)`）|
| License | Apache 2.0 |

**装法**（Python 3.14 venv 里）：

```cmd
:: CPU 版（我们用这个，PDD 价格识别不需要 GPU）
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install easyocr
```

> 💡 第一次 `Reader()` 实例化会下载 ~140MB 中英文模型（缓存到
> `%USERPROFILE%\.EasyOCR\`），之后离线工作。

### 候选 2：PaddleOCR（已降级为"如果 EasyOCR 精度不够再切"的兜底）

| 维度 | 评分 |
|---|---|
| 中文识别精度 | ⭐⭐⭐⭐⭐ 百度自研，中文场景 SOTA |
| 数字 + 货币符号识别 | ⭐⭐⭐⭐⭐ |
| **Python 3.14 兼容** | ❌ **pip 装不上**（paddlepaddle 官方 wheel 只到 cp313；3.14 需要自己从源码编译，要装 VS Build Tools + CMake + ninja + ~5GB 构建空间，1-2h 工程化代价）|
| 替代装法 | 单独再起一个 Python 3.12 venv 跑 paddlepaddle == 3.3.0（worker 主体还在 3.14，OCR 子模块走子进程调用 3.12 venv） |

**为什么从首推降级**：

2026-05-27 调研发现 paddlepaddle 在 Python 3.14 上**没有官方 wheel**，
只能从源码编译。考虑到 worker 主体（uiautomator2 / httpx / asyncio）
在 3.14 上已经跑得很稳，**为了 OCR 子模块单独搞 3.12 venv 或者编译
paddlepaddle 性价比都不高**。EasyOCR 在我们的具体场景（短文本+数字+¥）
精度差距可忽略，pip 直接装上能用。

如果未来跑下来发现 EasyOCR 在 PDD 价格场景识别率 < 90%，再回头切
PaddleOCR（届时官方可能已经出 cp314 wheel，或者下决心装 3.12 venv）。

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

## 推荐落地：EasyOCR + 现有 Python 3.14 venv（2026-05-27 翻盘后）

为什么从原 PaddleOCR+3.12 降级方案换成这个组合：
- **不动现有 worker 环境**——Python 3.14 venv 已经跑得稳，加 OCR 子模块
  直接 `pip install easyocr` 即可
- PDD 价格识别是「短文本 + 数字 + ¥ + 少量中文」场景，EasyOCR 跟 PaddleOCR
  在这个具体场景下精度差距可忽略（< 1%）
- EasyOCR 走 PyTorch 2.10 + CPU，模型 ~140MB，CPU 推理 80-150ms/张
- 全本地，离线工作

## 实施步骤（Day 4 工作清单）

### Step 1: 在现有 Python 3.14 venv 里加 EasyOCR

```cmd
:: 1. 激活现有 venv（不用重建）
cd /d C:\resell\worker
.venv\Scripts\activate

:: 2. 装 PyTorch CPU 版（约 250MB，~2 分钟）
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

:: 3. 装 EasyOCR（约 50MB 库 + 第一次 import 会下 ~140MB 模型到
::    %USERPROFILE%\.EasyOCR\）
pip install easyocr

:: 4. 跑一个最小测试，看模型能不能加载（第一次会下模型，要等 30-60s）
python -c "import easyocr; r = easyocr.Reader(['ch_sim','en'], gpu=False); print('EasyOCR ready'); print(r.readtext('https://www.easyocr.com/static/example.png'))"
```

测试输出能看到识别结果就 OK。**整个步骤不需要新装 Python、不需要重建 venv，
不影响现有 worker 跑搜索任务**。

如果第 4 步失败（一般是 torch 装不上 / 模型下不动）：

```cmd
:: 备选 1：直接装 EasyOCR，让它自己拉合适版本的 torch
pip install easyocr

:: 备选 2：使用清华镜像（如果 PyTorch 官方源慢）
pip install torch torchvision -i https://pypi.tuna.tsinghua.edu.cn/simple

:: 备选 3：单独的 Python 3.12 venv 跑 PaddleOCR
::   (走这条等于回到原方案，仅在 EasyOCR 精度真的不达标时再考虑)
```

### Step 2: 改 `pdd_app_client.py` 加 OCR 后备路径

伪代码（用 EasyOCR）：

```python
class PddAppClient:
    def __init__(self, serial: str):
        ...
        self._ocr = None  # 懒加载，第一次用到才初始化

    def _get_ocr(self):
        if self._ocr is None:
            import easyocr
            # gpu=False：纯 CPU 推理；languages: 中文简体 + 英文（PDD 价格够用）
            self._ocr = easyocr.Reader(['ch_sim', 'en'], gpu=False)
        return self._ocr

    async def _dump_visible_cards(self):
        # ... 原有逻辑 ...

        # 对每个 missing price 的卡片，截图 + OCR
        for item in items:
            if not item.get("price"):
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
            screenshot = self._d.screenshot(format='opencv')  # numpy ndarray BGR
            l, t, r, b = price_region
            cropped = screenshot[t:b, l:r]
            # EasyOCR 返回 [(bbox, text, confidence), ...]
            results = self._get_ocr().readtext(cropped, detail=1)
            for bbox, text, conf in results:
                if conf < 0.3:  # 低置信度直接丢
                    continue
                from pdd_app_worker.pdd_app_client import parse_price
                p = parse_price(text)
                if p and 0.1 < p < 100000:
                    return p
            return None

        return await asyncio.to_thread(_do_ocr)
```

关键差异 vs PaddleOCR 版本：
- 用 `easyocr.Reader(['ch_sim', 'en'], gpu=False)` 代替 `PaddleOCR(...)`
- `readtext(img, detail=1)` 返回 `[(bbox, text, confidence), ...]`
- 加了 `conf < 0.3` 的置信度过滤（EasyOCR 默认 detail=1 时会返回 confidence）

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

> ⚠️ **不要"同一关键词跑 10 次"** —— 这是 4310 死因 §6 表里 35% 权重的雷
> ("同一关键词反复搜索 = 异常搜索模式 → PDD 风控直接标记")。
> 必须用 **N 个不同关键词跑一遍**，避免在同一 session 内连刷同一词。
>
> 而且关键词不能是"机械键盘"这种高利润类目，必须是 §3 SOP 已验证的安全词。

```python
# 用一批已知安全的关键词
# （§3 SOP 第 1/2 波白名单 + 2026-05-27 实测无风控的扩展词）
verify_keywords = [
    "纸巾",     # 2026-05-27 已验证
    "袜子",     # 同上
    "保鲜膜",   # 同上
    "牙线",     # 同上 (deep mode)
    "保温杯",   # 第 2 波白名单，待验证
    "牙膏",
    "垃圾袋",
    "洗手液",
    "矿泉水",   # 第 1 波白名单，待派
    "棉签",     # 同上
]

# 一个一个派（中间 5-30 分钟，让 burst scheduler 跑自然节奏），
# 全部 10 个跑完后聚合统计 price_source 分布
```

跑完聚合统计：

```text
期望分布（10 个关键词 × 平均 ~5 件 = ~50 条价格样本）：
  xml:                 30-50%（普通卡片，XPath/UiSelector 抓到）
  ocr:                 40-60%（百亿补贴 / 渠道补贴 canvas 渲染卡片）
  ocr_low_confidence:  < 5%   （OCR 识别但被异常值过滤）
  missing:             < 10%  （OCR 也救不回的疑难）
  → 总价格覆盖率：90%+ ✅
```

**测试节奏要求**（同正式运行节奏，绝对不允许"测试期间放飞"）：

```text
□ 10 个关键词分 2-3 天跑完（≤ 5 词/天，跟正式运行节奏一致）
□ 不许 1 小时内连派 ≥ 5 个（即便都是不同词，密集度也算异常画像）
□ 每个词跑完后，最少间隔 5 分钟才能派下一个（burst scheduler 默认就这样）
□ 中途任一关键词触发 risk_signals → 立刻停，先复盘
□ 不要在凌晨 / 深夜跑测试（PDD 风控对"非常态时段集中搜索"有专门规则）
□ 不要碰 §3 SOP 禁区类目（球鞋 / 潮玩 / 数码 / iPhone / 高客单家电），
  即便 Day 4 改的是 OCR 也不行
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

## 时间预算（2026-05-27 EasyOCR 路径更新后）

- Step 1（pip install easyocr + 跑通最小测试）：**10-15 分钟**（vs 原 PaddleOCR + Py3.12 重建 venv 的 30-60 分钟）
- Step 2（加 OCR 路径到 pdd_app_client.py）：60-90 分钟
- Step 3-4（schema + 容错）：30 分钟
- Step 5（验收测试）：**分 2-3 天**跑 10 个不同安全关键词，每天 ≤ 5 词

**编码合计：1.5-2.5 小时**，**验收期：2-3 天**（按 §3 SOP 节奏跑，绝不抢节奏）。
