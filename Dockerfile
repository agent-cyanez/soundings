FROM python:3.12-slim
WORKDIR /app
COPY soundings.py .
CMD ["python", "-u", "soundings.py"]
