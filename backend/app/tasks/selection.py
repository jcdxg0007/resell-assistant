"""
Selection engine Celery tasks.
Handles periodic product discovery, price monitoring, and data collection.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.product import Product, PriceSnapshot, Platform, ProductScore
from app.models.xianyu import XianyuMarketData
from app.models.selection import Category, Keyword, KeywordProduct, KeywordScore
from app.services.xianyu.crawler import xianyu_crawler
from app.services.alibaba_1688.crawler import alibaba_1688_crawler
from app.services.xiaohongshu.crawler import xhs_crawler
from app.services import anti_risk
from app.services.browser import browser_manager
from app.services.selection.data_cleaning import clean_keyword_sample
from app.services.selection.keyword_scoring import (
    KeywordScoringInput, calculate_keyword_score,
)
from app.services.selection.product_scoring import (
    ProductScoringInput, calculate_product_score,
)


# ────────────────────────── 数据源开关（临时禁用）─────────
# 2026-05-23: PDD H5 端搜索已被官方关闭（对所有用户返空），1688
# 受连带影响。详见 docs/开发文档_转卖助手.md §1.4 —— 已确定要走
# APP 端方案或第三方电商数据 API 替代。在新通道上线前，临时把这
# 两条线从 instant_search 主链路里摘掉以避免：
#   - 持续消耗账号（反复触发 quarantine）
#   - 噪音风控预警（每次跑都触发 DingTalk）
#   - 浪费 compliance gate slot / 代理 IP
# 切回 False 即可恢复 —— V3/V4 代码、账号池、SOP 都保留完整。
_PDD_DISABLED: bool = True
_ALIBABA_1688_DISABLED: bool = True

# ────────────────── PDD APP worker 通道（Phase 1 联调用）──
# True 时把 PDD 路径切到家里 Windows worker + 物理手机方案（见
# docs/PDD-自建采集-roadmap.md）。注意必须同时把 `_PDD_DISABLED`
# 翻成 False，否则上面 §1.4 的禁用早早 short-circuit 就过不到这里。
#
# Phase 1 Day 1 状态：worker 端到端通了（拿 stub 验过），pdd_app_client
# 还没接 uiautomator2 真采集 → Day 2 完成后才能拔此开关 + 拔 _PDD_DISABLED。
_PDD_USE_APP_WORKER: bool = False


def _ms_to_dt(ms) -> datetime | None:
    """闲鱼列表页 publish_time_ms（毫秒时间戳）→ aware datetime；非法值返回 None。"""
    try:
        if not ms:
            return None
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def run_async(coro):
    """Helper to run async code from sync Celery tasks.

    Also tears down the browser_manager singleton on this loop so the next
    Celery task (which creates a new loop) starts with a clean slate, instead
    of inheriting a Playwright transport attached to a dead loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Guard against stale asyncpg connections inherited from the fork
        # parent or the previous task's dead loop. dispose() is cheap when
        # the pool is already empty.
        try:
            from app.core.database import engine
            loop.run_until_complete(engine.dispose())
        except Exception as e:
            logger.debug(f"Pre-task engine dispose ignored: {e}")
        # 同理重置 redis 连接池，否则复用死 loop 上的连接会抛
        # 'Future attached to a different loop / Event loop is closed'
        try:
            from app.core.redis import reset_redis_pool
            loop.run_until_complete(reset_redis_pool())
        except Exception as e:
            logger.debug(f"Pre-task redis reset ignored: {e}")
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(_shutdown_per_loop_resources())
        except Exception as e:
            logger.debug(f"Loop teardown ignored: {e}")
        loop.close()
        asyncio.set_event_loop(None)


async def _shutdown_per_loop_resources():
    """Dispose Playwright and SQLAlchemy engine so the next task starts clean.

    Both are bound to the current event loop; without explicit teardown the
    next Celery task reuses stale connections and hangs with
    'attached to a different loop' errors.
    """
    try:
        if browser_manager._browser and browser_manager._current_loop_matches():
            await browser_manager.stop()
    except Exception:
        pass
    finally:
        browser_manager._browser = None
        browser_manager._playwright = None
        browser_manager._contexts.clear()
        browser_manager._loop_id = None

    try:
        from app.core.database import engine
        await engine.dispose()
    except Exception:
        pass


def _find_active_account_sync(platform: str) -> dict | None:
    """Find an active logged-in account and pre-cache its cookies to disk.

    Uses a one-shot asyncpg connection with its own event loop.
    Must be called from sync context (before run_async).
    """
    import asyncpg
    import json as _json
    from pathlib import Path
    from app.core.config import get_settings
    settings = get_settings()
    raw_url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    async def _query():
        conn = await asyncpg.connect(raw_url)
        try:
            row = await conn.fetchrow(
                "SELECT id, account_name, proxy_url, user_agent, viewport, cookies_data "
                "FROM accounts "
                "WHERE platform = $1 AND is_active = true AND session_status = 'active' "
                "ORDER BY health_score DESC LIMIT 1",
                platform,
            )
            if not row:
                return None
            vp = row["viewport"]
            if isinstance(vp, str):
                vp = _json.loads(vp)
            account_id = str(row["id"])

            # Pre-write cookies to file so browser_manager.get_context can pick them up
            cookies_data = row["cookies_data"]
            if cookies_data:
                states_dir = Path(__file__).parent.parent.parent / "playwright_states"
                states_dir.mkdir(exist_ok=True)
                state_file = states_dir / f"{account_id}.json"
                state_file.write_text(cookies_data)
                logger.info(f"Pre-cached cookies to {state_file.name} for '{row['account_name']}'")

            return {
                "id": account_id,
                "account_name": row["account_name"],
                "proxy_url": row["proxy_url"],
                "user_agent": row["user_agent"],
                "viewport": vp,
            }
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_query())
    except Exception as e:
        logger.error(f"Sync account lookup failed: {e}")
        return None
    finally:
        loop.close()


def _load_crawler_cookies_sync(platform_tag: str) -> list[dict]:
    """**Legacy single-account** crawler cookie loader.

    .. deprecated:: use :mod:`app.services.crawler_accounts` —
        ``pick_crawler_account_sync`` returns
        ``(account_id, account_name, cookies)`` and handles rotation
        + 60m cooldown after an ``empty_result`` burn. This function
        is retained only for ad-hoc scripts / legacy smoke tests that
        don't want the extra machinery.

    Crawler 小号 account isolation convention:
    - platform='pdd_crawler'   → PDD 搜索专用小号
    - platform='taobao_crawler' (reserved, currently unused)
    - platform='xhs_crawler'   (reserved, P5)
    Operation accounts keep their original platform value (xianyu/pdd/...)
    so the two pools never cross-pollute.
    """
    import asyncpg
    import json as _json
    from app.core.config import get_settings
    settings = get_settings()
    raw_url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    async def _query():
        conn = await asyncpg.connect(raw_url)
        try:
            row = await conn.fetchrow(
                "SELECT account_name, cookies_data FROM accounts "
                "WHERE platform = $1 AND is_active = true "
                "AND session_status = 'active' "
                "ORDER BY health_score DESC LIMIT 1",
                f"{platform_tag}_crawler",
            )
            if not row or not row["cookies_data"]:
                return []
            try:
                state = _json.loads(row["cookies_data"])
            except Exception:
                return []
            cookies = state.get("cookies") or []
            logger.info(
                f"Loaded {len(cookies)} {platform_tag}_crawler cookies "
                f"from '{row['account_name']}'"
            )
            return cookies
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_query())
    except Exception as e:
        logger.warning(f"Crawler cookie lookup failed for {platform_tag}: {e}")
        return []
    finally:
        loop.close()


async def _get_crawler_context(account_id: str | None = None, account_config: dict | None = None):
    """Get a Playwright context for crawling."""
    if not browser_manager._browser:
        await browser_manager.start()

    config = account_config or {"proxy_url": None}
    ctx_id = account_id or "crawler_default"
    return await browser_manager.get_context(ctx_id, config)


