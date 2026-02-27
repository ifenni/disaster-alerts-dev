import os

from flask import Flask, send_from_directory

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
HTML_FILE = "activated_events_map.html"


@app.route("/")
def index():
    return send_from_directory(DATA_DIR, HTML_FILE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
