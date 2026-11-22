import os
import tempfile

# The backend publishes agent events (sim.day_closed, ...) into the agent bus when it exists.
# Point every test session at a throwaway bus directory so tests never touch <repo>/var/agent_bus.
_bus_tmp = tempfile.TemporaryDirectory(prefix="cheetah_bus_")
os.environ.setdefault("CHEETAH_BUS_DIR", os.path.join(_bus_tmp.name, "agent_bus"))
