"""Presets verified in successful deliveries of the retired firmware bot.

These are discovery buttons, not a whitelist or a cached firmware collection.
Every selection still performs the normal, validated supplier lookup.
"""
from dataclasses import dataclass
import hashlib

from .versions import SoftwareVersion


@dataclass(frozen=True)
class CatalogEntry:
    family: str
    variant: str = ''

    @property
    def key(self):
        return self.family + (':' + self.variant if self.variant else '')

    @property
    def label(self):
        return self.family + (' · ' + self.variant if self.variant else '')

    def version(self):
        value = SoftwareVersion.parse(self.family)
        return value.with_variant(self.variant) if self.variant else value


CATALOG = (
    CatalogEntry('BDPE.KOR.S5W'),
    CatalogEntry('DN8.KOR.S5W'),
    CatalogEntry('DN8HEV.KOR.S5W'),
    CatalogEntry('IGPE.KOR.S5W'),
    CatalogEntry('KA4.KOR.S5W'),
    CatalogEntry('LX2.KOR.S5W'),
    CatalogEntry('MQ4.KOR.S5W'),
    CatalogEntry('NQ5.KOR.S5W'),
    CatalogEntry('IG19.KOR.SOP', 'STD_H'),
    CatalogEntry('IG19.KOR.SOP', 'STD_M'),
    CatalogEntry('IGHEV19.KOR.SOP', 'STD_H'),
    CatalogEntry('LFFL.KOR.SOP', 'STD_H'),
)


def find_entry(key):
    return next((entry for entry in CATALOG if entry.key == key), None)


def catalog_message_id(callback_id):
    # Telegram callback IDs are not message IDs. A stable, separate negative
    # namespace prevents different clicks on one menu from sharing a DB key.
    digest = hashlib.blake2b(callback_id.encode(), digest_size=8).digest()
    return -((1 << 62) | (int.from_bytes(digest, 'big') & ((1 << 62) - 1)))
