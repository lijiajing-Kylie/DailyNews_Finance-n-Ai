"""Base scraper interface."""

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional
import httpx

from ..models import ContentItem

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Abstract base class for all scrapers."""

    def __init__(self, config: dict, http_client: httpx.AsyncClient):
        """Initialize scraper.

        Args:
            config: Scraper-specific configuration
            http_client: Shared async HTTP client
        """
        self.config = config
        self.client = http_client

    def _resolve_category(self, category: Optional[str], source_label: str) -> str:
        """Return the source's category, defaulting to "other" with a warning.

        Args:
            category: The category configured on the source, if any
            source_label: Human-readable label for the source (used in the warning)

        Returns:
            str: The configured category, or "other" when unset
        """
        if not category:
            logger.warning(
                "Source '%s' has no category configured; defaulting to 'other'",
                source_label,
            )
            return "other"
        return category

    @abstractmethod
    async def fetch(self, since: datetime) -> List[ContentItem]:
        """Fetch content items published since the given time.

        Args:
            since: Only fetch items published after this time

        Returns:
            List[ContentItem]: Fetched content items
        """
        pass

    def _generate_id(self, source_type: str, subtype: str, native_id: str) -> str:
        """Generate unique content item ID.

        Args:
            source_type: Source type (github, hackernews, etc.)
            subtype: Content subtype (event, release, story, etc.)
            native_id: Native ID from the source platform

        Returns:
            str: Unique ID in format {source}:{subtype}:{native_id}
        """
        return f"{source_type}:{subtype}:{native_id}"
