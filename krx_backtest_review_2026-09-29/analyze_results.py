"""Read-only diagnostics of the supplied saved replay; not a market-data rerun."""
import gzip,json,hashlib,csv,statistics,math,argparse
from collections import Counter,defaultdict
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--input-dir',type=Path,default=Path(__file__).resolve().parent.parent)
parser.add_argument('--output-dir',type=Path,default=Path(__file__).resolve().parent)
args=parser.parse_args()
ROOT=args.input_dir.resolve(); OUT=args.output_dir.resolve(); OUT.mkdir(parents=True,exist_ok=True)
for filename in ('result_full_2025_to_latest.json.gz','backtest_audit.json.gz'):
 if not (ROOT/filename).is_file(): parser.error(f'Missing input: {ROOT/filename}')
r=json.load(gzip.open(ROOT/'result_full_2025_to_latest.json.gz','rt'))
a=json.load(gzip.open(ROOT/'backtest_audit.json.gz','rt'))
curve=r['equity_curve']; sessions=[x['session'] for x in curve]; idx={s:i for i,s in enumerate(sessions)}
bydate=defaultdict(list)
for t in r['trades']: bydate[t['session']].append(t)
pos={}; paired=[]; days=[]; errs=[]
slip=r['strategy']['slippage_bps']/10000
for c in curve:
 for t in bydate[c['session']]:
  if t['side']=='buy':
   if t['symbol'] in pos: errs.append(('double_buy',t))
   pos[t['symbol']]=t
  else:
   b=pos.pop(t['symbol']); buy_value=b['quantity']*b['price']; sell_value=t['quantity']*t['price']
   pnl=sell_value-t['fee']-buy_value-b['fee']
   assert t['quantity']==b['quantity']
   assert abs(pnl-t['profit'])<1e-7
   raw_buy=b['price']/(1+slip); raw_sell=t['price']/(1-slip)
   paired.append(dict(symbol=t['symbol'],entry=b['session'],exit=t['session'],quantity=b['quantity'],buy_price=b['price'],sell_price=t['price'],entry_value=buy_value,entry_cost=buy_value+b['fee'],profit=pnl,net_return=pnl/(buy_value+b['fee']),holding_intervals=idx[t['session']]-idx[b['session']],reason='|'.join(t['reason']) if isinstance(t['reason'],list) else t['reason'],fees=b['fee']+t['fee'],gross_no_cost_pnl=(raw_sell-raw_buy)*b['quantity'],slippage_cost=(b['price']-raw_buy+raw_sell-t['price'])*b['quantity']))
 cash=r['initial_cash']+sum((1 if t['side']=='sell' else -1)*t['price']*t['quantity']-t['fee'] for t in r['trades'] if t['session']<=c['session'])
 assert abs(cash-c['cash'])<1e-6
 assert abs(c['cash']+c['holdings_value']+c['receivable']-c['equity'])<1e-6
 days.append(dict(session=c['session'],positions=len(pos),active_positions=len(pos)-('020760' in pos and c['session']>'2025-09-11'),exposure=c['exposure'],holdings_value=c['holdings_value'],equity=c['equity']))

def stat(xs):
 s=sorted(xs); n=len(s)
 def q(p):
  j=(n-1)*p; a=int(j); z=j-a
  return s[a]*(1-z)+s[min(a+1,n-1)]*z
 return dict(n=n,mean=statistics.mean(s),median=statistics.median(s),min=s[0],p10=q(.1),p90=q(.9),max=s[-1]) if n else {}

def trade_stats(ts):
 wins=[t for t in ts if t['profit']>0];losses=[t for t in ts if t['profit']<0]
 gp=sum(t['profit'] for t in wins);gl=-sum(t['profit'] for t in losses)
 return dict(n=len(ts),wins=len(wins),losses=len(losses),flat=len(ts)-len(wins)-len(losses),pnl=sum(t['profit'] for t in ts),profit_factor=gp/gl if gl else None,avg_win=statistics.mean(t['profit'] for t in wins) if wins else None,avg_loss=statistics.mean(t['profit'] for t in losses) if losses else None,avg_net_return=statistics.mean(t['net_return'] for t in ts),net_return_stats=stat([t['net_return'] for t in ts]),holding_intervals=stat([t['holding_intervals'] for t in ts]))
