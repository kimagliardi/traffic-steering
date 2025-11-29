# Use a lightweight Python image
FROM python:3.11-slim

# Prevent Python from writing pyc files to disc and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the agent code
COPY *.py .

# Expose ports: 8080 for API, 8000 for metrics
EXPOSE 8080 8000

# Default: run the API server
# Override with CMD ["python", "main.py"] to run the AI agent loop
CMD ["python", "api.py"]