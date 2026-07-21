# Fixture: missing_import
# Bug: json.loads() called without importing json

def parse_config(config_str: str) -> dict:
    """Parse JSON configuration string."""
    return json.loads(config_str)  # BUG: json not imported


def get_setting(config_str: str, key: str):
    data = parse_config(config_str)
    return data.get(key)
