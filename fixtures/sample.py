# fixtures/sample.py
# Sample file used by Task 5 "done when" criterion.
# Contains a deliberate bug: json.loads() called without importing json.
# The pipeline should detect this as a 'missing-import' finding.


def load_settings(raw: str) -> dict:
    """Load settings from a JSON string.

    Args:
        raw: JSON string to parse.

    Returns:
        Parsed settings dictionary.
    """
    # BUG: json module is not imported at the top of this file
    return json.loads(raw)


def get_debug_flag(raw: str) -> bool:
    settings = load_settings(raw)
    return settings.get("debug", False)


class AppConfig:
    """Application configuration loaded from JSON."""

    def __init__(self, json_str: str):
        self._data = load_settings(json_str)

    def get(self, key: str, default=None):
        return self._data.get(key, default)
