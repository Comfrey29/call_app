import asyncio

# Diccionari global per guardar els usuaris que es connecten
# Estructura: {"nom_usuari": (ip, port)}
usuaris_connectats = {}

class CentraletaUDPAsync(asyncio.DatagramProtocol):
    def __init__(self):
        super().__init__()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        print("🚀 Centraleta de Telefonia activada i escoltant al port 5060/UDP...")

    def datagram_received(self, data, addr):
        try:
            missatge = data.decode('utf-8').strip()
            print(f"📩 Paquet rebut des de {addr}: {missatge}")
            
            # 1. BATEC DE COR (Heartbeat) -> Format: "H:Joan"
            if missatge.startswith("H:"):
                nom = missatge.split(":")[1]
                usuaris_connectats[nom] = addr
                
            # 2. INTENT DE TRUCADA -> Format: "CALL:Arnau"
            elif missatge.startswith("CALL:"):
                nom_desti = missatge.split(":")[1]
                nom_origen = self.buscar_nom_per_adreca(addr)
                
                if nom_origen and nom_desti in usuaris_connectats:
                    print(f"📞 {nom_origen} està trucant a {nom_desti}...")
                    paquet = f"TRUCADA_DE:{nom_origen}".encode('utf-8')
                    self.transport.sendto(paquet, usuaris_connectats[nom_desti])
                else:
                    # Si el destí no està connectat, avisem a qui truca
                    self.transport.sendto(b"FINALITZADA", addr)

            # 3. TRUCADA ACCEPTADA -> Format: "ACCEPTADA_DE:Arnau"
            elif missatge.startswith("ACCEPTADA_DE:"):
                nom_origen = missatge.split(":")[1]
                print(f"✅ {nom_origen} ha agafat la trucada.")
                for usuari, adreca in usuaris_connectats.items():
                    if usuari != nom_origen:
                        self.transport.sendto(b"ACCEPTADA", adreca)

            # 4. CANCEL·LAR, REBUTJAR O PENJAR
            elif missatge.startswith("CANCEL:") or missatge.startswith("HANGUP:") or missatge.startswith("REBUTJADA_DE:"):
                print("🛑 S'ha penjat o rebutjat la trucada. Reiniciant línies.")
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
    # Obrim el sòcol UDP a la xarxa local del contenidor
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: CentraletaUDPAsync(),
        local_addr=('0.0.0.0', 5060)
    )
    
    try:
        # Manté el servidor despert 24sp/7
        await asyncio.sleep(3600 * 24 * 365)
    finally:
        transport.close()

if __name__ == '__main__':
    asyncio.run(main())
