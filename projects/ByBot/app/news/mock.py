from datetime import datetime, timezone

from app.models import Asset, NewsItem


def mock_news_item() -> NewsItem:
    return NewsItem(
        title="Major institution expands Bitcoin treasury allocation",
        summary="The institution announced a material new BTC purchase.",
        source="mock-wire",
        published_at=datetime.now(timezone.utc),
        asset_hint=Asset.BTC,
        importance=0.9,
    )
