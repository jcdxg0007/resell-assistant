"""详情页 OCR 文本 → 结构化字段抽取（§11.2 Step 2b）。

输入是 ``ocr.extract_text_blocks`` 跨多屏聚合后的文本块列表（每块含
text / conf / cx / cy）。这里**只做纯文本逻辑**（不碰设备、不碰 OCR 引擎），
方便单测 + 离线对着已存的 ``screen_*_ocr.txt`` 反复迭代规则。

真机 2026-06-08 OCR 实测要点（据 dip01 真彩 / dip02 风达百货 两条交叉校准）：
- 设备 1080×2400；底部导航 / 顶部吸顶栏文字每屏重复 → 需按 text 去重
- 常见 OCR 错字：夭→天、己→已、半→¥（拼单按钮的 ¥ 常被读成"半"）
- 读得稳：商品评价(N) / 已拼N件 / 店铺名(进店旁) / 店铺评价(N) / 畅销榜 /
  口碑标签云 / 规格栅格（标签-值上下对齐）
- 读不稳：券后价（拼单按钮自绘、conf 极低）→ best-effort，列表页卡片价才是主价

字段语义：
- comment_count    商品评价数（本商品）
- sold_count       已拼件数（本商品，吸顶栏）
- shop_name        店铺名
- shop_review_count 店铺评价数
- rank_badges      上榜/榜单短语
- review_tags      口碑标签云（高频好评短语）
- praise_rate      好评率（%）
- specs            规格属性 {标签: 值}
- coupon_price     券后实付价（best-effort，常 None）
- discount         立减/已优惠金额（元）
"""
from __future__ import annotations

import re
from typing import Any

# 跨屏聚合时给每屏 cy 加的偏移步长：每屏 cy 各自从 0 重计（屏高 ~2400），
# 加 idx*STRIDE 保证全局 y 序 = 屏序 + 屏内 y，且不同屏的块不会被"上下相邻"
# 误配（规格栅格/店铺名锚定都依赖同屏纵向相邻）。
SCREEN_CY_STRIDE = 100_000


# ── OCR 错字归一（仅用于"匹配"，不改原始展示文本） ──────────────
_OCR_FIX = str.maketrans({"夭": "天", "己": "已", "彐": "已"})


def _norm(s: str) -> str:
    return (s or "").translate(_OCR_FIX)


# 中文数量解析：吃下 "4,949" / "1.8万" / "900万+" / "3668.1万+" → int
_COUNT_RE = re.compile(r"([\d][\d,，.]*)\s*(万|亿)?")


def parse_count(s: str) -> int | None:
    """把 OCR 出的数量串解析成整数。失败返回 None。

    例：'4,949'→4949；'1.8万'→18000；'900万+'→9000000；'3668.1万+'→36681000
    """
    if not s:
        return None
    m = _COUNT_RE.search(s.replace("，", ","))
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        val = float(num)
    except ValueError:
        return None
    unit = m.group(2)
    if unit == "万":
        val *= 10_000
    elif unit == "亿":
        val *= 100_000_000
    return int(round(val))


