import env  # noqa: F401 — loads the repo-root .env before models reads DATABASE_URL
import time
from sqlalchemy import select
from models import initialize, SessionLocal, Command, Run, utcnow
from core import execute_command, run_step, advance_run_live

def main():
    initialize()
    print('CHEETAH v1.5 worker started — one worker only in SQLite development mode',flush=True)
    with SessionLocal() as db:
        # Worker restart: don't let the downtime between process death and restart become a
        # simulated-time catch-up burst for any live run that was still 'running'.
        for run in db.scalars(select(Run).where(Run.status=='running',Run.ingestion_mode=='live_events')).all():
            run.last_tick_wall_utc=None
        db.commit()
    last_tick={}
    while True:
        with SessionLocal() as db:
            cmds=db.scalars(select(Command).where(Command.status=='queued').order_by(Command.created_at).limit(10)).all()
            for cmd in cmds:
                try:execute_command(db,cmd)
                except Exception as exc:
                    db.rollback();bad=db.get(Command,cmd.command_id);bad.status='failed';bad.error=str(exc);bad.completed_at=utcnow();db.commit()
            runs=db.scalars(select(Run).where(Run.status=='running')).all()
            for run in runs:
                if run.ingestion_mode=='live_events':
                    advance_run_live(db,run,utcnow())
                else:
                    now=time.monotonic()
                    if now-last_tick.get(run.id,0)>=run.speed_seconds:
                        run_step(db,run);last_tick[run.id]=now
        time.sleep(.1)

if __name__=='__main__':main()
