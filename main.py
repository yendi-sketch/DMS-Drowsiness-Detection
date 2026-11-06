import os
# Matikan log tensorflow yang berisik
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
os.environ["CUDA_VISIBLE_DEVICES"] = "0"

from flask import Flask, request, jsonify, render_template, Response, send_from_directory, make_response, g
from flask_cors import CORS
import base64
import cv2
import numpy as np
import mysql.connector
import pickle
import time
import io
import csv
from datetime import datetime
from werkzeug.utils import secure_filename
from openpyxl.worksheet.hyperlink import Hyperlink

# Excel export
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

import gc
gc.disable()

# --- AI LIBRARIES ---
from deepface import DeepFace
import mediapipe as mp

app = Flask(__name__)
CORS(app)

# ================= KONFIGURASI =================
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "drowsiness_db")}

DRIVER_CACHE = {}
def get_db_connection():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        return conn
    except mysql.connector.Error as err:
        print(f"❌ DB Error: {err}")
        return None

def load_driver_cache():
    global DRIVER_CACHE
    print("📦 Loading driver embeddings into RAM...")
    conn = get_db_connection()
    if not conn:
        print("❌ DB connection failed")
        return

    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, driver_name, face_embedding FROM drivers 
        WHERE face_embedding IS NOT NULL""")

    DRIVER_CACHE = {}
    for row in cursor.fetchall():
        try:
            DRIVER_CACHE[row["id"]] = {"name": row["driver_name"],"embed": pickle.loads(row["face_embedding"])}
        except:
            continue

    cursor.close()
    conn.close()
    print(f"✅ Loaded {len(DRIVER_CACHE)} drivers into cache")

with app.app_context():
    load_driver_cache()

# --- THRESHOLD (BATAS AMBANG) ---
EAR_THRESHOLD = 0.21        # Batas mata tertutup (Ngantuk)
MAR_THRESHOLD = 0.5         # Batas mulut menguap
MATCH_THRESHOLD = 0.40      # Batas kemiripan wajah (0.40 = moderat, 0.50 = ketat)

# ================= MODE PENGUJIAN =================
# EVENT_BASED = hanya simpan event DROWSY/YAWNING
# CONTINUOUS = simpan setiap frame yang diproses
TEST_MODE = "EVENT_BASED"

test_stats = {"frames_processed": 0}

# ================= STATISTIK REAL-TIME =================
realtime_stats = {
    "total_requests": 0,
    "successful_requests": 0}

@app.before_request
def start_performance_timer():
    if request.endpoint == "detect" and request.method == "POST":
        g.detect_start_time = time.perf_counter()

@app.after_request
def finish_performance_timer(response):
    if request.endpoint == "detect" and request.method == "POST":
        start = getattr(g, "detect_start_time", None)

        if start is not None:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)

            data = request.get_json(silent=True) or {}
            request_id = data.get("request_id")

            record = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "metric_type": "BACKEND","request_id": request_id,
                "duration_ms": duration_ms,"frames": None,"fps": None}
            
            performance_stats.append(record)

            print(f"[PERF] type=BACKEND "
                f"request_id={request_id} "
                f"duration_ms={duration_ms}")

        realtime_stats["total_requests"] += 1

        if response.status_code == 200:
            realtime_stats["successful_requests"] += 1

    return response

# Global Streaming Buffer
stream_buffers = {}
annotated_stream_buffers = {}
stream_metadata = {}
last_stream_frame_time = {}
# Pembatas penyimpanan kejadian kantuk
EVENT_COOLDOWN_SECONDS = 10
last_saved_event = {}

# ================= TESTING STATISTICS =================
test_stats = {"frames_processed": 0}
performance_stats = []

# Inisialisasi MediaPipe Face Mesh
mp_face_mesh = mp.solutions.face_mesh
face_mesh = mp_face_mesh.FaceMesh(
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5,
    refine_landmarks=True)

# ================= HELPER FUNCTIONS =================
# --- MEDIAPIPE LOGIC (DROWSINESS) ---
def calculate_distance(p1, p2):
    return np.sqrt((p1.x - p2.x)**2 + (p1.y - p2.y)**2)

def get_ear(landmarks, indices):
    p1 = landmarks[indices[0]]; p4 = landmarks[indices[3]]
    p2 = landmarks[indices[1]]; p6 = landmarks[indices[5]]
    p3 = landmarks[indices[2]]; p5 = landmarks[indices[4]]
    dist_horizontal = calculate_distance(p1, p4)
    dist_vertical_1 = calculate_distance(p2, p6)
    dist_vertical_2 = calculate_distance(p3, p5)
    if dist_horizontal == 0: return 0
    return (dist_vertical_1 + dist_vertical_2) / (2.0 * dist_horizontal)

def get_mar(landmarks, indices):
    p1 = landmarks[indices[0]]; p4 = landmarks[indices[3]]
    p2 = landmarks[indices[1]]; p6 = landmarks[indices[5]]
    p3 = landmarks[indices[2]]; p5 = landmarks[indices[4]]
    dist_horizontal = calculate_distance(p1, p4)
    dist_vertical_1 = calculate_distance(p2, p6)
    dist_vertical_2 = calculate_distance(p3, p5)
    if dist_horizontal == 0: return 0
    return (dist_vertical_1 + dist_vertical_2) / (2.0 * dist_horizontal)

def analyze_drowsiness(image):
    h, w, _ = image.shape
    rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    with mp_face_mesh.FaceMesh(
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
        refine_landmarks=True) as face_mesh:
        results = face_mesh.process(rgb_image)

    ear = 0
    mar = 0
    is_drowsy = False
    face_detected = False
    face_bbox = None

    if results.multi_face_landmarks:
        face_detected = True

        for face_landmarks in results.multi_face_landmarks:
            lm = face_landmarks.landmark

            # Bounding box wajah dari landmark MediaPipe
            xs = [int(point.x * w) for point in lm]
            ys = [int(point.y * h) for point in lm]
            x1 = max(0, min(xs) - 10)
            y1 = max(0, min(ys) - 10)
            x2 = min(w - 1, max(xs) + 10)
            y2 = min(h - 1, max(ys) + 10)
            face_bbox = (x1, y1, x2, y2)

            LEFT_EYE = [33, 160, 158, 133, 153, 144]
            RIGHT_EYE = [362, 385, 387, 263, 373, 380]
            LIPS = [61, 13, 14, 291, 78, 308]

            left_ear = get_ear(lm, LEFT_EYE)
            right_ear = get_ear(lm, RIGHT_EYE)
            ear = (left_ear + right_ear) / 2.0
            mar = get_mar(lm, LIPS)

    if not face_detected:
        status = "NO FACE"
        is_drowsy = False
    elif ear < EAR_THRESHOLD:
        status = "DROWSY"
        is_drowsy = True
    elif mar > MAR_THRESHOLD:
        status = "YAWNING"
        is_drowsy = False
    else:
        status = "NORMAL"
        is_drowsy = False

    return is_drowsy, ear, mar, face_detected, face_bbox, status

# --- DEEPFACE LOGIC (RECOGNITION) ---
def extract_face_embedding(image):
    try:
        result = DeepFace.represent(
            img_path=image,
            model_name="Facenet",
            enforce_detection=False,
            detector_backend="opencv")
        return result[0]["embedding"] if result else None
    except Exception as e:
        print(f"⚠️ Embed Error: {e}")
        return None
def cosine_similarity(a, b):
    a = np.array(a); b = np.array(b)
    if np.linalg.norm(a) == 0 or np.linalg.norm(b) == 0: return 0
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

# ================= ROUTES =================
@app.route("/")
def index(): return render_template("index.html")
@app.route("/admin")
def drivers(): return render_template("admin.html")
@app.route("/reports")
def reports(): return render_template("reports.html")
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# --- API DRIVERS ---
@app.route("/api/drivers", methods=["GET", "POST"])
def drivers_api():
    conn = get_db_connection()
    if not conn: return jsonify({"error": "DB Error"}), 500
    cursor = conn.cursor(dictionary=True)

    if request.method == "GET":
        cursor.execute("SELECT * FROM drivers ORDER BY created_at DESC")
        drivers = cursor.fetchall()
        for d in drivers: 
            d["created_at"] = str(d["created_at"])
            if 'face_embedding' in d: del d['face_embedding'] # Hapus BLOB agar JSON valid
        cursor.close(); conn.close()
        return jsonify({"drivers": drivers}), 200
    
    elif request.method == "POST":
        driver_name = request.form.get("driver_name"); employee_id = request.form.get("employee_id")
        face_blob = None; photo_path = None

        if 'photo' in request.files:
            photo = request.files['photo']
            if photo.filename != '':
                filename = secure_filename(f"{employee_id}_{int(time.time())}.jpg")
                photo_path = os.path.join(UPLOAD_FOLDER, filename)
                photo.save(photo_path)
                
                img = cv2.imread(photo_path)
                embed = extract_face_embedding(img)
                if embed: face_blob = pickle.dumps(embed)
                else: return jsonify({"error": "Wajah tidak terdeteksi! Gunakan foto yang lebih jelas."}), 400

        try:
            cursor.execute("""
                INSERT INTO drivers (driver_name, employee_id, phone, email, photo_path, face_embedding)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (driver_name, employee_id, request.form.get("phone"), request.form.get("email"), photo_path, face_blob))
            conn.commit()
            
            # Reload cache agar driver yang baru didaftarkan langsung dikenali
            load_driver_cache()
            
            return jsonify({"message": "OK"}), 201
        except Exception as e: return jsonify({"error": str(e)}), 500
        finally: cursor.close(); conn.close()

