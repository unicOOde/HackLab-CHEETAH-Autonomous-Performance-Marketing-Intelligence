"""The UI subscribes to named SSE events one by one: an event type it doesn't list is silently dropped.

Every event type the backend emits must be in connectStream()'s `types` list.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'frontend' / 'CHEETAH_UI_Prototype_v1.5.html'


def emitted_types():
    types = set()
    for py in (ROOT / 'backend').rglob('*.py'):
        src = py.read_text()
        types |= set(re.findall(r"emit\(db,[^,]+,\s*['\"]([a-z_.]+)['\"]", src))
        types |= set(re.findall(r"STEP_EVENT\s*=\s*['\"]([a-z_.]+)['\"]", src))
    return types


def subscribed_types():
    m = re.search(r"const types=\[([^\]]*)\]", HTML.read_text())
    assert m, 'connectStream types list not found'
    return set(re.findall(r"'([a-z_.]+)'", m.group(1)))


def test_every_emitted_event_is_subscribed():
    emitted = emitted_types()
    assert {'incident.updated', 'investigation.step', 'deviation.detected'} <= emitted
    assert emitted - subscribed_types() == set()
