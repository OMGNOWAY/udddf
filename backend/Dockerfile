FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg for yt-dlp
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp needs a JS runtime to solve YouTube's challenges
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Real Google Chrome + a virtual display (Xvfb) for the Google auto-login (google_login.py).
# Chromium is also installed as a headless fallback.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && \
    apt-get install -y --no-install-recommends xvfb && \
    playwright install --with-deps chrome chromium && \
    rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
