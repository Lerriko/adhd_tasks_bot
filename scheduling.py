"""Преобразование введённого местного времени в UTC для хранения."""
import re
from datetime import datetime, timedelta, timezone


# Разбирает +03:00, UTC-03:30 или +3 и возвращает минуты от UTC.
# Регулярное выражение проверяет всю строку: знак, часы и необязательные минуты.
# Группы match[1], [2], [3] содержат эти три части; re.I игнорирует регистр.
def parse_offset(text):
    match = re.fullmatch(r'(?:UTC\s*)?([+-])(\d{1,2})(?::([0-5]\d))?', text.strip(), re.I)
    if not match:
        raise ValueError('Введи смещение в формате +03:00 или -05:00.')
    minutes = int(match[2]) * 60 + int(match[3] or 0)
    # Если минут в строке нет, берём 0. Затем применяем знак ко всему смещению.
    minutes *= 1 if match[1] == '+' else -1
    if not -720 <= minutes <= 840:
        raise ValueError('Допустимое смещение: от -12:00 до +14:00.')
    return minutes


# Форматирует минуты обратно в UTC+03:00. // — целые часы,
# % — оставшиеся минуты, :02 — две цифры с ведущим нулём.
def zone_label(offset):
    return f"UTC{'+' if offset >= 0 else '-'}{abs(offset) // 60:02}:{abs(offset) % 60:02}"


# Преобразует Unix-время в местное и делает строку для карточки.
# timezone здесь — фиксированное смещение, без сезонного перевода часов.
def local_date(timestamp, offset):
    zone = timezone(timedelta(minutes=offset))
    return datetime.fromtimestamp(timestamp, zone).strftime('%d.%m.%Y %H:%M') + ' ' + zone_label(offset)


# Читает местную дату и возвращает Unix-время. now передаётся явно,
# поэтому тесты могут проверять прошлое/будущее без привязки к реальным часам.
def parse_date(text, offset, now):
    try:
        date = datetime.strptime(text.strip(), '%d.%m.%Y %H:%M')
        # %d — день, %m — месяц, %Y — год, %H — часы, %M — минуты.
    except ValueError:
        raise ValueError('Введи существующую дату в формате ДД.ММ.ГГГГ ЧЧ:ММ.') from None
    timestamp = date.replace(tzinfo=timezone(timedelta(minutes=offset))).timestamp()
    # strptime создал дату без зоны. replace назначает ей введённую зону,
    # а timestamp переводит в общий момент времени (секунды с эпохи Unix).
    if timestamp <= now:
        raise ValueError('Это время уже прошло. Выбери будущую дату и время.')
    return timestamp
