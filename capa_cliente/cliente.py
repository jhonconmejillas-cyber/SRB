"""
Cliente del SRB.

- Operaciones SINCRONAS (consultar, reservar, modificar): envia la peticion al
  Gestor de Transacciones por un socket REQ y espera la respuesta.
- Operacion ASINCRONA (cancelar): envia la peticion, el GT contesta de inmediato
  "ACK" (recibido) y el cliente sigue con lo suyo. Mas tarde el Servicio de
  Cancelacion le envia una NOTIFICACION a su "buzon".

El buzon es un socket PULL que el cliente abre en su propia maquina. Lo atiende
un hilo en segundo plano, asi las notificaciones llegan aunque el cliente este
ocupado haciendo otras peticiones.
"""
import threading
import time

import zmq

from config import configuracion as config
from config import E_mensaje as m


class Cliente:
    def __init__(self, id_cliente, timeout_ms=config.TIMEOUT_MS, al_notificar=None):
        """
        al_notificar: funcion que se llama con cada notificacion que llega
                      (por defecto, imprimirla en pantalla).
        """
        self.id_cliente = id_cliente
        self.timeout_ms = timeout_ms
        self.direccion = config.direccion(config.HOST_CAPA_SERVICIOS, config.GT_PUERTO_CLIENTES)
        self.contexto = zmq.Context()          # un contexto se puede compartir entre hilos
        self.socket = self._nuevo_socket()

        # --- estado del buzon de notificaciones (se crea la primera vez que se cancela)
        self.al_notificar = al_notificar or self._imprimir_notificacion
        self.direccion_notificacion = None     # "tcp://IP:puerto" donde escucha el buzon
        self._hilo_buzon = None
        self._detener = threading.Event()      # bandera para pedirle al hilo que termine
        self._candado = threading.Condition()  # protege las dos variables siguientes
        self.cancelaciones_pendientes: set[str] = set()   # uuid de cancelaciones sin aviso aun
        self.notificaciones: list[dict] = []               # todos los avisos recibidos

    def _nuevo_socket(self):
        socket = self.contexto.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)       # al cerrar, no esperar mensajes pendientes
        socket.connect(self.direccion)
        return socket

    # ================================================================ envio sincrono
    def enviar(self, tipo, datos):
        """
        Envia una peticion y devuelve (respuesta, milisegundos que tardo).
        Si el GT no responde a tiempo, la respuesta es un ERROR NO_DISPONIBLE
        armado aqui mismo: el cliente nunca se queda colgado.
        """
        peticion = m.crear_peticion(tipo, self.id_cliente, datos)
        return self._enviar_peticion(peticion)

    def _enviar_peticion(self, peticion):
        inicio = time.perf_counter()
        self.socket.send(m.codificar(peticion))
        if self.socket.poll(self.timeout_ms, zmq.POLLIN):     # espera la respuesta un tiempo maximo
            respuesta = m.decodificar(self.socket.recv())
        else:
            # Un REQ que no recibio respuesta queda "trabado": se cambia por uno nuevo
            self.socket.close()
            self.socket = self._nuevo_socket()
            respuesta = m.crear_respuesta(peticion, m.ERROR, m.NO_DISPONIBLE,
                                          {"razon": f"el GT no respondio en {self.timeout_ms} ms"})
        ms = (time.perf_counter() - inicio) * 1000
        return respuesta, ms

    # ================================================================ operaciones
    def consultar(self, mes):
        return self.enviar(m.CONSULTA, {"mes": mes})

    def reservar(self, id_evento, fecha, n_asientos):
        return self.enviar(m.RESERVA, {"id_evento": id_evento, "fecha": fecha, "n_asientos": n_asientos})

    def modificar(self, id_reserva, n_asientos=None, fecha=None):
        """Cambia el numero de asientos, la fecha o ambos de una reserva propia."""
        datos = {"id_reserva": id_reserva}
        if n_asientos is not None:
            datos["n_asientos"] = n_asientos
        if fecha is not None:
            datos["fecha"] = fecha
        return self.enviar(m.MODIFICACION, datos)

    def cancelar(self, id_reserva):
        """
        Pide cancelar una reserva (ASINCRONO). Devuelve (respuesta, ms) con el
        ACK del GT; el resultado real llega despues por el buzon.
        """
        self._abrir_buzon()
        peticion = m.crear_peticion(m.CANCELACION, self.id_cliente, {
            "id_reserva": id_reserva,
            "direccion_notificacion": self.direccion_notificacion})
        # Se anota ANTES de enviar: el aviso podria llegar antes que el ACK
        with self._candado:
            self.cancelaciones_pendientes.add(peticion["uuid"])
        respuesta, ms = self._enviar_peticion(peticion)
        if respuesta["estado"] != m.ACK:
            # No fue aceptada: no va a llegar ningun aviso, no hay que esperarlo
            with self._candado:
                self.cancelaciones_pendientes.discard(peticion["uuid"])
        return respuesta, ms

    # ================================================================ buzon de notificaciones
    def _abrir_buzon(self):
        """Arranca el hilo del buzon (solo la primera vez) y espera a que este listo."""
        if self._hilo_buzon is not None:
            return
        listo = threading.Event()
        self._hilo_buzon = threading.Thread(target=self._escuchar_buzon, args=(listo,), daemon=True)
        self._hilo_buzon.start()
        if not listo.wait(5) or self.direccion_notificacion is None:
            raise RuntimeError("no se pudo abrir el buzon de notificaciones")

    def _escuchar_buzon(self, listo):
        """
        Lo que hace el hilo del buzon. El socket se crea y se usa SOLO dentro de
        este hilo (los sockets de ZeroMQ no se deben compartir entre hilos).
        """
        buzon = self.contexto.socket(zmq.PULL)
        buzon.setsockopt(zmq.LINGER, 0)
        try:
            # Abre el primer puerto libre del rango configurado (5600-5699 por defecto)
            puerto = buzon.bind_to_random_port("tcp://*", config.CLIENTE_PUERTO_NOTIF_MIN,
                                               config.CLIENTE_PUERTO_NOTIF_MAX + 1)
        except zmq.ZMQError:
            buzon.close()
            listo.set()
            return
        # Los servicios encuentran este buzon con la IP de la maquina de clientes
        self.direccion_notificacion = config.direccion(config.HOST_CAPA_CONSUMIDORES, puerto)
        listo.set()
        try:
            while not self._detener.is_set():
                # Revisa cada 200 ms si llego algo o si le pidieron detenerse
                if not buzon.poll(200, zmq.POLLIN):
                    continue
                try:
                    aviso = m.decodificar(buzon.recv())
                except ValueError:
                    continue
                with self._candado:
                    self.notificaciones.append(aviso)
                    self.cancelaciones_pendientes.discard(aviso.get("uuid"))
                    self._candado.notify_all()      # despierta a quien este esperando avisos
                self.al_notificar(aviso)
        finally:
            buzon.close(linger=0)

    def esperar_notificaciones(self, timeout_s):
        """
        Espera hasta timeout_s segundos a que lleguen los avisos de todas las
        cancelaciones pendientes. Devuelve cuantas quedaron sin aviso.
        """
        limite = time.monotonic() + timeout_s
        with self._candado:
            while self.cancelaciones_pendientes:
                falta = limite - time.monotonic()
                if falta <= 0:
                    break
                self._candado.wait(falta)
            return len(self.cancelaciones_pendientes)

    def _imprimir_notificacion(self, aviso):
        texto = f"NOTIFICACION reserva {aviso.get('datos', {}).get('id_reserva')} cancelada => {aviso.get('estado')}"
        if aviso.get("estado") != m.EXITO:
            texto += f" {aviso.get('motivo')}: {aviso.get('datos', {}).get('razon', '')}"
        else:
            texto += f" ({aviso['datos'].get('asientos_liberados')} asientos liberados)"
        print(f"[{time.strftime('%H:%M:%S')}] [C{self.id_cliente}] {texto}", flush=True)

    # ================================================================ cierre
    def cerrar(self):
        self._detener.set()                    # pide al hilo del buzon que termine
        if self._hilo_buzon is not None:
            self._hilo_buzon.join(timeout=2)
        self.socket.close(linger=0)
        self.contexto.term()
