"""
Arranca un proceso de la capa de negocio. Cada uno se corre en su propia terminal.

Uso (desde la raiz del proyecto):
    python -m capa_negocios.main gt            # Gestor de Transacciones
    python -m capa_negocios.main rmc           # un RMC (se pueden arrancar varios)
    python -m capa_negocios.main cancelacion   # Servicio de Cancelaciones y Notificaciones
"""
import sys
import time

import zmq

from config import configuracion as config
from config import E_mensaje as m
from capa_negocios.cliente_persistencia import ClientePersistencia
from capa_negocios.rmc import ServicioRMC
from capa_negocios.servicio_cancelacion import Notificador, ServicioCancelacion
from capa_negocios.gestor_transacciones import GestorTransacciones


def log(quien, texto):
    """Imprime una linea con la hora y quien la escribe. flush=True: que salga ya."""
    print(f"[{time.strftime('%H:%M:%S')}] [{quien}] {texto}", flush=True)


# ====================================================================== GT
def arrancar_gt():
    gt = GestorTransacciones()
    try:
        gt.ejecutar()                 # bucle infinito; se detiene con Ctrl+C
    except KeyboardInterrupt:
        log("GT", "detenido")
    finally:
        gt.cerrar()


# ====================================================================== RMC
def arrancar_rmc():
    contexto = zmq.Context()

    # El RMC se CONECTA al GT: el GT tiene la direccion fija y reparte el trabajo.
    # Es un REP: recibe una peticion, la resuelve y responde; luego la siguiente.
    socket_gt = contexto.socket(zmq.REP)
    direccion_gt = config.direccion(config.HOST_CAPA_SERVICIOS, config.GT_PUERTO_RMC)
    socket_gt.connect(direccion_gt)

    # El limite hacia la persistencia es menor que el del cliente: si la
    # persistencia no responde, el RMC alcanza a avisarle al cliente con un ERROR.
    persistencia = ClientePersistencia(contexto, timeout_ms=config.TIMEOUT_MS // 3)
    servicio = ServicioRMC(persistencia)
    log("RMC", f"conectado al GT en {direccion_gt}; persistencia en {persistencia.direccion}")

    try:
        while True:
            recibido = socket_gt.recv()          # espera la siguiente peticion
            peticion = {}
            try:
                peticion = m.decodificar(recibido)
                respuesta = servicio.atender(peticion)
            except ValueError:
                respuesta = m.crear_respuesta(peticion, m.ERROR, m.PETICION_INVALIDA,
                                              {"razon": "el mensaje no es JSON valido"})
            except Exception as e:          # pase lo que pase, el REP SIEMPRE responde
                respuesta = m.crear_respuesta(peticion, m.ERROR, m.NO_DISPONIBLE,
                                              {"razon": f"error inesperado: {e}"})

            motivo = f" {respuesta['motivo']}" if respuesta["motivo"] else ""
            log("RMC", f"{peticion.get('tipo', 'mensaje no valido')} {peticion.get('datos', '')} "
                       f"=> {respuesta['estado']}{motivo}")
            socket_gt.send(m.codificar(respuesta))
    except KeyboardInterrupt:
        log("RMC", "detenido")
    finally:
        persistencia.cerrar()
        socket_gt.close(linger=0)
        contexto.term()


# ====================================================================== CANCELACION
def arrancar_cancelacion():
    contexto = zmq.Context()

    # PULL: recibe las cancelaciones que el GT empuja (PUSH). No hay respuesta
    # hacia el GT: por eso es asincrono, el GT nunca se queda esperando.
    socket_gt = contexto.socket(zmq.PULL)
    direccion_gt = config.direccion(config.HOST_CAPA_SERVICIOS, config.GT_PUERTO_CANCELACION)
    socket_gt.connect(direccion_gt)

    persistencia = ClientePersistencia(contexto, timeout_ms=config.TIMEOUT_MS)
    servicio = ServicioCancelacion(persistencia)
    notificador = Notificador(contexto)
    log("CANCELACION", f"conectado al GT en {direccion_gt}; persistencia en {persistencia.direccion}")

    try:
        while True:
            recibido = socket_gt.recv()          # espera la siguiente cancelacion
            try:
                peticion = m.decodificar(recibido)
            except ValueError:
                log("CANCELACION", "mensaje que no es JSON; se ignora")
                continue
            try:
                notificacion = servicio.atender(peticion)
            except Exception as e:               # que un error raro no tumbe el servicio
                notificacion = m.crear_respuesta(peticion, m.ERROR, m.NO_DISPONIBLE,
                                                 {"razon": f"error inesperado: {e}"})
                notificacion["tipo"] = m.NOTIFICACION

            direccion = peticion.get("datos", {}).get("direccion_notificacion")
            enviada = notificador.enviar(direccion, notificacion)
            motivo = f" {notificacion['motivo']}" if notificacion["motivo"] else ""
            log("CANCELACION",
                f"reserva {notificacion['datos'].get('id_reserva')} del cliente {peticion.get('id_cliente')} "
                f"=> {notificacion['estado']}{motivo}; "
                + (f"notificado en {direccion}" if enviada else f"NO se pudo notificar a {direccion}"))
    except KeyboardInterrupt:
        log("CANCELACION", "detenido")
    finally:
        notificador.cerrar()
        persistencia.cerrar()
        socket_gt.close(linger=0)
        contexto.term()


if __name__ == "__main__":
    # El primer argumento dice que proceso arrancar
    procesos = {"gt": arrancar_gt, "rmc": arrancar_rmc, "cancelacion": arrancar_cancelacion}
    if len(sys.argv) != 2 or sys.argv[1] not in procesos:
        print("uso: python -m capa_negocios.main [gt|rmc|cancelacion]")
        sys.exit(1)
    procesos[sys.argv[1]]()
