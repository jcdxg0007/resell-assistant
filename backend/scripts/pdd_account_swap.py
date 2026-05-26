"""把一台物理手机上的 PDD crawler 账号从旧号切到新号。

用途：
- 旧号触发了风控墙（实名认证 / 滑块 / 封号），你已经在手机上换登了新号
- 数据库里需要把旧号 quarantine，把新号绑到同一台 device_serial 上

用法：
    python scripts/pdd_account_swap.py \\
        --device-serial PKT0220416005274 \\
        --old 4310 --new 7315 \\
        --reason "real_name_wall_2026-05-26"

设计原则：
- 旧号不删，只 quarantine（保留历史用于复盘）：
    is_active = false
    session_status = 'expired'
    lifecycle_stage = 'suspended'
    bound_device_serial = NULL
    suspended_reason = <reason>
- 新号若不存在则 INSERT，存在则 UPDATE，永远 bound_device_serial = <device>
- 新号的 cookies_data 留空（APP-only 账号不需要 H5 cookies）

幂等：可重复执行；如果新号已经绑好了，再跑一次就只是更新 updated_at。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg

from app.core.config import get_settings


async def _swap(
    device_serial: str,
    old_suffix: str,
    new_suffix: str,
    reason: str,
) -> None:
    raw_url = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    old_name = f"pdd_crawler_{old_suffix}"
    new_name = f"pdd_crawler_{new_suffix}"

    conn = await asyncpg.connect(raw_url)
    try:
        async with conn.transaction():
            # ── 1. quarantine 旧号 ────────────────────────────────
            old_row = await conn.fetchrow(
                "SELECT id, is_active, bound_device_serial FROM accounts "
                "WHERE account_name = $1 AND platform = 'pdd_crawler'",
                old_name,
            )
            if old_row:
                await conn.execute(
                    """
                    UPDATE accounts
                    SET is_active = false,
                        session_status = 'expired',
                        lifecycle_stage = 'suspended',
                        bound_device_serial = NULL,
                        suspended_reason = $1,
                        updated_at = NOW()
                    WHERE id = $2
                    """,
                    reason, old_row["id"],
                )
                print(
                    f"[OK] quarantined {old_name} "
                    f"(was bound to: {old_row['bound_device_serial'] or '—'})"
                )
            else:
                print(f"[WARN] {old_name} 不在 accounts 表里，跳过 quarantine")

            # ── 2. 取消其他号占用同一台设备 ─────────────────────────
            # 防御性：如果有别的 pdd_crawler 账号意外绑到这台手机，先解绑，
            # 保持 1-机-1-号铁律
            cleared = await conn.fetch(
                """
                UPDATE accounts
                SET bound_device_serial = NULL, updated_at = NOW()
                WHERE platform = 'pdd_crawler'
                  AND bound_device_serial = $1
                  AND account_name <> $2
                RETURNING account_name
                """,
                device_serial, new_name,
            )
            for r in cleared:
                print(f"[OK] 解绑 stale binding: {r['account_name']} ← {device_serial}")

            # ── 3. UPSERT 新号 ─────────────────────────────────────
            new_row = await conn.fetchrow(
                "SELECT id FROM accounts "
                "WHERE account_name = $1 AND platform = 'pdd_crawler'",
                new_name,
            )
            if new_row:
                await conn.execute(
                    """
                    UPDATE accounts
                    SET is_active = true,
                        session_status = 'none',
                        lifecycle_stage = 'nurturing',
                        bound_device_serial = $1,
                        suspended_reason = NULL,
                        cooldown_until = NULL,
                        updated_at = NOW()
                    WHERE id = $2
                    """,
                    device_serial, new_row["id"],
                )
                print(f"[OK] updated {new_name} → bound to {device_serial}")
            else:
                # APP-only 账号：cookies_data 留 NULL（H5 路径不会再用它）
                await conn.execute(
                    """
                    INSERT INTO accounts
                        (id, account_name, platform, identity_group, lifecycle_stage,
                         daily_publish_limit, daily_published_count,
                         health_score, is_active, session_status,
                         bound_device_serial, cookies_data, created_at, updated_at)
                    VALUES (gen_random_uuid(), $1, 'pdd_crawler', 'crawler', 'nurturing',
                            0, 0, 100, true, 'none',
                            $2, NULL, NOW(), NOW())
                    """,
                    new_name, device_serial,
                )
                print(f"[OK] inserted {new_name} → bound to {device_serial}")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device-serial", required=True,
                        help="物理手机 adb serial (e.g. PKT0220416005274)")
    parser.add_argument("--old", required=True,
                        help="旧号手机尾号（如 4310）")
    parser.add_argument("--new", required=True,
                        help="新号手机尾号（如 7315）")
    parser.add_argument("--reason", required=True,
                        help="quarantine 旧号的原因（如 real_name_wall_2026-05-26）")
    args = parser.parse_args()

    asyncio.run(_swap(
        device_serial=args.device_serial,
        old_suffix=args.old,
        new_suffix=args.new,
        reason=args.reason,
    ))


if __name__ == "__main__":
    main()
