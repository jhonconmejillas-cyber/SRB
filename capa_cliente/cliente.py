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

TOLERANCIA A FALLOS (patron "Lazy Pirate" de ZeroMQ, todo en el cliente):
  - El cliente conoce una lista de GT: [gt_1 primario, gt_2 respaldo].
    Le habla SOLO al GT actual; al respaldo solo pasa si el actual falla.
  - Cada intento espera la respuesta un tiempo maximo (poll con timeout).
  - Si no llega: el REQ queda trabado (no se puede volver a enviar sin
    recibir), asi que se cierra, se espera un poco y se crea uno nuevo.
  - La espera crece: 100, 200, 400, 800 ms... (retroceso exponencial) mas un
    poco de azar (jitter), para que muchos clientes no reintenten todos a la vez.
  - Tras CLIENTE_FALLOS_PARA_CAMBIAR fallos seguidos se pasa al siguiente GT.
  - Errores de negocio (sin cupo, no existe, dato invalido...) NO se reintentan:
    el sistema SI respondio, reintentar daria el mismo resultado.
  - Cada reintento lleva el MISMO uuid: asi la persistencia puede reconocer una
    operacion repetida y no hacerla dos veces.
"""
import random
import threading
import time

import zmq

from config import configuracion as config
from config import E_mensaje as m


def es_reintentable(respuesta):
    """
    Decide si vale la pena reintentar despues de recibir una respuesta.
    Solo ERROR NO_DISPONIBLE: quiere decir que una pieza del sistema no esta
    (el broker, los RMC, la persistencia), no que la peticion este mal.
    Todo lo demas (EXITO, ACK, FALLO SIN_CUPO, ERROR DATO_INVALIDO...) es una
    respuesta definitiva: reintentar daria lo mismo.
    """
    return respuesta["estado"] == m.ERROR and respuesta["motivo"] == m.NO_DISPONIBLE


class Cliente:
    def __init__(self, id_cliente, timeout_ms=None, al_notificar=None, gts=None,
                 intentos=None, fallos_para_cambiar=None, backoff_base_ms=None,
                 backoff_max_ms=None, mostrar_fallos=True):
        """
        al_notificar: funcion que se llama con cada notificacion que llega
                      (por defecto, imprimirla en pantalla).
        gts:          lista de GT ["IP" o "IP:puerto", ...]; el primero es el primario.
                      Por defecto GT_HOSTS del .env.
        El resto de parametros, si no se dan, salen del .env (ver configuracion.py).
        mostrar_fallos: imprimir en pantalla los reintentos y los cambios de GT.
        """
        self.id_cliente = id_cliente
        self.timeout_ms = timeout_ms or config.CLIENTE_TIMEOUT_MS
        self.intentos = intentos or config.CLIENTE_INTENTOS
        self.fallos_para_cambiar = fallos_para_cambiar or config.CLIENTE_FALLOS_PARA_CAMBIAR
        self.backoff_base_ms = backoff_base_ms or config.BACKOFF_BASE_MS
        self.backoff_max_ms = backoff_max_ms or config.BACKOFF_MAX_MS
        self.mostrar_fallos = mostrar_fallos

        # --- replicas del GT: se empieza por el primario (posicion 0)
        self.gts = [config.direccion_de(h, config.GT_PUERTO_CLIENTES) for h in (gts or config.GT_HOSTS)]
        self.gt_actual = 0                     # a cual le estamos hablando
        self.fallos_seguidos = 0               # timeouts seguidos con el GT actual
        self.direccion = self.gts[0]

        # --- contadores para las metricas y para la sustentacion
        self.total_reintentos = 0              # cuantos reintentos se han hecho en total
        self.cambios_de_gt = 0                 # cuantas veces se paso de un GT a otro
        self.ultimo_intentos = 0               # intentos que necesito la ultima peticion

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
        """Crea un REQ conectado al GT actual."""
        socket = self.contexto.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)       # al cerrar, no esperar mensajes pendientes
        socket.connect(self.direccion)
        return socket

    def _avisar(self, texto):
        if self.mostrar_fallos:
            print(f"[{time.strftime('%H:%M:%S')}] [C{self.id_cliente}] {texto}", flush=True)

    def _nombre_gt(self, i=None):
        """'gt_1 (tcp://...)' para los mensajes en pantalla."""
        i = self.gt_actual if i is None else i
        return f"gt_{i + 1} ({self.gts[i]})"

    # ================================================================ envio sincrono
    def enviar(self, tipo, datos):
        """
        Envia una peticion y devuelve (respuesta, milisegundos que tardo).
        Si ningun GT responde despues de todos los intentos, la respuesta es un
        ERROR NO_DISPONIBLE armado aqui mismo: el cliente nunca se queda colgado.
        """
        peticion = m.crear_peticion(tipo, self.id_cliente, datos)
        return self._enviar_peticion(peticion)

    def _espera_backoff(self, intento):
        """
        Retroceso exponencial: antes del reintento n se espera base * 2^n ms
        (100, 200, 400, 800...) sin pasar del maximo, mas un jitter: un extra al
        azar de hasta la mitad. Asi, si 8 clientes fallan a la vez, no vuelven
        a golpear al GT todos en el mismo milisegundo. Devuelve los ms a esperar.
        """
        espera = min(self.backoff_base_ms * (2 ** intento), self.backoff_max_ms)
        return espera + random.uniform(0, espera / 2)

    def _cambiar_de_gt(self):
        """Pasa al siguiente GT de la lista (gt_1 -> gt_2 -> gt_1 ...)."""
        anterior = self.gt_actual
        self.gt_actual = (self.gt_actual + 1) % len(self.gts)
        self.direccion = self.gts[self.gt_actual]
        self.fallos_seguidos = 0
        self.cambios_de_gt += 1
        self._avisar(f"FAILOVER: {self._nombre_gt(anterior)} no responde -> ahora uso {self._nombre_gt()}")

    def _enviar_peticion(self, peticion):
        """
        El corazon del Lazy Pirate. Repite hasta self.intentos veces:
          1. enviar por el REQ al GT actual y esperar con poll(timeout)
          2a. llego respuesta definitiva -> devolverla (aunque sea un error de negocio)
          2b. no llego (timeout) o llego NO_DISPONIBLE -> contar el fallo,
              si van muchos seguidos cambiar de GT, esperar (backoff) y repetir
        La peticion (y su uuid) es la misma en todos los intentos.
        """
        inicio = time.perf_counter()
        respuesta = None
        for intento in range(self.intentos):
            self.ultimo_intentos = intento + 1
            self.socket.send(m.codificar(peticion))
            if self.socket.poll(self.timeout_ms, zmq.POLLIN):      # espera un tiempo maximo
                respuesta = m.decodificar(self.socket.recv())
                if not es_reintentable(respuesta):
                    self.fallos_seguidos = 0                       # este GT si funciona
                    break                                          # respuesta definitiva
                razon = f"respondio NO_DISPONIBLE ({respuesta['datos'].get('razon', '')})"
            else:
                # Sin respuesta: este REQ quedo esperando un recv que nunca llegara.
                # No se puede volver a usar: se cierra y luego se crea uno nuevo.
                self.socket.close()
                self.socket = None
                respuesta = None
                razon = f"no respondio en {self.timeout_ms} ms"

            # ---- este intento fallo
            self.fallos_seguidos += 1
            if intento == self.intentos - 1:
                break                                              # no quedan intentos
            self.total_reintentos += 1
            espera = self._espera_backoff(intento)
            self._avisar(f"{peticion['tipo']}: {self._nombre_gt()} {razon}; "
                         f"reintento {intento + 1} en {espera:.0f} ms")
            if self.fallos_seguidos >= self.fallos_para_cambiar and len(self.gts) > 1:
                self._cambiar_de_gt()          # el reintento ya va al otro GT
            time.sleep(espera / 1000)          # retroceso antes de volver a intentar
            if self.socket is not None:
                self.socket.close()            # tambien se renueva si cambio de GT
            self.socket = self._nuevo_socket()

        if self.socket is None:                # el ultimo intento fue un timeout
            self.socket = self._nuevo_socket()
        if respuesta is None:
            respuesta = m.crear_respuesta(peticion, m.ERROR, m.NO_DISPONIBLE, {
                "razon": f"ningun GT respondio despues de {self.ultimo_intentos} intentos"})
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
