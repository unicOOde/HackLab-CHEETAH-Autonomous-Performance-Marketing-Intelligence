"""FC-01 — forecast adapter and reference manager (Master PDR v2.0 §4.2, §6.3, §6.4).

Trigger: when the stream's clock rolls over into a new day D+1 (day D closes), the worker calls
`issue_forecast`, which forecasts the aggregate daily Amount for D+1..D+horizon (default 7) with
TabPFN-TS (Prior Labs cloud client) from the last FORECAST_CONTEXT_DAYS (default 7) closed days up
to cutoff D — never anything later. The D+1 value becomes the immutable ForecastReference that day
D+1 is evaluated against; D+2.. are outlook only.

Non-fabrication: warm-up, missing configuration and provider failures all emit an explicit
`forecast.unavailable` with a reason and write no reference — there is no silent fallback model.
The token is read from the environment only and never persisted or emitted.
"""
import os, uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, timedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from models import DailyAmount, ForecastValue, ForecastReference

SOURCE='tabpfn_ts'
QUANTILES=(0.1,0.5,0.9)  # lower / point / upper = P10 / P50 / P90 (PDR §6.3, [PROPOSED])
MONTH_DAYS=[f'2026-10-{i:02d}' for i in range(1,32)]


def config():
    """Effective forecast-service configuration. `status` is 'configured' only when both the
    token and the package are present; the token value itself is never returned."""
    try:
        import importlib.metadata as md
        version=md.version('tabpfn-time-series')
    except Exception:
        version=None
    token=bool(os.getenv('TABPFN_TOKEN'))
    return {
        'status':'configured' if (token and version) else 'not_configured',
        'token_present':token,
        'package_version':version,
        'warmup_days':int(os.getenv('FORECAST_WARMUP_DAYS','7')),
        'context_days':int(os.getenv('FORECAST_CONTEXT_DAYS','7')),
        'horizon_days':int(os.getenv('FORECAST_HORIZON_DAYS','7')),
        'timeout_s':float(os.getenv('FORECAST_TIMEOUT_S','60')),
    }


def public_status(cfg=None):
    """Connection status for /health and the UI pill — booleans and versions only, no secrets."""
    cfg=cfg or config()
    return {'status':cfg['status'],'token_loaded':cfg['token_present'],'package_installed':bool(cfg['package_version']),
            'model_version':model_version(cfg) if cfg['package_version'] else None,
            'context_days':cfg['context_days'],'horizon_days':cfg['horizon_days'],'warmup_days':cfg['warmup_days']}


def context_window(history, cfg=None):
    """The model input: the last `context_days` closed days up to the cutoff. Shared by the worker
    and backend/forecast_backtest.py so the backtest measures exactly what runs live."""
    cfg=cfg or config()
    return history[-cfg['context_days']:]


def model_version(cfg=None):
    cfg=cfg or config()
    return f"tabpfn-ts-{cfg['package_version'] or 'unknown'}/p10-p50-p90"


def target_days(cutoff_day, horizon):
    d=date.fromisoformat(cutoff_day)
    return [(d+timedelta(days=i)).isoformat() for i in range(1,horizon+1)]


def _quantile_col(pred, q):
    for c in pred.columns:
        try:
            if abs(float(c)-q)<1e-9:return c
        except (TypeError,ValueError):
            continue
    raise KeyError(f'TabPFN-TS output has no {q} quantile column (columns: {list(pred.columns)})')


def predict_amount(history, horizon=1):
    """Pure provider call. `history` is [(day_iso, amount), ...] ordered by day, ending at the
    cutoff. Returns [{target_day, point, lower, upper}, ...] for the next `horizon` days. Raises on
    any provider problem — the caller turns that into an explicit unavailable state."""
    # Opt out of the package's PostHog usage telemetry: only the inference request itself should
    # leave the backend. setdefault so an operator can still explicitly opt in.
    os.environ.setdefault('TABPFN_DISABLE_TELEMETRY','1')
    import pandas as pd
    from tabpfn_time_series import TabPFNTSPipeline, TabPFNMode
    # tabpfn-time-series 1.4 defaults to LOCAL (torch on this machine) — CLIENT is the PDR's cloud
    # adapter. Auth is token-only for estimators: TABPFN_TOKEN from the environment.
    context=pd.DataFrame({
        'item_id':'amount_total',
        'timestamp':pd.to_datetime([d for d,_ in history]),
        'target':[float(a) for _,a in history],
    })
    pred=TabPFNTSPipeline(tabpfn_mode=TabPFNMode.CLIENT).predict_df(context,prediction_length=horizon,quantiles=list(QUANTILES))
    pred=pd.DataFrame(pred).reset_index().sort_values('timestamp')  # indexed by (item_id, timestamp)
    lo,mid,hi=(_quantile_col(pred,q) for q in QUANTILES)
    expected=target_days(history[-1][0],horizon)
    got=[pd.Timestamp(t).date().isoformat() for t in pred['timestamp']]
    if got!=expected:raise ValueError(f'TabPFN-TS returned timestamps {got}, expected {expected}')
    out=[]
    for i,day in enumerate(expected):
        row=pred.iloc[i]
        vals=sorted(max(0.0,float(row[c])) for c in (lo,mid,hi))  # revenue can't be negative; keep lower<=point<=upper
        out.append({'target_day':day,'lower':round(vals[0],2),'point':round(vals[1],2),'upper':round(vals[2],2)})
    return out


