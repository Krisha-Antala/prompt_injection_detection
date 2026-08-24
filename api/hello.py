from flask import Flask

app = Flask(__name__)

@app.route("/api/hello-test")
def hello():
    return {"ok": True, "msg": "api hello works"}

# Vercel expects app
application = app
