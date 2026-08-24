import os
import threading
import json
import sqlite3
import gc
import tempfile
from flask import Flask, jsonify, request, render_template
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Import our custom components
from detector import PromptInjectionDetector, HallucinationDetector, TRANSFORMERS_AVAILABLE
from guardrail import GuardrailWrapper
from dataset_generator import save_datasets, generate_local_datasets
from security_features import extract_document_text, scan_document, scan_private_data

# Memory optimization for free tier
gc.set_threshold(100, 10, 10)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _ensure_writable_data_dir():
    """Serverless filesystems (/var/task) are read-only: fall back to /tmp."""
    preferred = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
    try:
        os.makedirs(preferred, exist_ok=True)
        probe = os.path.join(preferred, ".wtest")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return preferred
    except Exception:
        alt = os.path.join(tempfile.gettempdir(), "pid_data")
        os.makedirs(alt, exist_ok=True)
        return alt


DATA_DIR = _ensure_writable_data_dir()

app = Flask(__name__, template_folder='templates', static_folder='static')
AUDIT_DB = os.path.join(DATA_DIR, "audit.db")
CUSTOM_MODEL_PATH = os.path.join(DATA_DIR, "models", "injection_detector")


def record_audit(event_type, payload, result):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with sqlite3.connect(AUDIT_DB) as db:
            db.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, event_type TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL)")
            db.execute("INSERT INTO audit(event_type, payload, result) VALUES (?, ?, ?)", (event_type, json.dumps(payload), json.dumps(result)))
    except Exception as e:
        # Audit logging must never break a live request (read-only FS on serverless)
        print(f"[audit] skipped: {e}")


@app.route("/api/security/scan", methods=["POST"])
def security_scan():
    data = request.get_json() or {}
    text = data.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "Text is required"}), 400
    result = scan_private_data(text)
    record_audit("private_data_scan", {"length": len(text)}, result)
    return jsonify(result)


@app.route("/api/security/scan-document", methods=["POST"])
def security_scan_document():
    data = request.get_json() or {}
    text = data.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "Document text is required"}), 400
    result = scan_document(text, data.get("filename", ""))
    record_audit("document_poisoning_scan", {"filename": result["filename"]}, result)
    return jsonify(result)


