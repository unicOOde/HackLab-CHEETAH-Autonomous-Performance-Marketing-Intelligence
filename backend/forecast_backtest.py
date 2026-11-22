"""Backtest FC-01 on a synthetic run (Master PDR §10.2: MAE, WAPE, interval coverage, bias).

Replays every cutoff D the worker would forecast at (warm-up..30) through the exact same
`forecasting.predict_amount` call, and compares each D+1 band with the actual daily Amount and the
run's injected anomaly labels. Calls the Prior Labs cloud API once per cutoff (needs TABPFN_TOKEN).

    .venv-backend/bin/python backend/forecast_backtest.py --run-id full

TABPFN_TOKEN is taken from the environment, or else from the repo-root .env (backend/env.py).
"""
import argparse, json, sys
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(REPO/'src'))


import env  # noqa: F401 — loads the repo-root .env (TABPFN_TOKEN etc.) unless already set
import pandas as pd
import forecasting
from cheetah_synth.backend_adapter import build_daily_amounts, load_journeys


def labels_for(run_dir, day):
    path=run_dir/'labels'/'anomalies.csv'
    if not path.exists():return []
    df=pd.read_csv(path)
    hit=df[(df['date_start']<=day)&(df['date_end']>=day)]
    return [f"{r.anomaly_id}:{r.type}(x{r.severity})" for r in hit.itertuples()]


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--run-id',default='full')
    p.add_argument('--out-dir',default=str(REPO/'data'/'synthetic'))
    args=p.parse_args(argv)
    cfg=forecasting.config()
    if cfg['status']!='configured':
        print(f"Forecast service not configured (token present: {cfg['token_present']}, package: {cfg['package_version']}).",file=sys.stderr)
        return 1
    run_dir=Path(args.out_dir)/args.run_id
    daily=build_daily_amounts(load_journeys(run_dir))
    history=[(r['day'],r['amount']) for r in daily]
    rows=[]
    for cutoff in range(cfg['warmup_days'],len(history)):
        f=forecasting.predict_amount(forecasting.context_window(history[:cutoff],cfg),1)[0]  # same input window as the worker
        day,actual=history[cutoff]
        assert f['target_day']==day
        status='below' if actual<f['lower'] else ('above' if actual>f['upper'] else 'within')
        rows.append({'day':day,'actual':actual,**{k:f[k] for k in ('point','lower','upper')},'status':status,'labels':labels_for(run_dir,day)})
        print(f"{day}  actual {actual:>10.2f}  P50 {f['point']:>10.2f}  [{f['lower']:>10.2f}, {f['upper']:>10.2f}]  {status:<6} {' '.join(rows[-1]['labels'])}",flush=True)
    err=[r['actual']-r['point'] for r in rows]
    total=sum(abs(r['actual']) for r in rows)
    summary={
        'run_id':args.run_id,'model_version':forecasting.model_version(cfg),'horizon_days':1,'context_days':cfg['context_days'],
        'cutoffs':len(rows),'first_evaluated_day':rows[0]['day'] if rows else None,
        'mae':round(sum(abs(e) for e in err)/len(err),2),
        'wape':round(sum(abs(e) for e in err)/total,4) if total else None,
        'bias':round(sum(err)/len(err),2),
        'p10_p90_coverage':round(sum(r['status']=='within' for r in rows)/len(rows),3),
        'breaches':[{'day':r['day'],'status':r['status'],'labels':r['labels']} for r in rows if r['status']!='within'],
    }
    print(json.dumps({k:v for k,v in summary.items() if k!='breaches'},indent=2))
    print(f"Breaches: {len(summary['breaches'])} — "+', '.join(f"{b['day']} {b['status']}{' ['+' '.join(b['labels'])+']' if b['labels'] else ' [no label]'}" for b in summary['breaches']))
    out=run_dir/'forecast_backtest.json'
    out.write_text(json.dumps({'summary':summary,'rows':rows},indent=2))
    print(f"Wrote {out}")
    return 0


if __name__=='__main__':
    sys.exit(main())
