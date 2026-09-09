"""Re-export the guarded dotenv loader for importers written against this path."""

from aizk.service.bootstrap import load_process_dotenv_once

__all__ = ["load_process_dotenv_once"]
