import asyncio
from contextlib import asynccontextmanager
import json
import sqlite3
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer
import pytest

from drivemods_bot.bank_notices import NoticeStore, create_app, parse_credit
from drivemods_bot.bank_notice_forwarder import notice_text


WRITE = 'w' * 43
READ = 'r' * 43
CARD = '1234'


def message(amount='1', *, card=CARD, sender='Тестовый отправитель', balance='5 250,85'):
    return f'Пополнение: {amount} KGS\n{sender}\nКарта: *{card}\nДоступно: {balance} KGS'


@pytest.mark.parametrize('amount,expected', [('1',100), ('9000',900000), ('6 500',650000),
    ('9\u00a0000,01',900001), ('6\u202f500.25',650025), ('0,01',1)])
def test_recognizes_tested_format_without_treating_balance_as_transfer(amount, expected):
    credit = parse_credit(message(amount), CARD)
    assert credit.amount_minor == expected and credit.currency == 'KGS' and credit.card_last4 == CARD
    assert not hasattr(credit, 'sender') and not hasattr(credit, 'balance')


@pytest.mark.parametrize('text', [message('-1'),message('0'),message('1.234'),message('1,000.01'),
    message('1 00'),message('1000001'),message(card='9999'),message().replace('KGS','USD'),
    'Ваш код 123456',message()+'\nКод 123456',message().replace('Пополнение','Списание'), ''])
def test_unrelated_or_ambiguous_input_is_not_a_credit(text):
    assert parse_credit(text, CARD) is None


@asynccontextmanager
async def client_for(path):
    store = NoticeStore(path)
    client = TestClient(TestServer(create_app(store,WRITE,READ,CARD)))
    await client.start_server()
    try:
        yield client, store
    finally:
        await client.close()


def post(client, payload, token=WRITE):
    return client.post('/capture', data=json.dumps(payload),
                       headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json'})


def test_phone_capture_is_private_idempotent_and_never_confirms_or_changes_an_order(tmp_path):
    async def scenario():
        path=tmp_path/'capture.sqlite3'
        async with client_for(path) as (client,store):
            event_id=str(uuid4())
            payload={'text':message('9000'),'event_id':event_id}
            response=await post(client,payload)
            assert response.status==200
            result=await response.json()
            assert result['status']=='captured' and result['amount_minor']==900000
            assert result['payment_confirmed'] is False and result['automatic_purchase'] is False
            assert result['bank_reference_present'] is False
            again=await (await post(client,payload)).json()
            assert again['status']=='duplicate' and again['notice_id']==result['notice_id']
            # Re-emitted phone events with a new UUID do not count the same notice twice.
            again=await (await post(client,{'text':message('9000'),'event_id':str(uuid4())})).json()
            assert again['status']=='duplicate'
            conflict=await post(client,{'text':message('6500'),'event_id':event_id})
            assert conflict.status==409
            row=store.after(0)[0]
            text=notice_text(row)
            assert '9000.00 KGS' in text and 'оплата заказа не подтверждена' in text
            assert 'Тестовый отправитель' not in text and '5250' not in text
            assert len(store.after(0))==1
        # Fingerprints persist across service restarts, without raw names/balances.
        reopened=NoticeStore(path)
        _,duplicate=reopened.capture(message('9000'),'notification',None,parse_credit(message('9000'),CARD))
        assert duplicate
        with reopened.connect() as con:
            tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'orders' not in tables and 'receipts' not in tables
            row=dict(con.execute('SELECT * FROM phone_notices').fetchone())
            serialized=json.dumps(row,ensure_ascii=False)
            assert 'Тестовый отправитель' not in serialized and '5 250,85' not in serialized
    asyncio.run(scenario())


def test_write_and_read_credentials_have_separate_privileges(tmp_path):
    async def scenario():
        async with client_for(tmp_path/'capture.sqlite3') as (client,store):
            assert (await post(client,{'text':message()}, token=READ)).status==401
            assert (await client.post('/capture',json={'text':message()})).status==401
            assert (await client.get('/internal/events',headers={'Authorization':'Bearer '+WRITE})).status==401
            assert (await client.get('/internal/events',headers={'Authorization':'Bearer '+READ})).status==200
            assert (await client.get('/capture',headers={'Authorization':'Bearer '+WRITE})).status==405
            assert store.after(0)==[]
    asyncio.run(scenario())


@pytest.mark.parametrize('payload', [[],None,{'text':{}},{'text':'x','kind':{}},
    {'text':message(),'payment_confirmed':True},{'text':message(),'order_id':'anything'},
    {'text':'\ud800'},{'text':message(),'event_id':123},{'text':message(),'event_id':'invalid'},
    {'text':'x'*4097}])
def test_client_cannot_supply_authorization_claims_or_malformed_events(tmp_path,payload):
    async def scenario():
        async with client_for(tmp_path/'capture.sqlite3') as (client,store):
            assert (await post(client,payload)).status==400
            assert store.after(0)==[]
    asyncio.run(scenario())


def test_unrecognized_push_is_ignored_and_transport_test_remains_explicitly_nonfinancial(tmp_path):
    async def scenario():
        async with client_for(tmp_path/'capture.sqlite3') as (client,store):
            ignored=await (await post(client,{'text':'Ваш код 123456'})).json()
            assert ignored['status']=='ignored_unrecognized_notification'
            assert store.after(0)==[]
            result=await (await post(client,{'text':'Проверка соединения','kind':'transport_test'})).json()
            assert result['amount_minor'] is None and result['payment_confirmed'] is False
            assert 'Это не банковский платёж' in notice_text(store.after(0)[0])
    asyncio.run(scenario())


def test_payload_limit_and_rate_limit_are_enforced(tmp_path):
    async def scenario():
        async with client_for(tmp_path/'capture.sqlite3') as (client,store):
            response=await client.post('/capture',data='x'*9000,
                headers={'Authorization':'Bearer '+WRITE,'Content-Type':'application/json'})
            assert response.status==413
            for _ in range(59):
                assert (await post(client,{'text':'not a credit'})).status==200
            assert (await post(client,{'text':'not a credit'})).status==429
            assert store.after(0)==[]
    asyncio.run(scenario())


@pytest.mark.parametrize('write,read,card',[('',READ,CARD),(WRITE,WRITE,CARD),(WRITE,READ,'x')])
def test_unsafe_receiver_configuration_is_rejected(tmp_path,write,read,card):
    with pytest.raises(ValueError):
        create_app(NoticeStore(tmp_path/'capture.sqlite3'),write,read,card)
