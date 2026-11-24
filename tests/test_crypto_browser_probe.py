"""The explicit runtime probe never needs a card, payment journal or checkout."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from crypto_checkout import stripe_browser


@pytest.mark.parametrize('available',[True,False])
def test_independent_probe_only_opens_blank_ephemeral_browser(tmp_path,monkeypatch,available):
    executable=tmp_path/'chromium'
    calls=[]
    class Page:
        async def goto(self,url,**options):
            assert url=='about:blank' and options['wait_until']=='domcontentloaded'
            calls.append('blank')
    class Factory:
        def __init__(self,path):
            assert path==executable
            self.available=available
        @asynccontextmanager
        async def __call__(self):
            calls.append('start')
            try:yield Page()
            finally:calls.append('close')
    monkeypatch.setattr(stripe_browser,'_PlaywrightFactory',Factory)
    assert asyncio.run(stripe_browser.probe_browser_runtime(executable)) is available
    assert calls==(['start','blank','close'] if available else [])
    assert list(tmp_path.iterdir())==[]


def test_independent_probe_failure_is_false_and_never_exposes_exception(tmp_path,monkeypatch,capsys,caplog):
    class Page:
        async def goto(self,*args,**options):raise OSError('PRIVATE BROWSER DETAILS')
    class Factory:
        available=True
        def __init__(self,path):pass
        @asynccontextmanager
        async def __call__(self):yield Page()
    monkeypatch.setattr(stripe_browser,'_PlaywrightFactory',Factory)
    assert asyncio.run(stripe_browser.probe_browser_runtime(tmp_path/'chromium')) is False
    assert not capsys.readouterr().out and 'PRIVATE BROWSER DETAILS' not in caplog.text
    assert list(tmp_path.iterdir())==[]
