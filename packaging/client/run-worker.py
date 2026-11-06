"""Managed task/SCM entry point. Output is bounded; identity and runtime stay local."""
from pathlib import Path
from app.worker_supervisor import launch

if __name__ == '__main__':
    raise SystemExit(launch(Path(__file__).resolve().parent))