def store_outlook(db, dataset_id, issue_day, values, model_version_, source):
    """Persist an outlook vintage and lock its first in-October value as the immutable D+1
    reference. Shared by POST /forecasts/outlooks and the worker. Raises IntegrityError if this
    exact vintage already exists (caller decides whether that's a 409 or a reuse)."""
    for v in values:
        db.add(ForecastValue(id='FCV-'+uuid.uuid4().hex[:16],dataset_id=dataset_id,issue_day=issue_day,target_day=v['target_day'],point=v['point'],lower=v['lower'],upper=v['upper'],model_version=model_version_,source=source))
    first=values[0]
    locked=first['target_day'] if first['target_day'] in MONTH_DAYS else None
    if locked:
        db.add(ForecastReference(id='FCR-'+uuid.uuid4().hex[:16],dataset_id=dataset_id,reference_day=locked,issue_day=issue_day,point=first['point'],lower=first['lower'],upper=first['upper'],model_version=model_version_,source=source))
    db.flush()
    return locked


def _existing_vintage(db, dataset_id, issue_day, model_version_):
    return db.scalars(select(ForecastValue).where(ForecastValue.dataset_id==dataset_id,ForecastValue.issue_day==issue_day,ForecastValue.model_version==model_version_,ForecastValue.source==SOURCE).order_by(ForecastValue.target_day)).all()


def issue_forecast(db, run, day, emit, predictor=None):
    """Called by the worker after day D's close has been committed. Emits exactly one of
    forecast.updated / forecast.unavailable. Does not commit (caller does)."""
    cfg=config()
    rows=db.execute(select(DailyAmount.day,DailyAmount.amount).where(DailyAmount.dataset_id==run.dataset_id,DailyAmount.day<=day).order_by(DailyAmount.day)).all()
    history=[(r[0],r[1]) for r in rows]  # cutoff guard: never a day after D, even when preloaded
    base={'issue_day':day,'source':SOURCE,'history_days':len(history),'horizon_days':cfg['horizon_days']}
    context=context_window(history,cfg)
    if len(history)<cfg['warmup_days']:
        emit(db,run.id,'forecast.unavailable',{**base,'reason':'warm_up','warmup_days':cfg['warmup_days']},day);return
    if predictor is None and cfg['status']!='configured':
        emit(db,run.id,'forecast.unavailable',{**base,'reason':'not_configured','token_present':cfg['token_present'],'package_installed':bool(cfg['package_version'])},day);return
    mv=model_version(cfg)
    existing=_existing_vintage(db,run.dataset_id,day,mv)
    if existing:  # another run on this dataset already issued this vintage — references are immutable, reuse
        values=[{'target_day':v.target_day,'point':v.point,'lower':v.lower,'upper':v.upper} for v in existing]
    else:
        # Make the trigger visible before the (seconds-long) provider call: a new day entered the stream.
        emit(db,run.id,'forecast.requested',{**base,'trigger':'day_rollover','new_day':target_days(day,1)[0],'context_days':[context[0][0],context[-1][0]],'model_version':mv},day)
        db.commit()
        fn=predictor or predict_amount
        ex=ThreadPoolExecutor(max_workers=1)
        try:
            values=ex.submit(fn,context,cfg['horizon_days']).result(timeout=cfg['timeout_s'])
        except FutureTimeout:
            emit(db,run.id,'forecast.unavailable',{**base,'reason':'provider_error','error_class':'Timeout','last_valid_issue_day':_last_valid_issue(db,run,day)},day);return
        except Exception as exc:
            emit(db,run.id,'forecast.unavailable',{**base,'reason':'provider_error','error_class':type(exc).__name__,'last_valid_issue_day':_last_valid_issue(db,run,day)},day);return
        finally:
            ex.shutdown(wait=False)
        try:
            store_outlook(db,run.dataset_id,day,values,mv,SOURCE)
        except IntegrityError:
            db.rollback()
            values=[{'target_day':v.target_day,'point':v.point,'lower':v.lower,'upper':v.upper} for v in _existing_vintage(db,run.dataset_id,day,mv)]
    emit(db,run.id,'forecast.updated',{**base,'model_version':mv,'context_days':[context[0][0],context[-1][0]],'locked_day':values[0]['target_day'] if values[0]['target_day'] in MONTH_DAYS else None,'daily':[{'date':v['target_day'],'point':v['point'],'lower':v['lower'],'upper':v['upper']} for v in values]},day)


def _last_valid_issue(db, run, day):
    return db.scalar(select(ForecastValue.issue_day).where(ForecastValue.dataset_id==run.dataset_id,ForecastValue.source==SOURCE,ForecastValue.issue_day<day).order_by(ForecastValue.issue_day.desc()).limit(1))
