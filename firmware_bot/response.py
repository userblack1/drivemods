from dataclasses import dataclass, field
import re

from aiogram.types import MessageEntity

from .config import Settings
from .versions import SoftwareVersion, response_families, response_variants


class SourceUnavailable(RuntimeError):
    pass


class SourceAccessDenied(SourceUnavailable):
    pass


class FirmwareNotFound(RuntimeError):
    pass


class ResponseRejected(SourceUnavailable):
    pass


class VersionMismatch(ResponseRejected):
    pass


class IncompleteResponse(ResponseRejected):
    pass


class CompletedResponseRejected(ResponseRejected):
    """The supplier finished, but this request cannot be delivered safely."""


class ResponseTimeout(SourceUnavailable):
    pass


ENTITY_TYPES = {
    "MessageEntityMention": "mention", "MessageEntityHashtag": "hashtag",
    "MessageEntityCashtag": "cashtag", "MessageEntityBotCommand": "bot_command",
    "MessageEntityUrl": "url", "MessageEntityEmail": "email",
    "MessageEntityPhone": "phone_number", "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic", "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough", "MessageEntitySpoiler": "spoiler",
    "MessageEntityCode": "code", "MessageEntityPre": "pre",
    "MessageEntityTextUrl": "text_link", "MessageEntityCustomEmoji": "custom_emoji",
}


def button_payload(button):
    # New Telegram layers nest callback/URL data in KeyboardInlineButton.type.
    return getattr(button, "type", button)


def convert_entities(entities) -> list[MessageEntity]:
    converted = []
    for entity in entities or []:
        name = type(entity).__name__
        kind = ENTITY_TYPES.get(name)
        if name == "MessageEntityBlockquote":
            kind = "expandable_blockquote" if getattr(entity, "collapsed", False) else "blockquote"
        if not kind:
            continue
        fields = dict(type=kind, offset=entity.offset, length=entity.length)
        for key in ("url", "language"):
            if getattr(entity, key, None):
                fields[key] = getattr(entity, key)
        if kind == "custom_emoji":
            fields["custom_emoji_id"] = str(entity.document_id)
        converted.append(MessageEntity(**fields))
    return converted


@dataclass
class SourceMessage:
    id: int
    text: str = ""
    entities: list[MessageEntity] = field(default_factory=list)
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    media_kind: str | None = None
    size: int = 0
    protected: bool = False
    raw: object = field(default=None, repr=False)

    @classmethod
    def from_telegram(cls, message):
        buttons = []
        for row in getattr(getattr(message, "reply_markup", None), "rows", []) or []:
            urls = [(button.text, button_payload(button).url) for button in row.buttons
                    if getattr(button_payload(button), "url", "").startswith(("https://", "http://"))]
            if urls:
                buttons.append(urls)
        media_kind = None
        if getattr(message, "photo", None):
            media_kind = "photo"
        elif getattr(message, "document", None):
            media_kind = "document"
        elif getattr(message, "media", None) and type(message.media).__name__ != "MessageMediaWebPage":
            media_kind = "unsupported"
        size = getattr(getattr(message, "file", None), "size", 0) or 0
        return cls(id=message.id, text=message.message or "", entities=convert_entities(message.entities),
                   buttons=buttons, media_kind=media_kind, size=size,
                   protected=bool(getattr(message, "noforwards", False)), raw=message)

    def urls(self) -> list[str]:
        links = re.findall(r"https?://[^\s<>]+", self.text)
        links.extend(e.url for e in self.entities if e.type == "text_link" and e.url)
        links.extend(url for row in self.buttons for _, url in row)
        return links


NOT_FOUND = re.compile(r"(?:не\s+найден[аоы]?|не\s+наш[её]л|ничего\s+не\s+найдено|нет\s+(?:такой\s+)?(?:прошивки|версии)|поиск\s+не\s+дал\s+результатов|не\s+соответствует\s+символам\s+software\s+version|not\s+found|no\s+results)", re.I)
ACCESS_DENIED = re.compile(r"(?:регистрация\s+в\s+боте\s+доступна\s+только|вы\s+не\s+зарегистрированы|доступ\s+запрещ[её]н|access\s+denied)", re.I)
GEN5_INSTRUCTION = re.compile(r"(?im)^[ \t]*(?:📋\ufe0f?[ \t]*)?инструкция[ \t]*:?[ \t]*$")
GEN5_INSTALLER = re.compile(r"(?im)^[ \t]*(?:📦\ufe0f?[ \t]*)?установщик\b")


def validate_response(version: SoftwareVersion, messages: list[SourceMessage], settings: Settings) -> None:
    text = "\n".join(message.text for message in messages)
    if ACCESS_DENIED.search(text):
        raise SourceAccessDenied("У подключённого Telegram-аккаунта нет доступа к источнику прошивок.")
    families = response_families(text)
    # Embedded hidden URLs count as links, but never as evidence of the model.
    if families and families != {version.family}:
        raise VersionMismatch("Источник вернул другую модель/регион или несколько вариантов.")
    variants = response_variants(text)
    if version.variant and variants and variants != {version.variant}:
        raise VersionMismatch("Источник вернул другой вариант STD_H/STD_M.")
    lowered = text.casefold()
    # GEN5 SOP uses a standalone instruction heading and an installer section;
    # GEN5W uses the configured 'instruction for installation' heading.
    gen5 = any(GEN5_INSTRUCTION.search(m.text) and GEN5_INSTALLER.search(m.text)
               and len(set(m.urls())) >= 2 for m in messages)
    has_installation = gen5 or any(marker in lowered for marker in settings.install_markers)
    if NOT_FOUND.search(text) and not has_installation:
        raise FirmwareNotFound("Данная прошивка или версия не найдена. Проверьте строку Software Version.")
    if version.family not in families:
        raise IncompleteResponse("Источник не подтвердил обозначение модели и региона.")
    if version.variant and version.variant not in variants:
        raise IncompleteResponse("Источник не подтвердил окончание версии STD_H/STD_M.")
    if not has_installation:
        raise IncompleteResponse("Не получена инструкция по установке.")
    if not gen5 and not any(marker in lowered for marker in settings.download_markers):
        raise IncompleteResponse("Не получены сведения о прошивке.")
    if not any(message.urls() for message in messages) and not any(m.media_kind == "document" for m in messages):
        raise IncompleteResponse("Не получены ссылки или файлы прошивки.")
    for message in messages:
        if message.protected:
            raise ResponseRejected("У источника включена защита пересылки.")
        if message.media_kind == "unsupported" or message.size > 49 * 1024 * 1024:
            raise ResponseRejected("В ответе есть вложение, которое бот не сможет передать целиком.")
