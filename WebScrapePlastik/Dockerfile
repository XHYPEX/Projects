FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The Maps scraper is disabled (SCRAPER_ENABLED defaults to false in
# backend/config.py), so the ~1.5GB Chromium download and its system libraries
# are not installed. backend/worker.py imports Playwright lazily, so the app
# starts fine without it. To re-enable the scraper, uncomment the line below,
# rebuild, and set SCRAPER_ENABLED=true.
# RUN apt-get update && apt-get install -y wget curl gnupg \
#     && rm -rf /var/lib/apt/lists/* \
#     && playwright install chromium --with-deps

COPY . .

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "run.py"]
