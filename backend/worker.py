import time
from sqlalchemy import select
from models import initialize, SessionLocal, Command, Run, utcnow
from core import execute_command,run_step

def main():
    initialize()
    print('CHEETAH v1.5 worker started — one worker only in SQLite development mode',flush=True)
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
                now=time.monotonic()
                if now-last_tick.get(run.id,0)>=run.speed_seconds:
                    run_step(db,run);last_tick[run.id]=now
        time.sleep(.25)

if __name__=='__main__':main()
