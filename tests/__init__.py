"""Load the repository as a plugin and keep test state outside live AstrBot data."""

import atexit
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType

_runtime = tempfile.TemporaryDirectory(
    prefix="codex-qq-tests-", ignore_cleanup_errors=True
)
atexit.register(_runtime.cleanup)
os.environ["ASTRBOT_ROOT"] = _runtime.name

# AstrBot normally supplies the package directory when installing this repository.
_plugin = ModuleType("astrbot_plugin_local_codex")
_plugin.__path__ = [str(Path(__file__).resolve().parent.parent)]
sys.modules["astrbot_plugin_local_codex"] = _plugin
