# Serverless entrypoint for Vercel.
# Imports the full Flask app; heavy ML deps are optional at runtime
# (detector.py degrades gracefully to heuristic-only mode without torch).
from app import app  # noqa: E402,F401

# Vercel Python runtime expects an ASGI/WSGI callable named "app"
application = app