# --- API STREAMING ---
@app.route('/api/stream/push/<esp32_id>', methods=['POST'])
def push_stream_frame(esp32_id):
    if 'image' not in request.files: return jsonify({"error": "No image"}), 400
    file = request.files['image']
    frame_data = file.read()

    now_perf = time.perf_counter()
    previous = last_stream_frame_time.get(esp32_id)

    if previous is not None:
        interval_ms = (now_perf - previous) * 1000
        print(f"[STREAM] {esp32_id} interval: {interval_ms:.0f} ms")

    last_stream_frame_time[esp32_id] = now_perf
    stream_buffers[esp32_id] = frame_data

    # Pertahankan hasil deteksi terakhir agar tidak hilang setiap frame kamera masuk.
    meta = stream_metadata.setdefault(esp32_id, {})
    meta.update({
        'last_seen': datetime.now(),
        'is_active': True,
        'drowsy_status': meta.get('drowsy_status', False)})
    return jsonify({"status": "received"}), 200

@app.route('/api/stream/<esp32_id>')
def stream_video(esp32_id):
    def generate():
        while True:
            # Tampilkan frame hasil analisis jika tersedia; jika belum, tampilkan frame asli.
            frame_data = stream_buffers.get(esp32_id)
            if frame_data:
                yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + frame_data + b'\r\n')
            time.sleep(0.1)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/stream/list', methods=['GET'])
