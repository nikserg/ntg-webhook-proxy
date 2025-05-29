import logging
from asyncio import sleep
import contextlib
from aiogram import Bot, Dispatcher
import os
from aiogram.types import Message
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web, ClientSession, ClientResponse, TCPConnector
from aiogram.webhook.aiohttp_server import setup_application, SimpleRequestHandler
import asyncio
import socket

# Настройка логирования
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Получение URL основного сервиса и токена из переменных окружения
MAIN_SERVICE_URL = os.getenv("MAIN_SERVICE_URL")
MAIN_SERVICE_DEV_URL = os.getenv("MAIN_SERVICE_DEV_URL", MAIN_SERVICE_URL)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DOWN_FOR_MAINTENANCE = os.getenv("DOWN_FOR_MAINTENANCE", "0") == "1"
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "")
WEBHOOK_URL = WEBHOOK_BASE + "/webhook"
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 10))  # Максимальное количество попыток

logger.info(f"MAIN_SERVICE_URL: {MAIN_SERVICE_URL}\n"
            f"TELEGRAM_TOKEN: {TELEGRAM_TOKEN}\n"
            f"WEBHOOK_URL: {WEBHOOK_URL}\n"
            f"MAX_RETRIES: {MAX_RETRIES}\n"
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

    retry_delay = 3  # задержка в секундах

    # Подготовка данных для отправки
    data = {
        "text": message.text,
        "chat_id": message.chat.id
    }

    # Для административного чата используем dev URL
    if message.chat.id == ADMIN_CHAT_ID:
        service_url = MAIN_SERVICE_DEV_URL
    else:
        service_url = MAIN_SERVICE_URL

    # Выполняем запросы с повторными попытками
    for attempt in range(MAX_RETRIES):
        try:
            # Явно указываем использование IPv6
            connector = TCPConnector(family=socket.AF_INET6)
            async with ClientSession(connector=connector) as session:
                async with session.post(
                        service_url,
                        json=data,
                        ssl=False,
                        timeout=300  # таймаут запроса в секундах
                ) as response: # type: ClientResponse
                    if response.status == 200:
                        result = await response.text()
                        return result  # Возвращаем ответ от сервиса
                    else:
                        logging.warning(f"Получен статус {response.status} от сервиса на попытке {attempt + 1}")
        except Exception as e:
            logging.error(f"Ошибка при отправке запроса на попытке {attempt + 1}: {repr(e)}")

        # Если это не последняя попытка, делаем паузу перед следующей
        if attempt < MAX_RETRIES - 1:
            await sleep(retry_delay)

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
    chat_id = message.chat.id

    # Проверка на режим обслуживания
    if DOWN_FOR_MAINTENANCE and chat_id != ADMIN_CHAT_ID:
        await message.answer(
            "[Техническое обслуживание бота. Пожалуйста, повторите сообщение через 5 минут. Не переживайте, ваш диалог не потеряется и продолжится с того же места.]")
        return

    # Проверяем, является ли сообщение текстовым
    if not message.text:
        await message.answer("Только текстовые сообщения")
        return
    logging.info(f"Входящее сообщение от {message.chat.id}: {message.text}")

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
        retry_count = 5
        for attempt in range(retry_count):
            try:
                logging.info(f"Установка вебхука на {WEBHOOK_URL}, попытка {attempt + 1}/{retry_count}")
                await bot.set_webhook(WEBHOOK_URL)
                webhook_info = await bot.get_webhook_info()
                if webhook_info.url == WEBHOOK_URL:
                    logging.info(f"Вебхук успешно установлен на {WEBHOOK_URL}")
                    return
                logging.warning(f"Вебхук установлен некорректно: {webhook_info.url}")
            except Exception as e:
                logging.error(f"Ошибка при установке вебхука: {str(e)}")

            await asyncio.sleep(5)  # Ждем 5 секунд перед повторной попыткой

        logging.error(f"Не удалось установить вебхук после {retry_count} попыток")
    else:
        logging.warning("WEBHOOK_URL не указан. Вебхук не будет установлен.")

async def on_shutdown(app):
    if WEBHOOK_URL:
        await bot.delete_webhook()

app = web.Application()
# Регистрируем вебхук-обработчик ДО добавления хуков
if WEBHOOK_URL and bot and dp:
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
