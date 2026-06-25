import aiosqlite

DB_NAME = "tracker.db"


async def create_database():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            website TEXT NOT NULL,
            status TEXT DEFAULT 'unknown'
        )
        """)

        await db.commit()


async def add_product(name, url, website):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO products(name,url,website) VALUES(?,?,?)",
            (name, url, website)
        )
        await db.commit()


async def get_products():
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id,name,url,website,status FROM products"
        )

        rows = await cursor.fetchall()

        return rows


async def remove_product(product_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM products WHERE id=?",
            (product_id,)
        )

        await db.commit()


async def update_status(product_id, status):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE products SET status=? WHERE id=?",
            (status, product_id)
        )

        await db.commit()