def stream_list():
    active = []
    now = datetime.now()
    for esp, meta in stream_metadata.items():
        if (now - meta['last_seen']).total_seconds() < 10:
            active.append({
                "esp32_id": esp,
                "is_active": True,
                "is_drowsy": meta.get('drowsy_status', False),
                "driver_name": meta.get('driver_name', 'Unknown'),
                "match": meta.get('match', 0.0),
                "status": meta.get('status', 'NORMAL')})
    return jsonify({"streams": active}), 200

@app.route('/api/stream/status/<esp32_id>', methods=['GET'])
def stream_status(esp32_id):
    if esp32_id not in stream_metadata:
        return jsonify({"error": "Device not found"}), 404
    meta = stream_metadata[esp32_id]
    has_buffer = esp32_id in stream_buffers
    
    return jsonify({
        "queue_size": 1 if has_buffer else 0,
        "last_update": meta['last_seen'].isoformat() if 'last_seen' in meta else None,
        "is_drowsy": meta.get('drowsy_status', False),
        "driver_name": meta.get('driver_name', 'Unknown'),
        "match": round(meta.get('match', 0.0), 2),
        "status": meta.get('status', 'NORMAL'),
        "ear": round(meta.get('ear', 0.0), 2),
        "mar": round(meta.get('mar', 0.0), 2)
    }), 200

