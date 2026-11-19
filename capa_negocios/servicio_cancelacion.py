"""
Servicio de Cancelaciones y Notificaciones (la parte ASINCRONA del sistema).

Como funciona una cancelacion, paso a paso:

  1. El cliente envia CANCELACION al GT. En los datos va el id de la reserva y
     la direccion de su "buzon" de avisos (direccion_notificacion).
  2. El GT NO espera: le contesta de inmediato "ACK" (recibido) al cliente y
     le pasa la peticion a este servicio por un canal PUSH -> PULL.
  3. Este servicio le pide a la persistencia liberar los asientos (CANCELAR_RESERVA).
  4. Cuando la persistencia contesta, este servicio le envia una NOTIFICACION
     directamente al cliente (PUSH -> el PULL del cliente), con el resultado.

     cliente --CANCELACION--> GT --PUSH/PULL--> [este servicio] --REQ/REP--> persistencia
        ^                      |                       |
        |<-------- ACK --------+                       |
        |<--------------- NOTIFICACION (PUSH/PULL) ----+

Este archivo tiene la logica (sin el bucle de red). El proceso se arranca con:
    python -m capa_negocios.main cancelacion
"""
import zmq

from config import E_mensaje as m
from capa_negocios.cliente_persistencia import PersistenciaNoDisponible


class ServicioCancelacion:
    def __init__(self, persistencia):
        self.persistencia = persistencia

    def atender(self, peticion):
        """
        Procesa una CANCELACION y devuelve la NOTIFICACION que hay que enviarle
        al cliente (un dict con forma de respuesta + "tipo": "NOTIFICACION").
        """
        datos = peticion.get("datos", {})
        id_reserva = datos.get("id_reserva")

        if peticion.get("tipo") != m.CANCELACION:
            resultado = m.crear_respuesta(peticion, m.ERROR, m.PETICION_INVALIDA,
                                          {"razon": f"este servicio solo atiende CANCELACION, no {peticion.get('tipo')}"})
        elif not m.es_entero_positivo(id_reserva):
            resultado = m.crear_respuesta(peticion, m.ERROR, m.DATO_INVALIDO,
                                          {"razon": f"id_reserva debe ser un entero mayor que 0: {id_reserva}"})
        else:
            # Se usa el mismo uuid de la cancelacion: sirve para seguirla en los
            # logs y para que la persistencia reconozca un reintento. Si la orden
            # llega dos veces (se perdio una respuesta, o el cliente la repitio por
            # otro GT), la segunda recibe el mismo EXITO y no "ya estaba cancelada".
            orden = m.crear_peticion(m.CANCELAR_RESERVA, peticion["id_cliente"],
                                     {"id_reserva": id_reserva}, peticion["uuid"])
            try:
                resultado = self.persistencia.enviar(orden)
            except PersistenciaNoDisponible as e:
                resultado = m.crear_respuesta(peticion, m.ERROR, m.NO_DISPONIBLE, {"razon": str(e)})

        # La notificacion es la respuesta de la persistencia, marcada como NOTIFICACION
        # y con el id de la reserva (para que el cliente sepa cual se cancelo).
        notificacion = dict(resultado)
        notificacion["tipo"] = m.NOTIFICACION
        notificacion["datos"] = {"id_reserva": id_reserva, **resultado.get("datos", {})}
        return notificacion


class Notificador:
    """
    Envia avisos a los clientes. Cada cliente tiene su propio "buzon" (un socket
    PULL en su maquina); aqui se guarda un socket PUSH por cada direccion para
    no tener que conectarse de nuevo en cada aviso.
    """

    def __init__(self, contexto):
        self.contexto = contexto
        self.sockets: dict[str, zmq.Socket] = {}    # direccion del cliente -> socket PUSH

    def enviar(self, direccion, notificacion):
        """Envia el aviso. Devuelve True si quedo en camino, False si no se pudo."""
        if not isinstance(direccion, str) or not direccion.startswith("tcp://"):
            return False
        socket = self.sockets.get(direccion)
        if socket is None:
            socket = self.contexto.socket(zmq.PUSH)
            socket.setsockopt(zmq.LINGER, 0)        # al cerrar, no esperar avisos pendientes
            socket.setsockopt(zmq.SNDHWM, 100)      # maximo 100 avisos en cola por cliente
            try:
                socket.connect(direccion)
            except zmq.ZMQError:
                socket.close()
                return False
            self.sockets[direccion] = socket
        try:
            # NOBLOCK: si el buzon del cliente esta lleno o desaparecio, no
            # quedarse trabado; el servicio tiene que seguir atendiendo a los demas.
            socket.send(m.codificar(notificacion), flags=zmq.NOBLOCK)
            return True
        except zmq.Again:
            return False

    def cerrar(self):
        for socket in self.sockets.values():
            socket.close(linger=0)
        self.sockets.clear()
