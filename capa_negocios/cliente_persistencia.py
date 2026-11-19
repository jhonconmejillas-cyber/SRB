"""
Cliente de la persistencia: el socket REQ con el que el RMC le pregunta cosas
al Servicio de Persistencia, con limite de espera y reintento.
Tambien lo usa el servicio de cancelacion. Asi, si la persistencia se cae,
nadie se queda esperando para siempre.
"""
import zmq

from config import configuracion as config
from config import E_mensaje as m


class PersistenciaNoDisponible(Exception):
    """La persistencia no respondio a tiempo."""


class ClientePersistencia:
    def __init__(self, contexto, timeout_ms, reintentos=1):
        self.contexto = contexto
        self.direccion = config.direccion(config.HOST_CAPA_PERSISTENCIA, config.SP_PUERTO)
        self.timeout_ms = timeout_ms
        self.reintentos = reintentos
        self.socket = self._nuevo_socket()

    def _nuevo_socket(self):
        """Crea un REQ conectado a la persistencia. LINGER 0: al cerrarlo no espera nada."""
        socket = self.contexto.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(self.direccion)
        return socket

    def enviar(self, peticion, reintentar=True):
        """
        Envia una peticion y devuelve la respuesta.
        Si no llega respuesta se reintenta. Es seguro tambien para las escrituras:
        el reintento lleva el mismo uuid y la persistencia no la hace dos veces.
        reintentar=False: un solo intento.
        """
        intentos = 1 + (self.reintentos if reintentar else 0)
        for _ in range(intentos):
            self.socket.send(m.codificar(peticion))
            if self.socket.poll(self.timeout_ms, zmq.POLLIN):
                return m.decodificar(self.socket.recv())
            # Sin respuesta: este REQ quedo esperando un recv que nunca llegara.
            # No se puede volver a usar; se cierra y se crea uno nuevo.
            self.socket.close()
            self.socket = self._nuevo_socket()
        # Se agotaron los intentos: quien llamo decide que responderle al cliente
        raise PersistenciaNoDisponible(f"la persistencia no respondio ({self.direccion})")

    def cerrar(self):
        self.socket.close(linger=0)