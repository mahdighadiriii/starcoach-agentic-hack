import sys, os
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)
import json
import argparse
from datetime import datetime,timezone
from pathlib import Path
from decimal import Decimal as D
from wtrader.data import load_frame,resample
from wtrader.config import load_settings
from wtrader.backtest import CostModel,run_backtest,metrics
from wtrader.ai_agent import BaselineSelector
parser=argparse.ArgumentParser();parser.add_argument('--out',default='reports/audit/sensitivity-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'.json')
args=parser.parse_args()
symbols=[x+'USDT' for x in ['BTC','ETH','SOL','XRP','DOGE','BNB','ADA','TRX']]
one={s:load_frame(Path('data'),'kucoin_proxy',s,60) for s in symbols}
a=max(v[0].open_time for v in one.values())+45*86400;b=a+15*86400
out={'scope':'validation only; diagnostic perturbations, no parameter selection','from':a,'to':b,'results':{}}
for fam in ['DONCHIAN_TREND','SCALP_REVERSION','SCALP_MOMENTUM']:
 variants=[{'atr_stop_multiplier':'2.5'},{'atr_stop_multiplier':'3.5'}] if fam=='DONCHIAN_TREND' else [{'scalp_stop_fraction':'.005','scalp_target_fraction':'.009'},{'scalp_stop_fraction':'.010','scalp_target_fraction':'.018'}]
 out['results'][fam]=[]
 for changes in variants:
  s=load_settings('config/strategy.yaml' if fam=='DONCHIAN_TREND' else 'config/scalp.yaml');s.strategy.enabled_families=[fam];s.strategy.market_filter_symbol=symbols[0];s.strategy.edge_gate='advisory'
  for k,v in changes.items():setattr(s.strategy,k,v)
  series={sym:(resample(v,60,s.market.signal_seconds),resample(v,60,s.market.context_seconds)) for sym,v in one.items()}
  r=run_backtest(s,series,a,b,BaselineSelector(),CostModel(),{},fam,episode_days=100000,record_decisions=False,exit_series=one,exit_s=60,fill_delay_s=60)
  m=metrics(r,1000000);out['results'][fam].append({'changes':changes,'metrics':m});print(fam,changes,m['net_return_pct'],m['trades'],flush=True)
p=Path(args.out);p.parent.mkdir(parents=True,exist_ok=True)
with p.open('x') as f:json.dump(out,f,indent=2)
