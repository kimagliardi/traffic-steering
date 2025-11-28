# Use a lightweight Python image
FROM python:3.9-slim

# Prevent Python from writing pyc files to disc and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies
# requests: for HTTP calls
# ollama: for communicating with LLM
# prometheus-client: for exposing metrics
RUN pip install --no-cache-dir requests ollama prometheus-client

# Copy the agent code
COPY smart_slice_agent.py .

# Expose the metrics port
EXPOSE 8000

# Run the agent
CMD ["python", "smart_slice_agent.py"]