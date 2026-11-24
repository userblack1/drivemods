"""One-use local consent form for the owner's encrypted purchasing-card vault.

The helper never pays, contacts Stripe or uploads files. It saves only encrypted
credentials and a separate private key after the explicit form consent.
"""
import argparse
import hmac
import html
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from scripts.connect_crypto import SetupHandler,SetupServer,private_paths,write_url_file
from crypto_checkout.card_vault import OwnerCard,OwnerCardVault


class CardSetupHandler(SetupHandler):
    def do_GET(self):
        if not self._allowed():return
        state=self.server.state
        body=("<p>Карта владельца с окончанием <strong>6994</strong> используется только для закупок "
              "у DriveMods: FULL до $90 USD, ECO до $60 USD, после подтверждённой оплаты клиента.</p>"
              "<p>После согласия реквизиты сохранятся в зашифрованном файле; ключ — отдельно. "
              "Их можно передать на ваш VPS как закрытые credentials. В GitHub и архив исходников они не входят.</p>"
              "<form method='post' action='"+html.escape(state.path,quote=True)+"' autocomplete='off'>"
              "<input type='hidden' name='csrf' value='"+html.escape(state.csrf,quote=True)+"'>")
        for key,label,length in [('pan','Номер карты владельца',23),('expiry','Срок MM/YY',5),
                                 ('cvc','Проверочный код карты',3),('name','Имя владельца',120)]:
            kind='text' if key=='name' else 'password'
            body+=("<label for='"+key+"'>"+label+"</label><input id='"+key+"' name='"+key+
                   "' type='"+kind+"' required maxlength='"+str(length)+"' autocomplete='off' spellcheck='false'>")
        body+=("<label><input style='width:auto' type='checkbox' name='consent' value='yes' required> "
               "Разрешаю сохранить мою карту для автоматических покупок DriveMods с указанными лимитами "
               "в защищённом хранилище на моём компьютере и VPS.</label>"
               "<button type='submit'>Зашифровать карту владельца</button></form>"
               "<small>Эта форма ничего не списывает. Подтверждения банка при 3DS выполняются владельцем.</small>")
        self._reply(200,'Подключение карты для закупок',body)

    def do_POST(self):
        if not self._allowed(post=True):return
        length=self.headers.get('Content-Length','')
        if (self.headers.get('Transfer-Encoding') or len(length)>4 or not length.isascii()
                or not length.isdecimal() or not 0<int(length)<=4096
                or self.headers.get_content_type()!='application/x-www-form-urlencoded'):
            self._reply(400,'Запрос отклонён');return
        card=None
        try:
            body=self.rfile.read(int(length));self._body_read=True
            f=parse_qs(body.decode('utf-8','strict'),keep_blank_values=True,strict_parsing=True,max_num_fields=6)
            if (set(f)!={'csrf','pan','expiry','cvc','name','consent'} or any(len(v)!=1 for v in f.values())
                    or not hmac.compare_digest(f['csrf'][0].encode(),self.server.state.csrf.encode())
                    or f['consent']!=['yes']):
                self._reply(403,'Нужно явное согласие владельца');return
            card=OwnerCard(re.sub(r'[\s-]','',f['pan'][0]),f['expiry'][0],f['cvc'][0],f['name'][0])
            _,stage=private_paths(self.server.state.root)
            OwnerCardVault.provision(card,stage/'owner-card.key',stage/'owner-card.card-vault',consent=True)
        except Exception:
            self._reply(400,'Карта не сохранена',
                        '<p>Проверьте поля и согласие. Существующие credentials не перезаписываются. Значения не отображаются.</p>')
            return
        finally:
            if card:card.clear()
        self.server.state.completed=True
        self._reply(200,'Карта 6994 зашифрована',
                    '<p>Закрытые файлы сохранены локально. Покупки и сетевые запросы не выполнялись.</p>')


class CardSetupServer(SetupServer):
    def __init__(self,root=ROOT,**kwargs):
        super().__init__(root,**kwargs)
        self.RequestHandlerClass=CardSetupHandler
        self.page_title='Защищённая карта владельца'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--serve',action='store_true');p.add_argument('--url-file',type=Path)
    args=p.parse_args()
    if not args.serve:p.print_help();return
    server=CardSetupServer()
    if args.url_file:write_url_file(ROOT,args.url_file,server.local_url)
    print(server.local_url,flush=True)
    try:server.serve_once_setup()
    finally:server.server_close()


if __name__=='__main__':main()
