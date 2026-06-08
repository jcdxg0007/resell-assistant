# PDD Step 3 设计与迁移方案（持久化 + 详情接入 + item_key 升级）

> 对应 roadmap `docs/PDD-自建采集-roadmap.md` §11.2 Phase 2。本文档供评审，
> **暂不动代码**。评审通过后按 §7 的步骤分批实现。
>
> 状态：**已定稿、批 1 实现中**（2026-06-08）。Step 2c（详情字段 OCR 抽取）已上线，
> `browse_detail_and_harvest` 现产出 `out["fields"]`（店铺名/评价数/已拼/好评率/
> 上榜/口碑标签/规格/券后价等）+ `goods_id / thumb_url / detail_url`。

## 决策（已定稿）

- **D1 = C**：新建 `pdd_goods`（商品级静态详情，按 goods_id upsert）+ `product_sightings`
  加 `goods_id` / `sold_count` / `coupon_price` 三列（时序信号）。
- **D2 = 保持**：`item_key` 仍为 `pdd:<sha1(clean_title)>`，**新增 goods_id 列作附加精确
  归并维度**，读取时 goods_id 优先归并、无则退回 item_key（不断历史）。
- **D3 = 接进现有 deep 模式**（非新增 mode）：关键词库里标记 deep 的词，搜索后触发
  `browse_results_with_dips`；**默认进 3 个详情，K 值留前端开关可调**；紧急/手动搜
  保留"深度搜"选项（可指定 deep + K）。属**批 2**。
- **D4**：`sold_count` + `coupon_price` 进每日 `product_sightings`（随日变）；`shop_name /
  comment_count / praise_rate / rank_badges / review_tags / specs / discount / thumb_url /
  detail_url` 进 `pdd_goods`（商品级，`specs/rank_badges/review_tags` 用 JSON 列）。

---

## 0. 现状（精确代码位置）

| 环节 | 位置 | 现状 |
|---|---|---|
| 详情收割 | `worker/pdd_app_worker/pdd_app_client.py::browse_results_with_dips` / `browse_detail_and_harvest` | **仅 smoke 脚本调用**，未接进生产 `search()` |
| 生产采集 | 同文件 `search(mode=...)` | `mode="deep"` 只是**搜索前 warmup 画像**，与详情 dip 无关 |
| worker→后端 | `worker/.../main.py` 返回 `result.items` | item 字段：`title / price / sales / badges / image(base64)` 等列表级 |
| 落库入口 | `backend/app/services/pdd_autobatch.py::persist_pdd_result` | 落 `pdd_search_runs` + 调 `_record_pdd_sightings` |
| 跨天观测写 | 同文件 `_record_pdd_sightings` → `sightings.record_sightings` | 只写 `item_key/title/price/heat/image_url` |
| 观测模型 | `backend/app/models/product_sighting.py::ProductSighting` | 列：`platform/item_key/seen_date/keyword/title/price/heat/image_url` |
| item_key | `backend/app/services/selection/sightings.py::pdd_item_key` | `pdd:<sha1(clean_title)[:32]>`（无稳定 id） |
| 消费 | `backend/app/api/v1/selection.py::_attach_sighting_stats` | 十维度分析按 item_key 附 first/last/days/history |

---

## 1. 数据模型：详情字段存哪（核心决策）

新详情字段：`goods_id, shop_name, comment_count, sold_count, praise_rate,
rank_badges(list), review_tags(list), specs(dict), coupon_price, discount,
thumb_url, detail_url`。

### 候选方案

- **A. 给 `product_sightings` 平铺加列**
  - ＋ 简单、跟"每日观测"语义一致（可看店铺评分/评价数跨天变化）
  - － 列变多；`rank_badges/review_tags/specs` 是结构化，得用 JSON/Text；只有被 dip
    的 K 条有值，其余 NULL（稀疏）
- **B. `product_sightings` 加单个 `detail JSONB` 列**
  - ＋ 一列容纳所有详情字段，弹性大、迁移轻
  - － 不利于 SQL 直接查/聚合（要 `->>`）；前端取值要约定 schema
