import sys, os
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)
"""Fixed strategy audit. No parameter search, no production artifact writes."""
import json,hashlib,statistics,time
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal as D
from wtrader.config import load_settings
from wtrader.data import load_csv,resample,load_frame
from wtrader.backtest import run_backtest,metrics,CostModel
from wtrader.ai_agent import BaselineSelector
import argparse
parser=argparse.ArgumentParser();parser.add_argument('--out',default='reports/audit/replay-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
args=parser.parse_args()
outdir=Path(args.out);outdir.mkdir(parents=True,exist_ok=False)
wallex_data=Path('data/wallex_audit')
report={'generated_at':datetime.now(timezone.utc).isoformat(),'protocol':{'selection':'fixed original hypotheses; no parameter optimization','risk_clock':'1m replay exits for all families; continuous risk state within each split','edge_gate':'offline advisory research only; strict runtime files unchanged','splits':'proxy 45d train/15d validation/30d test; Wallex 7d/3d/4d','warning':'proxy test window was previously examined; this is a reanalysis, not a pristine held-out validation'},'datasets':{}}
for source in ['kucoin_proxy','wallex']:
 symbols=['BTC','ETH','SOL','XRP','DOGE','BNB','ADA','TRX']
 symbols=[s+('USDT' if source=='kucoin_proxy' else 'TMN') for s in symbols]
 one={s:(load_frame(Path('data'),source,s,60) if source=='kucoin_proxy' else load_csv(wallex_data/f'{s}_60.csv')) for s in symbols}
 start=max(rows[0].open_time for rows in one.values());end=min(rows[-1].open_time for rows in one.values())+60
 cuts=[start,start+(45 if source=='kucoin_proxy' else 7)*86400,start+(60 if source=='kucoin_proxy' else 10)*86400,end]
 data={'start':start,'end':end,'symbols':symbols,'families':{},'row_counts':{s:len(v) for s,v in one.items()},'hashes':{s:hashlib.sha256((Path('data')/source/f'{s}_60.csv' if source=='kucoin_proxy' else wallex_data/f'{s}_60.csv').read_bytes()).hexdigest() for s in symbols}}
 for fam in ['DONCHIAN_TREND','SCALP_REVERSION','SCALP_MOMENTUM']:
  s=load_settings('config/strategy.yaml' if fam=='DONCHIAN_TREND' else 'config/scalp.yaml')
  s.strategy.enabled_families=[fam];s.strategy.market_filter_symbol=symbols[0];s.strategy.edge_gate='advisory'
  series={sym:(resample(v,60,s.market.signal_seconds),resample(v,60,s.market.context_seconds)) for sym,v in one.items()}
  def replay(a,b,costs=CostModel()):
   r=run_backtest(s,series,a,b,BaselineSelector(),costs,{},fam,episode_days=100000,record_decisions=True,exit_series=one,exit_s=60,fill_delay_s=60)
   m=metrics(r,1000000);m['trades_per_day']=round(len(r.trades)/((b-a)/86400),3)
   m['candidate_decision_points']=len(r.decisions);m['risk_rejections']={reason:sum(d.get('risk',{}).get('reason')==reason for d in r.decisions if d.get('risk')) for reason in sorted({d['risk']['reason'] for d in r.decisions if d.get('risk') and not d['risk']['approved']})}
   return r,m
  f={}
  for label,a,b in zip(['train','validation','test'],cuts,cuts[1:]):
   r,m=replay(a,b);f[label]=m
   if label=='test': (outdir/f'{source}_{fam}_test_trades.json').write_text(json.dumps([t.__dict__ for t in r.trades],indent=1))
   print(source,fam,label,m['net_return_pct'],m['trades'],flush=True)
  f['validation_cost_sensitivity']={}
  for name,c in [('zero_fee',CostModel(fee_fraction=D(0))),('wide_spread',CostModel(half_spread_bps=D(15)))]:
   f['validation_cost_sensitivity'][name]=replay(cuts[1],cuts[2],c)[1]
  # Two fixed forward windows in the previous test period; no fitting uses their outcomes.
  mid=cuts[2]+(cuts[3]-cuts[2])//2//3600*3600
  f['forward_windows']=[replay(cuts[2],mid)[1],replay(mid,cuts[3])[1]]
  data['families'][fam]=f
 report['datasets'][source]=data
 (outdir/'strategy_audit.json').write_text(json.dumps(report,indent=2))
