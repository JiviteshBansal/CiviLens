import os
import io
import base64
from flask import Flask, render_template, request, jsonify
from google.cloud import firestore, storage
import google.generativeai as genai
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth
from functools import wraps
import json

# --- Configuration ---
load_dotenv()
app = Flask(__name__)

# --- Firebase Admin SDK Initialization ---
# This uses the same service account key as before
cred = credentials.Certificate(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
firebase_admin.initialize_app(cred)

# --- Firebase Client Libraries Initialization ---
db = firestore.Client()
storage_client = storage.Client()
BUCKET_NAME = os.getenv("FIREBASE_STORAGE_BUCKET") 
bucket = storage_client.bucket(BUCKET_NAME)

# --- Gemini API Configuration ---
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
model = genai.GenerativeModel('gemini-pro-vision')

# --- Authentication Decorator ---
def check_token(f):
    """Decorator to verify Firebase ID token in the Authorization header."""
    @wraps(f)
    def wrap(*args,**kwargs):
        if not request.headers.get('Authorization'):
            return jsonify({'error': 'No token provided'}), 401
        try:
            # Expecting "Bearer <token>"
            token = request.headers['Authorization'].split(' ')[1]
            decoded_token = auth.verify_id_token(token)
            # Add user info to the request context
            request.user = decoded_token
        except Exception as e:
            print(f"Token verification failed: {e}")
            return jsonify({'error': 'Invalid token'}), 401
        return f(*args, **kwargs)
    return wrap

# --- API Routes ---

@app.route('/')
def index():
    """Renders the main HTML page."""
    maps_api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    firebase_config = os.getenv("FIREBASE_WEB_CONFIG") # Pass web config to frontend
    return render_template('index.html', maps_api_key=maps_api_key, firebase_config=firebase_config)

# @app.route('/')
# def index():
#     raw = os.getenv("FIREBASE_WEB_CONFIG", "")

#     # remove accidental surrounding quotes
#     if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
#         raw = raw[1:-1]

#     # convert JSON string to object
#     try:
#         firebase_config_obj = json.loads(raw)
#     except Exception:
#         firebase_config_obj = {}

#     # send clean JSON to template
#     firebase_config_json = json.dumps(firebase_config_obj)

#     maps_api_key = os.getenv("GOOGLE_MAPS_API_KEY")

#     return render_template(
#         'index.html',
#         maps_api_key=maps_api_key,
#         firebase_config=firebase_config_json
#     )




@app.route('/api/issues', methods=['GET'])
def get_issues():
    """Fetches all issues from Firestore."""
    try:
        issues_ref = db.collection('issues').order_by('timestamp', direction=firestore.Query.DESC)
        issues = []
        for doc in issues_ref.stream():
            issue_data = doc.to_dict()
            issue_data['id'] = doc.id
            if 'timestamp' in issue_data and issue_data['timestamp']:
                 issue_data['timestamp'] = issue_data['timestamp'].isoformat()
            issues.append(issue_data)
        return jsonify(issues), 200
    except Exception as e:
        return jsonify({"error": "Failed to fetch issues"}), 500

@app.route('/api/issues', methods=['POST'])
@check_token # This route is now protected
def create_issue():
    """Handles new issue submission from an authenticated user."""
    try:
        if 'image' not in request.files:
            return jsonify({"error": "No image file provided"}), 400
        
        user_uid = request.user['uid'] # Get user ID from the verified token
        
        image_file = request.files['image']
        description = request.form.get('description', '')
        latitude = float(request.form.get('latitude'))
        longitude = float(request.form.get('longitude'))

        # Upload Image to Firebase Storage
        blob = bucket.blob(f"issues/{user_uid}/{image_file.filename}_{firestore.SERVER_TIMESTAMP}")
        image_bytes = image_file.read()
        blob.upload_from_string(image_bytes, content_type=image_file.content_type)
        image_url = blob.public_url

        # Analyze Image with Gemini AI
        image_parts = [{"mime_type": image_file.content_type, "data": image_bytes}]
        prompt_parts = [f"""Analyze this image of an urban issue. The user described it as: "{description}", 
            Based on the image and description, provide a JSON object with: 
            1. "classification". 2. "severity" (score 1-10). 3. "priority" ("Low", "Medium", or "High"). 
            4. "suggestedAction". The response must be a clean, directly parsable JSON object.""", *image_parts]
        
        response = model.generate_content(prompt_parts)
        ai_response_text = response.text.strip().replace("```json", "").replace("```", "")
        ai_analysis = eval(ai_response_text)

        # Save Report to Firestore, now with userId
        issue_data = {
            "userId": user_uid,
            "description": description,
            "imageUrl": image_url,
            "location": firestore.GeoPoint(latitude, longitude),
            "timestamp": firestore.SERVER_TIMESTAMP,
            "status": "Reported",
            **ai_analysis,
        }
        
        db.collection('issues').add(issue_data)
        return jsonify({"success": True}), 201

    except Exception as e:
        print(f"Error creating issue: {e}")
        return jsonify({"error": "Failed to create issue"}), 500

if __name__ == '__main__':
    app.run(debug=True)