- **C. 单开 `pdd_goods` 详情表（按 `goods_id` 主键，存"最新一次"详情）**
  - ＋ 详情是"商品级、相对静态"的（店铺/规格/品牌），天然按 goods_id 归一，不随
    日重复；`product_sightings` 保持轻量只管"每日价格/热度趋势"
  - － 多一张表 + 一次 join；list-level 无 goods_id 的卡不进此表

### 推荐：**C + 少量 A**

- **`pdd_goods`（新表，商品级最新详情）**：`goods_id`(PK) / `shop_name` /
  `comment_count` / `praise_rate` / `rank_badges`(JSON) / `review_tags`(JSON) /
  `specs`(JSON) / `thumb_url` / `detail_url` / `first_harvested_at` /
  `last_harvested_at` / `last_title` / `last_price`。upsert by goods_id。
- **`product_sightings` 加两列（A 的最小集）**：`goods_id`(String, nullable, index)
  + `sold_count`(Integer, nullable)。
  - `goods_id`：让"每日观测"能挂到商品级详情，并作为**精确身份**（见 §2）。
  - `sold_count`：随日变化的热度信号，适合留在每日快照里（`coupon_price` 同理可选）。
- 理由：把"静态商品属性"（店铺/规格/标签）与"每日时序"（价/销量）**分层**，
  既不让 sightings 长胖，又能按 goods_id 精确归并与展示。

> 若你更想要"零新表、迁移最轻"，退而求其次选 **B**（sightings 加 `detail JSONB`），
> 实现量最小，但牺牲商品级归一与查询便利。**此项需你拍板（决策 D1）。**

---

## 2. item_key 升级为 `pdd:<goods_id>` —— 连续性陷阱（重点）

roadmap 原话："goods_id 落地后可升级为 `pdd:<goods_id>` 更精确"。但**直接替换会割裂
历史**：同一商品过去若干天记在 `pdd:<sha1(title)>` 下，改用 `pdd:<goods_id>` 后变成
两个 key，跨天趋势（first_seen/days_seen/history）断成两段。且**只有被 dip 的商品才有
goods_id**，list-level 卡仍只能用标题哈希 → 同一商品"被 dip 当天"和"没 dip 当天"落不
同 key，history 碎裂更严重。

### 推荐策略：**item_key 不变，goods_id 作为附加精确维度**

- `product_sightings.item_key` **保持** `pdd:<sha1(clean_title)>`（向后兼容、list-level
  与 detail 都能落、历史不断裂）。
- **新增 `goods_id` 列**：dip 到就填。
- `gather_sighting_stats` / `_attach_sighting_stats` 读取时：**优先按 goods_id 归并**
  （同一 goods_id 的多天观测合并成一条 history，即使它们 item_key 因标题微调而不同），
  无 goods_id 时退回 item_key 归并。→ 既精确又不丢历史，且能修复"标题被卖家改字导致
  同款被当两款"的老问题。
- 未来可选：维护一张 `item_key ↔ goods_id` 映射做回填，把历史 title-hash 观测并到
  goods_id 名下（Phase 2.5，非必须）。

> **决策 D2**：接受"item_key 不变 + goods_id 附加归并"（推荐，安全）？还是坚持 roadmap
> 的"item_key 直接升级为 pdd:<goods_id>"（更纯粹但断历史、且 list-level 无法用）？

---

## 3. 写入路径改动

1. **worker `browse_detail_and_harvest`**：`out` 已含 `fields`+`goods_id`+`thumb_url`+
   `detail_url`，无需改。
2. **worker `search()`（§4 接入后）**：把每条 dip 的 `goods_id/fields` 合并进对应
   `result.items[i]`（按标题匹配 dip 的 `title`）。新增 item 字段：
   `goods_id / thumb_url / detail_url / detail`(=fields)。
3. **后端 `persist_pdd_result` → `_record_pdd_sightings`**：
   - record 里加 `goods_id`（it.get("goods_id")）、`sold_count`（detail.sold_count）。
   - 若选方案 C：再 upsert `pdd_goods`（仅 it 有 goods_id 时）。
4. **`record_sightings`**：values/on_conflict set_ 增加 `goods_id/sold_count`
   （on_conflict 时也要更新，便于"先 list 落、后 dip 补 goods_id"的二次刷新）。

