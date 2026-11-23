import asyncio
from types import SimpleNamespace

import pytest

from firmware_bot.bridge import TelegramBridge, source_family_hints, links_callback
from firmware_bot.config import Settings
from firmware_bot.response import FirmwareNotFound
from firmware_bot.versions import SoftwareVersion
from test_firmware import FakeClient, source_card, terminal_message


def test_supplier_family_hints_show_model_and_region_without_guessing_platform():
    version = SoftwareVersion.parse('DN8.KOR.5W')
    cards = [
        SimpleNamespace(message='DN8.KOR.S5W_M.V---.---.------'),
        SimpleNamespace(message='DN8.KOR.ccNC_Lite.---.---.------'),
        SimpleNamespace(message='DN8.KOR.ccNC.---.---.------'),
        SimpleNamespace(message='DN8.EUR.S5W_M.V---.---.------'),
        SimpleNamespace(message='DN9.KOR.S5W_M.V---.---.------'),
        SimpleNamespace(message='https://example.com/firmware'),
    ]
    assert source_family_hints(version,cards)==['DN8.KOR.CCNC','DN8.KOR.CCNC_LITE','DN8.KOR.S5W']
    assert version.query=='DN8.KOR.5W' and version.family=='DN8.KOR.5W'


def test_display_hint_does_not_authorize_another_model_or_click_links(tmp_path):
    async def scenario():
        version=SoftwareVersion.parse('DN8.KOR.5W')
        card=source_card(102,'DN8.KOR.S5W')
        assert links_callback(card,version) is None
        client=FakeClient([(0,card),(0.005,terminal_message(103))])
        bridge=TelegramBridge(Settings(data_dir=tmp_path,response_timeout=0.5,settle_seconds=0.005,min_collect_seconds=0.01),client=client)
        bridge.source=SimpleNamespace(id=999)
        with pytest.raises(FirmwareNotFound,match='DN8.KOR.S5W') as captured:
            await bridge.lookup(version)
        assert 'Указано: DN8.KOR.5W' in str(captured.value)
        assert not bridge.is_blocked()
    asyncio.run(scenario())


def test_exact_family_does_not_get_a_replacement_suggestion():
    version=SoftwareVersion.parse('DN8.KOR.S5W')
    assert source_family_hints(version,[SimpleNamespace(message='DN8.KOR.S5W_M.V---.---.------')])==[]
