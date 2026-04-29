FROM python:3.12-slim
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.0-rc1 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV AWS_LWA_PORT=8080

EXPOSE 8080

CMD ["python", "main.py"]