---

## 4. 把 dip 收割接进生产 `search()`（碰风控暴露面）

**这是唯一增加详情页暴露面的一步，触发条件需你定。**

- **触发方式**：建议**新增独立 mode**（如 `mode="deep_harvest"`）或在 payload 加
  `harvest_dips: int`（K 值，0=不收割）。**不要**默认对所有 deep 任务开 dip。
- **频率/预算**：复用现有每日配额/burst 调度；建议每日仅对**少量关键词**或**人工指定**
  时触发，K 默认 2-3。dip 本身已是"拟人逛 + 概率递减"，但仍要纳入风控账本。
- **回流**：`browse_results_with_dips` 返回的 `harvested`（含 fields）按 title 合并进
  `result.items`；list-level 仍走原 `search` 的卡片 dump（见 §5）。

> **决策 D3**：dip 接入的触发与频率——(a) 新 mode + 人工/少量词触发（推荐）；
> (b) 给 deep 任务按低概率自动加 dip；(c) 暂不接生产、只先做后端 schema。

---

## 5. 深度模式"列表级路过卡"落库

dip 流程里 `_dump_visible_cards` 已逐屏 dump 路过的卡。目标：把这些 list-level 卡
**也按 sighting 落**（与 fast 模式一致），不只 K 条详情。

- 实现：`browse_results_with_dips` 累积去重的"路过卡"列表，连同 `harvested` 一起回传；
  `search()` 把两者合并进 `result.items`（dip 到的带 detail，路过的仅 list-level）。
- 去重：按归一标题；同一卡若既被路过又被 dip，合并为一条（带 detail）。

---

## 6. 消费侧（选品 API + 前端）

- `_attach_sighting_stats`：归并逻辑改为 goods_id 优先（§2）；payload item 增加
  `goods_id / shop_name / sold_count / praise_rate / rank_badges / review_tags /
  specs / detail_url`（从 `pdd_goods` join 或 sightings 列取）。
- 前端（§11.3「找原品」）：详情字段展示 + "抓链接/找原品"按钮（有 goods_id 时直接拼
  `https://mobile.yangkeduo.com/goods.html?goods_id=XXX`）。**前端属 Step 4，本方案
  只预留字段，不在第一批实现。**

---

## 7. 迁移与上线步骤（分批，建议顺序）

1. **批 1（纯后端、零风控）**：
   - alembic 迁移：`product_sightings` 加 `goods_id`(String64,nullable,index) +
     `sold_count`(Int,nullable)；若选 C 再建 `pdd_goods` 表。
   - `record_sightings` + `_record_pdd_sightings` 落新字段（worker 暂未回传时全 NULL，
     无副作用）。
   - `gather_sighting_stats`/`_attach_sighting_stats` 加 goods_id 归并 + 透出字段。
   - 上线验证：现有采集不受影响，新列存在且 NULL。
2. **批 2（worker 接入，碰风控）**：按 D3 定的方式接 dip 收割进 `search()`，字段回流。
3. **批 3**：list-level 路过卡落库。
4. **批 4（Step 4）**：前端展示 + 找原品按钮。

**回滚**：每批独立可回滚；批 1 的迁移仅加列/加表，回滚 = drop column/table（无数据
依赖）。**风险**：批 2 增加详情页暴露面——务必先在 smoke/单设备灰度，观察风控信号。

---

## 8. 待确认决策清单

- **D1 详情字段存储**：C（pdd_goods 表 + sightings 加 goods_id/sold_count，推荐）／
  B（sightings 加 detail JSONB，最省）／A（全平铺加列）。
- **D2 item_key**：保持 title-hash + goods_id 附加归并（推荐，不断历史）／直接升级为
  pdd:<goods_id>（断历史、list-level 不适用）。
- **D3 生产 dip 接入**：新 mode + 人工/少量词触发（推荐）／deep 任务低概率自动加 dip／
  暂不接、只先做批 1。
- **D4 字段取舍**：`coupon_price/discount` 要不要进每日 sightings（随日变）？`specs`
  存 JSON 是否够（前端只读不聚合）？

> 评审：你对 D1–D4 给个选择，我据此把批 1 的迁移 + 代码改动一次落到位。
