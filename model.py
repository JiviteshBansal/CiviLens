# test_model.py
from google import genai
import os
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")
MODEL = os.getenv("GENAI_MODEL", "models/gemini-2.5-flash")

print("API KEY found:", bool(API_KEY))
print("Testing model:", MODEL)

client = genai.Client(api_key=API_KEY)

try:
    resp = client.models.generate_content(
        model=MODEL,
        contents=[
            {"role":"user","parts":[{"text":"Return a JSON object: {\"ok\":true}"}]}
        ]
    )
    text = ""
    try:
        text = resp.text.strip()
    except Exception:
        try:
            text = resp.output_text.strip()
        except Exception:
            text = str(resp)
    print("Model response:", text[:400])
    print("MODEL OK")
except Exception as e:
    print("MODEL FAILED:", repr(e))
