import logging
import os
import subprocess
import sys
import threading

from flask import Flask, jsonify, render_template_string, request, send_from_directory

print("Flask is running with Python:", sys.executable)
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s"
)

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
HTML_FILE = "activated_events_map.html"
NEXT_PASS_SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "next_pass", "next_pass.py")
)
BASE_OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Shared state
processing_state = {"running": False, "latest_folder": None}


# ---- Background function to run next_pass ----
def run_next_pass(bbox):
    processing_state["running"] = True
    processing_state["latest_folder"] = None
    try:
        cmd = [
            "python",
            NEXT_PASS_SCRIPT,
            "-b",
            str(bbox["lat_min"]),
            str(bbox["lat_max"]),
            str(bbox["lon_min"]),
            str(bbox["lon_max"]),
        ]
        print("Running command:", " ".join(cmd))
        subprocess.run(cmd, check=True)

        # Find latest output folder
        import glob

        folders = sorted(
            glob.glob(os.path.join(BASE_OUTPUT_DIR, "nextpass_outputs_*")),
            key=os.path.getmtime,
            reverse=True,
        )
        if folders:
            processing_state["latest_folder"] = folders[0]
            print(
                "Next-pass output folder detected:", processing_state["latest_folder"]
            )
        else:
            print("No output folder found after next-pass run.")
    except Exception as e:
        print("Error running next_pass:", e)
    finally:
        processing_state["running"] = False


# ---- Serve original map ----
@app.route("/")
def index():
    return send_from_directory(DATA_DIR, HTML_FILE)


# ---- Ping endpoint ----
@app.route("/test_ping", methods=["GET"])
def test_ping():
    print("Ping received!")
    return "pong", 200


# ---- Process bbox ----
@app.route("/process_bbox", methods=["POST"])
def process_bbox():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    lat_min = data.get("lat_min")
    lat_max = data.get("lat_max")
    lon_min = data.get("lon_min")
    lon_max = data.get("lon_max")

    if None in (lat_min, lat_max, lon_min, lon_max):
        return jsonify({"error": "Missing bounding box data"}), 400

    print(
        f"Received bbox: lat_min={lat_min}, lat_max={lat_max}, lon_min={lon_min},"
        f" lon_max={lon_max}"
    )

    threading.Thread(target=run_next_pass, args=(data,)).start()
    return jsonify({"status": "processing started"})


# ---- Status endpoint ----
@app.route("/processing_status")
def processing_status():
    return jsonify({"running": processing_state["running"]})


# ---- Serve maps from latest next-pass folder ----
@app.route("/maps/<filename>")
def maps(filename):
    folder = processing_state.get("latest_folder")
    if folder and os.path.exists(os.path.join(folder, filename)):
        return send_from_directory(folder, filename)
    return f"File {filename} not found", 404


@app.route("/show_maps")
def show_maps():
    """
    Display run_output.txt first, then the two maps side by side below it.
    """
    folder = processing_state.get("latest_folder")
    if not folder:
        return (
            "<h3>No next-pass output yet. Draw a bounding box to start processing.</h3>"
        )

    satellite_map = "satellite_overpasses_map.html"
    opera_map = "opera_products_map.html"
    log_file = os.path.join(folder, "run_output.txt")
    log_content = ""
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            log_content = f.read()

    html = f"""
    <html>
      <head>
        <title>Next-Pass Results</title>
        <style>
          body {{ display:flex; flex-direction: column; margin:0; height:100vh; }}
          pre {{ flex: 0 0 30%; overflow:auto; padding:10px; background:#f4f4f4; }}
          .maps-row {{ display:flex; flex: 1 1 auto; }}
          iframe {{ width: 50%; height:100%; border:none; }}
        </style>
      </head>
      <body>
        <pre>{log_content}</pre>
        <div class="maps-row">
          <iframe src="/maps/{satellite_map}"></iframe>
          <iframe src="/maps/{opera_map}"></iframe>
        </div>
      </body>
    </html>
    """
    return render_template_string(html)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
