import unittest
from datetime import datetime, timezone

from scheduling import parse_offset, parse_date, local_date


class SchedulingTests(unittest.TestCase):
    # Эти проверки не используют ни Telegram, ни БД: только преобразование времени.
    # Проверяем получасовые смещения, знак, нулевой UTC и недопустимые значения.
    def test_fractional_and_negative_offsets(self):
        self.assertEqual(parse_offset('+05:30'), 330)
        self.assertEqual(parse_offset('UTC-03:30'), -210)
        self.assertEqual(parse_offset('+00:00'), 0)
        for invalid in ['+15:00', '-12:30', '+03:90', 'Москва']:
            with self.assertRaises(ValueError):
                # Тест успешен, если вызов внутри блока выбросит указанную ошибку.
                parse_offset(invalid)

    # Полночь в UTC+3 попадает на предыдущий день UTC; проверяем оба направления.
    def test_local_date_converts_to_utc(self):
        expected = datetime(2030, 1, 1, 21, 15, tzinfo=timezone.utc).timestamp()
        self.assertEqual(parse_date('02.01.2030 00:15', 180, 0), expected)
        self.assertEqual(local_date(expected, 180), '02.01.2030 00:15 UTC+03:00')

    # Несуществующая дата, свободный текст и прошедшее время должны дать ValueError.
    def test_invalid_and_past_dates(self):
        for invalid in ['31.02.2030 12:00', 'завтра', '01.01.2000 10:00']:
            with self.assertRaises(ValueError):
                parse_date(invalid, 0, 1800000000)
