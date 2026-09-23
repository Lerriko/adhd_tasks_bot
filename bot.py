"""Запуск: python bot.py. Обработчики переводят действия Telegram в операции SQLite."""
import asyncio
# asyncio управляет корутинами: пока одна ждёт сеть или таймер,
# другая может обработать сообщение. Это не отдельный поток для каждой функции.
import logging
import os
import time
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
                           BotCommand, ReplyKeyboardMarkup, KeyboardButton)
from dotenv import load_dotenv

# Bot обращается к Telegram API, Dispatcher принимает события, Router выбирает
# функцию для события. F — конструктор фильтров по полям, а Command — по командам.
# Аннотации Message / CallbackQuery поясняют тип аргумента для IDE и читателя.

from storage import Storage
from scheduling import parse_offset, zone_label, local_date, parse_date


MENU_SECTIONS = {'📥 Входящие': 'inbox', '☑️ Задачи': 'task', '🗂 Архив': 'archive'}


# Нижняя клавиатура: кнопки отправляют боту обычный текст.
# Это отличается от кнопок под карточкой, которые отправляют callback_data.
def main_menu():
    return ReplyKeyboardMarkup(
        # Внешний список — ряды клавиатуры, внутренний — кнопки в одном ряду.
        keyboard=[
            [KeyboardButton(text='📥 Входящие'), KeyboardButton(text='☑️ Задачи')],
            [KeyboardButton(text='🗂 Архив'), KeyboardButton(text='⚙️ Настройки')],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder='Отправь текст, фото или голосовое',
    )


# Собирает кнопки под сообщением из рядов пар (надпись, данные).
# Например, ("Готово", "archive:12") передаёт обработчику действие и ID записи.
def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


# Выбирает доступные действия по статусу записи.
# У архива остаётся только оригинал; у задачи есть выполнение и время дела.
def actions(note):
    n = note['id']
    if note['status'] == 'archive':
        return keyboard([[('Открыть оригинал', f'original:{n}')]])
    if note['status'] == 'task':
        rows = [[('✅ Выполнено', f'archive:{n}'), ('⏰ Напомнить', f'time:{n}')],
                [('📅 Время дела', f'event:{n}')]]
    else:
        rows = [[('✍️ Оформить сейчас', f'edit:{n}'), ('⏳ Через 30 минут', f'later:{n}')],
                [('⏰ Выбрать время разбора', f'time:{n}')],
                [('📥 Оставить заметкой', f'keep:{n}'), ('🗂 В архив', f'archive:{n}')]]
    return keyboard(rows + [[('Открыть оригинал', f'original:{n}')]])


# Быстрые варианты напоминаний. «За 5 минут» имеет смысл только
# при заданном event_at; своё время и интервалы доступны без него.
def reminder_options(note):
    n = note['id']
    rows = []
    if note['status'] == 'task' and note['event_at'] is not None:
        rows = [[('В момент дела', f'before0:{n}')],
                [('За 5 минут', f'before5:{n}'), ('За 15 минут', f'before15:{n}')]]
    return keyboard(rows + [
        [('📅 Своё время', f'date:{n}')],
        [('Через 30 минут', f'm30:{n}'), ('Через час', f'm60:{n}')],
        [('Через 24 часа', f'm1440:{n}'), ('Без напоминания', f'keep:{n}')]])


# Возвращает текст карточки, ничего не отправляя и не меняя в базе.
# event_at — время самого дела, due — момент отправки напоминания.
# Цепочка a or b or c берёт первое непустое значение для заголовка.
def card(note, offset=0):
    label = {'inbox': '📥 Заметка', 'task': '☑️ Задача', 'archive': '🗂 Архив'}[note['status']]
    media = '🖼 Изображение' if note['photo'] or note['image_document'] else '🎤 Голосовое' if note['voice'] else ''
    title = note['title'] or note['body'] or media
    attachment = '\n' + media if media and title != media else ''
    event = '\n📅 Время дела: ' + local_date(note['event_at'], offset) if note['event_at'] is not None else ''
    reminder = ('\n🔔 ' + ('Разобрать: ' if note['reminder_kind'] == 'review' else 'Напомнить: ')
                + local_date(note['due'], offset)) if note['due'] else ''
    return f"{label} #{note['id']}\n{title[:3500]}{attachment}{event}{reminder}"


# Создаёт и регистрирует обработчики. Вложенные функции помнят store
# из внешней функции (замыкание), поэтому имеют доступ к одной базе.
# Порядок важен: специальные команды идут раньше общего обработчика save.
# async def объявляет корутину; await отдаёт управление на время ожидания.
def make_router(store):
    router = Router()
    # Все обработчики этого роутера работают только в личном чате с ботом.
    router.message.filter(F.chat.type == 'private')
    router.callback_query.filter(F.message.chat.type == 'private')

    # /start сбрасывает ожидание ввода и показывает меню.
    # Перезапускать Python эта команда не умеет.
    @router.message(CommandStart())
    async def start(message: Message):
        store.cancel(message.from_user.id)
        await message.answer('Присылай текст, голосовые или картинки — я сразу сохраню их. Оформить задачу можно сейчас или позже.\n\n'
                             '/inbox — входящие\n/tasks — задачи\n/archive — архив\n/settings — местное время\n/cancel — отменить ввод\n\n'
                             'Разделы доступны на кнопках внизу. Переход в раздел отменяет незавершённый ввод.\n'
                             'Напоминания: через выбранный интервал или в точную дату. Для точной даты открой настройки.',
                             reply_markup=main_menu())

    # Сюда ведут и команда, и кнопка настроек. Два декоратора регистрируют
    # одну функцию для двух вариантов входа. ~F.forward_origin исключает пересылки.
    # wait_input запоминает, что следующий обычный текст будет смещением UTC.
    @router.message(F.text == '⚙️ Настройки', ~F.forward_origin)
    @router.message(Command('settings'))
    async def settings(message: Message):
        user = message.from_user.id
        offset = store.offset(user)
        store.wait_input(user, 'zone')
        current = zone_label(offset) if offset is not None else 'не настроено'
        await message.answer(f'Местное время: {current}.\nВведи смещение от UTC, например +03:00 для Москвы или +05:30.\n'
                             'Смещение постоянное: при переходе на летнее время его нужно изменить вручную. '
                             'Уже назначенные напоминания сохранят момент отправки.\n/cancel — отменить.',
                             reply_markup=main_menu())

    # Отменяет только диалог ввода. Сами записи и расписание не удаляются.
    @router.message(Command('cancel'))
    async def cancel(message: Message):
        store.cancel(message.from_user.id)
        await message.answer('Ввод отменён. Сохранённые записи и напоминания на месте.', reply_markup=main_menu())

    # Показывает страницу из пяти записей. Шестая нужна как признак продолжения.
    # Здесь offset — число пропускаемых записей, а store.offset — смещение времени.
    async def show_list(target, user_id, status, offset=0):
        notes = store.listing(user_id, status, offset)
        if not notes:
            await target.answer('Здесь пока пусто.')
        for note in notes[:5]:
            await target.answer(card(note, store.offset(user_id) or 0), reply_markup=actions(note))
        if len(notes) > 5:
            await target.answer('Следующие записи:', reply_markup=keyboard([
                [('Дальше →', f'list:{status}:{offset + 5}')]]))

    # Нижнее меню и команды открывают один и тот же список.
    # Перед переходом сбрасываем ожидание названия/даты, чтобы кнопку не сохранить
    # как название задачи. Пользователю сообщаем об отмене незавершённого ввода.
    @router.message(F.text.in_(MENU_SECTIONS), ~F.forward_origin)
    @router.message(Command('inbox', 'tasks', 'archive'))
    async def lists(message: Message):
        user = message.from_user.id
        had_input = store.input_state(user) is not None or store.db.execute(
            'SELECT 1 FROM pending WHERE user_id=?', (user,)).fetchone() is not None
        store.cancel(user)
        status = MENU_SECTIONS.get(message.text)
        if status is None:
            # /tasks@имя_бота и /tasks приводим к одному ключу /tasks.
            command = message.text.split()[0].split('@')[0]
            status = {'/inbox': 'inbox', '/tasks': 'task', '/archive': 'archive'}[command]
        label = next(label for label, value in MENU_SECTIONS.items() if value == status)
        await message.answer(label + ('\nНезавершённый ввод отменён. Записи и напоминания сохранены.' if had_input else ''),
                             reply_markup=main_menu())
        await show_list(message, user, status)

    # Разбирает callback_data кнопок, например before5:12 или list:inbox:5.
    # query.from_user.id — тот, кто нажал кнопку. Запись ищем только у него.
    # query.answer() закрывает индикатор ожидания на кнопке, а message.answer()
    # отправляет отдельное сообщение. Это разные операции Telegram.
    @router.callback_query()
    async def callback(query: CallbackQuery):
        parts = (query.data or '').split(':')
        # Формат проверяем до int(): данные кнопок могут быть некорректными.
        action = parts[0]
        user = query.from_user.id
        if action == 'list' and len(parts) == 3 and parts[1] in {'inbox', 'task', 'archive'} and parts[2].isdigit():
            await query.answer()
            await show_list(query.message, user, parts[1], int(parts[2]))
            return
        if len(parts) != 2 or not parts[1].isdigit():
            await query.answer('Неизвестная кнопка.')
            return
        note = store.get(int(parts[1]), user)
        # Не доверяем одному ID из кнопки: get проверяет также владельца.
        if not note:
            await query.answer('Запись не найдена.')
            return
        await query.answer()
        n = note['id']
        if action == 'original':
            # file_id позволяет повторно отправить медиа, не скачивая на диск.
            # Telegram хранит фото и файл-изображение как разные типы сообщений.
            caption = (note['body'] or '')[:1024] or None
            if note['photo']:
                await query.message.answer_photo(note['photo'], caption=caption)
            elif note['image_document']:
                await query.message.answer_document(note['image_document'], caption=caption)
            elif note['voice']:
                await query.message.answer_voice(note['voice'], caption=caption)
            else:
                await query.message.answer(note['body'])
            if (note['photo'] or note['image_document'] or note['voice']) and len(note['body'] or '') > 1024:
                await query.message.answer(note['body'])
            return
        if note['status'] == 'archive':
            await query.message.answer('Эта запись уже в архиве.')
            return
        if action == 'edit':
            store.wait_title(user, n)
            await query.message.answer(f'Напиши название задачи #{n} (до 200 символов).\n/cancel — отменить. Следующий обычный текст станет названием; пересланные сообщения сохранятся отдельно.')
        elif action == 'time':
            await query.message.answer('Когда напомнить?', reply_markup=reminder_options(note))
        elif action in {'before0', 'before5', 'before15'}:
            # before5 -> 5 минут -> 300 секунд до времени дела.
            if note['status'] != 'task' or note['event_at'] is None:
                await query.message.answer('Сначала назначь «📅 Время дела» у задачи.')
                return
            due = note['event_at'] - int(action.removeprefix('before')) * 60
            if due <= time.time():
                await query.message.answer('Это время напоминания уже прошло. Выбери другое.', reply_markup=reminder_options(note))
                return
            store.update(n, user, due=due, reminder_kind='task')
            store.cancel(user)
            await query.message.answer(card(store.get(n, user), store.offset(user) or 0), reply_markup=actions(note))
        elif action in {'date', 'event'}:
            # Это пока только начало диалога. Сам текст даты прочитает save().
            # zone_event помнит, что после настройки UTC мы ждём именно время дела.
            if action == 'event' and note['status'] != 'task':
                await query.message.answer('Сначала оформи запись как задачу.')
                return
            offset = store.offset(user)
            kind = action if offset is not None else ('zone_event' if action == 'event' else 'zone')
            store.wait_input(user, kind, n)
            if offset is None:
                await query.message.answer('Сначала введи своё смещение от UTC, например +03:00 для Москвы.\n/cancel — отменить.')
            else:
                purpose = 'дела' if action == 'event' else 'напоминания'
                await query.message.answer(f'Введи дату и время {purpose}: ДД.ММ.ГГГГ ЧЧ:ММ ({zone_label(offset)}).\n/cancel — отменить.')
        elif (action == 'later' and note['status'] == 'inbox') or action in {'m30', 'm60', 'm1440'}:
            # Интервалы считаем от текущего момента, не от event_at.
            minutes = 30 if action == 'later' else int(action[1:])
            store.update(n, user, due=time.time() + minutes * 60,
                         reminder_kind='review' if note['status'] == 'inbox' else 'task')
            store.cancel(user)
            await query.message.answer(f'Напомню о записи #{n} через {minutes} мин.\n' +
                                       card(store.get(n, user), store.offset(user) or 0), reply_markup=actions(note))
        elif action == 'keep':
            # None запишется как SQL NULL: активного напоминания больше нет.
            store.cancel(user)
            store.update(n, user, due=None, reminder_kind=None)
            await query.message.answer('Сохранено без напоминания.')
        elif action == 'archive':
            # Архив — смена статуса, а не физическое удаление данных.
            store.cancel(user)
            store.update(n, user, status='archive', due=None, reminder_kind=None)
            await query.message.answer('Готово, запись в архиве.')
        else:
            await query.message.answer('Эта кнопка устарела. Открой запись через меню.')

    # Общий обработчик сообщений: сначала проверяет поддерживаемый формат,
    # затем ожидаемый ввод даты/названия и только после этого создаёт заметку.
    # Пересланный текст и медиа сохраняются отдельно даже во время диалога ввода.
    # return завершает обработчик, чтобы одно сообщение не обработалось дважды.
    @router.message()
    async def save(message: Message):
        user = message.from_user.id
        if message.text and message.text.startswith('/'):
            await message.answer('Неизвестная команда. Список команд: /start')
            return
        image_document = message.document if message.document and (message.document.mime_type or '').startswith('image/') else None
        # MIME image/* отличает файл картинки от PDF и других документов.
        # Здесь файл только сохраняется по ID, содержимое не анализируется.
        if not (message.text or message.voice or message.photo or image_document):
            await message.answer('Я сохраняю текст, голосовые и картинки. Скриншот можно отправить как фото или как файл изображения.')
            return
        if message.text and not message.forward_origin:
            state = store.input_state(user)
            if state:
                # Состояние хранится в SQLite и не теряется при перезапуске.
                try:
                    if state['kind'] in {'zone', 'zone_event'}:
                        offset = parse_offset(message.text)
                        store.set_offset(user, offset)
                        store.cancel(user)
                        if state['note_id'] is not None:
                            kind = 'event' if state['kind'] == 'zone_event' else 'date'
                            store.wait_input(user, kind, state['note_id'])
                            purpose = 'дела' if kind == 'event' else 'напоминания'
                            await message.answer(f'Сохранено {zone_label(offset)}. Теперь введи дату {purpose}: ДД.ММ.ГГГГ ЧЧ:ММ.\n/cancel — отменить.')
                        else:
                            await message.answer(f'Местное время настроено: {zone_label(offset)}.')
                    else:
                        note = store.get(state['note_id'], user)
                        if not note or note['status'] == 'archive':
                            store.cancel(user)
                            await message.answer('Запись уже в архиве или недоступна. Ввод отменён.')
                            return
                        due = parse_date(message.text, store.offset(user), time.time())
                        # Переменная due здесь содержит разобранную дату. Только ниже
                        # решаем, записать её в event_at или в due самой записи.
                        if state['kind'] == 'event':
                            if note['status'] != 'task':
                                store.cancel(user)
                                await message.answer('Сначала оформи запись как задачу.')
                                return
                            store.update(note['id'], user, event_at=due)
                            # Меняем только время дела: прежний due остаётся прежним.
                            store.cancel(user)
                            updated = store.get(note['id'], user)
                            await message.answer(card(updated, store.offset(user)), reply_markup=actions(updated))
                            text = 'Когда напомнить?'
                            if note['due'] is not None:
                                text += '\nПрежнее напоминание сохранено. Выбери вариант, чтобы заменить его.'
                            await message.answer(text, reply_markup=reminder_options(updated))
                            return
                        store.update(note['id'], user, due=due,
                                     reminder_kind='review' if note['status'] == 'inbox' else 'task')
                        store.cancel(user)
                        await message.answer(card(store.get(note['id'], user), store.offset(user)), reply_markup=actions(note))
                except ValueError as error:
                    # Ошибка ввода не завершает диалог: пользователь может попробовать снова.
                    await message.answer(str(error) + '\n/cancel — отменить.')
                return
            pending = store.db.execute('SELECT 1 FROM pending WHERE user_id=?', (user,)).fetchone()
            # pending отвечает за название, input_state выше — за время/UTC.
            if pending and len(message.text) > 200:
                await message.answer('Сократи название до 200 символов или нажми /cancel.')
                return
            note = store.set_title(user, message.text)
            if note:
                await message.answer(card(note, store.offset(user) or 0), reply_markup=actions(note))
                return
        note = store.add(user, message.message_id, message.text or message.caption,
                         # У фото выбирается наибольшее разрешение из версий Telegram.
                         message.voice.file_id if message.voice else None,
                         photo=max(message.photo, key=lambda p: p.width * p.height).file_id if message.photo else None,
                         image_document=image_document.file_id if image_document else None)
        await message.answer(card(note, store.offset(user) or 0), reply_markup=actions(note))

    return router


# Фоновая корутина проверяет SQLite каждые пять секунд.
# Напоминания хранятся в базе, а не в таймерах памяти: после перезапуска
# просроченные уведомления снова попадут в выборку. event_at сам по себе
# не запускает уведомление — отправка определяется полем due.
async def reminders(bot, store):
    while True:
        for note in store.due():
            try:
                prefix = 'Вернёмся к записи?\n' if note['reminder_kind'] == 'review' else 'Пора вспомнить о задаче:\n'
                await bot.send_message(note['user_id'], prefix + card(note, store.offset(note['user_id']) or 0), reply_markup=actions(note))
                store.delivered(note['id'], note['due'])
                # Сначала отправка, затем отметка в БД. При сбое между ними
                # возможно повторное уведомление после запуска (не exactly-once).
            except TelegramForbiddenError:
                # Например, пользователь заблокировал бота: прекращаем повторные попытки.
                store.delivered(note['id'], note['due'])
            except TelegramRetryAfter as error:
                # Telegram попросил снизить частоту запросов. due пока сохраняем.
                await asyncio.sleep(error.retry_after)
            except TelegramAPIError:
                # При прочей ошибке API запись остаётся для следующей попытки.
                logging.warning('Не удалось отправить напоминание #%s; повторим позже.', note['id'])
        await asyncio.sleep(5)
        # sleep не блокирует обработчики Telegram, в отличие от time.sleep().


# Точка сборки приложения: настройки → база → обработчики → Telegram.
# Вызывается один раз при запуске процесса. Ни импорт модуля, ни /start
# не вызывают main автоматически.
async def main():
    load_dotenv()
    # Читаем .env в переменные окружения. Токен не выводим в логи.
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('Добавь BOT_TOKEN в .env по образцу .env.example и снова запусти python bot.py')
    store = Storage(os.getenv('DB_PATH', 'data/notebook.db'))
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(store))
    async with Bot(token) as bot:
        # Контекстный менеджер закроет сетевую сессию при выходе из блока.
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
            ('inbox', 'Входящие'), ('tasks', 'Задачи'), ('archive', 'Архив'),
            ('settings', 'Местное время'), ('cancel', 'Отменить ввод'), ('start', 'Помощь')]])
        worker = asyncio.create_task(reminders(bot, store))
        # Запускаем напоминания одновременно с получением новых событий.
        try:
            await dispatcher.start_polling(bot, handle_as_tasks=False, close_bot_session=False)
            # Polling получает события Telegram. handle_as_tasks=False обрабатывает
            # их последовательно; фоновая корутина reminders всё равно работает.
        finally:
            # Выполняется и при остановке, и при ошибке: завершаем фон и закрываем БД.
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            store.db.close()


if __name__ == '__main__':
    # Этот блок выполняется при python bot.py, но не при import bot в тестах.
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