async def _pdd_search_via_app_worker(keyword: str, mode: str = "fast") -> dict:
    """走家里 Windows worker + 物理手机 PDD APP 通道采集。

    输入：关键词 + mode（fast/deep）。
    输出：与 ``pdd_crawler.collect_market_data`` 兼容的 dict（active_listings,
    items, price_min, robust_price_min, price_median, total_sales 等），
    方便上游 scoring 流程不用改任何字段映射。

    任何失败（worker 离线 / 超时 / worker 内部 status != ok）都返回
    ``{"__unavailable__": True, "error": "..."}``，由 ``_fetch_platform_with_retry``
    处理重试和降级，保持与 H5 路径同样的容错形状。

    详见 docs/PDD-自建采集-roadmap.md §3.
    """
    from app.services.pdd_app_queue import (
        PddAppTask, enqueue_task, await_result, get_worker_status,
    )
    from app.services.pdd_search_run import persist_search_run

    status = await get_worker_status()
    if not status.get("online"):
        logger.warning(f"PDD APP worker offline (devices={status.get('devices', [])})")
        return {
            "platform": "pdd",
            "__unavailable__": True,
            "error": "pdd_app_worker_offline",
            "active_listings": 0,
            "items": [],
            "risk_signals": [],
        }

    timeout_s = 180 if mode == "deep" else 90
    task = PddAppTask(
        kind="search",
        payload={"keyword": keyword, "mode": mode},
        timeout_s=timeout_s,
    )
    logger.info(
        f"PDD APP worker: enqueue task_id={task.task_id} keyword='{keyword}' "
        f"mode={mode} timeout={timeout_s}s worker_devices={status.get('devices')}"
    )
    await enqueue_task(task)
    # 多给 30s buffer 覆盖 worker → backend HTTPS 往返
    result = await await_result(task.task_id, timeout_s=timeout_s + 30)

    if result is None:
        logger.warning(f"PDD APP worker: task_id={task.task_id} timed out")
        await persist_search_run(
            status="timeout", keyword_text=keyword, task_id=task.task_id,
            source="selection", mode=mode,
        )
        return {
            "platform": "pdd",
            "__unavailable__": True,
            "error": "pdd_app_worker_timeout",
            "active_listings": 0,
            "items": [],
            "risk_signals": [],
        }

    if result.status != "ok":
        logger.warning(
            f"PDD APP worker: task_id={task.task_id} status={result.status} "
            f"error={result.error} risk_signals={result.risk_signals}"
        )
        # 把 worker 报的风控信号转成 anti_risk.RiskSignal 兼容形状，
        # 让 _instant_search 的 DingTalk 聚合层能直接吃。
        from app.services.anti_risk import RiskSignal
        risk_objs = [
            RiskSignal(
                platform="pdd",
                signal_type=sig,
                detail=f"pdd_app_worker:{result.task_id}",
            )
            for sig in (result.risk_signals or [])
        ]
        await persist_search_run(
            status=result.status, keyword_text=keyword, task_id=task.task_id,
            source="selection", mode=mode, risk_signals=result.risk_signals,
            device_serial=result.device_serial, account_name=result.account_name,
            elapsed_ms=result.elapsed_ms, error=result.error,
        )
        return {
            "platform": "pdd",
            "__unavailable__": True,
            "error": f"pdd_app_worker:{result.status}:{result.error or 'unknown'}",
            "active_listings": 0,
            "items": [],
            "risk_signals": risk_objs,
        }

    # 把 worker 返回的商品列表映射回 H5 形状
    items = result.items or []
    prices = [float(it["price"]) for it in items if it.get("price")]
    sorted_prices = sorted(prices)
    # robust_price_min：去掉最低 5%，避免 1 元钓鱼链接干扰
    drop_n = max(1, int(len(sorted_prices) * 0.05)) if len(sorted_prices) > 20 else 0
    trimmed = sorted_prices[drop_n:] if drop_n else sorted_prices
    median = (
        sorted_prices[len(sorted_prices) // 2]
        if sorted_prices else None
    )

    payload = {
        "platform": "pdd",
        "active_listings": len(items),
        "items": items,
        "price_min": sorted_prices[0] if sorted_prices else None,
        "price_max": sorted_prices[-1] if sorted_prices else None,
        "price_avg": (sum(prices) / len(prices)) if prices else None,
        "robust_price_min": trimmed[0] if trimmed else None,
        "price_median": median,
        "total_sales": sum(it.get("sales", 0) for it in items),
        "device_serial": result.device_serial,
        "account_name": result.account_name,
        "risk_signals": [],  # ok 路径没有要冒泡的信号
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        f"PDD APP worker: task_id={task.task_id} OK — "
        f"items={len(items)} price_min={payload['price_min']} "
        f"robust_min={payload['robust_price_min']} device={result.device_serial}"
    )
    await persist_search_run(
        status="ok" if items else "empty",
        keyword_text=keyword, task_id=task.task_id, source="selection", mode=mode,
        items_count=len(items), price_min=payload["price_min"],
        price_median=payload["price_median"], items=items,
        device_serial=result.device_serial,
        account_name=result.account_name, elapsed_ms=result.elapsed_ms,
    )
    return payload


@celery_app.task(name="app.tasks.selection.xianyu_price_monitor")
def xianyu_price_monitor():
    """Monitor prices for products with active Xianyu listings."""
    logger.info("Starting Xianyu price monitor task")
    run_async(_xianyu_price_monitor())


async def _xianyu_price_monitor():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Product)
            .where(Product.is_active == True)
            .where(Product.source_platform.in_([Platform.PINDUODUO, Platform.TAOBAO]))
            .order_by(Product.last_crawled_at.asc().nulls_first())
            .limit(50)
        )
        products = result.scalars().all()
        logger.info(f"Price monitor: {len(products)} products to check")

        from app.services.compliance import compliance_gate

        for product in products:
            try:
                gate = await compliance_gate("xianyu", actor="scheduled")
                if not gate:
                    logger.warning(
                        f"xianyu_price_monitor: gate denied ({gate.reason}), "
                        f"stopping this cycle — will retry at next schedule"
                    )
                    break
                context = await _get_crawler_context()
                keyword = product.title[:20]
                market_data = await xianyu_crawler.collect_market_data(context, keyword)

                if market_data.get("active_listings", 0) > 0:
                    md = XianyuMarketData(
                        product_id=product.id,
                        keyword=keyword,
                        active_listings=market_data["active_listings"],
                        total_wants=market_data.get("total_wants", 0),
                        price_min=market_data.get("price_min"),
                        price_max=market_data.get("price_max"),
                        price_avg=market_data.get("price_avg"),
                        price_cv=market_data.get("price_cv"),
                        top5_sales=market_data.get("top5_sales"),
                        seller_distribution=market_data.get("seller_distribution"),
                        captured_at=datetime.now(timezone.utc),
                    )
                    db.add(md)

                product.last_crawled_at = datetime.now(timezone.utc)
                await db.commit()
                logger.info(f"Market data collected for: {product.title[:30]}")

            except Exception as e:
                logger.error(f"Price monitor failed for {product.id}: {e}")
                continue


def _in_window(hour: int, start: int, end: int) -> bool:
    """当前小时是否在活跃时段内。start==end 视为全天；start>end 视为跨夜。"""
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


@celery_app.task(name="app.tasks.selection.xianyu_auto_batch_tick")
def xianyu_auto_batch_tick():
    """闲鱼全自动采集 tick（beat 每 3 分钟唤醒，闸门全在任务内部判断）。

    与 PDD 自动跑批对称、但独立：开关 / 活跃时段 / 随机下次时刻全读自己那套
    xianyu_auto_* 配置，从词库挑 xianyu_safe 的词派闲鱼采集。
    """
    run_async(_xianyu_auto_batch_tick())


