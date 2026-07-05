FROM python:3.11-slim

WORKDIR /app

COPY server.py /app/server.py

# Port de les trucades
EXPOSE 5060/udp
# Port per l'UptimeRobot
EXPOSE 8080/tcp

CMD ["python", "/app/server.py"]
