FROM python:3.12-slim
WORKDIR /app
COPY mobile_app/requirements.txt ./mobile_app/
RUN pip install --no-cache-dir -r mobile_app/requirements.txt
COPY product_matcher.py given_industrial_products.json ./
COPY mobile_app/server.py mobile_app/history.py mobile_app/catalogue_editor.py ./mobile_app/
COPY mobile_app/static/ ./mobile_app/static/
RUN useradd --create-home --uid 10001 pocketmatch && mkdir -p /app/mobile_app/data && chown -R pocketmatch:pocketmatch /app
USER pocketmatch
ENV PORT=8001 PYTHONUNBUFFERED=1
EXPOSE 8001
CMD ["gunicorn", "--chdir", "mobile_app", "--bind", "0.0.0.0:8001", "--workers", "1", "--threads", "8", "--timeout", "60", "--graceful-timeout", "30", "server:create_app()"]