async def _xianyu_auto_batch_tick():
    import random as _random
    import time as _time
    from app.services.pdd_app_queue import (
        get_xianyu_auto_next_ts, set_xianyu_auto_next_ts, is_collection_paused,
    )
    from app.services.pdd_worker_config import get_runtime_config
    from app.services.xianyu_autobatch import dispatch_xianyu_batch

    async with AsyncSessionLocal() as db:
        cfg = await get_runtime_config(db)

    if not cfg.get("xianyu_auto_batch_enabled"):
        return
    if await is_collection_paused():
        logger.info("xianyu auto tick: 采集已暂停，跳过")
        return

    cn_tz = timezone(timedelta(hours=8))
    hour = datetime.now(cn_tz).hour
    start = int(cfg.get("xianyu_auto_active_start_hour", 9))
    end = int(cfg.get("xianyu_auto_active_end_hour", 23))
    if not _in_window(hour, start, end):
        return

    now = _time.time()
    next_ts = await get_xianyu_auto_next_ts()
    if next_ts is not None and now < next_ts:
        return

    gmin = int(cfg.get("xianyu_auto_interval_min_minutes", 40))
    gmax = int(cfg.get("xianyu_auto_interval_max_minutes", 120))
    if gmin > gmax:
        gmin, gmax = gmax, gmin
    await set_xianyu_auto_next_ts(now + _random.uniform(gmin, gmax) * 60)

    count = int(cfg.get("xianyu_auto_batch_count", 3))
    await dispatch_xianyu_batch(count=count)


@celery_app.task(name="app.tasks.selection.xianyu_product_discovery")
def xianyu_product_discovery():
    """Discover new products by searching preset category keywords."""
    logger.info("Starting Xianyu product discovery task")
    run_async(_xianyu_product_discovery())


async def _xianyu_product_discovery():
    discovery_keywords = [
        "相机兔笼", "GoPro配件", "显示器支架", "桌面收纳",
        "补光灯", "跟焦器", "筋膜枪", "迷你投影仪",
        "机械键盘配件", "氛围灯", "理线器", "便携咖啡机",
    ]

    async with AsyncSessionLocal() as db:
        from app.services.compliance import compliance_gate

        context = await _get_crawler_context()
        for keyword in discovery_keywords:
            try:
                gate = await compliance_gate("xianyu", actor="scheduled")
                if not gate:
                    logger.warning(
                        f"xianyu_product_discovery: gate denied ({gate.reason}), "
                        f"aborting discovery cycle"
                    )
                    break
                items = await xianyu_crawler.search_products(context, keyword, max_items=20)
                new_count = 0
                for item in items:
                    if not item.get("item_id"):
                        continue
                    existing = await db.execute(
                        select(Product).where(
                            Product.source_platform == Platform.XIANYU,
                            Product.source_id == item["item_id"],
                        )
                    )
                    if existing.scalar_one_or_none():
                        continue

                    product = Product(
                        source_platform=Platform.XIANYU,
                        source_url=item.get("url", ""),
                        source_id=item["item_id"],
                        title=item["title"],
                        price=item["price"],
                        image_urls=[item["image_url"]] if item.get("image_url") else None,
                        category=keyword,
                        seller_name=(item.get("seller_name") or None),
                        published_at=_ms_to_dt(item.get("publish_time_ms")),
                        last_crawled_at=datetime.now(timezone.utc),
                    )
                    db.add(product)
                    new_count += 1

                await db.commit()
                logger.info(f"Discovery '{keyword}': found {len(items)} items, {new_count} new")

            except Exception as e:
                logger.error(f"Discovery failed for '{keyword}': {e}")
                continue


@celery_app.task(name="app.tasks.selection.xhs_hot_article_scan")
def xhs_hot_article_scan():
    """Scan XHS for hot articles in target categories."""
    logger.info("Starting XHS hot article scan")
    run_async(_xhs_hot_article_scan())


async def _xhs_hot_article_scan():
    from app.models.xiaohongshu import XhsCompetitorNote
    from app.services.xiaohongshu.crawler import xhs_crawler
    from app.services.xiaohongshu.analyzer import analyze_comment_intent

    scan_keywords = [
        "相机配件", "摄影装备", "桌面好物", "数码配件",
        "补光灯推荐", "收纳神器", "投影仪推荐", "键盘推荐",
        "考研资料", "PPT模板", "简历模板",
    ]

    async with AsyncSessionLocal() as db:
        from app.services.compliance import compliance_gate

        context = await _get_crawler_context("xhs_scanner")
        for keyword in scan_keywords:
            try:
                gate = await compliance_gate("xiaohongshu", actor="scheduled")
                if not gate:
                    logger.warning(
                        f"xhs_hot_article_scan: gate denied ({gate.reason}), "
                        f"aborting scan cycle"
                    )
                    break
                notes = await xhs_crawler.search_notes(context, keyword, max_notes=50)
                saved = 0
                for note in notes:
                    note_id = note.get("xhs_note_id")
                    if not note_id:
                        continue
                    existing = await db.execute(
                        select(XhsCompetitorNote).where(XhsCompetitorNote.xhs_note_id == note_id)
                    )
                    if existing.scalar_one_or_none():
                        continue

                    comp = XhsCompetitorNote(
                        keyword=keyword,
                        xhs_note_id=note_id,
                        title=note.get("title", ""),
                        likes=note.get("likes", 0),
                        collects=note.get("collects", 0),
                        comments=note.get("comments", 0),
                        has_product_link=note.get("has_product_link", False),
                        captured_at=datetime.now(timezone.utc),
                    )
                    db.add(comp)
                    saved += 1

                await db.commit()
                logger.info(f"XHS scan '{keyword}': {len(notes)} notes, {saved} new")
            except Exception as e:
                logger.error(f"XHS scan failed for '{keyword}': {e}")


@celery_app.task(name="app.tasks.selection.xhs_topic_trending")
def xhs_topic_trending():
    """Update trending topic data from XHS."""
    logger.info("Starting XHS topic trending update")
    run_async(_xhs_topic_trending())


async def _xhs_topic_trending():
    from app.models.xiaohongshu import XhsHotTopic
    from app.services.xiaohongshu.crawler import xhs_crawler

    topics_to_track = [
        ("相机配件", "3C"), ("摄影装备", "3C"), ("桌面收纳", "家居"),
        ("数码好物", "3C"), ("投影仪", "3C"), ("机械键盘", "3C"),
        ("考研", "教育"), ("备考", "教育"), ("PPT模板", "职场"),
        ("手机摄影", "摄影"), ("Vlog", "摄影"),
    ]

    async with AsyncSessionLocal() as db:
        from app.services.compliance import compliance_gate

        context = await _get_crawler_context("xhs_topic_tracker")
        for topic_name, category in topics_to_track:
            try:
                gate = await compliance_gate("xiaohongshu", actor="scheduled")
                if not gate:
                    logger.warning(
                        f"xhs_topic_trending: gate denied ({gate.reason}), "
                        f"aborting cycle"
                    )
                    break
                data = await xhs_crawler.get_topic_data(context, topic_name)
                topic = XhsHotTopic(
                    topic_name=topic_name,
                    category=category,
                    view_count=data.get("view_count", 0),
                    note_count=data.get("note_count", 0),
                    is_trending=data.get("view_count", 0) > 10_000_000,
                    captured_at=datetime.now(timezone.utc),
                )
                db.add(topic)
            except Exception as e:
                logger.error(f"Topic tracking failed for '{topic_name}': {e}")

        await db.commit()
        logger.info(f"Topic trending updated: {len(topics_to_track)} topics")


@celery_app.task(name="app.tasks.selection.source_stock_check")
def source_stock_check():
    """Check stock availability on source platforms."""
    logger.info("Source stock check - not yet implemented")
    # TODO: Implement source platform stock checking


