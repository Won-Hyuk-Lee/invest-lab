"""Isolated, read-only reproduction of selected behaviors in the supplied engine.
Uses synthetic data, never the user's market data; no network, broker, or app IO.
Only the RSI function is AST-extracted because the supplied models.py is absent.
"""
import ast,copy,importlib.util,json,sys,types,argparse
from datetime import date,timedelta
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--input-dir',type=Path,default=Path(__file__).resolve().parent.parent)
parser.add_argument('--output-dir',type=Path,default=Path(__file__).resolve().parent)
args=parser.parse_args()
BASE=args.input_dir.resolve(); OUT=args.output_dir.resolve(); OUT.mkdir(parents=True,exist_ok=True)
for filename in ('indicators.py','research.py','strategies.py'):
    if not (BASE/filename).is_file(): parser.error(f'Missing input: {BASE/filename}')
for name in ('invest_lab','invest_lab.domain'):
    m=types.ModuleType(name);m.__path__=[];sys.modules[name]=m
name='invest_lab.domain.indicators';m=types.ModuleType(name)
tree=ast.parse((BASE/'indicators.py').read_text(encoding='utf-8-sig'))
nodes=[n for n in tree.body if isinstance(n,(ast.Import,ast.ImportFrom)) and not(isinstance(n,ast.ImportFrom) and (n.module or '').startswith('invest_lab'))]
nodes += [n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='rsi_wilder']
exec(compile(ast.Module(body=nodes,type_ignores=[]),str(BASE/'indicators.py'),'exec'),m.__dict__)
sys.modules[name]=m
for short in ('research','strategies'):
    name=f'invest_lab.domain.{short}'
    spec=importlib.util.spec_from_file_location(name,BASE/f'{short}.py')
    mod=importlib.util.module_from_spec(spec);sys.modules[name]=mod;spec.loader.exec_module(mod)
st=sys.modules['invest_lab.domain.strategies'];res=sys.modules['invest_lab.domain.research']
sessions=[];d=date(2024,1,2)
while len(sessions)<151:
    if d.weekday()<5:sessions.append(d.isoformat())
    d+=timedelta(days=1)
def make_data():
    bars=[];universe=[];bm=[]
    for i,s in enumerate(sessions):
        p=100+.1*i;at=res.decision_time(s).isoformat()
        row=dict(symbol='TEST',session=s,open=p,high=p+1,low=p-1,close=p,volume=1000000,turnover=p*1000000,price_basis='raw',available_at=at,ingested_at=at,venue='KRX',session_type='regular',final=True)
        bars.append(row);bm.append({**row,'symbol':'KOSPI'})
        universe.append(dict(session=s,symbols=['TEST'],available_at=at,ingested_at=at))
    at=res.decision_time(sessions[0]).isoformat()
    return dict(name='synthetic isolated behavior test',source_kind='synthetic',sessions=sessions,instruments=[dict(symbol='TEST',kind='etf_kr'),dict(symbol='KOSPI',kind='benchmark')],bars=bars,universe=universe,benchmarks=bm,actions=[],financial_facts=[],macro=[],etf_metadata=[dict(symbol='TEST',leverage=1,inverse=False,benchmark_id='KOSPI',available_at=at,ingested_at=at)],provenance={'source':'synthetic behavior fixture'})
