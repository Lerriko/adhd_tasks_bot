"""SQLite: записи и одно активное напоминание на запись."""
import sqlite3
import time
from pathlib import Path


class Storage:
    # Здесь нет отправки сообщений: класс отвечает только за данные.
    # notes — записи; pending — ожидаемые названия; input_state — ввод дат/UTC;
    # settings — смещение времени каждого пользователя.
    # Открывает файл SQLite и создаёт недостающие таблицы.
    # self хранит состояние конкретного экземпляра Storage.
    # row_factory позволяет читать строки как note["title"], а не по номеру столбца.
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, body TEXT, voice TEXT,
                title TEXT, status TEXT NOT NULL DEFAULT 'inbox',
                due REAL, reminder_kind TEXT,
                UNIQUE(user_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS pending (
                user_id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY, utc_offset INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS input_state (
                user_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, note_id INTEGER
            );
        """)
        # Добавляем поля к существующей базе, сохраняя все старые записи.
        # Такое обновление структуры называют миграцией. PRAGMA читает столбцы,
        # ALTER TABLE добавляет только отсутствующие; повторный запуск безопасен.
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(notes)')}
        with self.db:
            for name in ('photo', 'image_document'):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE notes ADD COLUMN {name} TEXT')
            if 'event_at' not in columns:
                self.db.execute('ALTER TABLE notes ADD COLUMN event_at REAL')

    # Сохраняет исходное сообщение, возвращает строку заметки.
    # INSERT OR IGNORE вместе с UNIQUE не создаёт дубль того же сообщения.
    # Знаки ? — параметры SQL: значения передаются отдельно, без склейки с запросом.
    def add(self, user_id, message_id, body=None, voice=None, photo=None, image_document=None):
        # with self.db фиксирует изменения при успехе и откатывает при исключении.
        # Он не закрывает соединение: это делается явно в main или в тестах.
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO notes(user_id,message_id,body,voice,photo,image_document) VALUES(?,?,?,?,?,?)',
                            (user_id, message_id, body, voice, photo, image_document))
        return self.db.execute('SELECT * FROM notes WHERE user_id=? AND message_id=?',
                               (user_id, message_id)).fetchone()

    # Ищет запись одновременно по ID и владельцу. fetchone() вернёт
    # одну строку или None. Проверка владельца не даёт читать чужие заметки.
    def get(self, note_id, user_id):
        return self.db.execute('SELECT * FROM notes WHERE id=? AND user_id=?', (note_id, user_id)).fetchone()

    # **values собирает именованные аргументы в словарь, например due=123.
    # Имена столбцов допускаются только из allowed, значения идут через ?.
    # Звёздочка перед values.values() разворачивает значения в общий кортеж.
    def update(self, note_id, user_id, **values):
        allowed = {'title', 'status', 'due', 'reminder_kind', 'event_at'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Unsupported fields')
        with self.db:
            self.db.execute(f"UPDATE notes SET {','.join(k + '=?' for k in values)} WHERE id=? AND user_id=?",
                            (*values.values(), note_id, user_id))

    # Выбирает последние записи нужного статуса, от новых к старым.
    # LIMIT 6 возвращает пять карточек и одну запись для проверки следующей страницы.
    def listing(self, user_id, status, offset=0):
        return self.db.execute('SELECT * FROM notes WHERE user_id=? AND status=? ORDER BY id DESC LIMIT 6 OFFSET ?',
                               (user_id, status, offset)).fetchall()

    # Запоминает, для какой записи ждём название. У пользователя может
    # быть только один активный ввод; прежний сначала отменяется.
    def wait_title(self, user_id, note_id):
        self.cancel(user_id)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO pending VALUES(?,?)', (user_id, note_id))

    # Удаляет ожидание ввода из обеих таблиц диалогов.
    # DELETE затрагивает только строки этого пользователя, не сами notes.
    def cancel(self, user_id):
        with self.db:
            self.db.execute('DELETE FROM pending WHERE user_id=?', (user_id,))
            self.db.execute('DELETE FROM input_state WHERE user_id=?', (user_id,))

    # Возвращает смещение UTC в минутах. None — пользователь ещё
    # не настроил время; 0 — уже выбрал UTC. Это разные состояния.
    def offset(self, user_id):
        row = self.db.execute('SELECT utc_offset FROM settings WHERE user_id=?', (user_id,)).fetchone()
        return row['utc_offset'] if row else None

    # Сохраняет настройку пользователя. REPLACE заменяет существующую
    # строку с таким же первичным ключом user_id.
    def set_offset(self, user_id, offset):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (user_id, offset))

    # Запоминает шаг диалога: zone/zone_event — ввод UTC,
    # date — дата напоминания, event — дата дела. note_id связывает шаг с записью.
    def wait_input(self, user_id, kind, note_id=None):
        self.cancel(user_id)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO input_state VALUES(?,?,?)', (user_id, kind, note_id))

    # Читает текущий шаг диалога или возвращает None, если ввода не ждём.
    def input_state(self, user_id):
        return self.db.execute('SELECT * FROM input_state WHERE user_id=?', (user_id,)).fetchone()

    # Если ждём название — превращает заметку в задачу и завершает ввод.
    # Старое напоминание о разборе отменяется. Если ожидания нет, возвращает None,
    # и обработчик save сможет сохранить текст как новую заметку.
    def set_title(self, user_id, title):
        pending = self.db.execute('SELECT note_id FROM pending WHERE user_id=?', (user_id,)).fetchone()
        if pending is None:
            return None
        note = self.get(pending['note_id'], user_id)
        with self.db:
            if note and note['status'] != 'archive':
                # Вспомогательные update/cancel также открывают with self.db.
                # Вложенные блоки sqlite3 не создают отдельные savepoint-транзакции.
                self.update(note['id'], user_id, title=title, status='task', due=None, reminder_kind=None)
            self.cancel(user_id)
        return self.get(note['id'], user_id) if note and note['status'] != 'archive' else None

    # Находит все наступившие напоминания вне архива.
    # SQL NULL не удовлетворяет сравнению <=, поэтому записи без due не попадут сюда.
    def due(self):
        return self.db.execute('SELECT * FROM notes WHERE due<=? AND status!=?', (time.time(), 'archive')).fetchall()

    # Снимает отправленное напоминание, сохраняя запись и время дела.
    # Условие due=? защищает новое расписание: пользователь мог перенести
    # уведомление, пока бот ожидал ответ Telegram на отправку.
    def delivered(self, note_id, due):
        with self.db:
            self.db.execute('UPDATE notes SET due=NULL, reminder_kind=NULL WHERE id=? AND due=?', (note_id, due))
