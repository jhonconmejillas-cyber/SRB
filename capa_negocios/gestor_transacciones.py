"""
Gestor de Transacciones (GT): la puerta de entrada del sistema.

Todo cliente le habla solo al GT. El GT mira el tipo de peticion y la manda
por uno de dos caminos:

  Canal SINCRONO (CONSULTA, RESERVA, MODIFICACION): se reenvia a un RMC y la
  respuesta del RMC se le devuelve al cliente.

      clientes (REQ) --> [ROUTER :GT_PUERTO_CLIENTES]  GT  [DEALER] --connect--> BROKER --> RMC (REP) x N

  Canal ASINCRONO (CANCELACION): se contesta de una vez "ACK" (recibido) al
  cliente y la peticion se empuja al Servicio de Cancelacion, sin esperarlo.

      GT [PUSH :GT_PUERTO_CANCELACION] --> Servicio de Cancelacion (PULL)

El GT abre (bind) el puerto de los clientes y el de cancelaciones. Hacia los
RMC ya no abre nada: se CONECTA al broker de su propia maquina (BROKER_HOST),
y el broker reparte entre todos los RMC. Asi se pueden lanzar N RMC sin tocar
el GT. El GT tampoco guarda datos: todo esta en la base. Por eso puede haber
dos GT (gt_1 primario y gt_2 respaldo) y el cliente puede pasar de uno a otro.
"""
import time

import zmq

from config import configuracion as config
from config import E_mensaje as m
from capa_negocios.metricas import RegistroMetricas

# Peticiones que van por el canal sincrono (al RMC)
TIPOS_SINCRONOS = {m.CONSULTA, m.RESERVA, m.MODIFICACION}


def log(texto):
    print(f"[{time.strftime('%H:%M:%S')}] [GT] {texto}", flush=True)


