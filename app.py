#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 main.py — Walkie-Talkie P2P amb múltiples pantalles (Contactes, Teclat,
           Trucada en curs i Trucada finalitzada)
================================================================================

Aquesta versió amplia l'aplicació de trucada P2P pura (STUN + UDP hole
punching + veu real, sense cap base de dades central ni servidor HTTP) amb
una interfície de diverses pantalles gestionades amb kivy.uix.screenmanager:

    'inici'     -> PantallaInici:     nom, adreça pròpia (STUN), navegació
    'contactes' -> PantallaContactes: llibreta d'adreces local (JSON al
                                       telèfon; NO és cap servidor remot)
    'teclat'    -> PantallaTeclat:    entrada manual d'IP:Port + teclat
                                       numèric tàctil + desar com a contacte
    'trucada'   -> PantallaTrucada:   pantalla única que canvia de contingut
                                       segons l'estat (Trucant/Entrant/Activa),
                                       amb nom, indicador de veu, altaveu i penjar
    'penjat'    -> PantallaPenjat:    resum de la trucada acabada (motiu,
                                       durada) i botó per tornar a l'inici

ARQUITECTURA MODULAR
---------------------
    XarxaP2P       -> socket UDP, STUN (RFC 5389 manual) i escolta persistent
    GestorAudio    -> micròfon/altaveu real (Android/jnius, PyAudio, simulat)
    GestorSo       -> tons de trucada generats per codi (sense fitxers externs)
    GestorContactes-> llibreta d'adreces local persistida en JSON
    PantallaXxx    -> cada pantalla només construeix el seu propi layout
    WalkieTalkieApp-> màquina d'estats de la trucada + cablejat entre tot

COMPATIBILITAT
--------------
- Xarxa: exclusivament socket, struct, threading, time, random.
- Sense .kv extern, tot en Python pur (compatible amb builds antics de
  python-for-android / Android 4.1+).
- `json` només s'usa per a la llibreta de contactes LOCAL (equivalent a
  guardar una agenda al telèfon); no hi ha cap crida de xarxa relacionada.

BUILDOZER.SPEC (Android)
--------------------------
    requirements = python3,kivy,pyjnius
    android.permissions = INTERNET,RECORD_AUDIO,MODIFY_AUDIO_SETTINGS,ACCESS_NETWORK_STATE
