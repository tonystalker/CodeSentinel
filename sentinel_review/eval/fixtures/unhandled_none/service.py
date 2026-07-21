# Fixture: unhandled_none
# Bug: accessing attribute on a value that may be None without guard

def get_user_display_name(user_record: dict | None) -> str:
    """Return display name for a user record."""
    # BUG: user_record can be None (e.g. when user not found)
    # Accessing .get() on None raises AttributeError
    return user_record.get("display_name") or user_record.get("username", "anonymous")


def process_response(response) -> str:
    """Extract body from an HTTP response object."""
    # BUG: response.json() may return None if body is empty; calling .get() on None crashes
    data = response.json()
    return data.get("message", "no message")  # AttributeError if data is None


class UserService:
    def __init__(self):
        self._cache: dict = {}

    def get_name(self, user_id: str) -> str:
        user = self._cache.get(user_id)
        # BUG: user can be None if not in cache; .name crashes
        return user.name  # AttributeError if user is None
