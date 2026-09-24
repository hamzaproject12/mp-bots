# Image officielle Playwright : Chromium et ses dependances sont deja
# installes. Evite l'etape `playwright install --with-deps` qui prend
# plusieurs minutes a chaque build.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pas de buffer : les logs Railway apparaissent immediatement
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
