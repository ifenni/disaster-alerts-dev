# Use Miniforge (arm64-compatible)
FROM condaforge/miniforge3:latest

# Set working directory
WORKDIR /app

# Copy project files
COPY . /app

# Create conda environment
RUN conda env create -f environment.yml

# Install your package in editable mode inside the environment
RUN /opt/conda/bin/conda run -n disaster-alerts pip install --no-cache-dir -e /app

# Make sure conda is activated in all RUN commands
SHELL ["conda", "run", "-n", "disaster-alerts", "/bin/bash", "-c"]

# Expose Flask port
EXPOSE 8000

# Install cron
RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*

# Add cron job (every 10 minutes)
RUN echo "*/10 * * * * /opt/conda/bin/conda run -n disaster-alerts /bin/bash /app/scripts/run.sh >> /app/logs/cron.log 2>&1" > /etc/cron.d/disaster-cron
RUN chmod 0644 /etc/cron.d/disaster-cron && crontab /etc/cron.d/disaster-cron

# Start cron + Flask together
CMD cron && python web/app.py
