# 1. Fem servir una imatge oficial de Python súper lleugera
FROM python:3.11-slim

# 2. Definim la carpeta de treball dins del servidor
WORKDIR /app

# 3. Copiem tots els fitxers del teu GitHub cap a dins del servidor
COPY . /app/

# 4. Exposem el port UDP oficial de la teva centraleta
EXPOSE 5060/udp

# 5. L'ordre exacta per arrencar el teu servidor asíncron
CMD ["python", "server.py"]
