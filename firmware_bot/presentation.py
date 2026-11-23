"""Customer-facing firmware price labels; source replies remain untouched."""
import re

from aiogram.types import MessageEntity


_SPACE = r"[ \t\u00a0\u202f\u2009]"
_PRICE = re.compile(
    rf"(?<![\w.,])(?P<first>6{_SPACE}*000){_SPACE}*/{_SPACE}*"
    rf"(?P<second>9{_SPACE}*000){_SPACE}*(?P<currency>RUB|РУБ|₽)(?!\w)",
    re.IGNORECASE,
)
_PROTECTED = {"url", "text_link", "email", "phone_number", "code", "pre"}


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def customer_text(text: str, entities: list[MessageEntity]) -> tuple[str, list[MessageEntity]]:
    """Replace only the agreed 6000/9000 RUB pair with 70/110 dollars.

    Telegram entity offsets count UTF-16 units. Edit each amount and currency
    separately so bold amounts, quotes and subsequent hidden links stay valid.
    Link labels, URLs and code/instruction fragments are not rewritten.
    """
    protected = [(e.offset, e.offset + e.length) for e in entities if e.type in _PROTECTED]
    protected.extend((_utf16(text[:m.start()]), _utf16(text[:m.end()]))
                     for m in re.finditer(r"https?://[^\s<>]+", text, re.IGNORECASE))
    edits = []
    for match in _PRICE.finditer(text):
        start, end = _utf16(text[:match.start()]), _utf16(text[:match.end()])
        if any(start < b and end > a for a, b in protected):
            continue
        for group, replacement in (("first", "70"), ("second", "110"), ("currency", "$")):
            a, b = match.span(group)
            edits.append((a, b, replacement))
    if not edits:
        return text, list(entities)

    units = [(_utf16(text[:a]), _utf16(text[:b]), _utf16(value)) for a, b, value in edits]

    def boundary(position: int, *, end: bool) -> int:
        shift = 0
        for a, b, size in units:
            if position <= a:
                break
            if position < b:
                return a + shift + (size if end else 0)
            shift += size - (b - a)
        return position + shift

    adjusted = []
    for entity in entities:
        start = boundary(entity.offset, end=False)
        end = boundary(entity.offset + entity.length, end=True)
        if end > start:
            adjusted.append(entity.model_copy(update={"offset": start, "length": end - start}))
    result = text
    for a, b, value in reversed(edits):
        result = result[:a] + value + result[b:]
    return result, adjusted
