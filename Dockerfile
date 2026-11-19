FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 delta \
    && useradd --uid 10001 --gid delta --no-create-home delta \
    && install -d -m 0700 -o delta -g delta /var/lib/delta-strategy
COPY dashboard.py dashboard.html chart_vwap.py delta_client.py delta_strategy.py live_strategy.py ./
USER delta
CMD ["python", "dashboard.py"]
