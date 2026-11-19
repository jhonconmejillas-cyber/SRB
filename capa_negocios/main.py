"""
Arranca un proceso de la capa de negocio. Cada uno se corre en su propia terminal.

Uso (desde la raiz del proyecto):
    python -m capa_negocios.main broker        # broker ROUTER/DEALER (uno por maquina de negocio)
    python -m capa_negocios.main gt            # Gestor de Transacciones
    python -m capa_negocios.main rmc           # un RMC (se pueden arrancar varios)
    python -m capa_negocios.main cancelacion   # Servicio de Cancelaciones y Notificaciones

Las direcciones salen del .env, pero se pueden cambiar con argumentos:
    python -m capa_negocios.main gt --broker tcp://127.0.0.1:5559
    python -m capa_negocios.main rmc --brokers 10.43.99.232,10.43.100.84
    python -m capa_negocios.main cancelacion --gts 10.43.99.232,10.43.100.84
"""
import argparse
import time

import zmq

from config import configuracion as config
from config import E_mensaje as m
from capa_negocios.cliente_persistencia import ClientePersistencia
from capa_negocios.rmc import ServicioRMC
from capa_negocios.servicio_cancelacion import Notificador, ServicioCancelacion
from capa_negocios.gestor_transacciones import GestorTransacciones
from capa_negocios.broker import arrancar_broker


def log(quien, texto):
    """Imprime una linea con la hora y quien la escribe. flush=True: que salga ya."""
    print(f"[{time.strftime('%H:%M:%S')}] [{quien}] {texto}", flush=True)


# ====================================================================== GT
def arrancar_gt(broker=None):
    gt = GestorTransacciones(broker=broker)
    try:
        gt.ejecutar()                 # bucle infinito; se detiene con Ctrl+C
    except KeyboardInterrupt:
        log("GT", "detenido")
    finally:
        gt.cerrar()


# ====================================================================== RMC
def arrancar_rmc(brokers=None):
    contexto = zmq.Context()

    # El RMC se CONECTA a los brokers: ellos tienen la direccion fija y reparten
    # el trabajo. Es un REP: recibe una peticion, la resuelve y responde.
    # Un mismo socket puede conectarse a VARIOS brokers: ZeroMQ toma peticiones
    # de todos por turnos y cada respuesta vuelve por donde llego la peticion.
    # Asi este RMC atiende a gt_1 y a gt_2, y si un broker se cae sigue con el otro.
    socket_gt = contexto.socket(zmq.REP)
    hosts = brokers or config.BROKERS
    direcciones = [config.direccion_de(h, config.BROKER_PUERTO_RMC) for h in hosts]
    for d in direcciones:
        socket_gt.connect(d)
    direccion_gt = ", ".join(direcciones)

    # El limite hacia la persistencia es menor que el del cliente: si la
    # persistencia no responde, el RMC alcanza a avisarle al cliente con un ERROR.
    persistencia = ClientePersistencia(contexto, timeout_ms=config.TIMEOUT_MS // 3)
    servicio = ServicioRMC(persistencia)
    log("RMC", f"conectado a los brokers {direccion_gt}; persistencia en {persistencia.direccion}")

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
def arrancar_cancelacion(gts=None):
    contexto = zmq.Context()

    # PULL: recibe las cancelaciones que los GT empujan (PUSH). No hay respuesta
    # hacia el GT: por eso es asincrono, el GT nunca se queda esperando.
    # Se conecta a TODOS los GT (primario y respaldo): si los clientes pasan a
    # gt_2 porque gt_1 se cayo, sus cancelaciones tambien llegan aqui.
    socket_gt = contexto.socket(zmq.PULL)
    hosts = gts or config.GT_HOSTS
    direcciones = [config.direccion_de(h, config.GT_PUERTO_CANCELACION) for h in hosts]
    for d in direcciones:
        socket_gt.connect(d)
    direccion_gt = ", ".join(direcciones)

    persistencia = ClientePersistencia(contexto, timeout_ms=config.TIMEOUT_MS)
    servicio = ServicioCancelacion(persistencia)
    notificador = Notificador(contexto)
    log("CANCELACION", f"conectado a los GT {direccion_gt}; persistencia en {persistencia.direccion}")

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


def separar(texto):
    """'a, b' -> ['a', 'b'];  None -> None (usar lo del .env)."""
    return [x.strip() for x in texto.split(",") if x.strip()] if texto else None


if __name__ == "__main__":
    # --- leer de la linea de comandos que proceso arrancar y, si se dan,
    #     las direcciones que reemplazan a las del .env
    parser = argparse.ArgumentParser(description="Procesos de la capa de negocio")
    parser.add_argument("proceso", choices=["broker", "gt", "rmc", "cancelacion"])
    parser.add_argument("--broker", help="(gt) direccion del broker, ej. tcp://127.0.0.1:5559")
    parser.add_argument("--brokers", help="(rmc) IP de los brokers separadas por coma")
    parser.add_argument("--gts", help="(cancelacion) IP de los GT separadas por coma")
    args = parser.parse_args()

    # --- arrancar el proceso pedido (cada uno se queda corriendo hasta Ctrl+C)
    if args.proceso == "broker":
        arrancar_broker()
    elif args.proceso == "gt":
        arrancar_gt(args.broker)
    elif args.proceso == "rmc":
        arrancar_rmc(separar(args.brokers))
    else:
        arrancar_cancelacion(separar(args.gts))
