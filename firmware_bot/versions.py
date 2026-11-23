from dataclasses import dataclass, replace
import re


TOKEN = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*){2,}(?![A-Za-z0-9_.-])")
VERSION_START = re.compile(r"(?:_[A-Z]{1,3})?[._]V(?=\d|$)")


class InvalidVersion(ValueError):
    pass


@dataclass(frozen=True)
class SoftwareVersion:
    query: str
    family: str
    selected_variant: str | None = None

    @property
    def variant(self) -> str | None:
        suffix = re.search(r"\.STD_[A-Z0-9]+$", self.query)
        return self.selected_variant or (suffix.group()[1:] if suffix else None)

    def with_variant(self, variant: str):
        if variant not in {"STD_H", "STD_M"} or (self.variant and self.variant != variant):
            raise InvalidVersion("Выбранный вариант не совпадает с окончанием Software Version.")
        return replace(self, selected_variant=variant)

    def selectable_variants(self) -> tuple[str, ...]:
        # These two alternatives were verified in the supplier's IG19 SOP catalogue.
        # The selected variant is still checked against a fresh supplier response.
        if self.query == self.family == "IG19.KOR.SOP":
            return ("STD_H", "STD_M")
        return ()

    @classmethod
    def parse(cls, text: str):
        raw = text.strip()
        query = raw.upper()
        if not raw.isascii() or len(query) > 160 or not TOKEN.fullmatch(query) or not any(c.isalpha() for c in query):
            raise InvalidVersion("Отправьте обозначение из строки Software Version, например LX2.KOR.S5W или LX2.KOR.S5W_M.V034.003.250117.")
        boundary = VERSION_START.search(query)
        family = query[:boundary.start()] if boundary else query.removesuffix("_M")
        # Reject bare revision numbers and strings without a complete platform code.
        if family.count(".") < 2:
            raise InvalidVersion("Нужна модель и регион из Software Version, а не только номер V034.003.")
        return cls(query, family)


def response_versions(text: str) -> list[SoftwareVersion]:
    text = re.sub(r"https?://[^\s<>]+", " ", text, flags=re.I)
    # DriveMods search cards mask the revision until the Links button is opened.
    # Only source classification accepts this; customer queries remain strict.
    text = re.sub(r"\.V-+(?:\.-+)+", ".V000.000.000000", text, flags=re.I)
    result = []
    for match in TOKEN.finditer(text):
        try:
            candidate = SoftwareVersion.parse(match.group())
        except InvalidVersion:
            continue
        # Real platform codes contain letters in their first segments. Skip dates and URLs.
        if all(re.search(r"[A-Z]", segment) for segment in candidate.family.split(".")[:3]):
            result.append(candidate)
    return result


def response_families(text: str) -> set[str]:
    return {version.family for version in response_versions(text)}


def response_variants(text: str) -> set[str]:
    return {version.variant for version in response_versions(text) if version.variant}
