"""Parse explicit requests for the preceding seconds of the current print."""
import re


def clip_seconds(text):
    value = str(text).strip().lower().replace('ё', 'е')
    command = re.match(r'^/(?:video|clip)(?:@[a-z0-9_]+)?(?:\s|$)', value)
    if command:
        value = value[command.end():].strip()
        if not value:
            return 30
    else:
        explicit = bool(re.search(r'\b(?:видео|клип|ролик|запись)\b', value))
        bare = bool(re.fullmatch(r'\d+(?:[.,]\d+)?\s*(?:с|сек|секунд[ауы]?|сек\.|s|мин|минут[ауы]?|м|min)\.?', value))
        if not explicit and not bare:
            return None
        value = re.sub(r'\b(?:видео|клип|ролик|запись|последние|последних|за|дай|покажи|сохрани|печати)\b', ' ', value).strip()
        if not value:
            return 30
    match = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(с|сек|секунд[ауы]?|s|мин|минут[ауы]?|м|min)?\.?', value)
    if not match:
        raise ValueError('invalid_clip_duration')
    amount = float(match[1].replace(',', '.'))
    seconds = amount * (60 if (match[2] or '').startswith(('м','min')) else 1)
    if not seconds.is_integer() or not 1 <= seconds <= 300:
        raise ValueError('invalid_clip_duration')
    return int(seconds)
