"""Astrophotography calibration matching service package.

Exposes :data:`app` so ``uvicorn app:app`` works alongside
``uvicorn app.main:app`` and ``uvicorn main:app``.
"""

__version__ = "1.0.0"

from .main import app, create_app

__all__ = ["app", "create_app", "__version__"]