def _classify_xianyu_result(result: dict) -> tuple[str, int, int | None, list | None, str | None]:
    """从 instant_search 返回的 dict 推导闲鱼任务记录字段。

    返回 (status, items_count, saved_count, risk_signals, error)。
    - 有 items / active_listings / saved_products>0 → ok
    - 跑通但没货 → empty
    - error 里含 risk/验证/captcha → risk_blocked，否则 failed
    """
    if not isinstance(result, dict):
        return "failed", 0, None, None, "non_dict_result"
    active = int(result.get("active_listings") or 0)
    saved = result.get("saved_products")
    saved_n = int(saved) if isinstance(saved, (int, float)) else None
    items = result.get("items") or []
    items_n = active or (len(items) if isinstance(items, list) else 0) or (saved_n or 0)
    err = result.get("error")
    risk = result.get("risk_signals") or None
    if items_n > 0:
        return "ok", items_n, saved_n, risk, None
    if err:
        low = str(err).lower()
        is_risk = any(k in low for k in ("risk", "captcha", "验证", "登录", "block"))
        return ("risk_blocked" if is_risk else "failed"), 0, saved_n, risk, str(err)[:2000]
    return "empty", 0, saved_n, risk, None


@celery_app.task(name="app.tasks.selection.instant_search")
def instant_search(keyword: str, platform: str = "xianyu", source: str = "manual"):
    """User-triggered instant search on the primary platform.

    P4 fan-out: xianyu (primary, guest) + pdd (crawler 小号 cookies) +
    1688 (guest) + xiaohongshu (placeholder, skipped).

    **Proxy isolation** — crawler traffic goes through
    ``SELECTION_CRAWLER_PROXY_URL`` (a short-term, rotating pool). We
    deliberately do **not** use the operation account's ``proxy_url``
    (long-term IP) here, so a crawler ban can never cascade to the money-
    earning xianyu store. See
    :data:`app.core.config.Settings.SELECTION_CRAWLER_PROXY_URL`.
    """
    logger.info(f"Instant search: '{keyword}' on {platform}")
    settings = get_settings()
    crawler_proxy = settings.SELECTION_CRAWLER_PROXY_URL or None
    if crawler_proxy:
        logger.info(
            f"Crawler pool proxy: {crawler_proxy.split(':')[0]}:***"
        )

    # ── Result cache: 5-minute window per (keyword, platform).
    # User-triggered spam (someone hammering "搜索" while the first
    # result loads) should not burn fresh gate slots — we return the
    # previous successful result instead. Failed results
    # (__unavailable__=True on all platforms, or any exception path)
    # are intentionally NOT cached so a transient failure doesn't
    # block genuine retries.
    cached = _cache_lookup_instant_search(keyword, platform)
    if cached is not None:
        logger.info(
            f"Instant search cache hit for '{keyword}' @ {platform} — "
            f"returning cached result (age={cached.get('_cache_age_seconds')}s)"
        )
        # 缓存命中也算一次用户发起的搜索任务，照样落「任务记录」（否则 5min 内
        # 复搜的词永远不出现在记录里）。失败 swallow。
        _persist_xianyu_run_if_needed(keyword, platform, source, cached)
        return cached

    # PDD / 1688 小号: check out the least-recently-used account that
    # isn't in cooldown. Returns (id, name, cookies) or None (→ guest
    # mode, which in 2026 triggers a login redirect for both platforms).
    # We still try so the orchestrator can report risk signals and
    # score the keyword on whatever platforms did succeed.
    from app.services.crawler_accounts import pick_crawler_account_sync
    if _PDD_DISABLED:
        pdd_account = None
        logger.info("PDD pool pick skipped (_PDD_DISABLED=True, see §1.4)")
    else:
        pdd_account = pick_crawler_account_sync("pdd")
        if pdd_account:
            _, name, cookies, area = (*pdd_account, None)[:4]
            logger.info(
                f"PDD pool pick: {name} ({len(cookies)} cookies, "
                f"bound_area={area or '(unbound)'})"
            )
        else:
            logger.warning("PDD crawler pool exhausted (all accounts in cooldown or missing)")
    if _ALIBABA_1688_DISABLED:
        alibaba_account = None
        logger.info("1688 pool pick skipped (_ALIBABA_1688_DISABLED=True, see §1.4)")
    else:
        alibaba_account = pick_crawler_account_sync("1688")
        if alibaba_account:
            _, name, cookies, area = (*alibaba_account, None)[:4]
            logger.info(
                f"1688 pool pick: {name} ({len(cookies)} cookies, "
                f"bound_area={area or '(unbound)'})"
            )
        else:
            logger.warning("1688 crawler pool exhausted (all accounts in cooldown or missing)")

    # ── Crash-safety wrapper:
    # ``pick_crawler_account_sync`` already bumped ``last_used_at`` on
    # the accounts above, so a worker crash here does NOT break the
    # LRU rotation (the crashed-on account naturally moves to the tail
    # of the rotation). Still, we want a single place that logs the
    # crash and surfaces it via DingTalk so ops sees it instead of it
    # being lost in a Celery traceback.
    try:
        result = run_async(_instant_search(
            keyword=keyword,
            platform=platform,
            crawler_proxy_url=crawler_proxy,
            pdd_account=pdd_account,
            alibaba_account=alibaba_account,
        ))
    except Exception as e:
        logger.exception(
            f"instant_search crashed for '{keyword}' @ {platform}: "
            f"{type(e).__name__}: {e}. "
            f"Accounts used: pdd={pdd_account[1] if pdd_account else '-'}, "
            f"1688={alibaba_account[1] if alibaba_account else '-'} "
            f"(health unchanged; last_used_at already bumped at pick time "
            f"so LRU rotation is preserved)."
        )
        raise
    _cache_store_instant_search(keyword, platform, result)
    # 闲鱼任务记录落库（与 PDD pdd_search_runs 对称，给「任务记录」抽屉用）。
    _persist_xianyu_run_if_needed(keyword, platform, source, result)
    return result


def _persist_xianyu_run_if_needed(keyword: str, platform: str, source: str, result: dict) -> None:
    """platform=='xianyu' 时把这次搜索落 xianyu_search_runs。失败只记日志。"""
    if platform != "xianyu":
        return
    try:
        from app.services.xianyu_search_run import persist_xianyu_run
        st, items_n, saved_n, risk, err = _classify_xianyu_result(result)
        run_async(persist_xianyu_run(
            status=st, keyword_text=keyword, source=source,
            items_count=items_n, saved_count=saved_n,
            risk_signals=risk if isinstance(risk, list) else None, error=err,
        ))
    except Exception as e:  # noqa: BLE001 — 记录失败不影响采集
        logger.warning(f"persist_xianyu_run skipped for '{keyword}': {e}")


# ─────────────────────────────── instant_search result cache ──
#
# Redis-backed, 5-minute TTL. Keyed on (keyword, platform) — we assume
# platform=='xianyu' dominates but the cache shape anticipates future
# fan-out points.
#
# Cache only successful (at least one platform returning data) results.
# This prevents the "user spam-clicks search" scenario from forcing the
# compliance gate to burn fresh slots on the same keyword, while still
# allowing an honest retry after a failure.

_INSTANT_SEARCH_CACHE_PREFIX = "instant_search:v1:"
_INSTANT_SEARCH_CACHE_TTL = 300


def _cache_key_instant_search(keyword: str, platform: str) -> str:
    # Stable key: strip + lower keyword; platform is already normalised
    # at the API layer. We avoid hashing because the visibility matters
    # when debugging ("why did search return stale data?").
    return f"{_INSTANT_SEARCH_CACHE_PREFIX}{platform}:{keyword.strip().lower()}"


def _cache_lookup_instant_search(keyword: str, platform: str) -> dict | None:
    import redis as _sync_redis
    import json
    try:
        client = _sync_redis.from_url(get_settings().REDIS_URL)
        key = _cache_key_instant_search(keyword, platform)
        raw = client.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        ttl = client.ttl(key)
        data["_cache_age_seconds"] = max(0, _INSTANT_SEARCH_CACHE_TTL - (ttl or 0))
        return data
    except Exception as e:
        logger.warning(f"instant_search cache lookup failed: {e}")
        return None


