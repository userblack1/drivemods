"""Amount-prefilled MBANK C2C ELQR links, never payment confirmation.

ELQR (NBKR rules, appendix 1) uses tyiyn and the final four SHA256 hex
characters. This is not the EMV CRC16/decimal-major-units format.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit


def parse_fields(value: str) -> list[tuple[str, str]]:
    fields, seen, pos = [], set(), 0
    while pos < len(value):
        header = value[pos:pos + 4]
        if len(header) != 4 or not header.isascii() or not header.isdecimal():
            raise ValueError("Некорректная структура ELQR.")
        tag, size = header[:2], int(header[2:])
        data = value[pos + 4:pos + 4 + size]
        if tag in seen or not size or len(data) != size:
            raise ValueError("Повторное или повреждённое поле ELQR.")
        fields.append((tag, data))
        seen.add(tag)
        pos += 4 + size
    return fields


def encode_fields(fields: list[tuple[str, str]]) -> str:
    parts = []
    for tag, value in fields:
        if not re.fullmatch(r"[0-9]{2}", tag) or not 1 <= len(value) <= 99:
            raise ValueError("Некорректное поле ELQR.")
        parts.append(f"{tag}{len(value):02d}{value}")
    return "".join(parts)


def checksum(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[-4:].upper()


@dataclass(frozen=True, repr=False)
class MBankQr:
    fields: tuple[tuple[str, str], ...]

    @classmethod
    def parse(cls, payload: str) -> "MBankQr":
        if not isinstance(payload, str) or not 1 <= len(payload) <= 2048:
            raise ValueError("Нужна платёжная ссылка MBANK из QR.")
        url = urlsplit(payload.strip())
        if (url.scheme != "https" or url.netloc != "pay.payqr.kg" or url.path not in {"/qr", "/qr/"}
                or url.query or not url.fragment or re.search(r"%(?![0-9a-fA-F]{2})", url.fragment)):
            raise ValueError("Нужна ссылка ELQR pay.payqr.kg без посторонних параметров.")
        try:
            text = unquote(url.fragment, errors="strict")
        except UnicodeError as exc:
            raise ValueError("Повреждена кодировка ELQR.") from exc
        fields = parse_fields(text)
        values = dict(fields)
        if not fields or fields[0] != ("00", "01") or fields[-1][0] != "63":
            raise ValueError("Неверная версия или контрольная сумма ELQR.")
        expected = values["63"]
        if not re.fullmatch(r"[0-9A-Fa-f]{4}", expected) or checksum(text[:-8]) != expected.upper():
            raise ValueError("Контрольная сумма ELQR не совпадает.")
        if values.get("53") != "417":
            raise ValueError("Этот способ поддерживает только QR MBANK в сомах (KGS).")
        if values.get("01") not in {"11", "12"} or values.get("52") != "9999" or not values.get("59"):
            raise ValueError("Нужен QR перевода MBANK с получателем.")
        route = dict(parse_fields(values.get("32", "")))
        if (route.get("00") != "c2c.mbank.kg" or route.get("01") != "02"
                or not re.fullmatch(r"[0-9]{12}", route.get("10", "")) or route.get("13") != "12"):
            raise ValueError("QR не соответствует поддерживаемому переводу MBANK; получатель не меняется.")
        if "54" in values and (not re.fullmatch(r"[0-9]{1,13}", values["54"]) or int(values["54"]) <= 0):
            raise ValueError("Сумма ELQR должна быть указана в тыйынах.")
        return cls(tuple(fields))

    @classmethod
    def read(cls, path: Path) -> "MBankQr":
        return cls.parse(path.read_text(encoding="utf-8-sig"))

    @property
    def recipient(self) -> str:
        return dict(self.fields)["59"]

    @property
    def amount_minor(self) -> int | None:
        amount = dict(self.fields).get("54")
        return int(amount) if amount is not None else None

    def with_amount(self, amount_minor: int) -> "MBankQr":
        if type(amount_minor) is not int or not 0 < amount_minor <= 9_999_999_999_999:
            raise ValueError("Нужна положительная сумма KGS в тыйынах.")
        fields = [(tag, value) for tag, value in self.fields if tag not in {"54", "63"}]
        route = [(tag, value) for tag, value in parse_fields(dict(fields)["32"]) if tag != "12"]
        route.append(("12", "12"))  # Ask the banking app not to edit the amount.
        route.sort(key=lambda item: item[0])
        fields = [(tag, encode_fields(route) if tag == "32" else value) for tag, value in fields]
        index = next(i for i, (tag, _) in enumerate(fields) if tag == "59")
        fields.insert(index, ("54", str(amount_minor)))
        body = encode_fields(fields)
        fields.append(("63", checksum(body)))
        # Preserve the existing static C2C type; no bank invoice or transaction
        # identifier is invented. A unique order remains in our own database.
        return self.parse(self._url(fields))

    @staticmethod
    def _url(fields) -> str:
        return urlunsplit(("https", "pay.payqr.kg", "/qr/", "", quote(encode_fields(list(fields)), safe="-._~")))

    def url(self) -> str:
        return self._url(self.fields)

    def write_png(self, path: Path) -> None:
        import qrcode
        from qrcode.image.pure import PyPNGImage
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_Q, box_size=9, border=4)
        qr.add_data(self.url())
        qr.make(fit=True)
        part = path.with_suffix(".part")
        with part.open("wb") as output:
            qr.make_image(image_factory=PyPNGImage).save(output)
        part.replace(path)
