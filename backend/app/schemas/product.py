from pydantic import BaseModel
from typing import Any
from datetime import datetime


class ProductOut(BaseModel):
    id: str
    source_platform: str
    title: str
    price: float
    original_price: float | None = None
    shipping_fee: float = 0.0
    category: str | None = None
    product_type: str = "physical"
    image_urls: list[str] | None = None
    sales_count: int = 0
    is_active: bool = True
    created_at: datetime

    model_config = {"from_attributes": True}


class ProductScoreOut(BaseModel):
    product_id: str
    score_type: str
    total_score: float
    decision: str
    dimension_scores: dict
    scored_at: datetime

    model_config = {"from_attributes": True}


class MarketDataOut(BaseModel):
    keyword: str
    active_listings: int
    total_wants: int
    price_min: float | None
    price_max: float | None
    price_avg: float | None
    price_cv: float | None
    top5_sales: Any | None
    seller_distribution: Any | None
    captured_at: datetime

    model_config = {"from_attributes": True}


class PricingOut(BaseModel):
    mode: str
    recommended_price: float
    price_floor: float
    estimated_profit: float
    profit_margin: float
    breakdown: dict


class SearchRequest(BaseModel):
    keyword: str
    platform: str = "xianyu"
    max_items: int = 30


class ScoreRequest(BaseModel):
    source_price: float
    shipping_fee: float = 0.0
    source_good_review_rate: float = 95.0
    has_compat_complaints: bool = False
    weekly_growth_rate: float = 5.0


class RecommendationOut(BaseModel):
    product: ProductOut
    score: ProductScoreOut | None = None
    market_data: MarketDataOut | None = None
    pricing: PricingOut | None = None
