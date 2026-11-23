import asyncio
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.types import Update
import pytest

from firmware_bot.catalog import CATALOG, catalog_message_id, find_entry
from firmware_bot.config import Settings
from firmware_bot.response import validate_response
from firmware_bot.storage import Database
from firmware_bot.telegram import make_router
from firmware_bot.service import Service
from firmware_bot.telegram import Messenger
from firmware_bot.cache import cache_key
from test_firmware import RecordingSession, complete_messages, gen5_messages
from test_firmware_concurrency import Bridge, until, close_service


def click(key, *, user=11, chat=None, click_id='click-1', menu_id=100):
    return Update.model_validate({'update_id':1, 'callback_query':{
        'id':click_id,'chat_instance':'catalog-test','data':'fw_catalog:'+key,
        'from':{'id':user,'is_bot':False,'first_name':'Test'},
        'message':{'message_id':menu_id,'date':1795240000,
                   'chat':{'id':user if chat is None else chat,'type':'private'},
                   'from':{'id':123456,'is_bot':True,'first_name':'Firmware'},'text':'Каталог'}}})


@pytest.mark.parametrize('entry',CATALOG,ids=lambda entry:entry.key)
def test_every_historically_delivered_catalog_entry_retains_exact_family_and_hardware(entry):
    version=entry.version()
    assert (version.query,version.family,version.variant or '')==(entry.family,entry.family,entry.variant)
    messages=gen5_messages(entry.family,entry.variant) if entry.variant else complete_messages(entry.family)
    validate_response(version,messages,Settings())
    assert len(('fw_catalog:'+entry.key).encode())<=64
    assert find_entry(entry.key)==entry


def test_catalog_click_ids_do_not_collide_with_message_ids_or_menu_reuse():
    first=catalog_message_id('first')
    assert first==catalog_message_id('first') and first!=catalog_message_id('next')
    assert -(2**63)<first<=-(2**62)


def test_all_catalog_buttons_feed_each_customers_exact_selection_into_the_existing_queue(tmp_path):
    async def scenario():
        settings=Settings(data_dir=tmp_path,owner_only=False,user_cooldown=0)
        db=Database(tmp_path/'queue.sqlite3')
        session=RecordingSession();bot=Bot('123456:TEST_TOKEN_ONLY',session=session)
        dispatcher=Dispatcher()
        dispatcher.include_router(make_router(settings,db,SimpleNamespace(is_blocked=lambda:False)))
        try:
            for user,entry in enumerate(CATALOG,100):
                await dispatcher.feed_update(bot,click(entry.key,user=user,click_id=f'customer-{user}'))
                row=db.latest(user)
                assert row['state']=='queued' and row['user_id']==row['chat_id']==user
                assert (row['query'],row['family'],row['variant'])==(entry.family,entry.family,entry.variant)
                assert row['message_id']<0
            assert len(db.queued())==len(CATALOG)
        finally:
            await bot.session.close();db.close()
    asyncio.run(scenario())


def test_repeated_clicks_and_completed_menu_reuse_do_not_return_someone_elses_firmware(tmp_path):
    async def scenario():
        settings=Settings(data_dir=tmp_path,owner_only=False,user_cooldown=0)
        db=Database(tmp_path/'queue.sqlite3');session=RecordingSession()
        bot=Bot('123456:TEST_TOKEN_ONLY',session=session);dispatcher=Dispatcher()
        dispatcher.include_router(make_router(settings,db,SimpleNamespace(is_blocked=lambda:False)))
        try:
            await dispatcher.feed_update(bot,click('DN8.KOR.S5W'))
            first=db.latest(11)
            await dispatcher.feed_update(bot,click('DN8.KOR.S5W'))
            await dispatcher.feed_update(bot,click('DN8.KOR.S5W',click_id='another-click'))
            assert len(db.queued())==1 and db.latest(11)['id']==first['id']
            await dispatcher.feed_update(bot,click('LX2.KOR.S5W',click_id='other-model'))
            assert len(db.queued())==1
            db.update(first['id'],'delivered')
            await dispatcher.feed_update(bot,click('LX2.KOR.S5W',click_id='fresh-after-completion'))
            assert db.latest(11)['query']=='LX2.KOR.S5W' and db.latest(11)['id']!=first['id']
            await dispatcher.feed_update(bot,click('anything',user=12,click_id='forged'))
            assert db.latest(12) is None
            await dispatcher.feed_update(bot,click('LX2.KOR.S5W',user=13,chat=11,click_id='foreign-chat'))
            assert db.latest(13) is None
        finally:
            await bot.session.close();db.close()
    asyncio.run(scenario())


