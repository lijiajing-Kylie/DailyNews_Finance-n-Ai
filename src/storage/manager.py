"""Storage manager for configuration and state persistence."""

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..models import Config, ContentItem


# Matches ${VAR_NAME} in string config values. Names follow env-var rules
# (ASCII letters, digits, underscore; must not start with a digit).
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_vars(value: Any) -> Any:
    """Recursively expand ``${VAR}`` references inside any string leaves.

    Containers (dicts, lists, tuples) are walked; non-string leaves are
    returned unchanged. Strings with no ``${...}`` tokens are returned
    unchanged. References to unset variables are **left as-is**, so
    ``${MISSING}`` round-trips to ``${MISSING}`` and surfaces as a clear
    downstream error rather than a silent empty string.

    This is intentionally identical to the behaviour ``RSSScraper`` uses
    for RSS feed URLs, so a single ``${VAR}`` convention works everywhere
    in the config (AI ``base_url``, feed URLs, webhook URLs, ...).
    """
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_expand_env_vars(v) for v in value)
    return value


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""

    pass


class StorageManager:
    """Manages file-based storage for configuration and state."""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "config.json"
        self.summaries_dir = self.data_dir / "summaries"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.summaries_dir.mkdir(parents=True, exist_ok=True)

    def load_config(self) -> Config:
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {self.config_path}\n"
                f"Please create it based on the template in README.md"
            )

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(
                f"Invalid JSON in configuration file: {self.config_path}\n" f"Error: {e}"
            ) from e

        # Expand ${VAR} references in every string value before pydantic
        # validation. Keeps credentials / private endpoints / tenant IDs
        # out of the JSON file so it is safe to commit to a public repo.
        data = _expand_env_vars(data)

        try:
            return Config.model_validate(data)
        except ValidationError as e:
            raise ConfigError(
                f"Configuration validation failed for {self.config_path}\n"
                f"Details: {e}"
            ) from e

    def save_config(self, config: Config, backup: bool = True) -> Path:
        """Save configuration to config.json, optionally backing up the existing file.

        Args:
            config: The Config object to save.
            backup: If True and config.json exists, copy it to config.json.bak first.

        Returns:
            Path to the saved config file.
        """
        if backup and self.config_path.exists():
            shutil.copy2(self.config_path, self.config_path.with_suffix(".json.bak"))

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config.model_dump(mode="json"), f, indent=2, ensure_ascii=False)
            f.write("\n")

        return self.config_path

    def save_daily_summary(self, date: str, markdown: str, language: str = "zh") -> Path:
        filename = f"horizon-{date}-{language}.md"
        filepath = self.summaries_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(markdown)

        return filepath

    def save_important_items(
        self,
        date: str,
        items: list[ContentItem],
        language: str = "zh",
        all_items_count: int | None = None,
    ) -> Path:
        """Persist the final digest items as JSON.

        Lets a later standalone step (e.g. re-pushing the webhook) rebuild
        the exact same ContentItem list a full pipeline run produced,
        without re-running fetch/score/enrich.
        """
        filename = f"horizon-{date}-{language}-items.json"
        filepath = self.summaries_dir / filename
        payload = {
            "all_items_count": all_items_count if all_items_count is not None else len(items),
            "items": [item.model_dump(mode="json") for item in items],
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        return filepath

    def load_important_items(
        self, date: str, language: str = "zh"
    ) -> tuple[list[ContentItem], int]:
        """Load a previously persisted digest: (items, all_items_count).

        Returns ([], 0) if no saved digest exists for that date/language.
        """
        filename = f"horizon-{date}-{language}-items.json"
        filepath = self.summaries_dir / filename
        if not filepath.exists():
            return [], 0

        with open(filepath, "r", encoding="utf-8") as f:
            payload = json.load(f)

        items = [ContentItem.model_validate(item) for item in payload["items"]]
        return items, payload.get("all_items_count", len(items))

    def find_latest_items_date(self, language: str = "zh") -> str | None:
        """Find the most recent date with a saved digest items JSON file."""
        candidates = sorted(self.summaries_dir.glob(f"horizon-*-{language}-items.json"))
        if not candidates:
            return None
        # "horizon-2026-07-09-zh-items.json" -> "2026-07-09"
        name_parts = candidates[-1].stem.split("-")
        return "-".join(name_parts[1:4])

    def load_subscribers(self) -> list:
        """Loads the list of email subscribers."""
        subscribers_path = self.data_dir / "subscribers.json"
        if not subscribers_path.exists():
            return []

        try:
            with open(subscribers_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return []

    def add_subscriber(self, email_addr: str):
        """Adds a new subscriber email."""
        subscribers = self.load_subscribers()
        if email_addr not in subscribers:
            subscribers.append(email_addr)
            self._save_subscribers(subscribers)

    def remove_subscriber(self, email_addr: str):
        """Removes a subscriber email."""
        subscribers = self.load_subscribers()
        if email_addr in subscribers:
            subscribers.remove(email_addr)
            self._save_subscribers(subscribers)

    def _save_subscribers(self, subscribers: list):
        """Helper to save subscribers list."""
        subscribers_path = self.data_dir / "subscribers.json"
        with open(subscribers_path, "w", encoding="utf-8") as f:
            json.dump(subscribers, f, indent=2)
