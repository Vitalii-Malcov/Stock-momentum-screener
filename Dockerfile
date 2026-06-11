FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY app.py .
COPY tickers.csv .

# Создаём директории
RUN mkdir -p logs cache history

# Запуск по умолчанию
ENTRYPOINT ["python", "app.py"]
CMD ["--min-change", "5", "--file", "tickers.csv", "--use-cache"]
