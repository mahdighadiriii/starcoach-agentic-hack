import sys, os
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)
"""Read-only, fixed-window dataset collection. New artifacts only; preserves old evidence."""
import json,time
from pathlib import Path
from wtrader.wallex import WallexClient,diagnose_network
from wtrader.data import save_csv
symbols=['BTCTMN','ETHTMN','SOLTMN','XRPTMN','DOGETMN','BNBTMN','ADATMN','TRXTMN']
import argparse
parser=argparse.ArgumentParser();parser.add_argument('--out',default='data/wallex_audit');parser.add_argument('--days',type=int,default=14);parser.add_argument('--end',type=int)
args=parser.parse_args()
root=Path(args.out);root.mkdir(parents=True,exist_ok=False)
from wtrader.config import load_dotenv, Secrets
load_dotenv()
secrets = Secrets.from_env()
c=WallexClient(bind_interface=secrets.wallex_bind_interface,proxy=secrets.wallex_proxy,read_attempts=1,timeout=3,connect_timeout=2)
end=args.end or int(time.time())//3600*3600;start=end-args.days*86400
report={}
for sym in symbols:
 out={};errors=[]
 for a in range(start,end,2000*60):
  b=min(end,a+2000*60)
  try:
   rows=c.candles(sym,60,a,b)
   out.update({r.open_time:r for r in rows})
  except Exception as e: errors.append({'from':a,'error':type(e).__name__})
  time.sleep(.1)
 rows=[out[t] for t in sorted(out)]
 save_csv(root/f'{sym}_60.csv',rows,{'source':'wallex','symbol':sym,'interval_s':60,'start':start,'end':end,'proxy':False,'tls_verified':True})
 gaps=sum(b.open_time-a.open_time!=60 for a,b in zip(rows,rows[1:]))
 report[sym]={'rows':len(rows),'expected_rows':(end-start)//60,'coverage':round(len(rows)/((end-start)//60),4),'gaps':gaps,'errors':errors,'first':rows[0].open_time if rows else None,'last':rows[-1].open_time if rows else None}
 print(sym,json.dumps(report[sym]),flush=True)
(root/'quality.json').write_text(json.dumps({'start':start,'end':end,'markets':report},indent=2))
c.close()
