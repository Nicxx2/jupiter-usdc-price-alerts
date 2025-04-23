# Use a slim Python image
FROM python:3.12-slim

# Set the working directory inside the container
WORKDIR /app

# Copy your Python script into the container
COPY main.py .

# Install required Python packages
RUN pip install --no-cache-dir requests

# Force run your script every time
ENTRYPOINT ["python", "/app/main.py"]
