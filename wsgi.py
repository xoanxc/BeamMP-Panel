"""Punto de entrada para gunicorn: gunicorn --workers 1 wsgi:app

Un solo proceso: el planificador de reinicios y el estado en memoria son únicos.
"""

from app import create_app

app = create_app(background=True)
