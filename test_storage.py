import tempfile
import time
import unittest
from pathlib import Path

from storage import Storage


class StorageTests(unittest.TestCase):
    # unittest запускает каждый test_* отдельно с setUp/tearDown вокруг него.
    # assertEqual проверяет равенство, assertIsNone — отсутствие значения.
    # Перед каждым тестом создаётся новая временная база. Рабочая data/notebook.db не используется.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.db'
        self.store = Storage(self.path)

    # Закрываем SQLite до удаления папки: Windows не даст удалить открытый файл.
    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    # Закрываем и заново открываем базу: голосовое и наступившее напоминание должны остаться.
    def test_saved_voice_and_reminder_survive_restart(self):
        note = self.store.add(1, 10, voice='telegram-file-id')
        self.store.update(note['id'], 1, due=time.time() - 1, reminder_kind='review')
        self.store.db.close()
        self.store = Storage(self.path)
        self.assertEqual(self.store.due()[0]['voice'], 'telegram-file-id')

    # Только в тестовой базе удаляем новые столбцы, имитируя старую версию схемы.
    # Повторное открытие должно вернуть столбцы без потери старых данных.
    def test_old_database_upgrade_preserves_notes_and_new_images(self):
        note = self.store.add(1, 10, 'Old note')
        self.store.update(note['id'], 1, due=12345, reminder_kind='review')
        with self.store.db:
            self.store.db.execute('ALTER TABLE notes DROP COLUMN photo')
            self.store.db.execute('ALTER TABLE notes DROP COLUMN image_document')
            self.store.db.execute('ALTER TABLE notes DROP COLUMN event_at')
        self.store.db.close()
        self.store = Storage(self.path)
        self.assertEqual(self.store.get(note['id'], 1)['due'], 12345)
        photo = self.store.add(1, 11, 'Screenshot', photo='photo-id')
        document = self.store.add(1, 12, image_document='document-id')
        self.store.update(note['id'], 1, event_at=13000)
        self.store.db.close()
        self.store = Storage(self.path)
        self.assertEqual(self.store.get(photo['id'], 1)['photo'], 'photo-id')
        self.assertEqual(self.store.get(document['id'], 1)['image_document'], 'document-id')
        self.assertEqual(self.store.get(note['id'], 1)['event_at'], 13000)
        self.assertEqual(self.store.get(note['id'], 1)['due'], 12345)

    # Чужой user_id не позволяет ни получить запись, ни обновить её.
    def test_users_cannot_read_or_modify_each_other(self):
        note = self.store.add(1, 10, 'hello')
        self.assertIsNone(self.store.get(note['id'], 2))
        self.store.update(note['id'], 2, status='archive')
        self.assertEqual(self.store.get(note['id'], 1)['status'], 'inbox')

    # Повтор одной пары user_id/message_id не должен создавать вторую заметку.
    def test_duplicate_delivery_does_not_duplicate_note(self):
        self.store.add(1, 10, 'hello')
        self.store.add(1, 10, 'hello')
        self.assertEqual(len(self.store.listing(1, 'inbox')), 1)

    # Оформление убирает напоминание о разборе; завершение убирает напоминание о задаче.
    def test_title_replaces_review_reminder_and_archive_cancels_task(self):
        note = self.store.add(1, 10, voice='voice')
        self.store.update(note['id'], 1, due=1, reminder_kind='review')
        self.store.wait_title(1, note['id'])
        task = self.store.set_title(1, 'Send examples')
        self.assertEqual(task['status'], 'task')
        self.assertIsNone(task['due'])
        self.store.update(note['id'], 1, due=1, reminder_kind='task')
        self.store.update(note['id'], 1, status='archive', due=None, reminder_kind=None)
        self.assertEqual(self.store.due(), [])

    # После отметки об отправке запись больше не попадает в выборку due().
    def test_delivered_reminder_does_not_repeat(self):
        note = self.store.add(1, 10, 'hello')
        self.store.update(note['id'], 1, due=1, reminder_kind='review')
        self.store.delivered(note['id'], 1)
        self.assertEqual(self.store.due(), [])

    # Подтверждение старого времени не должно стирать более новое напоминание.
    def test_old_delivery_cannot_cancel_rescheduled_reminder(self):
        note = self.store.add(1, 10, 'hello')
        self.store.update(note['id'], 1, due=100, reminder_kind='review')
        self.store.delivered(note['id'], 1)
        self.assertEqual(self.store.get(note['id'], 1)['due'], 100)


if __name__ == '__main__':
    unittest.main()