def test_catalog_is_visible_to_customers_and_does_not_start_a_search_until_they_select(tmp_path):
    async def scenario():
        settings=Settings(data_dir=tmp_path,owner_only=False)
        db=Database(tmp_path/'queue.sqlite3');session=RecordingSession()
        bot=Bot('123456:TEST_TOKEN_ONLY',session=session);dispatcher=Dispatcher()
        dispatcher.include_router(make_router(settings,db,SimpleNamespace()))
        try:
            update=Update.model_validate({'update_id':1,'message':{'message_id':1,'date':1795240000,
                'chat':{'id':11,'type':'private'},'from':{'id':11,'is_bot':False,'first_name':'Test'},'text':'/catalog'}})
            await dispatcher.feed_update(bot,update)
            messages=[m for m in session.methods if m.__api_method__=='sendMessage']
            assert len(messages)==1 and len(messages[0].reply_markup.inline_keyboard)==len(CATALOG)
            assert db.queued()==[]
        finally:
            await bot.session.close();db.close()
    asyncio.run(scenario())


def test_simultaneous_catalog_customers_receive_their_own_old_bot_family_and_std_variant(tmp_path):
    async def scenario():
        settings=Settings(data_dir=tmp_path,owner_only=False,user_cooldown=0)
        db=Database(tmp_path/'queue.sqlite3');session=RecordingSession()
        bot=Bot('123456:TEST_TOKEN_ONLY',session=session);dispatcher=Dispatcher();bridge=Bridge()
        dispatcher.include_router(make_router(settings,db,bridge))
        service=Service(settings,db,bridge,Messenger(bot,settings))
        try:
            await asyncio.gather(*(dispatcher.feed_update(bot,click(entry.key,user=user,click_id=f'load-{user}'))
                                   for user,entry in enumerate(CATALOG,100)))
            await until(service,lambda:db.counts().get('delivered')==len(CATALOG))
            assert bridge.peak==1
            assert set(bridge.queries)=={cache_key(entry.version()) for entry in CATALOG}
            for user,entry in enumerate(CATALOG,100):
                row=db.latest(user)
                assert row['state']=='delivered' and row['variant']==entry.variant
                sent=[m for m in session.methods if getattr(m,'chat_id',None)==user]
                assert sum('передан полностью' in (getattr(m,'text',None) or '') for m in sent)==1
                expected=entry.family+('.V141.240205.'+entry.variant if entry.variant else '_M.V034.003.250117')
                assert any(expected in (getattr(m,'text',None) or '') for m in sent)
        finally:
            await close_service(service);await bot.session.close();db.close()
    asyncio.run(scenario())


def test_catalog_acceptance_keeps_its_place_when_worker_starts_during_callback_answer(tmp_path):
    async def scenario():
        settings=Settings(data_dir=tmp_path,owner_only=False,user_cooldown=0)
        db=Database(tmp_path/'queue.sqlite3')
        class Session(RecordingSession):
            async def make_request(self,bot,method,timeout=None):
                if method.__api_method__=='answerCallbackQuery':
                    request=db.latest(11)
                    db.update(request['id'],'searching')
                return await super().make_request(bot,method,timeout)
        session=Session();bot=Bot('123456:TEST_TOKEN_ONLY',session=session);dispatcher=Dispatcher()
        dispatcher.include_router(make_router(settings,db,SimpleNamespace()))
        try:
            await dispatcher.feed_update(bot,click('DN8.KOR.S5W'))
            notices=[m.text for m in session.methods if m.__api_method__=='sendMessage']
            assert len(notices)==1 and 'Место в очереди: 1.' in notices[0]
            assert db.latest(11)['state']=='searching'
        finally:
            await bot.session.close();db.close()
    asyncio.run(scenario())
