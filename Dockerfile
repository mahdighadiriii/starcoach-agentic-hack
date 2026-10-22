FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY wtrader/ wtrader/
COPY prompts/ prompts/
COPY contracts/ contracts/
COPY config/ config/
COPY reports/edge_artifact.json reports/edge_artifact.json
# state/ and evidence/ are volumes; the image never contains secrets
VOLUME ["/app/state", "/app/evidence"]
ENTRYPOINT ["python", "-m", "wtrader"]
CMD ["shadow"]
