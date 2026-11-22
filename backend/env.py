"""Load the repo-root .env into os.environ at process start (app.py and worker.py import this
first, before models.py reads DATABASE_URL). Variables already set in the environment always win.
Set CHEETAH_SKIP_DOTENV=1 to skip loading (the test suites do, so they never pick up real keys)."""
import os
from pathlib import Path

DEFAULT_PATH=Path(__file__).resolve().parents[1]/'.env'


def load_env(path=DEFAULT_PATH):
    """Returns the names of the variables it set (never their values)."""
    if os.getenv('CHEETAH_SKIP_DOTENV')=='1':return []
    try:
        lines=Path(path).read_text().splitlines()
    except OSError:  # missing, or unreadable (e.g. a sandbox) — the backend still starts
        return []
    loaded=[]
    for raw in lines:
        line=raw.strip()
        if not line or line.startswith('#') or '=' not in line:continue
        if line.startswith('export '):line=line[7:].lstrip()
        key,value=line.split('=',1)
        key,value=key.strip(),value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in '"\'':value=value[1:-1]
        if not key or key in os.environ or value=='':continue
        os.environ[key]=value
        loaded.append(key)
    return loaded


load_env()
