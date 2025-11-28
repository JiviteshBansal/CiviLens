import os
import io
import json
import uuid
import traceback
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from google.cloud import firestore, storage
from google.cloud.firestore_v1 import GeoPoint
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth
from functools import wraps
from werkzeug.utils import secure_filename
from google import genai

# --- Configuration ---
load_dotenv()
app = Flask(__name__)

# --- Firebase Admin SDK Initialization ---
cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
if not cred_path:
    raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS not set")
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)

# --- Firebase Client Libraries Initialization ---
db = firestore.Client()
storage_client = storage.Client()
BUCKET_NAME = os.getenv("FIREBASE_STORAGE_BUCKET")  # must match Firebase Console exactly

# --- Generative AI Client (Gemini) ---
GENAI_API_KEY = os.getenv("GOOGLE_API_KEY")
if GENAI_API_KEY:
    genai_client = genai.Client(api_key=GENAI_API_KEY)
else:
    genai_client = None

# --- Authentication Decorator ---
def check_token(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({'error': 'No token provided'}), 401
        parts = auth_header.split(' ')
        if len(parts) != 2:
            return jsonify({'error': 'Invalid auth header'}), 401
        token = parts[1]
        try:
            decoded_token = auth.verify_id_token(token)
            request.user = decoded_token
        except Exception as e:
            print("Token verification failed:", e)
            return jsonify({'error': 'Invalid token'}), 401
        return f(*args, **kwargs)
    return wrap

# --- Routes ---

@app.route('/')
def index():
    raw = os.getenv("FIREBASE_WEB_CONFIG", "")
    if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
        raw = raw[1:-1]
    try:
        firebase_config_obj = json.loads(raw) if raw else {}
    except Exception:
        firebase_config_obj = {}
    maps_api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    return render_template('index.html', maps_api_key=maps_api_key, firebase_config_obj=firebase_config_obj)

@app.route('/api/issues', methods=['GET'])
def get_issues():
    try:
        # attempt to order by timestamp, but fallback if that fails
        try:
            issues_ref = db.collection('issues').order_by('timestamp', direction=firestore.Query.DESCENDING)
        except Exception as e:
            print("order_by timestamp failed, falling back to unordered query:", e)
            issues_ref = db.collection('issues')

        out = []
        for doc in issues_ref.stream():
            d = doc.to_dict() or {}
            d['id'] = doc.id

            # normalize location to { lat, lng } or None
            loc = d.get('location')
            normalized = None
            try:
                if hasattr(loc, 'latitude') and hasattr(loc, 'longitude'):
                    normalized = {"lat": float(loc.latitude), "lng": float(loc.longitude)}
                elif isinstance(loc, dict):
                    if '_latitude' in loc and '_longitude' in loc:
                        normalized = {"lat": float(loc['_latitude']), "lng": float(loc['_longitude'])}
                    elif 'latitude' in loc and 'longitude' in loc:
                        latv = float(loc['latitude']); lngv = float(loc['longitude'])
                        if -90 <= latv <= 90 and -180 <= lngv <= 180:
                            normalized = {"lat": latv, "lng": lngv}
                        elif -90 <= lngv <= 90 and -180 <= latv <= 180:
                            normalized = {"lat": lngv, "lng": latv}
            except Exception:
                normalized = None
            d['location'] = normalized

            # normalize timestamp to ISO or string
            if 'timestamp' in d and d['timestamp']:
                try:
                    # Firestore timestamp has .isoformat if it's a datetime
                    if hasattr(d['timestamp'], 'isoformat'):
                        d['timestamp'] = d['timestamp'].isoformat()
                    else:
                        d['timestamp'] = str(d['timestamp'])
                except Exception:
                    d['timestamp'] = str(d['timestamp'])

            out.append(d)
        return jsonify(out), 200
    except Exception as e:
        tb = traceback.format_exc()
        print("get_issues error:", tb)
        return jsonify({'error': 'Failed to fetch issues', 'detail': str(e)}), 500

@app.route('/api/issues', methods=['POST'])
@check_token
def create_issue():
    try:
        if 'image' not in request.files:
            return jsonify({"error": "No image file provided"}), 400

        user_uid = request.user.get('uid')
        image_file = request.files['image']
        description = request.form.get('description', '')
        try:
            latitude = float(request.form.get('latitude'))
            longitude = float(request.form.get('longitude'))
        except Exception:
            return jsonify({"error": "Invalid or missing latitude/longitude"}), 400

        if not BUCKET_NAME:
            return jsonify({"error": "FIREBASE_STORAGE_BUCKET not set"}), 500

        # sanitize filename and build blob name
        safe_name = secure_filename(image_file.filename) or f"{uuid.uuid4().hex}.jpg"
        timestamp_str = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        blob_name = f"issues/{user_uid}/{timestamp_str}_{uuid.uuid4().hex}_{safe_name}"

        # upload to storage
        try:
            bucket = storage_client.bucket(BUCKET_NAME)
            blob = bucket.blob(blob_name)
            image_bytes = image_file.read()
            blob.upload_from_string(image_bytes, content_type=image_file.content_type)
            try:
                blob.make_public()
                image_url = blob.public_url
            except Exception:
                # fallback if make_public fails
                image_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{blob_name}"
        except Exception as e:
            tb = traceback.format_exc()
            print("Storage upload failed:", tb)
            return jsonify({"error": "upload failed", "detail": str(e)}), 500

        # call AI model safely (optional)
        ai_analysis = {
            "classification": "unclassified",
            "severity": 1,
            "priority": "Low",
            "suggestedAction": "Manual review"
        }

        # attempt to call genai only if client available
        try:
            if genai_client:
                model_name = os.getenv("GENAI_MODEL", "models/gemini-2.5-flash")

                response = genai_client.models.generate_content(
                    model=model_name,
                    contents=[
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": (
                                        f'Analyze this urban issue image. '
                                        f'Description: "{description}". '
                                        'Return a JSON object with: '
                                        '"classification", "severity", "priority", "suggestedAction".'
                                    )
                                },
                                {
                                    "inline_data": {
                                        "mime_type": image_file.content_type,
                                        "data": image_bytes
                                    }
                                }
                            ]
                        }
                    ]
                )

                # Extract model text robustly
                raw = ""
                try:
                    raw = response.text.strip()
                except:
                    try:
                        raw = response.output_text.strip()
                    except:
                        raw = str(response)

                raw = raw.replace("```json", "").replace("```", "").strip()

                # Parse JSON safely
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        ai_analysis = parsed
                except:
                    start = raw.find('{')
                    end = raw.rfind('}')
                    if start != -1 and end != -1:
                        try:
                            parsed = json.loads(raw[start:end+1])
                            if isinstance(parsed, dict):
                                ai_analysis = parsed
                        except:
                            pass
        except Exception as e:
            print("AI model failed:", e)

        # prepare Firestore document
        try:
            location_point = GeoPoint(latitude, longitude)
        except Exception:
            location_point = None

        # use deterministic ISO timestamp so the new doc is immediately queryable
        now_iso = datetime.utcnow().isoformat()

        issue_data = {
            "imageUrl": image_url,
            "image_path": blob_name,
            "description": description,
            "location": location_point,
            "classification": ai_analysis.get("classification", "unclassified"),
            "severity": ai_analysis.get("severity", 1),
            "priority": ai_analysis.get("priority", "Low"),
            "suggestedAction": ai_analysis.get("suggestedAction", "Manual review"),
            "user_uid": user_uid,
            "user_email": request.user.get("email"),
            "timestamp": now_iso
        }

        # write to Firestore
        try:
            doc_ref = db.collection("issues").document()
            doc_ref.set(issue_data)
        except Exception as e:
            tb = traceback.format_exc()
            print("Firestore write failed:", tb)
            return jsonify({"error": "Failed to write to Firestore", "detail": str(e)}), 500

        # return the created object immediately so frontend can append it without refresh
        saved = issue_data.copy()
        saved['id'] = doc_ref.id
        # normalize location for client
        if isinstance(saved.get('location'), GeoPoint):
            saved['location'] = {"lat": float(saved['location'].latitude), "lng": float(saved['location'].longitude)}

        return jsonify(saved), 201

    except Exception as e:
        tb = traceback.format_exc()
        print("Error creating issue:", tb)
        return jsonify({"error": "Failed to create issue", "detail": str(e), "trace": tb}), 500

if __name__ == '__main__':
    app.run(debug=True)
