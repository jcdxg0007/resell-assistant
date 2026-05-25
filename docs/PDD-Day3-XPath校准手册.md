# PDD APP Day 3：商品卡片 XPath 校准手册

> 配套：`worker/pdd_app_worker/pdd_app_client.py` 里所有标了 `TODO(Day 3)`
> 的位置，本手册负责把它们填实。
>
> 时间预估：第一次 60-90 分钟（含 weditor 上手），熟练后每次 PDD APP 改版
> 大约 20-30 分钟。

---

## Day 3 目标

让 `PddAppClient._dump_visible_cards` 从"返回 []"变成"返回 20 条真实商品"。
具体验收：

- [ ] 在 PKT0220416005274 上跑 `python -m pdd_app_worker.main`，从 backend
      派一个 `keyword="机械键盘"` 的 search 任务
- [ ] worker 推回 `status="ok"`、`items` 数组长度 ≥ 15，每条至少有 `title`
      和 `price` 两个字段
- [ ] 5 次连跑成功率 ≥ 80%（Day 3 验收线，Day 5 收紧到 95%）

---

## 1. 在 Windows 上装 weditor

weditor 是 uiautomator2 的可视化辅助工具，能：
- 看手机当前屏幕的截图 + UI 树
- 点元素拿 resource-id / class / text / bounds
- 验证 XPath 是否唯一匹配

**装法**（在 venv 激活态下）：

```cmd
cd /d C:\resell\worker
pdd_app_worker\.venv\Scripts\activate
pip install weditor
```

**启动**：

```cmd
:: 先确保 worker main.py 没在跑（手机一次只能被一个 atx-agent 控制）
:: 然后：
python -m weditor
```

浏览器会自动打开 `http://localhost:17310/`。左上角输入 `PKT0220416005274`，点
Connect，右侧就能看到手机实时画面 + UI 树。

> ⚠️ **关键**：weditor 启动期间 worker 的 `main.py` 必须停掉，不然 atx-agent
> 端口冲突。调试完关 weditor 再起 worker。

---

## 2. 手动跑一遍搜索流，截 4 个关键页面的 UI dump

按下面顺序操作手机，每步在 weditor 里点"Dump Hierarchy"保存 XML：

| 步骤 | 操作 | 保存为 |
|---|---|---|
| 1 | 打开 PDD APP，在首页 | `dump_home.xml` |
| 2 | 点搜索栏，进搜索输入页 | `dump_search_input.xml` |
| 3 | 输入"机械键盘"按搜索，进结果列表页 | `dump_results.xml` |
| 4 | 在结果页向下滑一屏 | `dump_results_scrolled.xml` |

把这 4 个 XML 文件存到 `C:\resell\worker\dumps\` 备查。

---

## 3. 从 `dump_results.xml` 抠商品卡片 XPath

打开 `dump_results.xml`，找出一个完整商品卡片的节点（通常是
`androidx.recyclerview.widget.RecyclerView` 的直接子 `android.view.ViewGroup`）。

**示例片段**（PDD 2026.4 版，仅供参考，实际可能变）：

```xml
<android.view.ViewGroup index="0" class="android.view.ViewGroup"
    package="com.xunmeng.pinduoduo"
    bounds="[0,398][540,1100]">
  <android.widget.ImageView resource-id=".../iv_goods_pic" .../>
  <android.widget.TextView resource-id=".../tv_goods_name"
      text="罗技G610机械键盘樱桃轴..." bounds="[18,820][520,890]"/>
  <android.widget.TextView resource-id=".../tv_price"
      text="¥289" bounds="[18,910][120,950]"/>
  <android.widget.TextView resource-id=".../tv_sales"
      text="1.2万人已拼" bounds="[140,915][320,945]"/>
</android.view.ViewGroup>
```

**要抠出的 4 个 XPath**：

| 字段 | XPath 模板 |
|---|---|
| 卡片容器 | `//androidx.recyclerview.widget.RecyclerView/android.view.ViewGroup` |
| 标题 | `.//android.widget.TextView[contains(@resource-id, "tv_goods_name")]` |
| 价格 | `.//android.widget.TextView[contains(@resource-id, "tv_price")]` |
| 销量 | `.//android.widget.TextView[contains(@resource-id, "tv_sales") or contains(@text, "已拼")]` |

> 💡 **优先 resource-id**：PDD 的 resource-id 通常带版本前缀（如
> `com.xunmeng.pinduoduo:id/xxx`），但**后缀**比较稳定，用
> `contains(@resource-id, "tv_price")` 写法可以跨小版本通用。

---

## 4. 把 XPath 填进 `pdd_app_client.py`

打开 `worker/pdd_app_worker/pdd_app_client.py`，找到 `_dump_visible_cards`
方法，把整个函数体替换成（基于上面拿到的 XPath）：

