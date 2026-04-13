"""
Multi-channel notification service.
Supports DingTalk webhook (primary) and SMTP email (fallback).
"""
import hashlib
import hmac
import base64
import time
import urllib.parse
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import httpx
from loguru import logger

from app.core.config import get_settings

settings = get_settings()


class NotificationService:
    """Dual-channel notification: DingTalk + Email."""

    async def _get_dingtalk_config(self) -> tuple[str, str]:
        """Get DingTalk webhook config: prefer DB settings, fall back to env."""
        webhook_url = settings.DINGTALK_WEBHOOK_URL
        secret = settings.DINGTALK_SECRET
        try:
            from sqlalchemy import select
            from app.core.database import AsyncSessionLocal
            from app.models.system import SystemConfig
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(SystemConfig).where(SystemConfig.key.in_(["dingtalk_webhook_url", "dingtalk_secret"]))
                )
                for row in result.scalars().all():
                    if row.key == "dingtalk_webhook_url" and row.value:
                        webhook_url = row.value
                    elif row.key == "dingtalk_secret" and row.value:
                        secret = row.value
        except Exception:
            pass
        return webhook_url, secret

    async def send_dingtalk(self, title: str, content: str, at_all: bool = False) -> bool:
        """Send a message via DingTalk custom robot webhook."""
        webhook_url, secret = await self._get_dingtalk_config()

        if not webhook_url:
            logger.debug("DingTalk webhook not configured, skipping notification")
            return False

        url = webhook_url

        if secret:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url = f"{url}&timestamp={timestamp}&sign={sign}"

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### {title}\n\n{content}\n\n---\n*转卖助手 {datetime.now(timezone.utc).strftime('%H:%M')} UTC*",
            },
            "at": {"isAtAll": at_all},
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                data = resp.json()
                if data.get("errcode") == 0:
                    logger.info(f"DingTalk sent: {title}")
                    return True
                else:
                    logger.warning(f"DingTalk error: {data}")
                    return False
        except Exception as e:
            logger.error(f"DingTalk send failed: {e}")
            return False

    async def send_email(self, to: str, subject: str, body: str, html: bool = False) -> bool:
        """Send email via SMTP."""
        if not settings.SMTP_HOST or not settings.SMTP_USER:
            logger.debug("SMTP not configured, skipping email")
            return False

        try:
            import aiosmtplib

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = settings.SMTP_USER
            msg["To"] = to

            if html:
                msg.attach(MIMEText(body, "html", "utf-8"))
            else:
                msg.attach(MIMEText(body, "plain", "utf-8"))

            await aiosmtplib.send(
                msg,
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USER,
                password=settings.SMTP_PASSWORD,
                use_tls=True,
            )
            logger.info(f"Email sent to {to}: {subject}")
            return True
        except ImportError:
            logger.warning("aiosmtplib not installed, email disabled")
            return False
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return False

    async def notify(
        self,
        title: str,
        content: str,
        level: str = "info",
        email_to: str | None = None,
    ) -> dict:
        """
        Send notification via all configured channels.
        level: "info" | "warning" | "critical"
        """
        results = {"dingtalk": False, "email": False}

        prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "")
        full_title = f"{prefix} {title}" if prefix else title

        results["dingtalk"] = await self.send_dingtalk(
            full_title, content, at_all=(level == "critical")
        )

        if email_to and (level in ("warning", "critical") or not results["dingtalk"]):
            results["email"] = await self.send_email(email_to, full_title, content)

        return results

    async def notify_new_order(self, order_data: dict):
        """Notification for new orders (auto mode — brief)."""
        content = (
            f"**平台**: {order_data.get('sale_platform', '闲鱼')}\n"
            f"**商品**: {order_data.get('item_title', '')}\n"
            f"**金额**: ¥{order_data.get('sale_price', 0)}\n"
            f"**买家**: {order_data.get('buyer_name', '')}\n\n"
            f"系统正在自动采购..."
        )
        await self.notify("新订单 - 自动采购中", content, level="info")

    async def notify_new_order_manual(self, order_data: dict):
        """Notification for new orders (manual mode — detailed for hand-purchasing)."""
        sale_price = order_data.get("sale_price", 0)
        source_price = order_data.get("source_price", 0)
        fee = round(sale_price * 0.006, 2)
        profit = round(sale_price - fee - source_price, 2) if source_price else None

        buyer_name = order_data.get("buyer_name", "")
        buyer_phone = order_data.get("buyer_phone", "")
        buyer_address = order_data.get("buyer_address", "")

        lines = [
            f"**商品**: {order_data.get('item_title', '')}",
            f"**售价**: ¥{sale_price}",
        ]

        if source_price:
            lines.append(f"**采购价**: ¥{source_price}")
        if profit is not None:
            lines.append(f"**预估利润**: ¥{profit}")

        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("**收货信息（长按复制）**:")
        lines.append("")
        addr_parts = [buyer_name, buyer_phone, buyer_address]
        lines.append(" ".join(p for p in addr_parts if p))

        source_url = order_data.get("source_url", "")
        source_platform = order_data.get("source_platform", "")
        if source_url:
            platform_label = {"pinduoduo": "拼多多", "taobao": "淘宝"}.get(source_platform, source_platform)
            lines.append("")
            lines.append("---")
            lines.append("")
            lines.append(f"**货源**: {platform_label}")
            lines.append(f"[点击打开货源链接]({source_url})")

        lines.append("")
        lines.append(f"**订单号**: {order_data.get('sale_order_id', '')}")

        content = "\n".join(lines)
        await self.notify("新订单 - 待手动采购", content, level="warning")

    async def notify_high_profit_product(self, product_data: dict):
        """Notification template for high-profit product discovery."""
        content = (
            f"**商品**: {product_data.get('title', '')}\n"
            f"**采购价**: ¥{product_data.get('cost', 0)}\n"
            f"**建议售价**: ¥{product_data.get('recommended_price', 0)}\n"
            f"**预估利润**: ¥{product_data.get('estimated_profit', 0)}\n"
            f"**评分**: {product_data.get('score', 0)}/100"
        )
        await self.notify("高利润商品发现", content, level="info")

    async def notify_price_anomaly(self, product_title: str, old_price: float, new_price: float):
        """Notification for significant price changes."""
        change = ((new_price - old_price) / old_price * 100) if old_price else 0
        direction = "上涨" if change > 0 else "下降"
        content = (
            f"**商品**: {product_title}\n"
            f"**原价**: ¥{old_price}\n"
            f"**现价**: ¥{new_price}\n"
            f"**变动**: {direction} {abs(change):.1f}%"
        )
        await self.notify("价格异常", content, level="warning")

    async def notify_order_error(self, order_id: str, error: str):
        """Notification for order processing errors."""
        content = f"**订单**: {order_id}\n**错误**: {error}"
        await self.notify("订单异常", content, level="critical")

    async def notify_account_risk(self, account_name: str, reason: str):
        """Notification for account risk events."""
        content = f"**账号**: {account_name}\n**风险**: {reason}\n**建议**: 暂停该账号操作，检查风控状态"
        await self.notify("账号风控预警", content, level="critical")


notification_service = NotificationService()
