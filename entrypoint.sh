#!/bin/sh
# Single worker: this app holds shared in-process state (the poll loop's
# `state` dict in loop.py) that isn't safe to duplicate across workers.
exec gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 4 --timeout 120 app:app