@app.route('/api/stream/capture/<esp32_id>', methods=['GET'])
def stream_capture(esp32_id):
    if esp32_id not in stream_buffers and esp32_id not in annotated_stream_buffers:
        return jsonify({"error": "No frame buffered yet"}), 404
    capture = annotated_stream_buffers.get(esp32_id, stream_buffers.get(esp32_id))
    return Response(capture,mimetype='image/jpeg',headers={"Content-Disposition": f"attachment; filename=capture_{esp32_id}.jpg"})

@app.route('/api/stream/notify_drowsy/<esp32_id>', methods=['POST'])
def notify_drowsy(esp32_id):
    data = request.get_json(force=True, silent=True) or {}
    is_drowsy = data.get("is_drowsy", False)
    
    if esp32_id in stream_metadata:
        stream_metadata[esp32_id]['drowsy_status'] = is_drowsy
        stream_metadata[esp32_id]['last_seen'] = datetime.now()
        
    print(f"📢 Notification from {esp32_id}: drowsy={is_drowsy}")
    return jsonify({"status": "ok", "message": "Notification received"}), 200

# --- CORE LOGIC: DETECT & IDENTIFY ---
@app.route("/api/detect", methods=["POST"])
def detect():
    request_start = time.perf_counter()

    print("\n========== /api/detect DEBUG ==========")
    print("Content-Type:", request.content_type)
    print("Content-Length:", request.content_length)

    data = request.get_json(force=True, silent=True)

    print("JSON received:", data is not None)

    if data:
        print("Keys:", list(data.keys()))
        print("ESP32 ID:", data.get("esp32_id"))
        print("Image exists:", bool(data.get("image")))
        if data.get("image"):
            print("Image Base64 length:", len(data.get("image")))
    
    global test_stats
    data = request.get_json(force=True, silent=True)

    if not data:
            return jsonify({"error": "Invalid payload"}), 400

    esp32_id = data.get("esp32_id")
    img_b64 = data.get("image")
    
    if not esp32_id or not img_b64:
        return jsonify({"error": "Invalid payload"}), 400

    # ================= IMAGE DECODE =================
    try:
        img_bytes = base64.b64decode(img_b64)
        np_arr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        print("\n===== DEBUG /api/detect =====")
        print("ESP32 ID:", esp32_id)
        print("Image bytes:", len(img_bytes))
        print("Frame:", frame.shape if frame is not None else "NONE")
    except:
        return jsonify({"error": "Image decode failed"}), 500

    # ================= DROWSINESS & FACE DETECTION =================
    t_mediapipe = time.time()
    is_drowsy, ear, mar, face_detected, face_bbox, status = analyze_drowsiness(frame)

    print(f"⏱️ MediaPipe time: {time.time() - t_mediapipe:.3f} seconds")
    print("Face detected:", face_detected)
    print("EAR:", ear)
    print("MAR:", mar)
    print("STATUS:", status)

    driver_id = None
    driver_name = "Unknown"
    best_score = 0

    if not face_detected:
        print("⚠️ No face detected in frame")
        if esp32_id in stream_metadata:
            stream_metadata[esp32_id].update({
                'drowsy_status': False, 'last_seen': datetime.now(),
                'driver_name': 'No Face Detected', 'match': 0.0,
                'status': 'NO FACE', 'ear': 0.0, 'mar': 0.0
            })
        cv2.putText(frame, 'No Face Detected', (20, 35), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (255, 255, 255), 2, cv2.LINE_AA)
        ok, encoded = cv2.imencode('.jpg', frame)
        if ok:
            annotated_stream_buffers[esp32_id] = encoded.tobytes()
        return jsonify({
            "driver": "No Face Detected",
            "match": 0.0,
            "status": "NO FACE",
            "is_drowsy": False,
            "ear": 0.0,
            "mar": 0.0
        }), 200

    # ================= FACE RECOGNITION =================
    t_facenet = time.time()
    curr_embed = extract_face_embedding(frame)
    print(f"⏱️ FaceNet time: {time.time() - t_facenet:.3f} seconds")
    if curr_embed is not None:
        if DRIVER_CACHE:
            for did, d_data in DRIVER_CACHE.items():
                score = cosine_similarity(curr_embed, d_data["embed"])
                if score > best_score and score > MATCH_THRESHOLD:
                    best_score = score
                    driver_id = did
                    driver_name = d_data["name"]
        print("DRIVER CACHE:", len(DRIVER_CACHE))
        print("MATCH:", best_score)
        print("DRIVER:", driver_name)

    if driver_id:
        print(f"✅ MATCH: {driver_name} ({best_score:.4f})")
    else:
        print(f"⚠️ NO MATCH (best={best_score:.4f}). Driver unrecognized.")
        if esp32_id in stream_metadata:
            stream_metadata[esp32_id].update({
                'drowsy_status': False, 'last_seen': datetime.now(),
                'driver_name': 'Unrecognized', 'match': best_score,
                'status': 'UNRECOGNIZED', 'ear': ear, 'mar': mar
            })
        if face_bbox:
            x1, y1, x2, y2 = face_bbox
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 165, 255), 2)
            cv2.putText(frame, 'Unrecognized', (x1, max(25, y1 - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(frame, f'Match: {best_score:.2f}', (x1, min(frame.shape[0] - 10, y2 + 25)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
        ok, encoded = cv2.imencode('.jpg', frame)
        if ok:
            annotated_stream_buffers[esp32_id] = encoded.tobytes()
        return jsonify({
            "driver": "Unrecognized",
            "match": round(best_score, 4),
            "status": "UNRECOGNIZED",
            "is_drowsy": False,
            "ear": round(ear, 2),
            "mar": round(mar, 2)
        }), 200

    # ================= DETECT DROWSINESS (REGISTERED DRIVER ONLY) =================
    t_db = time.time()

    event_key = (driver_id, status)
    last_saved = last_saved_event.get(event_key, 0)
    if TEST_MODE == "CONTINUOUS" :
        should_save = True
    else:
        should_save = (status in ["DROWSY", "YAWNING"]and (t_db - last_saved >= EVENT_COOLDOWN_SECONDS))

    if should_save:
        conn = get_db_connection()
        if conn:
            cursor = conn.cursor(dictionary=True)
            try:
                fname = f"{esp32_id}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
                fpath = os.path.join(UPLOAD_FOLDER, fname)
                cv2.imwrite(fpath, frame)

                # ================= DB SAVE =================
                cursor.execute("INSERT IGNORE INTO devices (esp32_id) VALUES (%s)", (esp32_id,))
                
                cursor.execute("""
                    INSERT INTO detections
                    (driver_id, esp32_id, eye_aspect_ratio, mouth_aspect_ratio,
                     is_drowsy, image_path, matching_score, detection_status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (driver_id, esp32_id, ear, mar, status == "DROWSY", fpath, best_score, status))
                
                alert_type = "YAWNING" if status == "YAWNING" else "DROWSY"
                cursor.execute("""
                    INSERT INTO alerts (driver_id, esp32_id, alert_type, confidence, vehicle_number)
                    VALUES (%s,%s,%s,%s,%s)
                """, (driver_id, esp32_id, alert_type, 0.95, "UNKNOWN"))
                conn.commit()
                last_saved_event[event_key] = t_db
            except Exception as e:
                conn.rollback()
                print(f"❌ DB Error: {e}")
            finally:
                cursor.close()
                conn.close()
            print(f"⏱️ Database save time: {time.time() - t_db:.3f} seconds")

    status_label = status

    # --- BARIS BARU: Update metadata dashboard dengan latency ---
    meta = stream_metadata.setdefault(esp32_id, {})
    meta.update({
        'drowsy_status': is_drowsy,
        'last_seen': datetime.now(),
        'driver_name': driver_name,
        'match': best_score,
        'status': status_label,
        'ear': ear,
        'mar': mar
    })

    if face_bbox:
        x1, y1, x2, y2 = face_bbox
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(frame, driver_name, (x1, max(30, y1 - 35)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, f'Match: {best_score:.2f}', (x1, max(55, y1 - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, f'Status: {status_label}', (x1, min(frame.shape[0] - 10, y2 + 25)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)

    ok, encoded = cv2.imencode('.jpg', frame)
    if ok:
        annotated_stream_buffers[esp32_id] = encoded.tobytes()

    print(f"🧪 MODE={TEST_MODE} | "
        f"Driver={driver_name} | "
        f"Status={status_label} | "
        f"EAR={ear:.4f} | "
        f"MAR={mar:.4f} | "
        f"Match={best_score:.4f}")

    # Hitung jumlah frame yang selesai diproses.
    # Tidak ada lagi perhitungan processing time, response time, latency, atau FPS.
    test_stats["frames_processed"] += 1

    print(f"⏱️ Total Flask /api/detect time: {time.time() - request_start:.3f} seconds")

    return jsonify({
        "driver": driver_name,
        "match": round(best_score, 4),
        "status": status_label,
        "is_drowsy": is_drowsy,
        "ear": round(ear, 4),
        "mar": round(mar, 4)
    }), 200

# --- REPORTS API ---
def build_reports_query():
    """
    Report produksi tetap event-based:
    hanya record yang memang tersimpan di tabel detections
    (YAWNING/DROWSY), bukan status NORMAL.
    """
    query = """
        SELECT
            det.id,
            det.driver_id,
            d.driver_name,
            d.employee_id,
            det.esp32_id,
            det.detection_status AS status,
            det.eye_aspect_ratio,
            det.mouth_aspect_ratio,
            det.matching_score,
            det.image_path,
            det.created_at
        FROM detections det
        LEFT JOIN drivers d ON det.driver_id = d.id
        WHERE 1=1
    """
    params = []

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    driver_id = request.args.get("driver_id")

    if start_date:
        query += " AND DATE(det.created_at) >= %s"
        params.append(start_date)

    if end_date:
        query += " AND DATE(det.created_at) <= %s"
        params.append(end_date)

    if driver_id:
        query += " AND det.driver_id = %s"
        params.append(driver_id)

    query += " ORDER BY det.created_at DESC"
    return query, params


@app.route("/api/reports", methods=["GET"])
def get_reports():
    conn = get_db_connection()
    if not conn:
        return jsonify({"reports": []}), 500

    cursor = conn.cursor(dictionary=True)

    try:
        query, params = build_reports_query()
        cursor.execute(query, params)
        reports = cursor.fetchall()

        for r in reports:
            r["created_at"] = str(r["created_at"]) if r["created_at"] else None

            # Path yang dikirim ke browser tetap dapat diakses melalui /uploads.
            if r.get("image_path"):
                r["image_url"] = "/uploads/" + os.path.basename(r["image_path"])
            else:
                r["image_url"] = None

        return jsonify({"reports": reports}), 200

    except Exception as e:
        print(f"❌ Report Error: {e}")
        return jsonify({"reports": [], "error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

@app.route("/api/reports/export", methods=["GET"])
def export_reports():
    """
    Export report event-based ke Excel.
    NORMAL tidak disimpan dan karena itu tidak muncul di report produksi.
    Bukti gambar ditanam langsung ke worksheet Excel.
    """
    conn = get_db_connection()
    if not conn:
        return "DB Error", 500

    cursor = conn.cursor(dictionary=True)

    try:
        query, params = build_reports_query()
        cursor.execute(query, params)
        reports = cursor.fetchall()

    except Exception as e:
        cursor.close()
        conn.close()
        print(f"❌ Export query error: {e}")
        return f"Export query error: {e}", 500

    finally:
        cursor.close()
        conn.close()
        
    wb = Workbook()
    
    # ==========================================
    # SHEET 1 - DETECTION REPORT
    # ==========================================
    ws = wb.active
    ws.title = "Detection Report"

    headers = [
        "No", "Nama", "Employee ID", "ESP32 ID", 
        "Hasil Deteksi", "Timestamp", "EAR", "MAR",
        "Matching", "Bukti Gambar"
    ]
    ws.append(headers)
    
    # Format header.
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="4F46E5")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 24
    
    for no, r in enumerate(reports, start=1):
        row_number = no + 1
        timestamp = r.get("created_at")
        timestamp = str(timestamp) if timestamp else "-"
        
        matching = r.get("matching_score")
        matching_value = round(float(matching), 2) if matching is not None else None
        
        # Tambahkan 10 data berurutan sesuai header
        ws.append([
            no,
            r.get("driver_name") or "Unknown",
            r.get("employee_id") or "-",
            r.get("esp32_id") or "-",
            r.get("status") or "-",
            timestamp,
            round(float(r["eye_aspect_ratio"]), 4) if r.get("eye_aspect_ratio") is not None else None,
            round(float(r["mouth_aspect_ratio"]), 4) if r.get("mouth_aspect_ratio") is not None else None,
            matching_value if matching_value is not None else None,
            ""            # Bukti Gambar
        ])

        # Masukkan bukti gambar jika file masih tersedia (Geser ke kolom 12)
        image_path = r.get("image_path")
        if image_path:
            # Ambil nama file saja
            image_filename = str(image_path).replace("\\", "/").split("/")[-1]
            # Buat link ke gambar
            image_url = request.host_url.rstrip("/") + "/uploads/" + image_filename

            cell = ws.cell(row=row_number, column=10)
            cell.value = "📷"
            cell.hyperlink = image_url
            cell.style = "Hyperlink"
            cell.alignment = Alignment(horizontal="center", vertical="center")
        else:
            ws.cell(row=row_number, column=10).value = "-"

        # Alignment untuk 10 kolom
        for col in range(1, 11):
             ws.cell(row=row_number, column=col).alignment = Alignment(vertical="center")

    widths = {
        "A": 8, "B": 24, "C": 18, "D": 18, "E": 18, "F": 22,
        "G": 12, "H": 12, "I": 14, "J": 14
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width

    # SHEET 2 - PERFORMANCE TEST
    ws2 = wb.create_sheet("Performance Test")
    headers = ["Timestamp","FPS","Latensi End-to-End (ms)",
        "Waktu Pemrosesan Backend (ms)","Tingkat Keberhasilan (%)"]
    ws2.append(headers)
    for cell in ws2[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="0F766E")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws2.freeze_panes = "A2"
    ws2.row_dimensions[1].height = 35

    # Gabungkan E2E dan BACKEND berdasarkan request_id.
    # FPS tetap dicatat sebagai metrik per jendela waktu.
    request_rows = {}
    fps_rows = []

    for item in performance_stats:
        timestamp = item.get("timestamp")
        metric_type = item.get("metric_type")

        if not timestamp:
            continue

        if metric_type == "FPS":
            fps_rows.append({"timestamp": timestamp,"fps": item.get("fps")})

        elif metric_type in ["E2E", "BACKEND"]:
            request_id = item.get("request_id")

            if request_id is None:
                key = f"missing_{metric_type}_{len(request_rows)}"
            else:
                key = request_id
            row = request_rows.setdefault(key, {
                "timestamp": timestamp,"e2e_ms": None,"backend_ms": None})

            if metric_type == "E2E":
                row["e2e_ms"] = item.get("duration_ms")
            else:
                row["backend_ms"] = item.get("duration_ms")

# Gabungkan semua jenis baris untuk diurutkan secara kronologis.
    output_rows = []

    for row in request_rows.values():
        output_rows.append({"timestamp": row["timestamp"],
            "fps": None,"e2e": row["e2e_ms"],
            "backend": row["backend_ms"],"sort_type": 0})

    for row in fps_rows:
        output_rows.append({"timestamp": row["timestamp"],
            "fps": row["fps"],"e2e": None,"backend": None,
            "sort_type": 1})

    output_rows.sort(key=lambda row: (row["timestamp"], row["sort_type"]))

    for row in output_rows:
        fps = row["fps"]
        if fps is not None:
            fps = round(float(fps), 2)

        ws2.append([row["timestamp"],fps,row["e2e"],
            row["backend"],None])

    # Ringkasan tingkat keberhasilan request.
    total = realtime_stats.get("total_requests", 0)
    successful = realtime_stats.get("successful_requests", 0)

    success_rate = (successful / total * 100) if total else None

    ws2.append(["RINGKASAN TINGKAT KEBERHASILAN",
        None,None,None,
        round(success_rate, 2) if success_rate is not None else None])

    widths = {"A": 32,"B": 14,"C": 25,"D": 30,"E": 27}

    for col, width in widths.items():
        ws2.column_dimensions[col].width = width

    for row in ws2.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="center", wrap_text=True)

    for row in ws2.iter_rows(min_row=2, min_col=2, max_col=5):
        for cell in row:
            cell.number_format = "0.00"

    # Workbook dikirim sebagai file download.
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=drowsiness_report.xlsx"
    response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return response

# ================= PERFORMANCE TEST API =================
@app.route("/api/test/performance", methods=["POST"])
def receive_performance_stats():
    data = request.get_json(silent=True) or {}
    metric_type = data.get("metric_type")

    if metric_type not in ["FPS", "E2E", "BACKEND"]:
        return jsonify({"error": "Invalid metric_type"}), 400

    record = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "metric_type": metric_type,
        "request_id": data.get("request_id"),
        "duration_ms": data.get("duration_ms"),
        "frames": data.get("frames"),
        "fps": data.get("fps")}

    print(f"[PERF] type={record['metric_type']} "
    f"request_id={record.get('request_id')} "
    f"duration_ms={record.get('duration_ms')}")

    performance_stats.append(record)

    return jsonify({"status": "ok"}), 200

@app.route("/api/test/realtime/reset", methods=["POST"])
def reset_realtime_test_stats():
    realtime_stats["total_requests"] = 0
    realtime_stats["successful_requests"] = 0

    return jsonify({
        "status": "ok",
        "message": "Realtime test statistics reset"
    })

@app.route("/api/test/mode", methods=["POST"])
def set_test_mode():
    global TEST_MODE

    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "").upper()

    if mode not in ["EVENT_BASED", "CONTINUOUS"]:
        return jsonify({
            "error": "Mode harus EVENT_BASED atau CONTINUOUS"}), 400

    TEST_MODE = mode
    test_stats["frames_processed"] = 0

    print(f"🧪 TEST MODE → {TEST_MODE}")

    return jsonify({"status": "ok","mode": TEST_MODE}), 200

@app.route("/api/test/reset", methods=["POST"])
def reset_test_stats():
    test_stats["frames_processed"] = 0

    print("🔄 Testing statistics reset")

    return jsonify({
        "status": "ok",
        "message": "Testing statistics reset"
    }), 200


@app.route("/api/test/stats", methods=["GET"])
def get_test_stats():
    return jsonify({
        "mode": TEST_MODE,
        "frames_processed": test_stats["frames_processed"]
    }), 200


# --- STATS & ALERTS (DASHBOARD) ---

# --- STATS & ALERTS (DASHBOARD) ---
@app.route("/api/stats", methods=["GET"])
def get_stats():
    conn = get_db_connection(); 
    if not conn: return jsonify({}), 500
    cursor = conn.cursor(dictionary=True)
    stats = {}
    cursor.execute("SELECT COUNT(*) as t FROM drivers"); stats['total_drivers'] = cursor.fetchone()['t']
    cursor.execute("SELECT COUNT(*) as t FROM alerts WHERE DATE(created_at) = CURDATE()"); stats['alerts_today'] = cursor.fetchone()['t']
    cursor.execute("SELECT COUNT(*) as t FROM alerts WHERE alert_type IN ('DROWSY','YAWNING') AND DATE(created_at) = CURDATE()"); stats['critical_today'] = cursor.fetchone()['t']
    stats['alerts_week'] = 0 
    cursor.close(); conn.close()
    return jsonify(stats)

@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    conn = get_db_connection(); 
    if not conn: return jsonify({"alerts": []}), 500
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT a.id, d.driver_name, a.alert_type as status, a.confidence, a.esp32_id, a.created_at as alert_time FROM alerts a LEFT JOIN drivers d ON a.driver_id = d.id ORDER BY a.created_at DESC LIMIT 20")
    alerts = cursor.fetchall()
    cursor.close(); conn.close()
    return jsonify({"alerts": alerts})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7001, debug=True)