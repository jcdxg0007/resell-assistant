from app.models.product import Product, PriceSnapshot, ProductMatch, ProductScore, ProductImage
from app.models.virtual_product import VirtualProduct, VirtualDelivery
from app.models.xianyu import XianyuListing, XianyuMarketData
from app.models.xiaohongshu import (
    XhsNote, XhsNoteAnalytics, XhsHotTopic,
    XhsTrendingKeyword, XhsCompetitorNote, XhsShopProduct, XhsContentTemplate,
)
from app.models.order import Order, Logistics
from app.models.customer import Conversation, Message, ReplyTemplate
from app.models.ai_ops import DailyReport, DailyCheck, AiSuggestion
from app.models.system import Account, Task, Notification, SystemConfig, User
from app.models.selection import (
    Category, Keyword, KeywordProduct, KeywordScore, SelectionAnalysis,
    PddCategoryAccount,
)
from app.models.pdd_run import PddSearchRun
from app.models.xianyu_run import XianyuSearchRun
from app.models.logistics_run import LogisticsRun

__all__ = [
    "Product", "PriceSnapshot", "ProductMatch", "ProductScore", "ProductImage",
    "VirtualProduct", "VirtualDelivery",
    "XianyuListing", "XianyuMarketData",
    "XhsNote", "XhsNoteAnalytics", "XhsHotTopic",
    "XhsTrendingKeyword", "XhsCompetitorNote", "XhsShopProduct", "XhsContentTemplate",
    "Order", "Logistics",
    "Conversation", "Message", "ReplyTemplate",
    "DailyReport", "DailyCheck", "AiSuggestion",
    "Account", "Task", "Notification", "SystemConfig", "User",
    "Category", "Keyword", "KeywordProduct", "KeywordScore", "SelectionAnalysis",
    "PddCategoryAccount",
    "PddSearchRun",
    "XianyuSearchRun",
    "LogisticsRun",
]
