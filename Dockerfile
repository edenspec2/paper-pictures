FROM python:3.11-slim-bookworm
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxrender1 libxext6 libexpat1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py chemistry.py figures.py reader.js reader.py ./
COPY vendor ./vendor
EXPOSE 10000
CMD ["python", "-u", "app.py"]