================================================================================
"""

import socket
import struct
import threading
import time
import random
import math
import wave
import tempfile
import os
import json
import queue
import sys

# --------------------------------------------------------------------
# Configuració de Kivy ABANS d'importar la resta de mòduls de kivy.
# --------------------------------------------------------------------
try:
    from kivy.config import Config
    Config.set('input', 'mouse', 'mouse,multitouch_on_demand')
except Exception:
    pass

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import ScreenManager, Screen, NoTransition


# ============================================================
#  CONFIGURACIÓ GLOBAL
# ============================================================
STUN_HOST = "stun.l.google.com"
STUN_PORT = 19302
STUN_MAGIC_COOKIE = 0x2112A442
STUN_SOCKET_TIMEOUT = 5

TIPUS_PUNCH = b'C'   # trucada entrant / ping de foradat de NAT
TIPUS_CANCEL = b'K'  # qui truca cancel·la abans de l'ACK
TIPUS_ACK = b'A'     # el receptor ha despenjat (payload = el seu nom)
TIPUS_REJECT = b'R'  # el receptor ha rebutjat / està ocupat
TIPUS_BYE = b'B'     # penjar durant la trucada
TIPUS_AUDIO = b'V'   # paquet d'àudio (veu)

HOLEPUNCH_INTERVAL = 0.5
TRUCAR_TIMEOUT = 45

FREQ_MOSTREIG = 8000
MOSTRES_PER_FRAME = 320
MIDA_FRAME_BYTES = MOSTRES_PER_FRAME * 2

MAX_LINIES_LOG = 200

COLOR_FONS = (0.06, 0.07, 0.09, 1)
COLOR_TEXT = (0.90, 0.93, 0.95, 1)
COLOR_TEXT_TENUE = (0.55, 0.85, 0.55, 1)
COLOR_BLAU = (0.16, 0.47, 0.86, 1)
COLOR_VERD = (0.18, 0.66, 0.35, 1)
COLOR_VERMELL = (0.82, 0.20, 0.20, 1)
COLOR_TARONJA = (0.86, 0.55, 0.15, 1)
COLOR_CAIXA_CLARA = (0.86, 0.88, 0.90, 1)
COLOR_TECLA = (0.20, 0.20, 0.24, 1)


def log_consola(missatge):
    print("[WalkieP2P] {0}".format(missatge))


def _crear_boto(text, color_fons, mida_lletra='20sp'):
    return Button(
        text=text, background_normal='', background_color=color_fons,
        color=(1, 1, 1, 1), font_size=mida_lletra, bold=True)


# ============================================================
#  GENERACIÓ DE TONS DE TRUCADA (sense fitxers externs)
# ============================================================
def _generar_to_wav(ruta, patro, freq_mostreig=FREQ_MOSTREIG):
    mostres = bytearray()
    for freq, durada in patro:
        n = int(freq_mostreig * durada)
        for i in range(n):
            if freq > 0:
                valor = int(32767 * 0.3 * math.sin(2 * math.pi * freq * i / freq_mostreig))
            else:
                valor = 0
            mostres += struct.pack('<h', valor)

    wav_file = wave.open(ruta, 'wb')
    try:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(freq_mostreig)
        wav_file.writeframes(bytes(mostres))
    finally:
        wav_file.close()


PATRO_TO_SORTINT = [(425, 1.0), (0, 1.0)]
PATRO_TO_ENTRANT = [(950, 0.35), (1400, 0.35), (0, 0.3)]


class GestorSo:
    """Genera i reprodueix els tons de trucada (sortint/entrant) sense fitxers externs."""

    def __init__(self, callback_log=None):
        self.callback_log = callback_log or log_consola
        self.so_sortint = None
        self.so_entrant = None
        self._dir_temp = None
        self._preparar_sons()

    def _log(self, missatge):
        try:
            self.callback_log(missatge)
        except Exception:
            log_consola(missatge)

    def _preparar_sons(self):
        try:
            self._dir_temp = tempfile.mkdtemp(prefix="walkiep2p_so_")
            ruta_sortint = os.path.join(self._dir_temp, "to_sortint.wav")
            ruta_entrant = os.path.join(self._dir_temp, "to_entrant.wav")
            _generar_to_wav(ruta_sortint, PATRO_TO_SORTINT)
            _generar_to_wav(ruta_entrant, PATRO_TO_ENTRANT)

            from kivy.core.audio import SoundLoader
            self.so_sortint = SoundLoader.load(ruta_sortint)
            self.so_entrant = SoundLoader.load(ruta_entrant)
            if self.so_sortint:
                self.so_sortint.loop = True
            if self.so_entrant:
                self.so_entrant.loop = True
            self._log("Tons de trucada generats i carregats correctament.")
        except Exception as e:
            self._log("Avís: no s'han pogut preparar els tons de trucada ({0}). "
                       "La trucada funcionarà igualment, sense so.".format(e))
            self.so_sortint = None
            self.so_entrant = None

    def reproduir_sortint(self):
        self._aturar_tots()
        try:
            if self.so_sortint:
                self.so_sortint.play()
        except Exception as e:
            self._log("Avís reproduint to de trucada sortint: {0}".format(e))

    def reproduir_entrant(self):
        self._aturar_tots()
        try:
            if self.so_entrant:
                self.so_entrant.play()
        except Exception as e:
            self._log("Avís reproduint to de trucada entrant: {0}".format(e))

    def _aturar_tots(self):
        for so in (self.so_sortint, self.so_entrant):
            try:
                if so:
                    so.stop()
            except Exception:
                pass

    def aturar_tot(self):
        self._aturar_tots()


# ============================================================
#  MOTOR D'ÀUDIO EN DIRECTE (Android real / PyAudio / simulat)
# ============================================================
try:
    from jnius import autoclass  # noqa: F401
    _BACKEND_AUDIO = "android"
except Exception:
    try:
        import pyaudio  # noqa: F401
        _BACKEND_AUDIO = "pyaudio"
    except Exception:
        _BACKEND_AUDIO = "simulat"


class GestorAudio:
    """Captura de micròfon i reproducció per l'altaveu, amb fallback multiplataforma."""

    def __init__(self, callback_log=None):
        self.callback_log = callback_log or log_consola
        self.mode = _BACKEND_AUDIO
        self.actiu = False

        self._audio_record = None
        self._audio_track = None
        self._buffer_java = None

        self._pyaudio_instance = None
        self._pyaudio_stream_in = None
        self._pyaudio_stream_out = None

        self._log("Motor d'àudio seleccionat: {0}".format(self.mode))

    def _log(self, missatge):
        try:
            self.callback_log(missatge)
        except Exception:
            log_consola(missatge)

    def iniciar(self):
        try:
            if self.mode == "android":
                self._iniciar_android()
            elif self.mode == "pyaudio":
                self._iniciar_pyaudio()
            else:
                self._log("Mode d'àudio SIMULAT: no hi ha micròfon/altaveu reals "
                           "en aquest entorn.")
            self.actiu = True
            return True
        except Exception as e:
            self._log("ERROR inicialitzant l'àudio ({0}): {1}. "
                       "La trucada continuarà sense veu real.".format(self.mode, e))
            self.actiu = False
            return False

    def _iniciar_android(self):
        from jnius import autoclass
        AudioRecord = autoclass('android.media.AudioRecord')
        AudioTrack = autoclass('android.media.AudioTrack')
        AudioFormat = autoclass('android.media.AudioFormat')
        AudioManager = autoclass('android.media.AudioManager')
        MediaRecorder_AudioSource = autoclass('android.media.MediaRecorder$AudioSource')

        canal_in = AudioFormat.CHANNEL_IN_MONO
        canal_out = AudioFormat.CHANNEL_OUT_MONO
        format_pcm = AudioFormat.ENCODING_PCM_16BIT

        mida_min_in = AudioRecord.getMinBufferSize(FREQ_MOSTREIG, canal_in, format_pcm)
        mida_min_out = AudioTrack.getMinBufferSize(FREQ_MOSTREIG, canal_out, format_pcm)
        mida_buffer_in = max(mida_min_in, MIDA_FRAME_BYTES * 4)
        mida_buffer_out = max(mida_min_out, MIDA_FRAME_BYTES * 4)

        self._audio_record = AudioRecord(
            MediaRecorder_AudioSource.MIC, FREQ_MOSTREIG, canal_in, format_pcm, mida_buffer_in)
        self._audio_track = AudioTrack(
            AudioManager.STREAM_VOICE_CALL, FREQ_MOSTREIG, canal_out, format_pcm,
            mida_buffer_out, AudioTrack.MODE_STREAM)

        self._buffer_java = bytearray(MIDA_FRAME_BYTES)
        self._audio_record.startRecording()
        self._audio_track.play()

    def _iniciar_pyaudio(self):
        import pyaudio
        self._pyaudio_instance = pyaudio.PyAudio()
        self._pyaudio_stream_in = self._pyaudio_instance.open(
            format=pyaudio.paInt16, channels=1, rate=FREQ_MOSTREIG,
            input=True, frames_per_buffer=MOSTRES_PER_FRAME)
        self._pyaudio_stream_out = self._pyaudio_instance.open(
            format=pyaudio.paInt16, channels=1, rate=FREQ_MOSTREIG,
            output=True, frames_per_buffer=MOSTRES_PER_FRAME)

    def capturar_frame(self):
        if not self.actiu:
            return None
        try:
            if self.mode == "android":
                n = self._audio_record.read(self._buffer_java, 0, len(self._buffer_java))
                if n and n > 0:
                    return bytes(self._buffer_java[:n])
                return None
            elif self.mode == "pyaudio":
                return self._pyaudio_stream_in.read(
                    MOSTRES_PER_FRAME, exception_on_overflow=False)
            else:
                time.sleep(MOSTRES_PER_FRAME / float(FREQ_MOSTREIG))
                return b'\x00' * MIDA_FRAME_BYTES
        except Exception as e:
            self._log("Avís capturant àudio: {0}".format(e))
            return None

    def reproduir_frame(self, dades):
        if not self.actiu or not dades:
            return
        try:
            if self.mode == "android":
                self._audio_track.write(dades, 0, len(dades))
            elif self.mode == "pyaudio":
                self._pyaudio_stream_out.write(dades)
        except Exception as e:
            self._log("Avís reproduint àudio: {0}".format(e))

    def activar_altaveu(self, activar):
        """Alterna l'altaveu (mans lliures) durant una trucada Android real."""
        if self.mode != "android":
            self._log("Canvi d'altaveu no disponible en aquest entorn (mode {0}).".format(self.mode))
            return False
        try:
            from jnius import autoclass, cast
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Context = autoclass('android.content.Context')
            activitat = PythonActivity.mActivity
            gestor_audio_android = cast(
                'android.media.AudioManager', activitat.getSystemService(Context.AUDIO_SERVICE))
            gestor_audio_android.setSpeakerphoneOn(bool(activar))
            self._log("Altaveu {0}.".format("activat" if activar else "desactivat"))
            return True
        except Exception as e:
            self._log("Avís: no s'ha pogut canviar l'altaveu: {0}".format(e))
            return False

    def aturar(self):
        self.actiu = False
        try:
            if self.mode == "android":
                if self._audio_record:
                    try:
                        self._audio_record.stop()
                    except Exception:
                        pass
                    try:
                        self._audio_record.release()
                    except Exception:
                        pass
                if self._audio_track:
                    try:
                        self._audio_track.stop()
                    except Exception:
                        pass
                    try:
                        self._audio_track.release()
                    except Exception:
                        pass
            elif self.mode == "pyaudio":
                if self._pyaudio_stream_in:
                    self._pyaudio_stream_in.stop_stream()
                    self._pyaudio_stream_in.close()
                if self._pyaudio_stream_out:
                    self._pyaudio_stream_out.stop_stream()
                    self._pyaudio_stream_out.close()
                if self._pyaudio_instance:
                    self._pyaudio_instance.terminate()
        except Exception as e:
            self._log("Avís alliberant recursos d'àudio: {0}".format(e))
        finally:
            self._audio_record = None
            self._audio_track = None
            self._pyaudio_stream_in = None
            self._pyaudio_stream_out = None
            self._pyaudio_instance = None


