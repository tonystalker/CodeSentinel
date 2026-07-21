# Fixture: hardcoded_secret
# Bug: API key and database password hardcoded directly in source

import requests


# BUG: Hardcoded API key — will be committed to version control
API_KEY = "sk-prod-a1b2c3d4e5f6789012345678abcdefgh"
DB_PASSWORD = "super_secret_password_123"  # BUG: hardcoded credential


def fetch_data(endpoint: str) -> dict:
    """Fetch data from the API."""
    # BUG: API key in code, not environment variable
    response = requests.get(
        endpoint,
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    return response.json()


def get_db_connection():
    """Get database connection using hardcoded credentials."""
    import psycopg2
    # BUG: password hardcoded, not from environment
    return psycopg2.connect(
        host="db.example.com",
        database="production",
        user="admin",
        password=DB_PASSWORD,  # BUG: hardcoded credential
    )
