"""Load the portable worker without importing Linux-only server fixtures."""
import importlib.util
from pathlib import Path


def load_client_worker():
    path = Path(__file__).resolve().parents[2]/'packaging/client/worker.py'
    spec = importlib.util.spec_from_file_location('portable_client_worker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