# ============================================================
#  LLIBRETA DE CONTACTES LOCAL (JSON al telèfon, NO és un servidor)
# ============================================================
class GestorContactes:
    """
    Agenda local senzilla (nom, IP, port) persistida en un fitxer JSON dins
    el directori privat de dades de l'app. Això és equivalent a com
    qualsevol app de trucades desa la seva llibreta d'adreces al telèfon;
    NO hi ha cap servidor remot ni sincronització involucrada.
    """

    def __init__(self, ruta_fitxer, callback_log=None):
        self.ruta_fitxer = ruta_fitxer
        self.callback_log = callback_log or log_consola
        self.contactes = []
        self._carregar()

    def _log(self, missatge):
        try:
            self.callback_log(missatge)
        except Exception:
            log_consola(missatge)

    def _carregar(self):
        try:
            if os.path.exists(self.ruta_fitxer):
                with open(self.ruta_fitxer, 'r', encoding='utf-8') as f:
                    dades = json.load(f)
                    if isinstance(dades, list):
                        self.contactes = dades
        except Exception as e:
            self._log("Avís: no s'han pogut carregar els contactes ({0}).".format(e))
            self.contactes = []

    def _desar(self):
        try:
            with open(self.ruta_fitxer, 'w', encoding='utf-8') as f:
                json.dump(self.contactes, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            self._log("ERROR desant els contactes: {0}".format(e))
            return False

    def afegir(self, nom, ip, port):
        contacte = {"nom": nom, "ip": ip, "port": int(port)}
        self.contactes.append(contacte)
        self._desar()
        return contacte

    def eliminar(self, index):
        if 0 <= index < len(self.contactes):
            del self.contactes[index]
            self._desar()

    def llistar(self):
        return list(self.contactes)


# ============================================================
#  CAPA DE XARXA: XarxaP2P
# ============================================================
class XarxaP2P:
    """
    Socket UDP + STUN + un ÚNIC fil d'escolta persistent. Deliberadament
    NOMÉS un fil fa recvfrom(): si n'hi hagués dos (per exemple un de
    control i un altre d'àudio), la recepció de paquets es repartiria de
    forma no determinista entre tots dos threads, fent "perdre" paquets de
    control o d'àudio de manera aleatòria. La capa d'aplicació decideix
    què fer amb cada paquet rebut.
    """

    def __init__(self, callback_log=None):
        self.callback_log = callback_log or log_consola
        self.socket_udp = None
        self._stop_escolta = threading.Event()
        self._fil_escolta = None

    def _log(self, missatge):
        try:
            self.callback_log(missatge)
        except Exception:
            log_consola(missatge)

    def crear_socket(self, port_local=0):
        if self.socket_udp is not None:
            try:
                self.socket_udp.close()
            except Exception:
                pass
            self.socket_udp = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port_local))
            s.settimeout(STUN_SOCKET_TIMEOUT)
            self.socket_udp = s
            self._log("Socket UDP creat al port local {0}.".format(s.getsockname()[1]))
            return True
        except OSError as e:
            self._log("ERROR creant el socket UDP: {0}".format(e))
            return False
        except Exception as e:
            self._log("ERROR inesperat creant el socket: {0}".format(e))
            return False

    def _construir_stun_binding_request(self):
        msg_type = 0x0001
        msg_length = 0x0000
        transaction_id = bytes(bytearray(random.getrandbits(8) for _ in range(12)))
        capcalera = struct.pack("!HHI12s", msg_type, msg_length,
                                 STUN_MAGIC_COOKIE, transaction_id)
        return capcalera, transaction_id

    def _parsejar_stun_response(self, dades, transaction_id_enviat):
        if len(dades) < 20:
            return None, None
        try:
            msg_type, msg_length, cookie, transaction_id = struct.unpack(
                "!HHI12s", dades[:20])
        except struct.error:
            return None, None
        if cookie != STUN_MAGIC_COOKIE or transaction_id != transaction_id_enviat:
            return None, None

        cos = dades[20:20 + msg_length]
        offset = 0
        ip_resultat, port_resultat = None, None
        while offset + 4 <= len(cos):
            try:
                attr_type, attr_len = struct.unpack("!HH", cos[offset:offset + 4])
            except struct.error:
                break
            valor = cos[offset + 4: offset + 4 + attr_len]
            if len(valor) < attr_len:
                break
            if attr_type == 0x0020 and len(valor) >= 8:
                familia = valor[1] if isinstance(valor[1], int) else ord(valor[1])
                if familia == 0x01:
                    port_xor = struct.unpack("!H", valor[2:4])[0]
                    port_resultat = port_xor ^ (STUN_MAGIC_COOKIE >> 16)
                    ip_xor = struct.unpack("!I", valor[4:8])[0]
                    ip_int = ip_xor ^ STUN_MAGIC_COOKIE
                    ip_resultat = socket.inet_ntoa(struct.pack("!I", ip_int))
            elif attr_type == 0x0001 and len(valor) >= 8 and ip_resultat is None:
                port_resultat = struct.unpack("!H", valor[2:4])[0]
                ip_resultat = socket.inet_ntoa(valor[4:8])
            avanc = attr_len + ((4 - attr_len % 4) % 4)
            offset += 4 + avanc
        return ip_resultat, port_resultat

    def consultar_stun(self):
        if self.socket_udp is None:
            self._log("ERROR: cal crear el socket abans de consultar l'STUN.")
            return None, None
        self._log("Consultant servidor STUN ({0}:{1})...".format(STUN_HOST, STUN_PORT))
        for intent in range(1, 4):
            capcalera, transaction_id = self._construir_stun_binding_request()
            try:
                self.socket_udp.sendto(capcalera, (STUN_HOST, STUN_PORT))
                dades, _ = self.socket_udp.recvfrom(2048)
                ip, port = self._parsejar_stun_response(dades, transaction_id)
                if ip and port:
                    self._log("STUN OK -> IP pública: {0}  Port públic: {1}".format(ip, port))
                    return ip, port
                self._log("Resposta STUN inesperada, reintentant ({0}/3)...".format(intent))
            except socket.timeout:
                self._log("Timeout consultant STUN (intent {0}/3).".format(intent))
            except socket.gaierror as e:
                self._log("ERROR de resolució DNS de l'STUN: {0}".format(e))
                break
            except OSError as e:
                self._log("Error de xarxa consultant STUN: {0}".format(e))
            except Exception as e:
                self._log("Error inesperat consultant STUN: {0}".format(e))
        self._log("No s'ha pogut obtenir la IP pública via STUN.")
        return None, None

    def iniciar_escolta(self, callback_paquet):
        if self.socket_udp is None:
            self._log("ERROR: no es pot escoltar sense un socket creat.")
            return
        self._stop_escolta.clear()
        self._fil_escolta = threading.Thread(
            target=self._bucle_escolta, args=(callback_paquet,), daemon=True)
        self._fil_escolta.start()

    def _bucle_escolta(self, callback_paquet):
        try:
            self.socket_udp.settimeout(1.0)
        except Exception:
            pass
        while not self._stop_escolta.is_set():
            try:
                dades, origen = self.socket_udp.recvfrom(4096)
                if not dades:
                    continue
                tipus = dades[0:1]
                payload = dades[1:]
                try:
                    callback_paquet(tipus, payload, origen[0], origen[1])
                except Exception as e:
                    self._log("Error processant paquet rebut: {0}".format(e))
            except socket.timeout:
                continue
            except OSError as e:
                self._log("Avís de xarxa al bucle d'escolta: {0}".format(e))
            except Exception as e:
                self._log("Error inesperat al bucle d'escolta: {0}".format(e))
        self._log("Bucle d'escolta de xarxa aturat.")

    def enviar(self, tipus_byte, payload=b'', destinatari=None):
        if self.socket_udp is None or destinatari is None:
            return False
        try:
            if isinstance(payload, str):
                payload = payload.encode('utf-8')
            self.socket_udp.sendto(tipus_byte + payload, destinatari)
            return True
        except OSError as e:
            self._log("Avís enviant paquet: {0}".format(e))
            return False
        except Exception as e:
            self._log("Error inesperat enviant paquet: {0}".format(e))
            return False

    def tancar(self):
        self._stop_escolta.set()
        if self.socket_udp:
            try:
                self.socket_udp.close()
            except Exception:
                pass
            self.socket_udp = None