def _cache_store_instant_search(
    keyword: str, platform: str, result: dict
) -> None:
    """Store a successful result. Results where every live platform
    came back ``__unavailable__`` are not cached — those are failure
    modes the user likely wants to retry, not reuse.
    """
    import redis as _sync_redis
    import json
    try:
        # Did we get at least one live platform's data?
        dims = result.get("dimensions") or {}
        any_live = any(
            not dims.get(p, {}).get("unavailable", True)
            for p in ("xianyu", "pdd", "1688", "xhs")
        ) if dims else False
        if not any_live and not result.get("active_listings"):
            logger.debug(
                "instant_search result has no live dimension, skipping cache"
            )
            return
        client = _sync_redis.from_url(get_settings().REDIS_URL)
        key = _cache_key_instant_search(keyword, platform)
        # Strip any transient fields before caching.
        payload = {k: v for k, v in result.items() if not k.startswith("_cache")}
        client.setex(key, _INSTANT_SEARCH_CACHE_TTL, json.dumps(payload, default=str))
    except Exception as e:
        logger.warning(f"instant_search cache store failed: {e}")


async def ensure_keyword_exists(db, keyword: str) -> Keyword:
    """Return the Keyword row for ``keyword``, creating it under the
    'uncategorized' bucket if the user typed a term that isn't curated yet.

    Uses ``INSERT ... ON CONFLICT DO NOTHING`` so concurrent Celery workers
    won't race-duplicate the row (the table has a UNIQUE(category_id, text)).
    """
    existing = await db.execute(
        select(Keyword).where(Keyword.text == keyword).limit(1)
    )
    row = existing.scalar_one_or_none()
    if row:
        return row

    cat_result = await db.execute(
        select(Category).where(Category.slug == "uncategorized").limit(1)
    )
    uncategorized = cat_result.scalar_one_or_none()
    if uncategorized is None:
        raise RuntimeError(
            "'uncategorized' seed category missing — "
            "did you forget to run `alembic upgrade head`?"
        )

    stmt = (
        pg_insert(Keyword)
        .values(
            category_id=uncategorized.id,
            text=keyword,
            target_platforms=["xianyu", "taobao", "pdd", "xiaohongshu"],
            max_items_per_platform=90,
            schedule_enabled=False,
            is_active=True,
        )
        .on_conflict_do_nothing(index_elements=["category_id", "text"])
    )
    await db.execute(stmt)
    await db.flush()

    refreshed = await db.execute(
        select(Keyword).where(
            Keyword.category_id == uncategorized.id,
            Keyword.text == keyword,
        ).limit(1)
    )
    return refreshed.scalar_one()


def _jsonify_risk_signals(payload):
    """Recursively convert any RiskSignal dataclass buried in ``payload``
    into a plain dict so Celery's JSON serializer doesn't choke when the
    task returns. ``_instant_search`` bundles risk_signals from each
    platform's crawl result into its return value (both the error-path
    fallback and the success path), and those signals are created as
    ``@dataclass`` instances — perfectly fine in-process, lethal as a
    Celery result.
    """
    from dataclasses import is_dataclass, asdict
    if is_dataclass(payload):
        return asdict(payload)
    if isinstance(payload, list):
        return [_jsonify_risk_signals(x) for x in payload]
    if isinstance(payload, dict):
        return {k: _jsonify_risk_signals(v) for k, v in payload.items()}
    return payload


async def _fetch_platform_with_retry(
    name: str,
    coro_factory,
    timeout_sec: int = 120,
    proxy_url: str | None = None,
    empty_check=None,
) -> dict:
    """Run ``coro_factory()`` with one retry on failure/empty, return dict.

    On terminal failure returns ``{"platform": name, "__unavailable__": True,
    "error": "..."}`` so the orchestrator can flag the corresponding scoring
    dimension without blowing up.
    """
    from app.services.proxy_service import invalidate_short_group

    def _is_empty(r: dict) -> bool:
        if empty_check is not None:
            return empty_check(r)
        return (
            (r.get("active_listings", 0) or 0) == 0
            and (r.get("total_notes", 0) or 0) == 0
        )

    last_err: str | None = None
    # Accumulate risk signals across both attempts so the orchestrator
    # can still fire a DingTalk alert even when every attempt comes back
    # empty (that's precisely the "platform is silently blocking us"
    # scenario we want to surface).
    accumulated_risks: list = []
    for attempt in range(2):
        try:
            result = await asyncio.wait_for(coro_factory(), timeout=timeout_sec)
            if isinstance(result, dict):
                accumulated_risks.extend(result.get("risk_signals") or [])
                # Short-circuit: when the compliance gate denies a call
                # (outside active hours / wait ceiling), retrying just
                # re-hits the gate and wastes another 60s+ of Celery
                # time. Surface the denial directly so the orchestrator
                # can tag the dimension and move on.
                err = result.get("error") or ""
                if result.get("__unavailable__") and err.startswith("compliance_gate:"):
                    logger.info(
                        f"{name}: compliance gate denied — skipping retry "
                        f"({err})"
                    )
                    return result
            if result and not _is_empty(result):
                return result
            last_err = "empty result"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning(f"{name} attempt {attempt + 1} failed: {last_err}")
        if attempt == 0 and proxy_url and proxy_url.startswith("qgshort:"):
            try:
                await invalidate_short_group(name)
                logger.info(f"{name}: rotating IP before retry")
            except Exception:
                pass

    logger.warning(f"{name}: giving up after retries — {last_err}")
    return {
        "platform": name,
        "__unavailable__": True,
        "error": last_err or "unknown",
        "risk_signals": accumulated_risks,
    }


