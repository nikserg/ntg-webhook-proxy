import logging
from asyncio import sleep
import contextlib
from aiogram import Bot, Dispatcher
import os
from aiogram.types import Message
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web, ClientSession, ClientResponse
from aiogram.webhook.aiohttp_server import setup_application, SimpleRequestHandler
import asyncio

# Настройка логирования
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Получение URL основного сервиса и токена из переменных окружения
MAIN_SERVICE_URL = os.getenv("MAIN_SERVICE_URL") + '/internal'
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "")
WEBHOOK_URL = WEBHOOK_BASE + "/webhook"

logger.info(f"MAIN_SERVICE_URL: {MAIN_SERVICE_URL}\n"
            f"TELEGRAM_TOKEN: {TELEGRAM_TOKEN}\n"
            f"WEBHOOK_URL: {WEBHOOK_URL}"
            )

# Хранилище для отслеживания активной обработки сообщений
active_chats = set()

# Создаём бота только если есть токен
if TELEGRAM_TOKEN:
    bot = Bot(token=TELEGRAM_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
else:
    logging.info("TELEGRAM_TOKEN не указан. Бот не будет запущен.")
    bot = None
    storage = None
    dp = None


async def keep_typing(chat_id: int, interval: float = 4.0):
    """Периодически отправляет статус 'печатает' в чат."""
    while True:
        await bot.send_chat_action(chat_id, "typing")
        await asyncio.sleep(interval)


async def process_message_with_retries(message: Message):
    """Отправляет сообщение в основной сервис с повторными попытками."""

    max_retries = 3
    retry_delay = 1.0  # начальная задержка в секундах

    # Выполняем запросы с повторными попытками
    for attempt in range(max_retries):
        try:
            async with ClientSession() as session:
                async with session.post(
                        MAIN_SERVICE_URL,
                        data=message.text,  # отправляем текст без изменений
                        timeout=30  # таймаут запроса в секундах
                ) as response: # type: ClientResponse
                    if response.status == 200:
                        result = await response.text()
                        return result  # Возвращаем ответ от сервиса
                    else:
                        logging.warning(f"Получен статус {response.status} от сервиса на попытке {attempt + 1}")
        except Exception as e:
            logging.error(f"Ошибка при отправке запроса на попытке {attempt + 1}: {str(e)}")

        # Если это не последняя попытка, делаем паузу перед следующей
        if attempt < max_retries - 1:
            # Увеличиваем задержку с каждой попыткой
            await sleep(retry_delay * (2 ** attempt))

    # Если все попытки исчерпаны, возвращаем сообщение об ошибке
    return "[Ой! Кажется, у меня техническая проблема под кодовым названием Кокосик]"


@contextlib.asynccontextmanager
async def typing_action(chat_id: int):
    """Контекстный менеджер, поддерживающий статус 'печатает' активным."""
    task = asyncio.create_task(keep_typing(chat_id))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@dp.message()
async def handle_message(message: Message):
    # Выходим, если бот не инициализирован (для тестов)
    if not bot:
        return

    # Проверяем, является ли сообщение текстовым
    if not message.text:
        await message.answer("Только текстовые сообщения")
        return
    logging.info(f"Входящее сообщение от {message.chat.id}: {message.text}")
    chat_id = message.chat.id
    # Проверяем, обрабатывается ли уже сообщение для данного чата
    if chat_id in active_chats:
        logging.info(f"Сообщение от {chat_id} проигнорировано, так как обработка уже идёт.")
        await message.answer("Пожалуйста, подожди, пока я отвечу на твое предыдущее сообщение.")
        return
    # Добавляем чат в список активных
    active_chats.add(chat_id)

    # Обрабатываем сообщение, поддерживая статус 'печатает'
    try:
        async with typing_action(chat_id):
            response = await process_message_with_retries(message)
            logging.info(f"Ответ для {chat_id}: {response}")
            await message.answer(response)
    finally:
        # Убираем чат из списка активных
        active_chats.remove(chat_id)


# Создание и запуск aiohttp-приложения

async def on_startup(app):
    if WEBHOOK_URL:
        logging.info(f"Установка вебхука на {WEBHOOK_URL}")
        await bot.set_webhook(WEBHOOK_URL)
    else:
        logging.warning("WEBHOOK_URL не указан. Вебхук не будет установлен.")


async def on_shutdown(app):
    if WEBHOOK_URL:
        await bot.delete_webhook()


app = web.Application()
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if WEBHOOK_URL:
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