# ============================================================
#  PANTALLES (cada una construeix només el seu propi layout)
# ============================================================
class PantallaInici(Screen):
    def __init__(self, **kwargs):
        super(PantallaInici, self).__init__(**kwargs)
        arrel = BoxLayout(orientation='vertical', padding=14, spacing=8)

        arrel.add_widget(Label(text="WALKIE-TALKIE P2P", font_size='24sp', bold=True,
                                color=COLOR_TEXT, size_hint=(1, 0.07)))

        self.input_nom = TextInput(
            hint_text="El teu nom (es mostrarà a qui truquis)",
            multiline=False, size_hint=(1, 0.07),
            background_color=COLOR_CAIXA_CLARA, font_size='16sp')
        arrel.add_widget(self.input_nom)

        arrel.add_widget(Label(text="LA TEVA ADREÇA:", size_hint=(1, 0.05),
                                color=COLOR_TEXT, bold=True, halign='left'))
        self.input_ip_local = TextInput(
            text="Prem 'DESCOBRIR LA MEVA IP'...", readonly=True, multiline=False,
            halign='center', font_size='18sp', size_hint=(1, 0.08),
            background_color=COLOR_CAIXA_CLARA)
        arrel.add_widget(self.input_ip_local)

        self.btn_stun = _crear_boto("DESCOBRIR LA MEVA IP (STUN)", COLOR_TARONJA, '16sp')
        self.btn_stun.size_hint = (1, 0.08)
        arrel.add_widget(self.btn_stun)

        fila_nav = BoxLayout(orientation='horizontal', spacing=10, size_hint=(1, 0.14))
        self.btn_anar_contactes = _crear_boto("📇 CONTACTES", COLOR_BLAU, '18sp')
        self.btn_anar_teclat = _crear_boto("⌨️ TRUCADA MANUAL", COLOR_BLAU, '18sp')
        fila_nav.add_widget(self.btn_anar_contactes)
        fila_nav.add_widget(self.btn_anar_teclat)
        arrel.add_widget(fila_nav)

        arrel.add_widget(Label(text="REGISTRE D'ACTIVITAT:", size_hint=(1, 0.04),
                                color=COLOR_TEXT, bold=True, halign='left'))
        scroll = ScrollView(size_hint=(1, 0.35))
        self.label_log = Label(text="", size_hint_y=None, halign='left', valign='top',
                                color=COLOR_TEXT_TENUE, font_size='12sp')
        self.label_log.bind(width=lambda inst, val: setattr(inst, 'text_size', (val, None)))
        self.label_log.bind(texture_size=lambda inst, val: setattr(inst, 'height', val[1]))
        scroll.add_widget(self.label_log)
        arrel.add_widget(scroll)

        self.add_widget(arrel)


class PantallaContactes(Screen):
    def __init__(self, **kwargs):
        super(PantallaContactes, self).__init__(**kwargs)
        arrel = BoxLayout(orientation='vertical', padding=14, spacing=8)

        capcalera = BoxLayout(orientation='horizontal', size_hint=(1, 0.08), spacing=8)
        self.btn_tornar = _crear_boto("⬅", COLOR_BLAU, '18sp')
        self.btn_tornar.size_hint = (0.2, 1)
        capcalera.add_widget(self.btn_tornar)
        capcalera.add_widget(Label(text="CONTACTES", font_size='20sp', bold=True, color=COLOR_TEXT))
        arrel.add_widget(capcalera)

        self.scroll = ScrollView(size_hint=(1, 0.78))
        self.llista = BoxLayout(orientation='vertical', size_hint_y=None, spacing=6)
        self.llista.bind(minimum_height=self.llista.setter('height'))
        self.scroll.add_widget(self.llista)
        arrel.add_widget(self.scroll)

        self.btn_afegir = _crear_boto("➕ AFEGIR CONTACTE NOU", COLOR_VERD, '18sp')
        self.btn_afegir.size_hint = (1, 0.1)
        arrel.add_widget(self.btn_afegir)

        self.add_widget(arrel)

    def actualitzar_llista(self, contactes, on_trucar, on_eliminar):
        self.llista.clear_widgets()
        if not contactes:
            self.llista.add_widget(Label(
                text="Encara no tens cap contacte desat.\nPrem '+ AFEGIR CONTACTE NOU'.",
                color=COLOR_TEXT_TENUE, size_hint_y=None, height=60))
            return
        for index, contacte in enumerate(contactes):
            fila = BoxLayout(orientation='horizontal', size_hint_y=None, height=58, spacing=6)
            info = Label(
                text="{0}\n{1}:{2}".format(
                    contacte.get('nom', '(sense nom)'),
                    contacte.get('ip', '?'), contacte.get('port', '?')),
                color=COLOR_TEXT, font_size='14sp', halign='left', valign='middle',
                size_hint=(0.6, 1))
            info.bind(size=lambda inst, val: setattr(inst, 'text_size', val))
            btn_trucar = _crear_boto("📞", COLOR_VERD, '18sp')
            btn_trucar.size_hint = (0.2, 1)
            btn_eliminar = _crear_boto("🗑", COLOR_VERMELL, '18sp')
            btn_eliminar.size_hint = (0.2, 1)
            btn_trucar.bind(on_press=lambda inst, c=contacte: on_trucar(c))
            btn_eliminar.bind(on_press=lambda inst, i=index: on_eliminar(i))
            fila.add_widget(info)
            fila.add_widget(btn_trucar)
            fila.add_widget(btn_eliminar)
            self.llista.add_widget(fila)


