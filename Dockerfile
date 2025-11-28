# Use official Python runtime as a parent image
FROM python:3.11-slim

# Set working directory in container
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire application (but NOT firebase-key.json, which should be passed via env var)
COPY app.py model.py /app/
COPY templates/ ./templates/

# Expose the Flask default port
EXPOSE 5000

# Set environment variables (these should be overridden at runtime)
ENV FLASK_APP=app.py
ENV PYTHONUNBUFFERED=1

# Run the Flask app
CMD ["python", "app.py"]
