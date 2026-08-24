# Serverless entrypoint for Vercel.
# Vercel rewrites route using the rewritten destination path (/api),
# so strip the /api prefix before Flask sees it.
from app import app  # noqa: E402,F401

_original_wsgi = app.wsgi_app


def _vercel_wsgi(environ, start_response):
    path = environ.get("PATH_INFO", "")
    if path.startswith("/api"):
        environ["PATH_INFO"] = path[3:] or "/"
        environ["SCRIPT_NAME"] = "/api"
    return _original_wsgi(environ, start_response)


app.wsgi_app = _vercel_wsgi
application = app
