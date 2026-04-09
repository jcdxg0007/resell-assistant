"""
Smart pricing engine for Xianyu listings.
Calculates recommended sale price based on competition level and market data.
"""
from dataclasses import dataclass

XIANYU_FEE_RATE = 0.006  # 0.6%
DEFAULT_RETURN_RESERVE = 0.08  # 8% reserve for returns
MIN_PROFIT_BUFFER = 5.0  # minimum ¥5 profit


@dataclass
class PricingResult:
    mode: str
    recommended_price: float
    price_floor: float
    estimated_profit: float
    profit_margin: float
    breakdown: dict


def calculate_price_floor(cost: float, shipping: float = 0.0) -> float:
    """Calculate the absolute minimum sale price."""
    base = cost + shipping + MIN_PROFIT_BUFFER
    return round(base / (1 - XIANYU_FEE_RATE - DEFAULT_RETURN_RESERVE), 2)


def calculate_profit(sale_price: float, cost: float, shipping: float = 0.0) -> dict:
    """Calculate detailed profit breakdown."""
    fee = round(sale_price * XIANYU_FEE_RATE, 2)
    return_reserve = round(sale_price * DEFAULT_RETURN_RESERVE, 2)
    gross = sale_price - cost - shipping
    net = gross - fee - return_reserve
    margin = (net / cost * 100) if cost > 0 else 0
    return {
        "sale_price": sale_price,
        "cost": cost,
        "shipping": shipping,
        "platform_fee": fee,
        "return_reserve": return_reserve,
        "gross_profit": round(gross, 2),
        "net_profit": round(net, 2),
        "margin_pct": round(margin, 1),
    }


def smart_pricing(
    cost: float,
    shipping: float,
    xianyu_active_listings: int,
    xianyu_avg_price: float | None = None,
    xianyu_top5_prices: list[float] | None = None,
) -> PricingResult:
    """Determine pricing strategy based on market competition."""
    floor = calculate_price_floor(cost, shipping)

    # Information gap pricing: very few competitors
    if xianyu_active_listings <= 5:
        multiplier = 2.0 if xianyu_active_listings <= 2 else 1.6
        price = round(max(cost * multiplier, floor), 2)
        breakdown = calculate_profit(price, cost, shipping)
        return PricingResult(
            mode="信息差定价",
            recommended_price=price,
            price_floor=floor,
            estimated_profit=breakdown["net_profit"],
            profit_margin=breakdown["margin_pct"],
            breakdown=breakdown,
        )

    # Competitive pricing: moderate competition
    if xianyu_active_listings <= 20 and xianyu_top5_prices:
        avg_top5 = sum(xianyu_top5_prices) / len(xianyu_top5_prices)
        price = round(max(avg_top5 * 0.95, floor), 2)
        breakdown = calculate_profit(price, cost, shipping)
        return PricingResult(
            mode="竞争定价",
            recommended_price=price,
            price_floor=floor,
            estimated_profit=breakdown["net_profit"],
            profit_margin=breakdown["margin_pct"],
            breakdown=breakdown,
        )

    # Thin margin pricing: heavy competition
    if xianyu_avg_price and xianyu_avg_price > floor:
        price = round(min(xianyu_avg_price * 0.9, floor + 10), 2)
        price = max(price, floor)
        breakdown = calculate_profit(price, cost, shipping)
        if breakdown["net_profit"] < MIN_PROFIT_BUFFER:
            return PricingResult(
                mode="建议放弃",
                recommended_price=0,
                price_floor=floor,
                estimated_profit=0,
                profit_margin=0,
                breakdown=calculate_profit(floor, cost, shipping),
            )
        return PricingResult(
            mode="薄利定价",
            recommended_price=price,
            price_floor=floor,
            estimated_profit=breakdown["net_profit"],
            profit_margin=breakdown["margin_pct"],
            breakdown=breakdown,
        )

    # Fallback
    price = floor
    breakdown = calculate_profit(price, cost, shipping)
    return PricingResult(
        mode="保底定价",
        recommended_price=price,
        price_floor=floor,
        estimated_profit=breakdown["net_profit"],
        profit_margin=breakdown["margin_pct"],
        breakdown=breakdown,
    )
