"""
Broker ROUTER/DEALER: el "repartidor" entre los GT y los RMC.

    GT (DEALER) --connect--> [ROUTER :BROKER_PUERTO_GT]  BROKER  [DEALER :BROKER_PUERTO_RMC] <--connect-- RMC (REP) x N

Por que existe:
  - Ni el GT ni los RMC tienen que conocerse. Los dos van a buscar al broker,
    que tiene la direccion fija. Se pueden agregar o quitar RMC sin tocar nada.
  - El DEALER reparte por turnos (round-robin) entre los RMC conectados:
    con 3 RMC, la peticion 1 va al RMC A, la 2 al B, la 3 al C, la 4 al A...
  - Hay un broker en cada maquina de negocio. Cada RMC se conecta a LOS DOS,
    asi que cualquier RMC puede atender peticiones de cualquier GT.

El broker no abre los mensajes ni guarda nada: solo los pasa de un lado al
otro. Por eso casi nunca falla y, si se cae, se vuelve a lanzar sin perder datos.

Como sabe a quien devolver cada respuesta:
  El ROUTER le pone a cada mensaje un primer frame con la identidad de quien
  lo envio (el GT). Ese frame viaja hasta el RMC y vuelve con la respuesta.
  Al regresar, el ROUTER lo lee, sabe a que GT entregarla y lo quita.
"""
import time

import zmq

from config import configuracion as config


def log(texto):
    print(f"[{time.strftime('%H:%M:%S')}] [BROKER] {texto}", flush=True)


def arrancar_broker(puerto_gt=None, puerto_rmc=None):
    """Abre los dos puertos del broker y queda pasando mensajes hasta Ctrl+C."""
    # Si no se dan los puertos, se usan los del .env (5559 y 5556 por defecto)
    puerto_gt = puerto_gt or config.BROKER_PUERTO_GT
    puerto_rmc = puerto_rmc or config.BROKER_PUERTO_RMC
    contexto = zmq.Context()                    # todo programa ZeroMQ necesita un contexto

    # ROUTER (frontend): aqui se conectan los GT y mandan sus peticiones
    frontend = contexto.socket(zmq.ROUTER)
    frontend.bind(config.direccion_bind(puerto_gt))

    # DEALER (backend): aqui se conectan los RMC; reparte el trabajo por turnos
    backend = contexto.socket(zmq.DEALER)
    backend.bind(config.direccion_bind(puerto_rmc))

    log(f"GT se conectan al puerto {puerto_gt}, RMC al puerto {puerto_rmc}")
    try:
        # zmq.proxy hace el bucle por nosotros: lo que entra por un lado sale
        # por el otro, en las dos direcciones. Se queda aqui para siempre.
        # Nota: si no hay ningun RMC conectado, las peticiones esperan en el
        # broker hasta que llegue uno (el cliente vera su timeout).
        zmq.proxy(frontend, backend)
    except KeyboardInterrupt:
        log("detenido")
    finally:
        frontend.close(linger=0)
        backend.close(linger=0)
        contexto.term()
