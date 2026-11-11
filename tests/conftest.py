import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_package():
    # The repo root is the plugin package; load it as "crg" so relative imports work
    # without importing __init__'s Hermes-only code paths at module import time.
    spec = importlib.util.spec_from_file_location("crg", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["crg"] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package()