@app.route("/api/security/upload", methods=["POST"])
def security_upload():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Choose a document first."}), 400
    filename = secure_filename(uploaded.filename)
    try:
        content = uploaded.read()
        text = extract_document_text(filename, content)
        if not text.strip():
            return jsonify({"error": "No readable text was found in this document."}), 400
        privacy = scan_private_data(text)
        poisoning = scan_document(text, filename)
        unsafe = privacy["detected"] or poisoning["poisoning_detected"]
        result = {
            "filename": filename,
            "safe": not unsafe,
            "status": "UNSAFE" if unsafe else "SAFE",
            "risk": "high" if unsafe else "low",
            "private_data": privacy["findings"],
            "document_scan": poisoning,
            "text_length": len(text),
        }
        record_audit("document_upload_scan", {"filename": filename}, result)
        return jsonify(result)
    except (ValueError, ImportError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Could not read this document: {exc}"}), 400


@app.route("/api/audit/logs", methods=["GET"])
def audit_logs():
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    if not os.path.exists(AUDIT_DB):
        return jsonify({"logs": []})
    with sqlite3.connect(AUDIT_DB) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT id, created_at, event_type, payload, result FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify({"logs": [dict(row) for row in rows]})

# Initialize detectors ONCE and share them between app endpoints and the guardrail.
# This avoids loading the same transformer models twice (~saves 500MB+ RAM and startup time).
injection_detector = PromptInjectionDetector(CUSTOM_MODEL_PATH)
guardrail = GuardrailWrapper(injection_detector=injection_detector)
hallucination_detector = None  # created lazily on first hallucination request

# Lock guarding detector swaps during background training
_detector_lock = threading.Lock()

def get_hallucination_detector():
    """Lazy singleton so the NLI + embedding models only load when actually used."""
    global hallucination_detector
    if hallucination_detector is None:
        with _detector_lock:
            if hallucination_detector is None:
                hallucination_detector = HallucinationDetector()
                guardrail.set_hallucination_detector(hallucination_detector)  # share one instance
    return hallucination_detector

# Global variables to track training status
training_status = "idle"  # idle, training, completed, failed
training_error = ""

def run_training_in_background():
    global training_status, training_error, injection_detector
    training_status = "training"
    try:
        from train import train_model
        # Execute the training pipeline
        train_model()

        # Reload the prompt injection detector with the newly trained model
        new_detector = PromptInjectionDetector(CUSTOM_MODEL_PATH)
        with _detector_lock:
            injection_detector = new_detector
            guardrail.set_injection_detector(new_detector)

        training_status = "completed"
    except Exception as e:
        training_status = "failed"
        training_error = str(e)
        print(f"Background training failed: {e}")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/detect/injection", methods=["POST"])
def detect_injection():
    data = request.get_json() or {}
    text = data.get("text", "")
    
    if not text.strip():
        return jsonify({"error": "Empty input text"}), 400
        
    res = injection_detector.predict(text)
    return jsonify(res)

@app.route("/api/detect/hallucination", methods=["POST"])
def detect_hallucination():
    data = request.get_json() or {}
    context = data.get("context", "")
    response = data.get("response", "")
    
    if not context.strip() or not response.strip():
        return jsonify({"error": "Context and Response are required"}), 400
        
    res = get_hallucination_detector().predict(context, response)
    # On serverless (ML unavailable) use LLM-based check
    if res.get("method") == "unavailable":
        res = guardrail._llm_grounding_check(context, response)
    return jsonify(res)

@app.route("/api/guardrail/chat", methods=["POST"])
def guardrail_chat():
    data = request.get_json() or {}
    prompt = data.get("prompt", "")
    context = data.get("context", None)
    provider = data.get("provider", "auto")
    
    if not prompt.strip():
        return jsonify({"error": "Prompt is required"}), 400
    privacy = scan_private_data(prompt)
    # Never send detected secrets/PII to the external model.
    safe_prompt = privacy["redacted_text"]
    res = guardrail.run_safe_query(safe_prompt, context, provider=provider)
    res["privacy_scan"] = {
        "private_data_detected": privacy["detected"],
        "findings": privacy["findings"],
        "model_received_redacted_prompt": privacy["redacted_text"] != prompt,
    }
    record_audit("guardrail_chat", {"length": len(prompt)}, res)
    return jsonify(res)

@app.route("/api/dataset/stats", methods=["GET"])
def dataset_stats():
    # On serverless, DATA_DIR is /tmp (ephemeral) but bundled data lives in BASE_DIR/data
    # Fallback to bundled files so the demo shows counts even before first expansion.
    def count_json(primary, fallback):
        for p in [primary, fallback]:
            if os.path.exists(p):
                try:
                    with open(p, "r") as f:
                        return len(json.load(f))
                except Exception:
                    continue
        return 0

    inj_primary = os.path.join(DATA_DIR, "injections.json")
    hal_primary = os.path.join(DATA_DIR, "hallucinations.json")
    inj_fallback = os.path.join(BASE_DIR, "data", "injections.json")
    hal_fallback = os.path.join(BASE_DIR, "data", "hallucinations.json")
    
    stats = {
        "injections_count": count_json(inj_primary, inj_fallback),
        "hallucinations_count": count_json(hal_primary, hal_fallback),
        "custom_model_exists": os.path.exists(CUSTOM_MODEL_PATH)
    }
    return jsonify(stats)

@app.route("/api/dataset/expand", methods=["POST"])
def expand_dataset():
    data = request.get_json() or {}
    try:
        num_samples = int(data.get("num_samples", 20))
    except Exception:
        num_samples = 20
    num_samples = max(5, min(num_samples, 200))
    
    try:
        # Generate/append synthetic samples - respects the selected count
        save_datasets(DATA_DIR, num_samples=num_samples)
        
        # Load and verify counts
        stats = {
            "status": "success",
            "injections_count": 0,
            "hallucinations_count": 0
        }
        with open(os.path.join(DATA_DIR, "injections.json"), "r") as f:
            stats["injections_count"] = len(json.load(f))
        with open(os.path.join(DATA_DIR, "hallucinations.json"), "r") as f:
            stats["hallucinations_count"] = len(json.load(f))
            
        return jsonify(stats)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/model/train", methods=["POST"])
def trigger_training():
    global training_status, training_error
    
    if not TRANSFORMERS_AVAILABLE:
        return jsonify({
            "status": "unsupported",
            "message": "Model training requires PyTorch, which is not available on this serverless deployment. Run training locally or on a full deployment."
        }), 501
    
    if training_status == "training":
        return jsonify({"status": "already_running", "message": "Training is already in progress."})
        
    training_status = "training"
    training_error = ""
    
    # Spawn background thread for training
    thread = threading.Thread(target=run_training_in_background)
    thread.start()
    
    return jsonify({"status": "started", "message": "Model training pipeline kicked off in background."})

@app.route("/api/model/train/status", methods=["GET"])
def training_status_endpoint():
    global training_status, training_error
    return jsonify({
        "status": training_status,
        "error": training_error
    })

if __name__ == "__main__":
    # Ensure standard directory setup
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "models"), exist_ok=True)
    
    # Load or generate baseline datasets if missing
    if not os.path.exists(os.path.join(DATA_DIR, "injections.json")):
        print("Generating baseline seed datasets...")
        save_datasets(DATA_DIR)
        
    # Render provides PORT env var; default 5000 for local
    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("FLASK_DEBUG", "false").strip().lower() == "true"
    print(f"Launching Flask Server on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