class PantallaTeclat(Screen):
    def __init__(self, **kwargs):
        super(PantallaTeclat, self).__init__(**kwargs)
        arrel = BoxLayout(orientation='vertical', padding=14, spacing=8)

        capcalera = BoxLayout(orientation='horizontal', size_hint=(1, 0.08), spacing=8)
        self.btn_tornar = _crear_boto("⬅", COLOR_BLAU, '18sp')
        self.btn_tornar.size_hint = (0.2, 1)
        capcalera.add_widget(self.btn_tornar)
        capcalera.add_widget(Label(text="TRUCADA MANUAL", font_size='20sp', bold=True,
                                    color=COLOR_TEXT))
        arrel.add_widget(capcalera)

        arrel.add_widget(Label(text="Nom del contacte (opcional, per desar-lo):",
                                size_hint=(1, 0.05), color=COLOR_TEXT, halign='left'))
        self.input_nom_contacte = TextInput(
            hint_text="Ex: L'Arnau", multiline=False, size_hint=(1, 0.08),
            background_color=COLOR_CAIXA_CLARA)
        arrel.add_widget(self.input_nom_contacte)

        arrel.add_widget(Label(text="IP i Port del teu amic:", size_hint=(1, 0.05),
                                color=COLOR_TEXT, halign='left'))
        fila_inputs = BoxLayout(orientation='horizontal', size_hint=(1, 0.08), spacing=8)
        self.input_ip = TextInput(
            hint_text="IP (ex: 82.34.12.55)", multiline=False, size_hint=(0.6, 1),
            background_color=COLOR_CAIXA_CLARA, font_size='18sp')
        self.input_port = TextInput(
            hint_text="Port", multiline=False, input_filter='int', size_hint=(0.4, 1),
            background_color=COLOR_CAIXA_CLARA, font_size='18sp')
        fila_inputs.add_widget(self.input_ip)
        fila_inputs.add_widget(self.input_port)
        arrel.add_widget(fila_inputs)

        self.graella_teclat = self._crear_teclat_numeric()
        arrel.add_widget(self.graella_teclat)

        self.btn_trucar = _crear_boto("📞 TRUCAR ARA", COLOR_BLAU, '20sp')
        self.btn_trucar.size_hint = (1, 0.11)
        arrel.add_widget(self.btn_trucar)

        self.btn_desar = _crear_boto("💾 DESAR COM A CONTACTE", COLOR_VERD, '16sp')
        self.btn_desar.size_hint = (1, 0.09)
        arrel.add_widget(self.btn_desar)

        self.add_widget(arrel)

    def _crear_teclat_numeric(self):
        """Teclat numèric tàctil per introduir IP/Port còmodament en pantalles antigues."""
        graella = GridLayout(cols=3, size_hint=(1, 0.30), spacing=4)
        for tecla in ['1', '2', '3', '4', '5', '6', '7', '8', '9', '.', '0', '⌫']:
            btn = Button(text=tecla, background_normal='', background_color=COLOR_TECLA,
                         color=(1, 1, 1, 1), font_size='18sp')
            btn.bind(on_press=self._prem_tecla)
            graella.add_widget(btn)
        return graella

    def _prem_tecla(self, instance):
        tecla = instance.text
        camp = self.input_ip if self.input_ip.focus else self.input_port
        if tecla == '⌫':
            camp.text = camp.text[:-1]
        elif tecla == '.' and camp is self.input_port:
            return  # el port no porta punts
        else:
            camp.text += tecla


class PantallaTrucada(Screen):
    """
    Una única pantalla el contingut de la qual canvia segons l'estat de la
    trucada (Trucant / Entrant / Activa) mitjançant mostrar_panell().
    """

    def __init__(self, **kwargs):
        super(PantallaTrucada, self).__init__(**kwargs)
        arrel = BoxLayout(orientation='vertical', padding=24, spacing=12)

        self.label_nom = Label(text="", font_size='30sp', bold=True,
                                color=COLOR_TEXT, size_hint=(1, 0.20))
        arrel.add_widget(self.label_nom)

        self.label_estat = Label(text="", font_size='20sp', bold=True,
                                  color=COLOR_TEXT, size_hint=(1, 0.10))
        arrel.add_widget(self.label_estat)

        self.label_durada = Label(text="", font_size='16sp',
                                   color=COLOR_TEXT_TENUE, size_hint=(1, 0.08))
        arrel.add_widget(self.label_durada)

        self.indicador_activitat = Label(text="", font_size='16sp',
                                          color=COLOR_TEXT_TENUE, size_hint=(1, 0.10))
        arrel.add_widget(self.indicador_activitat)

        arrel.add_widget(BoxLayout(size_hint=(1, 0.12)))  # espai flexible

        self.panell_botons = BoxLayout(orientation='vertical', spacing=10, size_hint=(1, 0.40))
        arrel.add_widget(self.panell_botons)

        self.add_widget(arrel)

    def mostrar_panell(self, mode, app):
        """mode: 'trucant' | 'entrant' | 'activa'. `app` aporta els botons compartits."""
        self.panell_botons.clear_widgets()
        self.indicador_activitat.text = ""
        self.label_durada.text = ""

        if mode == 'trucant':
            self.panell_botons.add_widget(app.btn_cancelar)
        elif mode == 'entrant':
            fila = BoxLayout(orientation='horizontal', spacing=10, size_hint=(1, 1))
            fila.add_widget(app.btn_despenjar)
            fila.add_widget(app.btn_rebutjar)
            self.panell_botons.add_widget(fila)
        elif mode == 'activa':
            self.panell_botons.add_widget(app.btn_altaveu)
            self.panell_botons.add_widget(app.btn_penjar)


class PantallaPenjat(Screen):
    def __init__(self, **kwargs):
        super(PantallaPenjat, self).__init__(**kwargs)
        arrel = BoxLayout(orientation='vertical', padding=24, spacing=14)

        arrel.add_widget(Label(text="TRUCADA FINALITZADA", font_size='26sp', bold=True,
                                color=COLOR_TEXT, size_hint=(1, 0.2)))
        self.label_nom = Label(text="", font_size='22sp', color=COLOR_TEXT, size_hint=(1, 0.15))
        arrel.add_widget(self.label_nom)
        self.label_motiu = Label(text="", font_size='16sp', color=COLOR_TEXT_TENUE,
                                  size_hint=(1, 0.15))
        arrel.add_widget(self.label_motiu)
        self.label_durada = Label(text="", font_size='16sp', color=COLOR_TEXT_TENUE,
                                   size_hint=(1, 0.15))
        arrel.add_widget(self.label_durada)
        arrel.add_widget(BoxLayout(size_hint=(1, 0.15)))
        self.btn_tornar = _crear_boto("⬅ TORNAR A L'INICI", COLOR_BLAU, '20sp')
        self.btn_tornar.size_hint = (1, 0.2)
        arrel.add_widget(self.btn_tornar)

        self.add_widget(arrel)


