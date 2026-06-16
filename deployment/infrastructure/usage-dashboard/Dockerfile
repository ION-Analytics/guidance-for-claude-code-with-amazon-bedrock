FROM python:3.12-slim
WORKDIR /app
# Install corp CA certs so pip can reach PyPI through the corporate proxy
COPY corp_certs.pem /usr/local/share/ca-certificates/corp_certs.crt
RUN update-ca-certificates
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates/ templates/
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "120", "app:app"]
