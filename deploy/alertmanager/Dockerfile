FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mock_slack_receiver.py .
EXPOSE 9099
CMD ["uvicorn", "mock_slack_receiver:app", "--host", "0.0.0.0", "--port", "9099"]
