FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway cron entrypoint - process exits after completion
CMD ["python", "-m", "src.jobs.weekly"]
