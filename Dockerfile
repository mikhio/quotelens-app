FROM python:3.12-slim
WORKDIR /app
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
COPY quotelens ./quotelens
COPY static ./static
COPY data ./data
RUN useradd --uid 10001 --create-home app && mkdir /runtime && chown app:app /runtime
USER app
ENV HOST=0.0.0.0 PORT=8000 APP_DATA_DIR=/runtime PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1
CMD ["python", "-m", "quotelens.server"]