# ============================================================
#  APLICACIÓ KIVY
# ============================================================
class WalkieTalkieApp(App):
    """Cablejat entre pantalles + màquina d'estats de la trucada."""

    def build(self):
        Window.clearcolor = COLOR_FONS
        self.title = "Walkie-Talkie P2P"

        # ---- Estat intern ----
        self.estat = 'REPOS'
        self.xarxa = None
        self.peer_ip = None
        self.peer_port = None
        self.nom_remot = ""
        self.altaveu_activat = False
        self.linies_log = []
        self._hora_inici_trucada = None
        self._parpelleig_event = None
        self._cronometre_event = None
        self._pantalla_retorn = 'inici'
        self._event_trucant_actiu = threading.Event()
        self._event_trucada_activa = threading.Event()
        self._cua_audio_entrant = queue.Queue(maxsize=50)

        self.gestor_so = GestorSo(callback_log=self._log_extern)
        self.gestor_audio = GestorAudio(callback_log=self._log_extern)
        ruta_contactes = os.path.join(self.user_data_dir, "contactes.json")
        self.gestor_contactes = GestorContactes(ruta_contactes, callback_log=self._log_extern)

        # ---- Botons compartits entre estats de trucada (es creen un sol cop) ----
        self.btn_cancelar = _crear_boto("CANCEL·LAR", COLOR_VERMELL, '20sp')
        self.btn_cancelar.bind(on_press=self.on_cancelar_pressed)
        self.btn_despenjar = _crear_boto("✅ DESPENJAR", COLOR_VERD, '20sp')
        self.btn_despenjar.bind(on_press=self.on_despenjar_pressed)
        self.btn_rebutjar = _crear_boto("❌ REBUTJAR", COLOR_VERMELL, '20sp')
        self.btn_rebutjar.bind(on_press=self.on_rebutjar_pressed)
        self.btn_penjar = _crear_boto("📵 PENJAR", COLOR_VERMELL, '20sp')
        self.btn_penjar.bind(on_press=self.on_penjar_pressed)
        self.btn_altaveu = _crear_boto("🔊 ALTAVEU: OFF", COLOR_BLAU, '18sp')
        self.btn_altaveu.bind(on_press=self.on_altaveu_pressed)

        # ---- Pantalles ----
        self.pantalla_inici = PantallaInici(name='inici')
        self.pantalla_contactes = PantallaContactes(name='contactes')
        self.pantalla_teclat = PantallaTeclat(name='teclat')
        self.pantalla_trucada = PantallaTrucada(name='trucada')
        self.pantalla_penjat = PantallaPenjat(name='penjat')

        self.sm = ScreenManager(transition=NoTransition())
        for pantalla in (self.pantalla_inici, self.pantalla_contactes, self.pantalla_teclat,
                          self.pantalla_trucada, self.pantalla_penjat):
            self.sm.add_widget(pantalla)

        # ---- Vinculacions ----
        self.pantalla_inici.btn_stun.bind(on_press=self.on_stun_pressed)
        self.pantalla_inici.btn_anar_contactes.bind(
            on_press=lambda inst: self._anar_a_pantalla('contactes', origen='inici'))
        self.pantalla_inici.btn_anar_teclat.bind(
            on_press=lambda inst: self._anar_a_pantalla('teclat', origen='inici'))

        self.pantalla_contactes.btn_tornar.bind(on_press=lambda inst: self._tornar_enrere())
        self.pantalla_contactes.btn_afegir.bind(
            on_press=lambda inst: self._anar_a_pantalla('teclat', origen='contactes'))

        self.pantalla_teclat.btn_tornar.bind(on_press=lambda inst: self._tornar_enrere())
        self.pantalla_teclat.btn_trucar.bind(on_press=self.on_trucar_des_de_teclat)
        self.pantalla_teclat.btn_desar.bind(on_press=self.on_desar_contacte)

        self.pantalla_penjat.btn_tornar.bind(
            on_press=lambda inst: self._tornar_a_inici_des_de_penjat())

        self._entrar_estat_repos()
        self.sm.current = 'inici'
        self._actualitzar_llista_contactes()
        return self.sm

    # ============================================================
    #  NAVEGACIÓ ENTRE PANTALLES
    # ============================================================
    def _anar_a_pantalla(self, nom, origen=None):
        if origen:
            self._pantalla_retorn = origen
        if nom == 'contactes':
            self._actualitzar_llista_contactes()
        self.sm.current = nom

    def _tornar_enrere(self):
        self.sm.current = self._pantalla_retorn or 'inici'

    # ============================================================
    #  UTILITAT THREAD-SAFE PER TOCAR LA UI
    # ============================================================
    def _ui(self, funcio, *args, **kwargs):
        Clock.schedule_once(lambda dt: funcio(*args, **kwargs))

    def _log_extern(self, missatge):
        self._ui(self.afegir_log, missatge)

    def afegir_log(self, missatge):
        marca = time.strftime("%H:%M:%S")
        self.linies_log.append("[{0}] {1}".format(marca, missatge))
        self.linies_log = self.linies_log[-MAX_LINIES_LOG:]
        self.pantalla_inici.label_log.text = "\n".join(self.linies_log)

    def _obtenir_nom_local(self):
        try:
            return self.pantalla_inici.input_nom.text.strip() or "Anònim"
        except Exception:
            return "Anònim"

    # ============================================================
    #  CONTACTES
    # ============================================================
    def _actualitzar_llista_contactes(self):
        self.pantalla_contactes.actualitzar_llista(
            self.gestor_contactes.llistar(),
            on_trucar=self._trucar_des_de_contacte,
            on_eliminar=self._eliminar_contacte)

    def _trucar_des_de_contacte(self, contacte):
        self.iniciar_trucada_a(
            contacte.get('ip', ''), str(contacte.get('port', '')),
            nom_hint=contacte.get('nom'))

    def _eliminar_contacte(self, index):
        self.gestor_contactes.eliminar(index)
        self.afegir_log("Contacte eliminat.")
        self._actualitzar_llista_contactes()

    def on_desar_contacte(self, instance):
        nom = self.pantalla_teclat.input_nom_contacte.text.strip()
        ip = self.pantalla_teclat.input_ip.text.strip()
        port = self.pantalla_teclat.input_port.text.strip()
        if not nom:
            self.afegir_log("ERROR: posa un nom per desar el contacte.")
            return
        try:
            socket.inet_aton(ip)
            port_int = int(port)
            if not (0 < port_int < 65536):
                raise ValueError("Port fora de rang")
        except (OSError, ValueError):
            self.afegir_log("ERROR: IP o port no vàlids; no s'ha desat el contacte.")
            return
        self.gestor_contactes.afegir(nom, ip, port_int)
        self.afegir_log("Contacte '{0}' desat correctament.".format(nom))
        self._anar_a_pantalla('contactes')

    # ============================================================
    #  BOTÓ: DESCOBRIR LA MEVA IP (STUN)
    # ============================================================
    def on_stun_pressed(self, instance):
        if self.estat != 'REPOS':
            self.afegir_log("ERROR: no es pot reiniciar la xarxa durant una trucada.")
            return
        self.pantalla_inici.btn_stun.disabled = True
        self.pantalla_inici.btn_stun.text = "CONSULTANT STUN..."
        threading.Thread(target=self._flux_stun, daemon=True).start()

    def _flux_stun(self):
        self.xarxa = XarxaP2P(callback_log=self._log_extern)
        if not self.xarxa.crear_socket():
            self._ui(self.afegir_log, "No s'ha pogut crear el socket UDP.")
            self._ui(self._restaurar_boto_stun)
            return
        ip, port = self.xarxa.consultar_stun()
        if ip and port:
            self._ui(self._actualitza_ip_local, ip, port)
            self.xarxa.iniciar_escolta(self._paquet_rebut)
            self._ui(self.afegir_log, "Xarxa activa: escoltant trucades entrants...")
        self._ui(self._restaurar_boto_stun)

    def _actualitza_ip_local(self, ip, port):
        self.pantalla_inici.input_ip_local.text = "{0}:{1}".format(ip, port)

    def _restaurar_boto_stun(self):
        self.pantalla_inici.btn_stun.disabled = False
        self.pantalla_inici.btn_stun.text = "DESCOBRIR LA MEVA IP (STUN)"

    # ============================================================
    #  INICIAR TRUCADA (comú per a Teclat i Contactes)
    # ============================================================
    def on_trucar_des_de_teclat(self, instance):
        ip = self.pantalla_teclat.input_ip.text
        port = self.pantalla_teclat.input_port.text
        nom = self.pantalla_teclat.input_nom_contacte.text.strip() or None
        self.iniciar_trucada_a(ip, port, nom_hint=nom)

    def iniciar_trucada_a(self, ip_text, port_text, nom_hint=None):
        if self.xarxa is None or self.xarxa.socket_udp is None:
            self.afegir_log("ERROR: primer descobreix la teva IP a la pantalla d'inici.")
            return False
        if self.estat != 'REPOS':
            self.afegir_log("ERROR: ja hi ha una trucada en curs.")
            return False

        ip_desti = (ip_text or "").strip()
        port_txt = (port_text or "").strip()
        if not ip_desti or not port_txt:
            self.afegir_log("ERROR: cal indicar la IP i el Port.")
            return False
        try:
            socket.inet_aton(ip_desti)
        except OSError:
            self.afegir_log("ERROR: la IP introduïda no té un format vàlid.")
            return False
        try:
            port_desti = int(port_txt)
            if not (0 < port_desti < 65536):
                raise ValueError("Port fora de rang")
        except ValueError:
            self.afegir_log("ERROR: el port ha de ser un número entre 1 i 65535.")
            return False

        self.peer_ip, self.peer_port = ip_desti, port_desti
        self._ui(self._entrar_estat_trucant, ip_desti, port_desti, nom_hint)
        threading.Thread(target=self._bucle_trucar, args=(ip_desti, port_desti), daemon=True).start()
        return True

    def _bucle_trucar(self, ip, port):
        self._event_trucant_actiu.set()
        payload_nom = self._obtenir_nom_local().encode('utf-8')
        temps_inici = time.time()
        while self._event_trucant_actiu.is_set():
            if (time.time() - temps_inici) > TRUCAR_TIMEOUT:
                self._event_trucant_actiu.clear()
                self._ui(self._entrar_estat_penjat, "No hi ha resposta.")
                break
            self.xarxa.enviar(TIPUS_PUNCH, payload_nom, destinatari=(ip, port))
            time.sleep(HOLEPUNCH_INTERVAL)

    # ============================================================
    #  BOTONS DE CONTROL DE TRUCADA
    # ============================================================
    def on_cancelar_pressed(self, instance):
        self._event_trucant_actiu.clear()
        if self.peer_ip:
            self.xarxa.enviar(TIPUS_CANCEL, destinatari=(self.peer_ip, self.peer_port))
        self._ui(self._entrar_estat_penjat, "Trucada cancel·lada per tu.")

    def on_despenjar_pressed(self, instance):
        if self.estat != 'ENTRANT':
            return
        nom_meu = self._obtenir_nom_local().encode('utf-8')
        self.xarxa.enviar(TIPUS_ACK, nom_meu, destinatari=(self.peer_ip, self.peer_port))
        self._ui(self.afegir_log, "Trucada acceptada.")
        self._ui(self._entrar_estat_activa)
        self._iniciar_fils_audio()

    def on_rebutjar_pressed(self, instance):
        if self.estat != 'ENTRANT':
            return
        self.xarxa.enviar(TIPUS_REJECT, destinatari=(self.peer_ip, self.peer_port))
        self._ui(self._entrar_estat_penjat, "Has rebutjat la trucada.")

    def on_penjar_pressed(self, instance):
        if self.estat != 'ACTIVA':
            return
        self.xarxa.enviar(TIPUS_BYE, destinatari=(self.peer_ip, self.peer_port))
        self._aturar_trucada_activa()
        self._ui(self._entrar_estat_penjat, "Has penjat la trucada.")

    def on_altaveu_pressed(self, instance):
        self.altaveu_activat = not self.altaveu_activat
        ok = self.gestor_audio.activar_altaveu(self.altaveu_activat)
        self.btn_altaveu.text = "🔊 ALTAVEU: {0}".format("ON" if self.altaveu_activat else "OFF")
        if not ok:
            self.afegir_log("Avís: el canvi d'altaveu no té efecte real en aquest entorn.")

    # ============================================================
    #  ESTATS DE LA MÀQUINA DE TRUCADES
    # ============================================================
    def _entrar_estat_repos(self):
        self.estat = 'REPOS'
        self.peer_ip = None
        self.peer_port = None
        self.nom_remot = ""
        self.gestor_so.aturar_tot()
        self._programar_parpelleig(False)
        self._programar_cronometre(False)
        self.afegir_log("Estat: REPÒS.")

    def _entrar_estat_trucant(self, ip, port, nom_hint=None):
        self.estat = 'TRUCANT'
        self.nom_remot = nom_hint or ""
        identificacio = nom_hint if nom_hint else "{0}:{1}".format(ip, port)
        self.pantalla_trucada.label_nom.text = identificacio
        self.pantalla_trucada.label_estat.text = "TRUCANT..."
        self.pantalla_trucada.label_estat.color = COLOR_TEXT
        self.pantalla_trucada.mostrar_panell('trucant', self)
        self.gestor_so.reproduir_sortint()
        self.sm.current = 'trucada'
        self.afegir_log("Trucant a {0} ({1}:{2})...".format(identificacio, ip, port))

    def _entrar_estat_entrant(self, nom_remot, ip, port):
        self.estat = 'ENTRANT'
        self.peer_ip, self.peer_port = ip, port
        self.nom_remot = nom_remot or "Algú"
        self.pantalla_trucada.label_nom.text = self.nom_remot
        self.pantalla_trucada.label_estat.text = "ET TRUCA!"
        self.pantalla_trucada.label_estat.color = COLOR_TARONJA
        self.pantalla_trucada.mostrar_panell('entrant', self)
        self.gestor_so.reproduir_entrant()
        self._programar_parpelleig(True)
        self.sm.current = 'trucada'
        self.afegir_log("Trucada entrant de {0} ({1}:{2}).".format(self.nom_remot, ip, port))

    def _entrar_estat_activa(self):
        self.estat = 'ACTIVA'
        self._hora_inici_trucada = time.time()
        self.altaveu_activat = False
        self.gestor_so.aturar_tot()
        self._programar_parpelleig(False)
        self.pantalla_trucada.label_nom.text = self.nom_remot or "{0}:{1}".format(
            self.peer_ip, self.peer_port)
        self.pantalla_trucada.label_estat.text = "CONVERSA ACTIVA — PARLA ARA"
        self.pantalla_trucada.label_estat.color = COLOR_VERD
        self.pantalla_trucada.indicador_activitat.text = "🎙️ Nivell de veu: [░░░░░░░░░░]"
        self.btn_altaveu.text = "🔊 ALTAVEU: OFF"
        self.pantalla_trucada.mostrar_panell('activa', self)
        self.sm.current = 'trucada'
        self._programar_cronometre(True)
        self.afegir_log("Trucada connectada amb {0}:{1}. Àudio en directe actiu.".format(
            self.peer_ip, self.peer_port))

    def _entrar_estat_penjat(self, motiu):
        nom_per_mostrar = self.nom_remot or (
            "{0}:{1}".format(self.peer_ip, self.peer_port) if self.peer_ip else "")
        durada = 0
        if self._hora_inici_trucada is not None:
            durada = max(0, int(time.time() - self._hora_inici_trucada))
        self.estat = 'PENJAT'
        self._hora_inici_trucada = None
        self.gestor_so.aturar_tot()
        self._programar_parpelleig(False)
        self._programar_cronometre(False)

        minuts, segons = divmod(durada, 60)
        self.pantalla_penjat.label_nom.text = nom_per_mostrar
        self.pantalla_penjat.label_motiu.text = motiu
        self.pantalla_penjat.label_durada.text = "Durada: {0:02d}:{1:02d}".format(minuts, segons)

        self.sm.current = 'penjat'
        self.afegir_log(motiu)
        Clock.schedule_once(self._tornar_a_inici_des_de_penjat, 5)

    def _tornar_a_inici_des_de_penjat(self, dt=None):
        if self.estat == 'PENJAT':
            self._entrar_estat_repos()
            self.sm.current = 'inici'

    def _programar_parpelleig(self, activar):
        if activar:
            if self._parpelleig_event is None:
                self._parpelleig_event = Clock.schedule_interval(self._toggle_parpelleig, 0.5)
        else:
            if self._parpelleig_event is not None:
                self._parpelleig_event.cancel()
                self._parpelleig_event = None
            self.pantalla_trucada.label_estat.opacity = 1

    def _toggle_parpelleig(self, dt):
        lbl = self.pantalla_trucada.label_estat
        lbl.opacity = 0.35 if lbl.opacity > 0.9 else 1.0

    def _programar_cronometre(self, activar):
        if activar:
            if self._cronometre_event is None:
                self._cronometre_event = Clock.schedule_interval(self._actualitzar_cronometre, 1.0)
        else:
            if self._cronometre_event is not None:
                self._cronometre_event.cancel()
                self._cronometre_event = None

    def _actualitzar_cronometre(self, dt):
        if self._hora_inici_trucada is None:
            return
        durada = int(time.time() - self._hora_inici_trucada)
        minuts, segons = divmod(durada, 60)
        self.pantalla_trucada.label_durada.text = "{0:02d}:{1:02d}".format(minuts, segons)

    def _actualitzar_indicador(self, dades):
        try:
            n_mostres = len(dades) // 2
            if n_mostres == 0:
                self.pantalla_trucada.indicador_activitat.text = "🎙️ Nivell de veu: [░░░░░░░░░░]"
                return
            pas = max(1, n_mostres // 40)
            total, comptats = 0, 0
            for i in range(0, n_mostres, pas):
                valor = struct.unpack_from('<h', dades, i * 2)[0]
                total += abs(valor)
                comptats += 1
            amplitud_mitjana = (total / comptats) if comptats else 0
            nivell = min(10, int((amplitud_mitjana / 32768.0) * 60))
            barra = "█" * nivell + "░" * (10 - nivell)
            self.pantalla_trucada.indicador_activitat.text = "🎙️ Nivell de veu: [{0}]".format(barra)
        except Exception:
            pass

    # ============================================================
    #  FILS D'ÀUDIO EN DIRECTE
    # ============================================================
    def _iniciar_fils_audio(self):
        if not self.gestor_audio.iniciar():
            self._ui(self.afegir_log,
                     "AVÍS: sense maquinari d'àudio disponible; la trucada "
                     "continuarà activa però sense veu real.")
        self._event_trucada_activa.set()
        threading.Thread(target=self._bucle_enviament_audio, daemon=True).start()
        threading.Thread(target=self._bucle_reproduccio_audio, daemon=True).start()

    def _bucle_enviament_audio(self):
        while self._event_trucada_activa.is_set() and self.estat == 'ACTIVA':
            frame = self.gestor_audio.capturar_frame()
            if frame:
                self.xarxa.enviar(TIPUS_AUDIO, frame, destinatari=(self.peer_ip, self.peer_port))
            else:
                time.sleep(0.01)
        self._log_extern("Fil d'enviament d'àudio aturat.")

    def _bucle_reproduccio_audio(self):
        while self._event_trucada_activa.is_set() and self.estat == 'ACTIVA':
            try:
                dades = self._cua_audio_entrant.get(timeout=1.0)
            except queue.Empty:
                continue
            self.gestor_audio.reproduir_frame(dades)
            self._ui(self._actualitzar_indicador, dades)
        self._log_extern("Fil de recepció d'àudio aturat.")

    def _aturar_trucada_activa(self):
        self._event_trucada_activa.clear()
        try:
            self.gestor_audio.aturar()
        except Exception:
            pass
        try:
            while True:
                self._cua_audio_entrant.get_nowait()
        except queue.Empty:
            pass

    # ============================================================
    #  DISPATCHER DE PAQUETS DE XARXA (fil d'escolta persistent!)
    # ============================================================
    def _paquet_rebut(self, tipus, payload, ip_origen, port_origen):
        if tipus == TIPUS_PUNCH:
            nom_remot = payload.decode('utf-8', errors='ignore').strip() if payload else "Algú"
            if self.estat == 'REPOS':
                self._ui(self._entrar_estat_entrant, nom_remot, ip_origen, port_origen)
            elif self.estat == 'ENTRANT' and ip_origen == self.peer_ip:
                pass
            elif self.estat in ('TRUCANT', 'ACTIVA'):
                self.xarxa.enviar(TIPUS_REJECT, destinatari=(ip_origen, port_origen))

        elif tipus == TIPUS_CANCEL:
            if self.estat == 'ENTRANT' and ip_origen == self.peer_ip:
                self._ui(self._entrar_estat_penjat,
                          "{0} ha cancel·lat la trucada.".format(self.nom_remot))

        elif tipus == TIPUS_ACK:
            if (self.estat == 'TRUCANT' and ip_origen == self.peer_ip
                    and port_origen == self.peer_port):
                nom_confirmat = payload.decode('utf-8', errors='ignore').strip() if payload else ""
                if nom_confirmat:
                    self.nom_remot = nom_confirmat
                self._event_trucant_actiu.clear()
                self._ui(self._entrar_estat_activa)
                self._iniciar_fils_audio()

        elif tipus == TIPUS_REJECT:
            if (self.estat == 'TRUCANT' and ip_origen == self.peer_ip
                    and port_origen == self.peer_port):
                self._event_trucant_actiu.clear()
                self._ui(self._entrar_estat_penjat,
                          "La trucada ha estat rebutjada (o l'altre està ocupat).")

        elif tipus == TIPUS_BYE:
            if (self.estat == 'ACTIVA' and ip_origen == self.peer_ip
                    and port_origen == self.peer_port):
                self._aturar_trucada_activa()
                self._ui(self._entrar_estat_penjat, "L'altre usuari ha penjat.")

        elif tipus == TIPUS_AUDIO:
            if (self.estat == 'ACTIVA' and ip_origen == self.peer_ip
                    and port_origen == self.peer_port):
                try:
                    self._cua_audio_entrant.put_nowait(payload)
                except queue.Full:
                    pass

    # ============================================================
    #  TANCAMENT DE L'APLICACIÓ
    # ============================================================
    def on_stop(self):
        self._event_trucant_actiu.clear()
        self._event_trucada_activa.clear()
        try:
            self.gestor_audio.aturar()
        except Exception:
            pass
        try:
            self.gestor_so.aturar_tot()
        except Exception:
            pass
        if self.xarxa:
            self.xarxa.tancar()
        return True


# ============================================================
#  PUNT D'ENTRADA
# ============================================================
if __name__ == "__main__":
    try:
        WalkieTalkieApp().run()
    except Exception as e:
        print("[WalkieP2P] ERROR CRÍTIC no controlat: {0}".format(e))
        sys.exit(1) 