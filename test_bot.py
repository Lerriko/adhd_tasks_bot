import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from bot import make_router
from storage import Storage


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    # unittest обнаруживает методы test_*. IsolatedAsyncioTestCase создаёт
    # отдельный цикл asyncio для каждого теста. assert* проверяют ожидания.
    # Асинхронная подготовка каждого теста: отдельная база, бот и Dispatcher.
    # Токен вымышленный; AsyncMock заменяет сеть, сообщения в Telegram не уходят.
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Storage(Path(self.temp.name) / 'db.sqlite')
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        self.bot.session.make_request = AsyncMock(return_value=True)
        # Подменяем только сетевой вызов; роутер и хранилище остаются настоящими.
        self.dp = Dispatcher()
        self.dp.include_router(make_router(self.store))
        self.counter = 0

    # После теста закрываем сетевую сессию, SQLite и удаляем временную папку.
    async def asyncTearDown(self):
        await self.bot.session.close()
        self.store.db.close()
        self.temp.cleanup()

    # Собирает искусственное событие Telegram и передаёт настоящим обработчикам.
    # Это позволяет проверить последовательность нажатий без живого бота.
    async def send(self, text=None, callback=None, voice=None, forwarded=False, photo=False, document=None, caption=None):
        self.counter += 1
        user = {'id': 42, 'is_bot': False, 'first_name': 'Test'}
        message = {'message_id': self.counter, 'date': 1,
                   'chat': {'id': 42, 'type': 'private'}, 'from': user}
        if text:
            message['text'] = text
        if forwarded:
            message['forward_origin'] = {'type': 'hidden_user', 'date': 1, 'sender_user_name': 'Someone'}
        if voice:
            message['voice'] = {'file_id': voice, 'file_unique_id': 'unique', 'duration': 2}
        if photo:
            message['photo'] = [
                {'file_id': 'small', 'file_unique_id': 's', 'width': 90, 'height': 90},
                {'file_id': 'large', 'file_unique_id': 'l', 'width': 1280, 'height': 1280}]
        if document:
            message['document'] = {'file_id': 'image-file', 'file_unique_id': 'd', 'mime_type': document}
        if caption:
            message['caption'] = caption
        payload = {'update_id': self.counter}
        if callback:
            payload['callback_query'] = {'id': str(self.counter), 'from': user,
                                         'chat_instance': 'test', 'message': message, 'data': callback}
        else:
            payload['message'] = message
        await self.dp.feed_update(self.bot, Update.model_validate(payload))
        # model_validate превращает словарь в Update; feed_update запускает
        # ту же маршрутизацию, которую рабочий бот использует при polling.

    # Сценарий: голосовое → отложенный разбор → задача → напоминание → архив.
    async def test_voice_to_task_to_archive(self):
        await self.send(voice='voice-id')
        await self.send(callback='later:1')
        self.assertEqual(self.store.get(1, 42)['reminder_kind'], 'review')
        await self.send(callback='edit:1')
        await self.send(text='Отправить макет')
        self.assertEqual(self.store.get(1, 42)['status'], 'task')
        await self.send(callback='m60:1')
        self.assertEqual(self.store.get(1, 42)['reminder_kind'], 'task')
        await self.send(callback='archive:1')
        self.assertIsNone(self.store.get(1, 42)['due'])
        self.assertEqual(self.store.get(1, 42)['status'], 'archive')

    # После /cancel следующий текст должен стать заметкой, а не названием старой.
    async def test_cancel_title_preserves_new_note(self):
        await self.send(text='First')
        await self.send(callback='edit:1')
        await self.send(text='/cancel')
        await self.send(text='Second')
        self.assertEqual(len(self.store.listing(42, 'inbox')), 2)

    # Неверная дата сохраняет ожидание повторного ввода; отмена не стирает расписание.
    async def test_custom_date_setup_validation_and_cancel(self):
        await self.send(text='Разобрать идею')
        await self.send(callback='date:1')
        await self.send(text='+05:30')
        self.assertEqual(self.store.offset(42), 330)
        await self.send(text='не дата')
        self.assertEqual(self.store.input_state(42)['kind'], 'date')
        self.assertIsNone(self.store.get(1, 42)['due'])
        await self.send(text='01.01.2099 10:00')
        note = self.store.get(1, 42)
        self.assertEqual(note['reminder_kind'], 'review')
        self.assertIsNone(self.store.input_state(42))
        await self.send(callback='date:1')
        await self.send(text='/cancel')
        self.assertEqual(self.store.get(1, 42)['due'], note['due'])

    # Смена UTC не сдвигает назначенное уведомление; настройка переживает открытие базы заново.
    async def test_settings_do_not_move_existing_reminder(self):
        await self.send(text='Заметка')
        await self.send(callback='later:1')
        due = self.store.get(1, 42)['due']
        await self.send(text='/settings')
        await self.send(text='-03:30')
        self.assertEqual(self.store.get(1, 42)['due'], due)
        self.store.db.close()
        self.store = Storage(Path(self.temp.name) / 'db.sqlite')
        self.assertEqual(self.store.offset(42), -210)

    # Голосовое сохраняется отдельно, пока пользователь вводит дату для другой записи.
    async def test_voice_does_not_interrupt_date_input(self):
        await self.send(text='Заметка')
        self.store.set_offset(42, 0)
        await self.send(callback='date:1')
        await self.send(voice='new-voice')
        self.assertEqual(self.store.input_state(42)['kind'], 'date')
        self.assertEqual(len(self.store.listing(42, 'inbox')), 2)

    # Название кнопки меню не становится названием задачи; переход завершает ввод.
    async def test_menu_navigation_cancels_title_without_saving_button(self):
        await self.send(text='Заметка')
        await self.send(callback='edit:1')
        await self.send(text='☑️ Задачи')
        self.assertIsNone(self.store.get(1, 42)['title'])
        await self.send(text='Новая заметка')
        self.assertEqual(len(self.store.listing(42, 'inbox')), 2)

    # Переход между разделами сбрасывает ввод даты/UTC, но сохраняет прежнее напоминание.
    async def test_menu_exits_date_input_and_preserves_reminder(self):
        await self.send(text='Заметка')
        await self.send(callback='later:1')
        due = self.store.get(1, 42)['due']
        self.store.set_offset(42, 180)
        await self.send(callback='date:1')
        await self.send(text='📥 Входящие')
        self.assertIsNone(self.store.input_state(42))
        self.assertEqual(self.store.get(1, 42)['due'], due)
        await self.send(text='⚙️ Настройки')
        self.assertEqual(self.store.input_state(42)['kind'], 'zone')
        await self.send(text='🗂 Архив')
        self.assertIsNone(self.store.input_state(42))
        self.assertEqual(len(self.store.listing(42, 'inbox')), 1)

    # Пересланное сообщение с текстом кнопки — заметка, а не команда навигации.
    async def test_forwarded_menu_label_is_saved_as_note(self):
        await self.send(text='📥 Входящие', forwarded=True)
        self.assertEqual(self.store.get(1, 42)['body'], '📥 Входящие')

    # Проверяем время дела, все три относительных варианта и своё время.
    # Перенос дела не сдвигает уведомление автоматически; keep убирает только due.
    async def test_event_and_custom_reminder_are_independent(self):
        await self.send(text='Выйти из дома')
        await self.send(callback='edit:1')
        await self.send(text='Выйти из дома')
        await self.send(callback='event:1')
        self.assertEqual(self.store.input_state(42)['kind'], 'zone_event')
        await self.send(text='+03:00')
        await self.send(text='01.01.2099 14:30')
        event = self.store.get(1, 42)['event_at']
        self.assertIsNone(self.store.get(1, 42)['due'])
        await self.send(callback='before5:1')
        self.assertEqual(self.store.get(1, 42)['due'], event - 300)
        await self.send(callback='before15:1')
        self.assertEqual(self.store.get(1, 42)['due'], event - 900)
        await self.send(callback='before0:1')
        self.assertEqual(self.store.get(1, 42)['due'], event)
        await self.send(callback='date:1')
        await self.send(text='01.01.2099 14:23')
        self.assertEqual(self.store.get(1, 42)['due'], event - 420)
        await self.send(callback='event:1')
        await self.send(text='01.01.2099 15:00')
        self.assertEqual(self.store.get(1, 42)['event_at'], event + 1800)
        self.assertEqual(self.store.get(1, 42)['due'], event - 420)
        await self.send(callback='keep:1')
        self.assertIsNone(self.store.get(1, 42)['due'])
        self.assertEqual(self.store.get(1, 42)['event_at'], event + 1800)

    # Если вариант «за 5 минут» уже в прошлом, не перезаписываем прежний due.
    async def test_past_relative_reminder_keeps_previous_schedule(self):
        import time
        await self.send(text='Звонок')
        self.store.update(1, 42, status='task', event_at=time.time() + 60, due=12345678900, reminder_kind='task')
        await self.send(callback='before5:1')
        self.assertEqual(self.store.get(1, 42)['due'], 12345678900)

    # Выбирается большая версия фото, оригинал и подпись сохраняются после создания задачи.
    async def test_photo_task_preserves_original_and_caption(self):
        await self.send(photo=True, caption='Скрин задания', forwarded=True)
        self.assertEqual(self.store.get(1, 42)['photo'], 'large')
        await self.send(callback='edit:1')
        await self.send(text='Сделать задание')
        await self.send(callback='m30:1')
        await self.send(callback='original:1')
        request = self.bot.session.make_request.call_args.args[1]
        # Последний вызов mock содержит объект исходящего запроса Telegram.
        self.assertEqual(request.photo, 'large')
        self.assertEqual(request.caption, 'Скрин задания')
        self.assertEqual(self.store.get(1, 42)['reminder_kind'], 'task')

    # PNG сохраняется во время ввода даты, PDF отклоняется без создания записи.
    async def test_image_file_saved_during_date_input(self):
        await self.send(text='Заметка')
        self.store.set_offset(42, 180)
        await self.send(callback='date:1')
        await self.send(document='image/png')
        self.assertEqual(self.store.input_state(42)['kind'], 'date')
        await self.send(callback='original:2')
        request = self.bot.session.make_request.call_args.args[1]
        self.assertEqual(request.document, 'image-file')
        await self.send(document='application/pdf')
        self.assertEqual(len(self.store.listing(42, 'inbox')), 2)


if __name__ == '__main__':
    unittest.main()
