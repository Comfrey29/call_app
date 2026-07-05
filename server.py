import asyncio

usuaris_connectats = {}

# --- MINI-SERVIDOR HTTP (Per UptimeRobot) ---
async def handle_uptime_robot(reader, writer):
    # Responem amb un OK HTTP estàndard perquè UptimeRobot no doni error
    resposta = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK"
    writer.write(resposta.encode('utf-8'))
    await writer.drain()
    writer.close()

class CentraletaUDPAsync(asyncio.DatagramProtocol):
    def __init__(self):
        super().__init__()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            missatge = data.decode('utf-8').strip()
            print(f"📩 [{addr}] {missatge}")
            
            # 1. BATEC DE COR
            if missatge.startswith("H:"):
                nom = missatge.split(":")[1]
                usuaris_connectats[nom] = addr
                
            # 2. TRUCADA
            elif missatge.startswith("CALL:"):
                nom_desti = missatge.split(":")[1]
                nom_origen = self.buscar_nom_per_adreca(addr)
                
                if nom_origen and nom_desti in usuaris_connectats:
                    paquet = f"TRUCADA_DE:{nom_origen}".encode('utf-8')
                    self.transport.sendto(paquet, usuaris_connectats[nom_desti])
                else:
                    self.transport.sendto(b"FINALITZADA", addr)

            # 3. ACCEPTADA
            elif missatge.startswith("ACCEPTADA_DE:"):
                nom_origen = missatge.split(":")[1]
                for usuari, adreca in usuaris_connectats.items():
                    if usuari != nom_origen:
                        self.transport.sendto(b"ACCEPTADA", adreca)

            # 4. PENJADA O CANCEL·LADA
            elif missatge.startswith("CANCEL:") or missatge.startswith("HANGUP:") or missatge.startswith("REBUTJADA_DE:"):
                for adreca in usuaris_connectats.values():
                    self.transport.sendto(b"FINALITZADA", adreca)

        except Exception as e:
            print(f"⚠️ Error processant paquet UDP: {e}")

    def buscar_nom_per_adreca(self, addr):
        for nom, adreca in usuaris_connectats.items():
            if adreca == addr:
                return nom
        return None

async def main():
    loop = asyncio.get_running_loop()
    
    # 1. Obrim el sòcol UDP (Per a les trucades mòbils)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: CentraletaUDPAsync(),
        local_addr=('0.0.0.0', 5060)
    )
    
    # 2. Obrim el sòcol TCP (Perquè UptimeRobot ens faci ping)
    server_web = await asyncio.start_server(handle_uptime_robot, '0.0.0.0', 8080)
    
    print("🚀 Servidor ACTIU! UDP: 5060 (Trucades) | TCP: 8080 (UptimeRobot HTTP)")
    
    try:
        # Mantenim el codi encès indefinidament
        await asyncio.sleep(3600 * 24 * 365)
    finally:
        transport.close()
        server_web.close()

if __name__ == '__main__':
    asyncio.run(main())
