# Use Python 3.10 runtime
FROM python:3.10

# Set workdir
WORKDIR /app

# Copy project files
COPY . .

# Install dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Railway will run this command automatically if no Procfile is provided
CMD ["python3.10", "bot.py"]