async def _instant_search(
    keyword: str,
    platform: str,
    crawler_proxy_url: str | None = None,
    pdd_account: tuple | None = None,
    alibaba_account: tuple | None = None,
    # Back-compat: direct cookie injection (used by unit / smoke tests
    # that don't want to go through the account-pool layer).
    pdd_cookies: list[dict] | None = None,
    alibaba_cookies: list[dict] | None = None,
):
    if platform != "xianyu":
        return {"error": f"Platform {platform} not yet supported"}

    # Unpack the pool-picked accounts. The pool now returns a 4-tuple
    # ``(id, name, cookies, bound_area)``, but we still accept the legacy
    # 3-tuple from tests/smoke-runners that haven't migrated — the 4th
    # slot simply defaults to None (→ no area pinning, legacy group pool).
    def _unpack_account(tpl):
        if not tpl:
            return None, None, None, None
        if len(tpl) == 4:
            return tpl
        if len(tpl) == 3:
            return tpl[0], tpl[1], tpl[2], None
        raise ValueError(f"unexpected account tuple shape: {tpl!r}")

    pdd_account_id, _pdd_name, _pdd_cookies, pdd_bound_area = _unpack_account(pdd_account)
    if pdd_account_id:
        pdd_cookies = _pdd_cookies
    else:
        pdd_cookies = pdd_cookies or []

    alibaba_account_id, _ali_name, _ali_cookies, alibaba_bound_area = _unpack_account(alibaba_account)
    if alibaba_account_id:
        alibaba_cookies = _ali_cookies
    else:
        alibaba_cookies = alibaba_cookies or []

    if pdd_bound_area:
        logger.info(f"PDD account bound to area {pdd_bound_area}")
    if alibaba_bound_area:
        logger.info(f"1688 account bound to area {alibaba_bound_area}")

    proxy_url = crawler_proxy_url  # legacy name used below for the retry helper

    # ── Per-platform rate-limit gates. Redis-backed sliding window
    # shared across Celery workers; a failed gate returns an empty
    # placeholder so the orchestrator tags the dimension unavailable
    # rather than blocking the whole run.
    async def _gated(name: str, coro):
        allowed = await anti_risk.rate_limit_guard(name, sleep_if_blocked=False)
        if not allowed:
            logger.warning(f"{name}: hourly rate limit hit, skipping this run")
            return {
                "platform": name,
                "__unavailable__": True,
                "error": "rate_limited",
            }
        return await coro

    # ── Crawler factories: each builds its own anonymous context so the
    # three live platforms don't share fingerprints / proxies. All go
    # through the short-term crawler proxy pool — never the operation
    # account's long-term IP.
    #
    # Every factory funnels through ``compliance_gate`` first. For user-
    # triggered instant_search we pass actor="user" so the active-hours
    # rule is waived (humans at the dashboard legitimately run searches
    # at odd hours), but the pacing floor + jitter still apply: two
    # consecutive searches for the same platform will be spaced ≥60s
    # apart even if the UI submits them back-to-back.
    from app.services.compliance import compliance_gate

    async def _pass_gate(name: str) -> dict | None:
        decision = await compliance_gate(name, actor="user")
        if not decision:
            logger.warning(
                f"{name}: compliance gate denied — {decision.reason}"
            )
            return {
                "platform": name,
                "__unavailable__": True,
                "error": f"compliance_gate:{decision.reason}",
                "risk_signals": [],
            }
        return None

    async def _xianyu_call() -> dict:
        blocked = await _pass_gate("xianyu")
        if blocked:
            return blocked
        ctx = await browser_manager.get_anonymous_context(
            proxy_url=proxy_url, platform="xianyu"
        )
        # 「单词商品量」上限对闲鱼也生效：取运行时配置的 target_count_max 作为采集上限
        xy_max = 100
        try:
            from app.services.pdd_worker_config import get_runtime_config
            async with AsyncSessionLocal() as _cfg_db:
                _cfg = await get_runtime_config(_cfg_db)
            xy_max = max(1, int(_cfg.get("target_count_max") or 100))
        except Exception:  # noqa: BLE001 — 配置读不到就退回默认 100
            pass
        return await xianyu_crawler.collect_market_data(ctx, keyword, max_items=xy_max)

    # V3/V4 helpers — keep platform branches symmetric.
    from app.services.account_fingerprint import (
        get_or_init_fingerprint,
        merge_frozen_into,
        freeze_platform_cookies,
    )

    async def _pdd_call() -> dict:
        if _PDD_DISABLED:
            return {
                "platform": "pdd",
                "__unavailable__": True,
                "error": "platform_disabled:pdd_h5_search_closed",
                "risk_signals": [],
            }
        # H5 网页搜索通道已彻底下线（高风险，且 2026 起 PDD 对游客搜索返空），
        # 模块 app/services/pinduoduo/crawler.py 已删除。现在唯一的 PDD 通道是
        # 家里物理手机的 APP worker。拔 _PDD_DISABLED 时必须同时把
        # _PDD_USE_APP_WORKER 翻 True，否则没有可用通道、直接返回 unavailable。
        if _PDD_USE_APP_WORKER:
            return await _pdd_search_via_app_worker(keyword, mode="fast")
        return {
            "platform": "pdd",
            "__unavailable__": True,
            "error": "no_pdd_channel:h5_removed_and_app_worker_off",
            "risk_signals": [],
        }

    async def _1688_call() -> dict:
        if _ALIBABA_1688_DISABLED:
            return {
                "platform": "1688",
                "__unavailable__": True,
                "error": "platform_disabled:upstream_consistently_returns_empty",
                "risk_signals": [],
            }
        blocked = await _pass_gate("1688")
        if blocked:
            return blocked
        ctx = await browser_manager.get_anonymous_context(
            proxy_url=proxy_url, platform="1688",
            proxy_area=alibaba_bound_area, account_id=alibaba_account_id,
        )
        eff_cookies = alibaba_cookies
        if alibaba_account_id:
            try:
                fp = await get_or_init_fingerprint(alibaba_account_id)
                eff_cookies = merge_frozen_into(
                    fp, "1688", alibaba_cookies, ".1688.com"
                )
            except Exception as e:
                logger.warning(f"1688 frozen-cookie merge failed: {e}")
        result = await alibaba_1688_crawler.collect_market_data(
            ctx, keyword, cookies=eff_cookies,
        )
        if alibaba_account_id and (result.get("active_listings", 0) or 0) > 0:
            try:
                session_cookies = await ctx.cookies()
                await freeze_platform_cookies(
                    alibaba_account_id, "1688", session_cookies
                )
            except Exception as e:
                logger.warning(f"1688 freeze cookies failed: {e}")
        return result

    async def _xhs_call() -> dict:
        # XHS currently runs in placeholder mode
        # (``XhsHongshuCrawler._XHS_DISABLED=True`` short-circuits
        # ``collect_market_data``). We still pass through the
        # compliance gate so that flipping ``_XHS_DISABLED`` back on
        # can never quietly bypass rule 1 — the gate is the contract,
        # not an optimisation. XHS lives in phase 2 alongside xianyu
        # (~60s), so the gate's 5-25s jitter is absorbed by xianyu's
        # own runtime and adds zero wall-clock cost to instant_search.
        blocked = await _pass_gate("xiaohongshu")
        if blocked:
            return blocked
        ctx = await browser_manager.get_anonymous_context(
            proxy_url=proxy_url, platform="xiaohongshu"
        )
        return await xhs_crawler.collect_market_data(ctx, keyword)

    # Two-phase fan-out (ordering matters — see comment).
    #
    # Phase 1: PDD + 1688 concurrently.
    # Phase 2: xianyu + xhs placeholder.
    #
    # Why split? PDD needs window.rawData SSR hydration, which is a
    # single-threaded JS-heavy step; 1688 relies on DOM hydration.
    # Xianyu's crawler runs for ~60s (4 API pages + click-next-page
    # animations + homepage warmup) and hits Chromium's JS event loop
    # hard enough that a parallel PDD/1688 context starves and times
    # out before hydration completes — we measured both reliably flip
    # to 0 items when co-scheduled with xianyu.
    #
    # PDD and 1688 run together fine (they're short, ~10s combined) so
    # we keep that parallelism. Total wall-clock goes up by ~10s in the
    # worst case; in return we get consistent cross-platform data.
    _phase1_active = [
        n for n, off in [("pdd", _PDD_DISABLED), ("1688", _ALIBABA_1688_DISABLED)]
        if not off
    ]
    logger.info(
        f"Instant search '{keyword}': phase 1 → "
        + (" + ".join(_phase1_active) if _phase1_active
           else "disabled (pdd+1688 short-circuit, see §1.4)")
    )
    pdd_result, tb_result = await asyncio.gather(
        _gated("pdd",
               _fetch_platform_with_retry(
                   "pdd", _pdd_call, proxy_url=proxy_url,
                   empty_check=lambda r: (r.get("active_listings", 0) or 0) == 0,
               )),
        _gated("1688",
               _fetch_platform_with_retry(
                   "1688", _1688_call, proxy_url=proxy_url,
                   empty_check=lambda r: (r.get("active_listings", 0) or 0) == 0,
               )),
    )

    # ── Report crawler-pool outcome (success → +1 health, empty_result
    # → quarantine 60m). Classification:
    #   rate_limited (Redis gate)    → skip, keep rotation place
    #   retry-exhausted + empty      → BURN the account (the platform is
    #                                  silently blocking this号 — this is
    #                                  exactly the scenario the pool
    #                                  rotation exists for)
    #   retry-exhausted + exception  → skip (likely proxy / network)
    #   success                      → +1 health
    from app.services.crawler_accounts import report_crawler_result
    for platform_tag, account_id, result in (
        ("pdd", pdd_account_id, pdd_result),
        ("1688", alibaba_account_id, tb_result),
    ):
        if not account_id:
            continue
        err = result.get("error") or ""
        unavailable = result.get("__unavailable__", False)
        if unavailable and err == "rate_limited":
            # Our own Redis rate-gate kicked in before the crawl — account
            # didn't even get used, don't burn its place in the rotation.
            continue
        sigs = result.get("risk_signals") or []
        has_empty_signal = any(
            getattr(s, "signal_type", None) == "empty_result" for s in sigs
        )
        zero_items = (result.get("active_listings", 0) or 0) == 0
        # "empty result" is the magic string _fetch_platform_with_retry
        # uses to say it burned both attempts on zero items. That's the
        # clearest signal that the platform is shadow-banning this号.
        retry_exhausted_empty = unavailable and err == "empty result"
        burnt = has_empty_signal or retry_exhausted_empty or (
            not unavailable and zero_items
        )
        if unavailable and not retry_exhausted_empty:
            # Exception-path failure (proxy/network) — don't blame the号.
            continue
        await report_crawler_result(
            account_id,
            burnt=burnt,
            reason=f"{platform_tag}:empty_result" if burnt else None,
        )

    logger.info(
        f"Instant search '{keyword}': phase 2 → xianyu + xhs"
    )
    xy_result, xhs_result = await asyncio.gather(
        _gated("xianyu",
               _fetch_platform_with_retry(
                   "xianyu", _xianyu_call, proxy_url=proxy_url)),
        # XHS placeholder never fails so we skip the retry helper for it
        # — it just returns the zero-filled dict.
        _xhs_call(),
    )
    xianyu_unavailable = xy_result.get("__unavailable__", False)
    pdd_unavailable = pdd_result.get("__unavailable__", False)
    taobao_unavailable = tb_result.get("__unavailable__", False)
    # XHS: placeholder mode explicitly counts as unavailable for scoring.
    xhs_unavailable = (
        xhs_result.get("__unavailable__", False)
        or xhs_result.get("placeholder", False)
    )

    # ── Aggregate risk signals across platforms and fire a single
    # DingTalk alert. Cooldown logic lives inside anti_risk.flush_risk_alerts
    # so we won't spam on every run when a platform stays flagged.
    #
    # Pool-level suppression: if a platform's crawler pool was exhausted
    # (account is None → we knowingly ran in guest mode), its empty_result
    # risk signal is by-design noise. The burn has already been captured
    # by an earlier run + account cooldown; re-alerting every time the
    # ding arrives for "pool is cooling" just trains operators to ignore
    # the channel. We drop those signals here before flush_risk_alerts
    # sees them.
    signals_by_platform: dict[str, list[anti_risk.RiskSignal]] = {}
    pool_exhausted = {
        "pdd": pdd_account_id is None,
        "1688": alibaba_account_id is None,
    }
    for name, res in (
        ("xianyu", xy_result), ("pdd", pdd_result),
        ("1688", tb_result), ("xhs", xhs_result),
    ):
        sigs = res.get("risk_signals") if isinstance(res, dict) else None
        if not sigs:
            continue
        if pool_exhausted.get(name):
            logger.info(
                f"anti_risk: suppressing {len(sigs)} {name} signal(s) — "
                f"pool exhausted, guest-mode empty is expected"
            )
            continue
        signals_by_platform[name] = sigs
    if signals_by_platform:
        try:
            await anti_risk.flush_risk_alerts(keyword, signals_by_platform)
        except Exception as e:
            logger.warning(f"anti_risk alert dispatch failed: {e}")

    market_data = xy_result if not xianyu_unavailable else {}
    items = market_data.get("items", []) if isinstance(market_data, dict) else []

    if not items:
        logger.warning(f"Instant search '{keyword}': no xianyu items — aborting scoring")
        return _jsonify_risk_signals(market_data or {
            "error": "xianyu crawl failed",
            "platforms": {
                "pdd": pdd_result, "taobao": tb_result, "xiaohongshu": xhs_result,
            },
        })

    async with AsyncSessionLocal() as db:
        saved_product_ids = []
        product_data: dict[str, dict] = {}
        for item in items:
            if not item.get("item_id"):
                continue
            existing = await db.execute(
                select(Product).where(
                    Product.source_platform == Platform.XIANYU,
                    Product.source_id == item["item_id"],
                )
            )
            product = existing.scalar_one_or_none()
            # products.title is VARCHAR(512); some sellers cram the full listing
            # description into the title field, which overflows. Truncate safely.
            safe_title = (item.get("title") or "")[:500]
            if product:
                product.price = item.get("price", product.price)
                product.last_crawled_at = datetime.now(timezone.utc)
                if not product.published_at:
                    product.published_at = _ms_to_dt(item.get("publish_time_ms"))
                if not product.seller_name and item.get("seller_name"):
                    product.seller_name = item["seller_name"]
            else:
                product = Product(
                    source_platform=Platform.XIANYU,
                    source_url=item.get("url", ""),
                    source_id=item["item_id"],
                    title=safe_title,
                    price=item["price"],
                    image_urls=[item["image_url"]] if item.get("image_url") else None,
                    category=keyword,
                    sales_count=item.get("want_count", 0),
                    seller_name=(item.get("seller_name") or None),
                    published_at=_ms_to_dt(item.get("publish_time_ms")),
                    last_crawled_at=datetime.now(timezone.utc),
                )
                db.add(product)

            await db.flush()
            pid = str(product.id)
            saved_product_ids.append(pid)
            product_data[pid] = {
                "price": item.get("price") or 0,
                "want_count": item.get("want_count", 0),
                "title": item.get("title", ""),
            }

        if saved_product_ids and market_data.get("active_listings", 0) > 0:
            for pid in saved_product_ids:
                md = XianyuMarketData(
                    product_id=pid,
                    keyword=keyword,
                    active_listings=market_data["active_listings"],
                    total_wants=market_data.get("total_wants", 0),
                    price_min=market_data.get("price_min"),
                    price_max=market_data.get("price_max"),
                    price_avg=market_data.get("price_avg"),
                    price_cv=market_data.get("price_cv"),
                    top5_sales=market_data.get("top5_sales"),
                    seller_distribution=market_data.get("seller_distribution"),
                    captured_at=datetime.now(timezone.utc),
                )
                db.add(md)

        await db.commit()
        logger.info(
            f"Instant search '{keyword}': saved {len(saved_product_ids)} products, "
            f"market_data active_listings={market_data.get('active_listings', 0)}"
        )

        # 跨天同款观测（Phase 1）：按 xy:<item_id> 记当日一条，绝不连累采集
        try:
            from app.services.selection.sightings import (
                xianyu_item_key, record_sightings,
            )
            sight_recs = []
            for item in items:
                key = xianyu_item_key(item.get("item_id"))
                if not key:
                    continue
                sight_recs.append({
                    "item_key": key,
                    "title": (item.get("title") or "")[:500],
                    "price": item.get("price"),
                    "heat": item.get("want_count", 0),
                    "image_url": item.get("image_url"),
                })
            if sight_recs:
                await record_sightings(db, "xianyu", sight_recs, keyword=keyword)
        except Exception as e:
            logger.warning(f"xianyu record_sightings failed: {e}")

        # ─── P2 scoring pipeline ─────────────────────────────────────
        # 1) make sure this keyword exists in the curated library
        # 2) clean the sample (relevance + suite split + robust stats)
        # 3) score the keyword as a market (KeywordScore)
        # 4) score each product against its group's cleaned baseline
        # 5) rank by product total and write KeywordProduct links
        #    (hard-filter relevance < 4.0 out of the links, but keep the
        #     ProductScore row so operators can still investigate)
        try:
            keyword_row = await ensure_keyword_exists(db, keyword)
        except Exception as e:
            logger.error(f"ensure_keyword_exists failed: {e}")
            market_data["saved_products"] = len(saved_product_ids)
            return market_data

        cleaning_items = [
            {
                "product_id": pid,
                "title": d.get("title", ""),
                "price": d.get("price", 0) or 0,
                "item_wants": d.get("want_count", 0) or 0,
            }
            for pid, d in product_data.items()
        ]
        # Pre-compute the 1688 anchor here so both clean_keyword_sample
        # (for outlier-floor detection) and product_scoring (for profit
        # margin) see the same value.
        source_anchor = (
            float(tb_result.get("robust_price_min"))
            if (not taobao_unavailable and tb_result.get("robust_price_min"))
            else None
        )
        cleaned_items, single_stats, suite_stats = clean_keyword_sample(
            raw_items=cleaning_items,
            keyword=keyword,
            taobao_min_price=source_anchor,  # now fed by 1688 (see _instant_search)
        )

        # ---- Keyword-level score ----
        seller_dist = market_data.get("seller_distribution", {}) or {}
        top1_ratio = 0.0
        if seller_dist:
            total_sellers = sum(seller_dist.values())
            top1_count = max(seller_dist.values())
            top1_ratio = (top1_count / total_sellers * 100) if total_sellers > 0 else 0.0

        # Extract cross-platform signals from the gather results.
        # All "robust_price_min" values drop the bottom 5% before taking min
        # so a stray 1-yuan decoy doesn't blow up the margin calc.
        xhs_heat = (
            None if xhs_unavailable else (xhs_result.get("heat_score") or 0.0)
        )
        # tb_result now holds the 1688 market summary (variable name kept
        # to avoid churn further down the scoring pipeline). 1688 plays
        # the source-anchor role taobao used to — its `robust_price_min`
        # feeds both the cross-platform gap and the profit margin
        # calculation. See alibaba_1688/crawler.py for the field spec.
        source_min = (
            None if taobao_unavailable else tb_result.get("robust_price_min")
        )
        source_median = (
            None if taobao_unavailable else tb_result.get("price_median")
        )
        pdd_min = (
            None if pdd_unavailable else pdd_result.get("robust_price_min")
        )

        # Cross-platform gap: xianyu median vs 1688 median as percent gap.
        xy_median = market_data.get("price_avg") or 0.0
        gap_avg: float | None = None
        if source_median and source_median > 0 and xy_median > 0:
            gap_avg = round((xy_median - source_median) / source_median * 100.0, 1)

        kw_input_kwargs: dict = dict(
            active_listings=market_data.get("active_listings", 0) or 0,
            price_cv=market_data.get("price_cv", 0) or 0,
            top1_seller_ratio=top1_ratio,
            new_listing_ratio_7d=market_data.get("new_listing_ratio_7d", 0.0) or 0.0,
            total_wants=market_data.get("total_wants", 0) or 0,
        )
        if xhs_heat is not None:
            kw_input_kwargs["xhs_hotness"] = xhs_heat
        if gap_avg is not None:
            kw_input_kwargs["cross_platform_gap_avg"] = gap_avg
        kw_input = KeywordScoringInput(**kw_input_kwargs)

        kw_result = calculate_keyword_score(kw_input)
        kw_dim_dict = {
            d.name: {
                "score": d.score,
                "max": d.max_score,
                "label": d.label,
                "has_data": d.has_data,
            }
            for d in kw_result.dimensions
        }

        # ── Persist per-platform keyword summaries inline in the JSON field.
        # Avoids a schema migration while still letting the UI surface raw
        # cross-platform numbers next to the score. The _ prefix signals
        # "not a scored dimension, metadata only" (same pattern as _risk_tags).
        risk_tags: list[str] = []
        if pdd_unavailable:
            risk_tags.append("pdd_unavailable")
        if taobao_unavailable:
            risk_tags.append("1688_unavailable")
        if xhs_unavailable:
            # `xhs_placeholder` distinguishes "we chose not to crawl XHS"
            # from "the XHS crawler failed", which matters when the
            # dedicated XHS workstream eventually ships.
            risk_tags.append(
                "xhs_placeholder" if xhs_result.get("placeholder") else "xhs_unavailable"
            )

        def _platform_summary(res: dict, keys: tuple[str, ...]) -> dict:
            return {k: res.get(k) for k in keys}

        kw_dim_dict["_pdd_summary"] = (
            {"unavailable": True, "error": pdd_result.get("error")}
            if pdd_unavailable
            else _platform_summary(
                pdd_result,
                ("active_listings", "price_min", "robust_price_min",
                 "price_median", "total_sales", "captured_at"),
            )
        )
        kw_dim_dict["_1688_summary"] = (
            {"unavailable": True, "error": tb_result.get("error")}
            if taobao_unavailable
            else _platform_summary(
                tb_result,
                ("active_listings", "price_min", "robust_price_min",
                 "price_median", "total_sales", "captured_at"),
            )
        )
        kw_dim_dict["_xhs_summary"] = (
            {"unavailable": True, "placeholder": xhs_result.get("placeholder", False)}
            if xhs_unavailable
            else _platform_summary(
                xhs_result,
                ("total_notes", "avg_likes", "heat_score",
                 "product_note_ratio", "captured_at"),
            )
        )
        # Record risk_signal summaries for ops visibility. These are
        # already used by the DingTalk alert above; saving them inline
        # means we can reconstruct why a keyword was tagged after the fact.
        for name, sigs in signals_by_platform.items():
            kw_dim_dict[f"_{name}_risk_summary"] = anti_risk.risk_summary(sigs)
        if risk_tags:
            kw_dim_dict["_risk_tags"] = risk_tags

        db.add(KeywordScore(
            keyword_id=keyword_row.id,
            total_score=kw_result.total_score,
            dimension_scores=kw_dim_dict,
            decision=kw_result.decision,
            scored_at=datetime.now(timezone.utc),
        ))

        # ---- Product-level scores ----
        # Taobao/PDD anchor prices come from the keyword-level crawl (we
        # don't do same-item matching yet), so every product in the keyword
        # shares the same cross-platform reference. This is a coarse but
        # directionally correct signal — per-item image matching is P4+.
        settings = get_settings()
        scored: list[tuple] = []  # [(cleaned_product, ProductScoringResult)]
        for cp in cleaned_items:
            try:
                stats = suite_stats if cp.is_suite else single_stats
                pp_kwargs: dict = dict(
                    price=cp.price,
                    item_wants=cp.item_wants,
                    title=cp.title,
                    relevance_score=cp.relevance_score,
                    price_stats=stats,
                    is_suspicious_low=cp.is_suspicious_low,
                    logistics_cost=settings.SELECTION_LOGISTICS_COST,
                    loss_rate=settings.SELECTION_LOSS_RATE,
                )
                if source_min is not None:
                    # Legacy keyword arg name — the scoring engine uses
                    # this as the generic "cheapest external anchor"; we
                    # feed 1688 here since taobao is offline in P4.
                    pp_kwargs["taobao_match_price"] = float(source_min)
                if pdd_min is not None:
                    pp_kwargs["pdd_min_price"] = float(pdd_min)
                pp_input = ProductScoringInput(**pp_kwargs)
                pp_result = calculate_product_score(pp_input)
                dim_dict = {
                    d.name: {
                        "score": d.score,
                        "max": d.max_score,
                        "label": d.label,
                        "has_data": d.has_data,
                    }
                    for d in pp_result.dimensions
                }
                # Persist the merged risk tags inline on the score row so
                # the UI can surface them without rerunning the cleaner.
                merged_tags = list(dict.fromkeys(cp.risk_tags + pp_result.risk_tags))
                if merged_tags:
                    dim_dict["_risk_tags"] = merged_tags
                db.add(ProductScore(
                    product_id=cp.product_id,
                    score_type="product_10d",
                    total_score=pp_result.total_score,
                    dimension_scores=dim_dict,
                    decision=pp_result.decision,
                    scored_at=datetime.now(timezone.utc),
                ))
                scored.append((cp, pp_result))
            except Exception as e:
                logger.error(f"Auto-score failed for {cp.product_id}: {e}")

        # ---- KeywordProduct links: rank by total, drop low-relevance ----
        scored.sort(key=lambda x: x[1].total_score, reverse=True)
        now = datetime.now(timezone.utc)
        rank = 1
        for cp, _pp in scored:
            if cp.relevance_score < 4.0:
                continue  # hard-filter irrelevant matches out of the link table
            existing_link = await db.execute(
                select(KeywordProduct).where(
                    KeywordProduct.keyword_id == keyword_row.id,
                    KeywordProduct.product_id == cp.product_id,
                ).limit(1)
            )
            link = existing_link.scalar_one_or_none()
            if link:
                link.last_seen_at = now
                link.last_rank_in_search = rank
            else:
                db.add(KeywordProduct(
                    keyword_id=keyword_row.id,
                    product_id=cp.product_id,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_rank_in_search=rank,
                ))
            rank += 1

        # Update the keyword's last_crawled_at heartbeat
        keyword_row.last_crawled_at = now

        await db.commit()
        logger.info(
            f"Instant search '{keyword}': keyword_score={kw_result.total_score} "
            f"({kw_result.decision}); {len(scored)} products scored, "
            f"{rank - 1} linked; suspicious_single={single_stats.suspicious_count}"
        )

    market_data["saved_products"] = len(saved_product_ids)
    return _jsonify_risk_signals(market_data)
