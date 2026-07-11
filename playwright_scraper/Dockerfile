# Playwright's official image ships Chromium plus every system library it
# needs already installed — same reasoning as whatsapp_forwarder/Dockerfile:
# Railway's default nixpacks Python builder does not, and fighting Playwright
# onto it isn't worth it. Deploy this service with builder = "Dockerfile"
# (root directory playwright_scraper/), same as whatsapp_forwarder.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# No volume needed — this service is fully stateless (no login/session to
# persist between requests, unlike whatsapp_forwarder).
CMD ["python", "main.py"]