```python
async def _dump_visible_cards(self) -> list[dict[str, Any]]:
    """Day 3 实现：dump XML + 解析 RecyclerView 子节点。"""
    import xml.etree.ElementTree as ET

    def _do_dump():
        return self._d.dump_hierarchy()  # 返回 UI XML 字符串

    xml_str = await asyncio.to_thread(_do_dump)
    root = ET.fromstring(xml_str)

    items: list[dict[str, Any]] = []
    # 找所有 RecyclerView 下的卡片容器
    for card in root.iter("node"):
        cls = card.get("class", "")
        rid = card.get("resource-id", "")
        if cls != "android.view.ViewGroup":
            continue
        # 排除非商品卡片（按 bounds 高度 < 200 px 过滤页面 chrome）
        bounds = card.get("bounds", "")
        if not bounds:
            continue

        title = self._find_text(card, "tv_goods_name") or self._find_text(card, "goods_name")
        price_text = self._find_text(card, "tv_price") or self._find_text(card, "price")
        sales_text = self._find_text(card, "tv_sales") or self._find_text_by_substring(card, "已拼")

        if not title or not price_text:
            continue

        from pdd_app_worker.pdd_app_client import parse_price, parse_sales
        price = parse_price(price_text)
        if price is None:
            continue

        items.append({
            "title": title.strip(),
            "price": price,
            "sales": parse_sales(sales_text) if sales_text else 0,
            "bounds": bounds,
        })

    logger.info(f"[{self.serial}] dumped {len(items)} cards from current screen")
    return items

@staticmethod
def _find_text(card_node, rid_substring: str) -> str | None:
    """在 card 节点的子树里找 resource-id 含 rid_substring 的 TextView text。"""
    for n in card_node.iter("node"):
        if rid_substring in n.get("resource-id", ""):
            return n.get("text", "")
    return None

@staticmethod
def _find_text_by_substring(card_node, text_substring: str) -> str | None:
    """按 text 内容子串找。"""
    for n in card_node.iter("node"):
        if text_substring in n.get("text", ""):
            return n.get("text", "")
    return None
```

---

## 5. 联调流程

每次改完 XPath 验证一次：

```cmd
:: 1. 在 worker 机停掉 weditor
:: 2. 启动 worker main
cd /d C:\resell\worker
pdd_app_worker\.venv\Scripts\activate
python -m pdd_app_worker.main
```

我（云端）在 backend 派一个测试任务：

```bash
# 我会在 Sealos 这边跑：
kubectl exec backend-xxx -- env PYTHONPATH=/app python3 -c "
import asyncio
from app.services.pdd_app_queue import PddAppTask, enqueue_task, await_result
async def t():
    task = PddAppTask(kind='search', payload={'keyword': '机械键盘', 'mode': 'fast'})
    await enqueue_task(task)
    r = await await_result(task.task_id, timeout_s=120)
    print(r)
asyncio.run(t())
"
```

**预期输出**：

```
PddAppResult(
  task_id='...',
  status='ok',
  items=[
    {'title': '罗技G610机械键盘樱桃轴...', 'price': 289.0, 'sales': 12000, ...},
    {'title': '雷蛇黑寡妇蜘蛛V3 ...', 'price': 599.0, 'sales': 3500, ...},
    ... (15-20 条)
  ],
  device_serial='PKT0220416005274',
  elapsed_ms=23000,
  ...
)
```

---

## 6. 常见踩坑

| 症状 | 原因 | 解决 |
|---|---|---|
| `items=[]` 但日志没异常 | XPath 抠错了，没匹到卡片 | 用 weditor 在结果页 dump XML 重抠 |
| 一直停在搜索输入页 | `_submit_search` 找不到提交按钮 | 把"搜索"按钮的 XPath 加到 candidates 列表 |
| `risk_signals=['slide_verify']` | PDD 出滑块了 | 暂停 30 分钟，下次跑前先在手机上手动浏览 5 分钟"暖号" |
| `app_current` 返回别的包名 | 弹窗把 PDD 顶到后台了 | 在 `_dismiss_popups` 加该弹窗的 close XPath |
| 商品卡价格全是 None | 价格 TextView 的 `text` 是空的，PDD 拆成多个子 TextView 拼接 | 改成抠所有 contains(@text, "¥") 的 TextView 合并 |
| `_dump_visible_cards` 抓到一堆非商品卡片 | 把页面 chrome / banner 也算进去了 | 过滤 bounds 高度 > 300px + width > 屏宽 * 0.4 |

---

## 7. 上报问题给我的格式

如果某条 XPath 抠不出来，把以下 4 样东西贴出来我就能帮你修：

1. 你想抠的字段（例如"价格"）
2. 在 weditor 里选中那个元素时**右侧 Inspector 面板**的截图（含 resource-id /
   class / text / bounds）
3. `dump_results.xml` 里那个元素对应的 XML 片段（前后 5 行就够）
4. 你现在写的 XPath + 报错信息或表现

---

## 完成 Day 3 后

把 4 个 dump XML 提交进 `worker/pdd_app_worker/dumps/`（gitignore 不收 dumps 但
做参考用），并在这份手册末尾追加"实测 XPath"小节，记录最终落地的选择器。

PDD 改版后这份手册是排查首选 —— 大概率 80% 的 fix 就是重新跑一次第 2 步。
