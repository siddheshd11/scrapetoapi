FROM python:3.11-slim

WORKDIR /app

# Copy requirements first
COPY req.txt .

# Install dependencies
RUN pip install --no-cache-dir -r req.txt

# Copy all application files
COPY . .

# Use a shell script to properly handle environment variables
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