class GestorTransacciones:
    def __init__(self, ruta_metricas="resultados/gt_metricas.csv", broker=None):
        """broker: "tcp://IP:puerto" del broker; por defecto el del .env (BROKER_HOST)."""
        self.contexto = zmq.Context()

        # ROUTER: puede atender a muchos clientes a la vez y recuerda quien envio
        # cada mensaje (la "identidad" va en el primer frame del mensaje).
        self.frontend = self.contexto.socket(zmq.ROUTER)      # lado de los clientes
        self.frontend.bind(config.direccion_bind(config.GT_PUERTO_CLIENTES))

        # DEALER conectado al broker (lado de los RMC). El sobre del cliente
        # (su identidad) viaja intacto hasta el RMC y vuelve con la respuesta.
        # IMMEDIATE: si el broker no esta conectado, send() falla de una vez
        # (zmq.Again) en vez de guardar el mensaje; asi el cliente recibe un
        # ERROR NO_DISPONIBLE y no se queda esperando.
        self.direccion_broker = broker or config.direccion(config.BROKER_HOST, config.BROKER_PUERTO_GT)
        self.backend = self.contexto.socket(zmq.DEALER)
        self.backend.setsockopt(zmq.IMMEDIATE, 1)
        self.backend.setsockopt(zmq.LINGER, 0)
        self.backend.connect(self.direccion_broker)

        # PUSH: empuja cancelaciones al servicio de cancelacion, sin esperar respuesta.
        self.canal_cancelacion = self.contexto.socket(zmq.PUSH)
        self.canal_cancelacion.setsockopt(zmq.LINGER, 0)
        self.canal_cancelacion.bind(config.direccion_bind(config.GT_PUERTO_CANCELACION))

        # El poller vigila varios sockets a la vez y avisa cual tiene algo que leer
        self.poller = zmq.Poller()
        self.poller.register(self.frontend, zmq.POLLIN)
        self.poller.register(self.backend, zmq.POLLIN)

        # Peticiones enviadas al RMC que esperan respuesta:
        # uuid -> (hora de entrada, tipo, id_cliente). Sirve para medir el tiempo.
        self.pendientes: dict[str, tuple[float, str, int]] = {}
        self.metricas = RegistroMetricas(config.RAIZ / ruta_metricas)

    # ------------------------------------------------------------ bucle principal
    def ejecutar(self):
        log(f"clientes en el puerto {config.GT_PUERTO_CLIENTES}, broker en {self.direccion_broker}, "
            f"cancelaciones en el puerto {config.GT_PUERTO_CANCELACION}")
        while True:
            eventos = dict(self.poller.poll(1000))     # espera hasta 1 s a que llegue algo
            if self.frontend in eventos:
                self.atender_cliente()
            if self.backend in eventos:
                self.atender_rmc()
            self.limpiar_pendientes()

    # ------------------------------------------------------------ llega algo de un cliente
    def atender_cliente(self):
        frames = self.frontend.recv_multipart()        # [identidad, b"", json]
        try:
            peticion = m.decodificar(frames[-1])
        except ValueError:
            self.responder_directo(frames, m.crear_respuesta(
                {}, m.ERROR, m.PETICION_INVALIDA, {"razon": "el mensaje no es JSON valido"}))
            log("rechazado: mensaje que no es JSON")
            return

        tipo = peticion.get("tipo")
        cliente = peticion.get("id_cliente")

        if tipo == m.CANCELACION:                      # camino asincrono
            self.atender_cancelacion(frames, peticion)
            return

        if tipo not in TIPOS_SINCRONOS:                # tipos internos o desconocidos
            motivo = m.NO_IMPLEMENTADO if tipo in m.TIPOS else m.PETICION_INVALIDA
            self.responder_directo(frames, m.crear_respuesta(
                peticion, m.ERROR, motivo, {"razon": f"el GT no acepta peticiones {tipo}"}))
            log(f"rechazada {tipo} del cliente {cliente} ({motivo})")
            return

        try:
            # NOBLOCK: si el broker no esta conectado, no esperar; avisar al cliente
            self.backend.send_multipart(frames, flags=zmq.NOBLOCK)   # el sobre viaja intacto
        except zmq.Again:
            self.responder_directo(frames, m.crear_respuesta(
                peticion, m.ERROR, m.NO_DISPONIBLE, {"razon": "el broker de los RMC no esta conectado"}))
            log(f"rechazada {tipo} del cliente {cliente}: broker no conectado")
            return
        self.pendientes[peticion.get("uuid")] = (time.perf_counter(), tipo, cliente)
        log(f"<- {tipo} del cliente {cliente}")

    # ------------------------------------------------------------ cancelacion (asincrona)
    def atender_cancelacion(self, frames, peticion):
        """
        Acepta la cancelacion de inmediato: la empuja al servicio de cancelacion
        y le responde ACK al cliente SIN esperar a que se haga. El resultado real
        le llegara al cliente despues, como NOTIFICACION.
        """
        inicio = time.perf_counter()
        cliente = peticion.get("id_cliente")
        datos = peticion.get("datos", {})
        direccion = datos.get("direccion_notificacion")

        # Revisiones minimas: sin estos datos el servicio no podria hacer su trabajo
        if not m.es_entero_positivo(datos.get("id_reserva")):
            respuesta = m.crear_respuesta(peticion, m.ERROR, m.DATO_INVALIDO,
                                          {"razon": f"id_reserva invalido: {datos.get('id_reserva')}"})
        elif not isinstance(direccion, str) or not direccion.startswith("tcp://"):
            respuesta = m.crear_respuesta(peticion, m.ERROR, m.DATO_INVALIDO,
                                          {"razon": f"direccion_notificacion invalida: {direccion}"})
        else:
            try:
                # NOBLOCK: si el servicio de cancelacion no esta conectado, no esperar
                self.canal_cancelacion.send(frames[-1], flags=zmq.NOBLOCK)
                respuesta = m.crear_respuesta(peticion, m.ACK, datos={
                    "id_reserva": datos["id_reserva"],
                    "razon": "cancelacion recibida; se notificara cuando se haga efectiva"})
            except zmq.Again:
                respuesta = m.crear_respuesta(peticion, m.ERROR, m.NO_DISPONIBLE,
                                              {"razon": "el servicio de cancelacion no esta disponible"})

        self.responder_directo(frames, respuesta)
        ms = (time.perf_counter() - inicio) * 1000
        motivo = f" {respuesta['motivo']}" if respuesta["motivo"] else ""
        log(f"<- CANCELACION del cliente {cliente} (reserva {datos.get('id_reserva')}) "
            f"=> {respuesta['estado']}{motivo}")
        self.metricas.registrar(m.CANCELACION, respuesta["estado"], respuesta["motivo"],
                                ms, cliente, peticion.get("uuid"))

    # ------------------------------------------------------------ llega la respuesta de un RMC
    def atender_rmc(self):
        frames = self.backend.recv_multipart()
        try:
            respuesta = m.decodificar(frames[-1])
        except ValueError:
            respuesta = {}
        # Buscar cuando entro esta peticion para calcular cuanto tardo
        inicio, tipo, cliente = self.pendientes.pop(respuesta.get("uuid"), (None, "?", "?"))
        ms = (time.perf_counter() - inicio) * 1000 if inicio is not None else 0.0
        motivo = f" {respuesta.get('motivo')}" if respuesta.get("motivo") else ""
        log(f"-> {tipo} al cliente {cliente}: {respuesta.get('estado')}{motivo} ({ms:.1f} ms)")
        if inicio is not None:
            self.metricas.registrar(tipo, respuesta.get("estado"), respuesta.get("motivo"),
                                    ms, cliente, respuesta.get("uuid"))
        self.frontend.send_multipart(frames)           # el ROUTER lo entrega al cliente correcto

    # ------------------------------------------------------------ apoyo
    def responder_directo(self, frames, respuesta):
        """Responde al cliente desde el GT, sin pasar por el RMC (se conserva el sobre)."""
        self.frontend.send_multipart(frames[:-1] + [m.codificar(respuesta)])

    def limpiar_pendientes(self):
        """Olvida las peticiones cuya respuesta ya no llegara (p. ej. se cayo el RMC)."""
        limite = time.perf_counter() - 2 * config.TIMEOUT_MS / 1000
        vencidas = [u for u, (inicio, _, _) in self.pendientes.items() if inicio < limite]
        for u in vencidas:
            _, tipo, cliente = self.pendientes.pop(u)
            log(f"sin respuesta del RMC para {tipo} del cliente {cliente}; se descarta")

    def cerrar(self):
        self.metricas.cerrar()
        self.frontend.close(linger=0)
        self.backend.close(linger=0)
        self.canal_cancelacion.close(linger=0)
        self.contexto.term()
