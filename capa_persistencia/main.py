"""
Proceso del Servicio de Persistencia: es el unico que toca la base de datos.

Abre (bind) el puerto SP_PUERTO con un socket REP: recibe una peticion,
la resuelve con ServicioPersistencia y responde; luego espera la siguiente.
Como atiende de a una, nunca hay dos cambios a la vez sobre la base.

Uso (desde la raiz del proyecto):
    python -m capa_persistencia.main
"""

import time

import zmq

from config import configuracion as config
from config import E_mensaje as m
from capa_persistencia.db import BaseDatos
from capa_persistencia.servicio_persistencia import ServicioPersistencia


def log(texto):
    """Imprime una linea con la hora. flush=True: que salga en pantalla ya."""
    print(f"[{time.strftime('%H:%M:%S')}] [PERSISTENCIA] {texto}", flush=True)


def main():
    bd = BaseDatos()                           # abre el archivo de la base
    servicio = ServicioPersistencia(bd)        # la logica (sin red)

    print("Estado inicial de la base de datos:")
    bd.imprimir_estado(solo_ocupadas=True)

    contexto = zmq.Context()                   # todo programa ZeroMQ necesita un contexto
    socket = contexto.socket(zmq.REP)          # REP = responde cada peticion que recibe
    socket.bind(config.direccion_bind(config.SP_PUERTO))
    log(f"escuchando en el puerto {config.SP_PUERTO}")

    try:
        while True:
            mensaje = socket.recv()            # espera hasta que llegue una peticion
            peticion = {}
            try:
                peticion = m.decodificar(mensaje)
                respuesta = servicio.atender(peticion)
            except ValueError:
                respuesta = m.crear_respuesta(peticion, m.ERROR, m.PETICION_INVALIDA,
                                              {"razon": "el mensaje no es JSON valido"})
            except Exception as e:          # pase lo que pase, el REP SIEMPRE responde
                respuesta = m.crear_respuesta(peticion, m.ERROR, m.NO_DISPONIBLE,
                                              {"razon": f"error inesperado: {e}"})

            motivo = f" {respuesta['motivo']}" if respuesta["motivo"] else ""
            log(f"{peticion.get('tipo', 'mensaje no valido')} {peticion.get('datos', '')} "
                f"=> {respuesta['estado']}{motivo}")
            socket.send(m.codificar(respuesta))   # devuelve la respuesta a quien pregunto
    except KeyboardInterrupt:                     # Ctrl+C: se muestra como quedo la base
        log("detenido")
        print("Estado final de la base de datos:")
        bd.imprimir_estado(solo_ocupadas=True)
    finally:                                      # pase lo que pase, cerrar todo en orden
        socket.close(linger=0)
        contexto.term()
        bd.cerrar()


if __name__ == "__main__":
    main()