realized=sum(t['profit'] for t in paired); end=curve[-1]
open_cost=sum(p['cost_per_share']*p['quantity'] for p in r['open_positions'].values())
open_book_values={}
# No per-symbol terminal prices are supplied; do not fabricate stale-position marks.
summary={
 'limitations':['Original dataset.json.gz and per-symbol OHLC/benchmark bars absent; cannot rerun altered strategy or independently reproduce KOSPI return.','All cost counterfactuals keep the saved transactions and quantities fixed.'],
 'result_canonical_hash':hashlib.sha256(json.dumps(r,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
 'hash_matches_audit':hashlib.sha256(json.dumps(r,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()==a['result_hash'],
 'equity_change':end['equity']-r['initial_cash'],'net_realized':realized,'open_cost':open_cost,'open_mark':end['holdings_value'],'unrealized':end['holdings_value']-open_cost,
 'trade_stats':trade_stats(paired),'entry_notional_stats':stat([t['price']*t['quantity'] for t in r['trades'] if t['side']=='buy']),
 'position_day_counts':dict(sorted(Counter(d['positions'] for d in days).items())),
 'mean_positions':statistics.mean(d['positions'] for d in days),'mean_active_positions':statistics.mean(d['active_positions'] for d in days),
 'exposure_stats':stat([c['exposure'] for c in curve]),'by_year':{},'reason_exact':{},'reason_overlap':{},
 'fee_total':sum(t['fee'] for t in r['trades']),
 'slippage_all_fixed_trades':sum(t['quantity']*abs(t['price']-t['price']/(1+(slip if t['side']=='buy' else -slip))) for t in r['trades']),
 'fees_closed':sum(t['fees'] for t in paired),'slippage_closed':sum(t['slippage_cost'] for t in paired),
 'gross_no_cost_realized_fixed_trades':sum(t['gross_no_cost_pnl'] for t in paired),
 'tax_change_fixed_trades':sum(t['quantity']*t['price']*.0005 for t in r['trades'] if t['side']=='sell' and t['session']<'2025-12-29'),
 'top_winners':sorted(paired,key=lambda t:t['profit'],reverse=True)[:10],'top_losers':sorted(paired,key=lambda t:t['profit'])[:10],
 'closed_holding_intervals':dict(sorted(Counter(t['holding_intervals'] for t in paired).items())),
 'unknown_errors':errs,
 'fixed_trades_zero_cost_portfolio_pnl':end['equity']-r['initial_cash']+sum(t['fee'] for t in r['trades'])+sum(t['quantity']*abs(t['price']-t['price']/(1+(slip if t['side']=='buy' else -slip))) for t in r['trades']),
 'reconciliation':{'daily_cash_and_equity':True,'closed_trade_profits':True,'final_quantities':{s:b['quantity'] for s,b in pos.items()}=={s:p['quantity'] for s,p in r['open_positions'].items()}},
}
base=r['initial_cash']
for y in sorted({s[:4] for s in sessions}):
 ds=[d for d in days if d['session'][:4]==y];ts=[t for t in paired if t['exit'][:4]==y]
 summary['by_year'][y]={'return':ds[-1]['equity']/base-1,'mean_positions':statistics.mean(d['positions'] for d in ds),'mean_exposure':statistics.mean(d['exposure'] for d in ds),'slot_full_days':sum(d['positions']==5 for d in ds),'sessions':len(ds),'closed_trades':trade_stats(ts)}
 base=ds[-1]['equity']
for y, yearly in summary['by_year'].items():
 ts=[t for t in paired if t['exit'][:4]==y]
 yearly['gross_no_cost_realized_fixed_trades']=sum(t['gross_no_cost_pnl'] for t in ts)
for reason in sorted({t['reason'] for t in paired}):
 summary['reason_exact'][reason]=trade_stats([t for t in paired if t['reason']==reason])
for reason in sorted({s for t in paired for s in t['reason'].split('|')}):
 summary['reason_overlap'][reason]=trade_stats([t for t in paired if reason in t['reason'].split('|')])
for name, rows in [('round_trips.csv',paired),('daily_diagnostics.csv',days)]:
 with (OUT/name).open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
(OUT/'computed_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k not in ['top_winners','top_losers']},indent=2,ensure_ascii=False))
print('TOP WINNERS',[(x['symbol'],x['entry'],x['exit'],round(x['profit']),round(x['net_return']*100,2)) for x in summary['top_winners']])
print('TOP LOSERS',[(x['symbol'],x['entry'],x['exit'],round(x['profit']),round(x['net_return']*100,2)) for x in summary['top_losers']])
