FROM python:3.12-slim

WORKDIR /app

# System deps for pymupdf and python-docx
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        libmupdf-dev \
        libfreetype6-dev \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (Linux version — no pywin32)
COPY requirements-linux.txt .
RUN pip install --no-cache-dir -r requirements-linux.txt

# Copy application code
COPY . .

# Working dirs for S3 downloads and chunk output
RUN mkdir -p data/s3_test chunks/requirements

CMD ["python", "-m", "app.messaging.consumer"]
