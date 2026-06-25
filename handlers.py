from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from config import OWNER_ID
from database import Database
from states import AddProductState, RemoveProductState

router = Router()
db = Database()


def detect_website(url: str):
    url = url.lower()

    if "amazon" in url:
        return "Amazon"

    if "flipkart" in url:
        return "Flipkart"

    if "zepto" in url:
        return "Zepto"

    if "bigbasket" in url:
        return "BigBasket"

    return "Unknown"


@router.message(Command("start"))
async def start(message: Message):

    if message.from_user.id != OWNER_ID:
        return

    await message.answer(
        "✅ Tracker Alert Bot Started\n\n"
        "/add\n"
        "/list\n"
        "/remove"
    )


@router.message(Command("add"))
async def add_product(message: Message, state: FSMContext):

    if message.from_user.id != OWNER_ID:
        return

    await state.set_state(AddProductState.waiting_for_name)

    await message.answer("📦 Product Name?")


@router.message(AddProductState.waiting_for_name)
async def product_name(message: Message, state: FSMContext):

    await state.update_data(name=message.text)

    await state.set_state(AddProductState.waiting_for_link)

    await message.answer("🔗 Product Link?")


@router.message(AddProductState.waiting_for_link)
async def product_link(message: Message, state: FSMContext):

    data = await state.get_data()

    name = data["name"]
    url = message.text

    website = detect_website(url)

    try:
        await db.add_product(name, url, website)

        await message.answer(
            f"✅ Product Added\n\n"
            f"📦 {name}\n"
            f"🌐 {website}"
        )

    except Exception:
        await message.answer("❌ Product already exists.")

    await state.clear()


@router.message(Command("list"))
async def list_products(message: Message):

    if message.from_user.id != OWNER_ID:
        return

    products = await db.get_products()

    if not products:
        await message.answer("No products added.")
        return

    text = "📋 Product List\n\n"

    for product in products:
        text += (
            f"{product[0]}. {product[1]}\n"
            f"🌐 {product[3]}\n\n"
        )

    await message.answer(text)


@router.message(Command("remove"))
async def remove_product(message: Message, state: FSMContext):

    if message.from_user.id != OWNER_ID:
        return

    await state.set_state(RemoveProductState.waiting_for_product_id)

    await message.answer("Enter Product ID")


@router.message(RemoveProductState.waiting_for_product_id)
async def delete_product(message: Message, state: FSMContext):

    try:
        product_id = int(message.text)

        await db.delete_product(product_id)

        await message.answer("✅ Product Removed")

    except Exception:
        await message.answer("❌ Invalid Product ID")

    await state.clear()
