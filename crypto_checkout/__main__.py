import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from .tron import TronReader, address_bytes


async def inspect(address, out):
    address_bytes(address)
    reader = TronReader(os.getenv('TRONGRID_API_KEY',''))
    try:
        height = await reader.height()
        txids = await reader.candidates(address,int((time.time()-86400)*1000))
        verified = []
        for txid in txids[:5]:
            verified.extend(t for t in await reader.transfers(txid) if t.address == address)
        report = {'network':'TRON mainnet','token':'USDT','address_checksum_valid':True,
                  'solidified_height':height,'history_candidates_24h':len(txids),
                  'checked_receipts':min(5,len(txids)),'verified_incoming_events':len(verified),
                  'read_only':True,'bank_access_used':False,'private_keys_used':False,
                  'invoices_created':0,'card_charges':0}
        if out:
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        await reader.close()


def main():
    parser = argparse.ArgumentParser(description='Isolated crypto checkout diagnostics; never buys a license')
    sub = parser.add_subparsers(dest='command',required=True)
    cmd = sub.add_parser('inspect',help='Read only the last 24h of USDT receipts')
    cmd.add_argument('--address',required=True)
    cmd.add_argument('--out',type=Path)
    demo = sub.add_parser('demo',help='Run synthetic end-to-end fulfillment, no real funds')
    demo.add_argument('--out',type=Path,default=Path('data/crypto-checkout/demo'))
    args = parser.parse_args()
    if args.command == 'inspect':
        asyncio.run(inspect(args.address,args.out))
    else:
        from .demo import run_demo
        asyncio.run(run_demo(args.out))


if __name__ == '__main__':
    main()
