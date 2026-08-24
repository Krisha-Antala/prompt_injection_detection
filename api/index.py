from flask import Flask, jsonify
app = Flask(__name__)
@app.route('/')
def hello(): return jsonify({'ok': True, 'msg': 'minimal works'})
@app.route('/api/test2')
def t2(): return jsonify({'ok': True})
application = app