def _dedup_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """跨屏聚合后按 (归一文本) 去重，保留首次出现（吸顶栏/底栏每屏重复）。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for b in blocks:
        key = _norm((b.get("text") or "").strip())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


# 规格栅格的标签判定（PDD"商品详情"下的属性表）
_SPEC_LABEL_RE = re.compile(
    r"^(是否.+|.+地$|品牌$|风格$|材质$|颜色$|笔头.*|适用.*|净含量$|规格$|"
    r"容量$|尺寸$|产地$|型号$|货号$|分类$|类型$|功能$)"
)


def extract_detail_fields(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """跨屏聚合的 OCR 文本块 → 结构化详情字段。所有字段 best-effort，取不到=None/空。"""
    blocks = [b for b in blocks if (b.get("text") or "").strip()]
    uniq = _dedup_blocks(blocks)
    # 归一后的 (text, cx, cy, conf) 元组，便于匹配
    items = [
        (_norm((b.get("text") or "").strip()),
         int(b.get("cx", 0)), int(b.get("cy", 0)), float(b.get("conf", 0)))
        for b in uniq
    ]
    full = " ".join(t for t, *_ in items)

    out: dict[str, Any] = {
        "comment_count": None,
        "sold_count": None,
        "shop_name": None,
        "shop_review_count": None,
        "brand_review_count": None,
        "rank_badges": [],
        "review_tags": [],
        "praise_rate": None,
        "specs": {},
        "coupon_price": None,
        "discount": None,
    }

    # ── 商品评价数：商品评价(4,949)
    m = re.search(r"商品评价\s*[(（]\s*([\d,，.万亿+]+)", full)
    if m:
        out["comment_count"] = parse_count(m.group(1))

    # ── 店铺评价数 / 品牌评价数
    m = re.search(r"店铺评价\s*[(（]\s*([\d,，.万亿+]+)", full)
    if m:
        out["shop_review_count"] = parse_count(m.group(1))
    m = re.search(r"品牌评价\s*[(（]\s*([\d,，.万亿+]+)", full)
    if m:
        out["brand_review_count"] = parse_count(m.group(1))

    # ── 已拼件数（本商品）：取**单独成块**的"已拼N件"（吸顶栏），
    #    排除店铺级"近30天已拼1.8万件" / "全店…"。
    sold_vals: list[int] = []
    for t, _cx, _cy, _c in items:
        if re.fullmatch(r"已拼\s*[\d,，.]+\s*万?\s*件", t):
            v = parse_count(t)
            if v:
                sold_vals.append(v)
    if sold_vals:
        # 吸顶栏那条会重复出现且通常是最小的本品数；取众数/最小更稳
        out["sold_count"] = min(sold_vals)

    # ── 店铺名。两条路，按优先级：
    #    ① **全局找带明确店铺后缀的整块**（旗舰店/专营店/专卖店/百货/商行/商城）。
    #       品牌旗舰页里"进店"那屏常漏识店名，只剩左侧 logo（如真彩页"荣麟数码"）；
    #       而店名整块"Truecolor真彩旗舰店"可能落在另一屏，全局搜才能捞到。
    #    ② 兜底：锚定"进店"同高度左侧、最靠近"进店"的非统计/非营销文本。
    badge_stop = {"旗舰店", "专营店", "专卖店", "官方旗舰", "旗舰",
                  "品牌", "自营", "认证", "好店"}
    # 店铺卡营销话术（潜力好店/连续N个月入选/官方授权/正品…），不含统计数字、
    # 躲过 _looks_like_stat，但绝不会是店名——单独黑名单兜掉。
    shop_mkt_re = re.compile(
        r"(好店|潜力|入选|授权|正品|回头客|飙升|隔日达|条好评|新增.*好评)"
    )
    shop_suffix_re = re.compile(r"(旗舰店|专营店|专卖店|官方旗舰|百货|商行|商城)")
    suffix_cands = [
        (t, c) for t, _cx, _cy, c in items
        if shop_suffix_re.search(t)
        and t not in badge_stop
        and not _looks_like_stat(t)
        and not shop_mkt_re.search(t)
        and 3 <= len(t) <= 24
    ]
    if suffix_cands:
        # 多个候选：取最长（最完整店名）→ 同长取置信度最高
        out["shop_name"] = max(suffix_cands, key=lambda x: (len(x[0]), x[1]))[0]

    if not out["shop_name"]:
        jin = next((it for it in items if "进店" in it[0]), None)
        if jin:
            _jt, jx, jy, _jc = jin
            cands: list[tuple[str, int, float]] = []
            for t, cx, cy, c in items:
                if "进店" in t:
                    continue
                # 店名与"进店"基本同一行；营销/统计行都在下方 100+px，收窄窗口排除
                if abs(cy - jy) > 90 or cx >= jx - 60:
                    continue
                if _looks_like_stat(t) or t in badge_stop or shop_mkt_re.search(t):
                    continue
                if len(t) < 2 or len(t) > 24:
                    continue
                cands.append((t, cx, c))
            if cands:
                from collections import Counter
                cnt = Counter(t for t, _cx, _c in cands)

                def _shopname_score(tc: tuple[str, int, float]) -> tuple:
                    t, cx, c = tc
                    name_like = 1 if re.search(
                        r"(旗舰店|专营店|专卖店|官方旗舰|百货|商行|商城|店$)", t
                    ) else 0
                    return (name_like, cx, cnt[t], c)

                out["shop_name"] = max(cands, key=_shopname_score)[0]

    # ── 榜单 / 上榜
    for t, *_ in items:
        if re.search(r"(畅销榜|热销.*第\d+名|入选.*榜单|好店榜)", t):
            out["rank_badges"].append(t)

    # ── 好评率：好评率98% / 好评率超99%。要求至少两位数字——OCR 常把
    #    "好评率超9X%"截断成"好评率超9"，单位数多半是残值，宁可不取。
    m = re.search(r"好评率\s*[超达]?\s*(\d{2,}(?:\.\d+)?)\s*%?", full)
    if m:
        try:
            v = float(m.group(1))
            if 1 <= v <= 100:
                out["praise_rate"] = v
        except ValueError:
            pass

    # ── 口碑标签云：店铺/商品评价区下方的短好评词（2-6 字、无标点、无统计）
    tags = _extract_review_tags(items)
    if out["shop_name"]:  # 店铺名常紧贴"进店"落进标签窗口，去掉
        tags = [t for t in tags if t != out["shop_name"]]
    out["review_tags"] = tags

    # ── 规格属性栅格：标签块 → 正下方同列的值块
    out["specs"] = _extract_specs(items)

    # ── 券后实付价（best-effort，常失败）：券后¥X / 券后半X（半=¥误读）
    m = re.search(r"券[后後]\s*[¥￥半\^]?\s*([\d]+(?:\.[\d]+)?)", full)
    if m:
        try:
            out["coupon_price"] = float(m.group(1))
        except ValueError:
            pass
    # ── 立减/已优惠金额（元）
    m = re.search(r"(?:立减|已优惠|已减)\s*([\d.]+)\s*元", full)
    if m:
        try:
            out["discount"] = float(m.group(1))
        except ValueError:
            pass

    return out


_STAT_RE = re.compile(
    r"(已拼|全店|近\d+天|好评|万\+|亿|总售|老店|发起拼单|参与|拼成|查看全部|"
    r"运力|送达|无理由|秒退|包邮|赔|保障|在拼|拼单|分钟|小时|限\d+件|"
    r"优惠|立减|券|\d{2,})"
)


def _looks_like_stat(t: str) -> bool:
    """是否像"统计/营销/按钮"文案（用于排除，避免误当店铺名/标签）。"""
    return bool(_STAT_RE.search(t))


_NAV_STOP = {"店铺", "收藏", "客服", "首页", "顶部", "分享", "进店",
             "商品详情", "店铺保障", "品牌介绍", "查看全部"}


def _extract_review_tags(items: list[tuple[str, int, int, int]]) -> list[str]:
    """口碑标签云：PDD 评价区下方那块**网格状的短好评词**（服务满意/书写流畅/
    好用/质量很好…，每行 3-4 个等距排列）。

    关键判定：**同一行≥3 个纯 2-5 中文字的短词** 才算标签行——真实评论是
    长句/带标点、评论人名一行只 1-2 个，都凑不出≥3 短词，自然被排除（真彩页
    那种"商品评价(15)"后面跟真实评论的，这里会正确地返回空）。再叠加"评价(...)
    之下、进店/商品详情之上"的版面区间，把底部规格栅格也排除掉。
    """
    start_y = None
    for t, _cx, cy, _c in items:
        if re.search(r"评价\s*[(（]", t):
            start_y = cy
            break
    end_y = None
    for t, _cx, cy, _c in items:
        if re.search(r"(进店|店铺保障|商品详情|品牌介绍)", t):
            end_y = cy if end_y is None else min(end_y, cy)

    # 限定到评价区→店铺卡之间
    region = [
        (t, cx, cy) for t, cx, cy, _c in items
        if (start_y is None or cy > start_y) and (end_y is None or cy < end_y)
    ]
    # 按行分组（同一视觉行 cy 接近）
    rows: dict[int, list[tuple[int, str]]] = {}
    for t, cx, cy in region:
        rows.setdefault(round(cy / 30), []).append((cx, t))

    tags: list[str] = []
    for _key in sorted(rows):
        shorts = [
            (cx, t) for cx, t in rows[_key]
            if re.fullmatch(r"[\u4e00-\u9fa5]{2,5}", t)
            and t not in _NAV_STOP
            and not _looks_like_stat(t)
        ]
        if len(shorts) >= 3:  # 一行≥3 短词 = 标签网格行
            tags.extend(t for _cx, t in sorted(shorts))

    seen: set[str] = set()
    return [x for x in tags if not (x in seen or seen.add(x))][:12]


def _extract_specs(items: list[tuple[str, int, int, int]]) -> dict[str, str]:
    """规格栅格：标签块（是否双头/发货地/风格…）→ **同列最近的值块**。

    两套模板：风达页"标签在上、值在下"；真彩品牌页"值在上、标签在下"。所以
    上下都找，取同列（|cx-lx|<100）、纵向最近（|dy|≤130）、本身不是标签、够短
    的块当值。
    """
    specs: dict[str, str] = {}
    labels = [it for it in items if _SPEC_LABEL_RE.match(it[0])]
    for lt, lx, ly, _lc in labels:
        best = None
        best_dy = 999
        for vt, vx, vy, _vc in items:
            if vt == lt:
                continue
            dy = vy - ly
            if dy == 0 or abs(dy) > 130:   # 上下都看，但别太远
                continue
            if abs(vx - lx) > 100:          # 同列
                continue
            if _SPEC_LABEL_RE.match(vt):    # 别把另一个标签当值
                continue
            if _looks_like_stat(vt) or len(vt) > 16:
                continue
            if abs(dy) < best_dy:
                best_dy = abs(dy)
                best = vt
        if best:
            specs[lt] = best
    return specs


# ── OCR dump 文本（screen_NN_ocr.txt）反解析成 blocks，给离线工具用 ──────
_DUMP_LINE_RE = re.compile(
    r"y=\s*(-?\d+)\s+x=\s*(-?\d+)\s+conf=([\d.]+)\s*\|\s*(.*)"
)


def parse_ocr_dump(text: str) -> list[dict[str, Any]]:
    """把 ``screen_NN_ocr.txt`` 一行行解析回 blocks（cy/cx/conf/text）。"""
    blocks: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        m = _DUMP_LINE_RE.match(line.strip())
        if not m:
            continue
        blocks.append({
            "cy": int(m.group(1)),
            "cx": int(m.group(2)),
            "conf": float(m.group(3)),
            "text": m.group(4).strip(),
        })
    return blocks
