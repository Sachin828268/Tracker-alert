import sqlite3
import logging
from typing import Optional
from config import DB_PATH

logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database schema."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                url         TEXT    NOT NULL,
                site        TEXT    NOT NULL,
                in_stock    INTEGER NOT NULL DEFAULT 0,
                last_checked TEXT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, url)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_id ON products(user_id)
        """)
        conn.commit()
    logger.info("Database initialized.")


def add_product(user_id: int, name: str, url: str, site: str) -> tuple[bool, str]:
    """
    Add a product for a user.
    Returns (success: bool, message: str).
    """
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO products (user_id, name, url, site)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, name, url, site),
            )
            conn.commit()
        return True, "Product added successfully."
    except sqlite3.IntegrityError:
        return False, "You are already tracking this URL."
    except Exception as e:
        logger.error(f"add_product error: {e}")
        return False, "Database error while adding product."


def list_products(user_id: int) -> list[dict]:
    """Return all products for a user."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM products WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def remove_product(user_id: int, product_id: int) -> bool:
    """Remove a product by ID for a user. Returns True if deleted."""
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM products WHERE id = ? AND user_id = ?",
            (product_id, user_id),
        )
        conn.commit()
    return cursor.rowcount > 0


def get_all_products() -> list[dict]:
    """Return every tracked product (for the background checker)."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM products").fetchall()
    return [dict(row) for row in rows]


def update_stock_status(product_id: int, in_stock: bool):
    """Update the stock status and last-checked timestamp."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE products
            SET in_stock = ?, last_checked = datetime('now')
            WHERE id = ?
            """,
            (1 if in_stock else 0, product_id),
        )
        conn.commit()


def get_product_by_id(product_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
    return dict(row) if row else None