rules=st.StrategyRules(strategy_type='etf_trend')
base=make_data()
baseline=st.replay(base,rules,start_session=sessions[124],end_session=sessions[140])
buy=next(t for t in baseline['trades'] if t['side']=='buy');sell=next(t for t in baseline['trades'] if t['side']=='sell')
out={'scope':'Synthetic engine behavior checks; not a real-data rerun or the application test suite.','checks':[]}
out['checks'].append(dict(name='holding_expiry',configured=10,entry=buy['session'],exit=sell['session'],open_to_open_intervals=sessions.index(sell['session'])-sessions.index(buy['session']),reproduced=True))
# A revision not publicly released until after the entire replay changes an old fill.
revdata=copy.deepcopy(base)
original=next(b for b in revdata['bars'] if b['session']==buy['session'])
rev={**original,'open':original['open']+1,'high':original['high']+1,'available_at':'2026-09-29T09:00:00+00:00','ingested_at':'2026-09-29T09:00:00+00:00'}
revdata['bars'].append(rev)
changed=st.replay(revdata,rules,start_session=sessions[124],end_session=sessions[140])
changedbuy=next(t for t in changed['trades'] if t['side']=='buy')
assert changedbuy['price']!=buy['price']
out['checks'].append(dict(name='future_raw_revision_changes_historical_fill',original_fill=buy['price'],future_revision_fill=changedbuy['price'],revision_available_at=rev['available_at'],reproduced=True))
# A sell intent after one interval is lost on a halted morning; no retry on reopening.
halted=copy.deepcopy(base);halt_index=127
h=next(b for b in halted['bars'] if b['session']==sessions[halt_index])
h.update(open=0,high=0,low=0,volume=0,turnover=0,valuation_only=True,tradable=False)
# Keep a different instrument tradable so an empty-universe guard does not abort.
halted['instruments'].append(dict(symbol='OTHER',kind='kospi'))
for u,b in zip(halted['universe'],base['bars']):
    u['symbols'].append('OTHER');halted['bars'].append({**b,'symbol':'OTHER'})
next(u for u in halted['universe'] if u['session']==sessions[halt_index])['symbols']=['OTHER']
r2=st.replay(halted,st.StrategyRules(strategy_type='etf_trend',max_holding_sessions=1),start_session=sessions[124],end_session=sessions[130])
testtrades=[t for t in r2['trades'] if t['symbol']=='TEST']
firstsell=next(t for t in testtrades if t['side']=='sell')
assert firstsell['session']==sessions[129]
out['checks'].append(dict(name='sell_intent_dropped_after_unfilled',first_requested_execution=sessions[127],reopening=sessions[128],actual_execution=firstsell['session'],delay_sessions=1,reproduced=True))
# The current-day universe is available at 18:00, but gates that same day's open fill.
univ=copy.deepcopy(base)
univ['instruments'].append(dict(symbol='OTHER',kind='kospi'))
for u,b in zip(univ['universe'],base['bars']):
    u['symbols'].append('OTHER');univ['bars'].append({**b,'symbol':'OTHER'})
next(u for u in univ['universe'] if u['session']==buy['session'])['symbols']=['OTHER']
rr=st.replay(univ,rules,start_session=sessions[124],end_session=sessions[126])
first_day_test_buys=[t for t in rr['trades'] if t['symbol']=='TEST' and t['side']=='buy' and t['session']==buy['session']]
assert not first_day_test_buys
out['checks'].append(dict(name='evening_universe_gates_same_day_open',execution_session=buy['session'],universe_available_at=res.decision_time(buy['session']).isoformat(),baseline_has_buy=True,changed_evening_universe_has_buy=False,reproduced=True))
# Risk estimate excludes the entry commission; it is not a hard realized loss ceiling.
q=st.size_position(equity=10000000,cash=10000000,price=100,stop_distance=10,volume20=1000000,rules=st.StrategyRules(sell_tax_bps=20))
out['checks'].append(dict(name='small_size_example',price=100,stop_distance=10,quantity=q,position_value=q*100,allocation_pct=q*100/10000000*100,risk_budget_pct=.25))
# Validate stable indicator conventions with monotonic and constant prices.
rsi=sys.modules['invest_lab.domain.indicators'].rsi_wilder
out['indicator_sanity']={'up':rsi(list(range(1,151))),'down':rsi(list(range(151,1,-1))),'constant':rsi([100]*150),'too_short':rsi([100]*14)}
assert out['indicator_sanity']==dict(up=100.0,down=0.0,constant=50.0,too_short=None)
(OUT/'synthetic_checks.json').write_text(json.dumps(out,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps(out,indent=2,ensure_ascii=False))
