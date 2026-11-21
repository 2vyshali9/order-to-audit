FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/app.db
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME /data
EXPOSE 8000
# Seeds the Section 5 master data only if the database is empty, then serves the app.
CMD ["sh", "-c", "python -m app.seed --if-empty && gunicorn -w 2 -b 0.0.0.0:8000 wsgi:app"]
