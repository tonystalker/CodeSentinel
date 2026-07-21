# Fixture: sql_injection
# Bug: user-controlled input directly concatenated into SQL query

import sqlite3


def get_user(db_path: str, username: str) -> dict | None:
    """Fetch a user record by username."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    # BUG: SQL injection — username directly interpolated into query string
    # Fix: use parameterized query: cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    query = f"SELECT * FROM users WHERE username = '{username}'"
    cursor.execute(query)
    row = cursor.fetchone()
    conn.close()
    return {"username": row[0], "email": row[1]} if row else None


def delete_user(db_path: str, user_id: str) -> None:
    """Delete a user by ID."""
    conn = sqlite3.connect(db_path)
    # BUG: SQL injection via string concatenation
    conn.execute("DELETE FROM users WHERE id = " + user_id)
    conn.commit()
    conn.close()
