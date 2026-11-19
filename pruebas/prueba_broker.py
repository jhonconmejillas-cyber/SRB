"""
Prueba la escalabilidad con brokers, todo en un solo computador:

    gt_1 :5555 --> broker_1 (:5559 | :5556) --+
                                              +--> RMC A, RMC B, RMC C  (cada uno conectado a los DOS brokers)
    gt_2 :5565 --> broker_2 (:5569 | :5566) --+

Revisa que:
  - los dos GT atienden (cada uno por su broker),
  - el trabajo se reparte entre los 3 RMC (round-robin),
  - si se cae un RMC, los otros siguen atendiendo,
  - si se cae broker_1, gt_2 sigue funcionando (los RMC siguen con broker_2).

Uso:  python -m pruebas.prueba_broker
Logs: resultados/broker_*.log
"""
import os
import subprocess
import sys
import time

import zmq

from config import configuracion as config
from config import E_mensaje as m

BD_PRUEBA = "datos/prueba_broker.db"
resultados: list[bool] = []


def revisar(nombre, condicion):
    """Anota si una prueba paso o fallo y lo imprime."""
    resultados.append(condicion)
    print(f"[{'PASA' if condicion else 'FALLA'}] {nombre}")


def arrancar(nombre, modulo, *argumentos, extra=None):
    """Lanza un proceso con su propio log. extra: variables de entorno distintas para el."""
    entorno = dict(os.environ, BD_RUTA=BD_PRUEBA, PYTHONIOENCODING="utf-8", **(extra or {}))
    log = open(config.RAIZ / "resultados" / f"broker_{nombre}.log", "w", encoding="utf-8")
    proceso = subprocess.Popen([sys.executable, "-m", modulo, *argumentos], cwd=config.RAIZ,
                               env=entorno, stdout=log, stderr=subprocess.STDOUT)
    proceso.log = log
    return proceso


def pedir(contexto, puerto_gt, tipo, datos, timeout_ms=3000):
    """Una peticion a un GT con un REQ nuevo; devuelve la respuesta o None si no contesto."""
    socket = contexto.socket(zmq.REQ)
    socket.setsockopt(zmq.LINGER, 0)
    socket.connect(f"tcp://127.0.0.1:{puerto_gt}")
    try:
        socket.send(m.codificar(m.crear_peticion(tipo, 1, datos)))
        if socket.poll(timeout_ms, zmq.POLLIN):
            return m.decodificar(socket.recv())
        return None
    finally:
        socket.close()


def atendidas(nombre):
    """Cuantas peticiones atendio un RMC (lineas de su log que dicen '=>')."""
    texto = (config.RAIZ / "resultados" / f"broker_{nombre}.log").read_text(encoding="utf-8")
    return sum(1 for linea in texto.splitlines() if "=>" in linea)


def main():
    (config.RAIZ / "resultados").mkdir(exist_ok=True)
    subprocess.run([sys.executable, "-m", "capa_persistencia.cargar_datos"], cwd=config.RAIZ,
                   env=dict(os.environ, BD_RUTA=BD_PRUEBA), stdout=subprocess.DEVNULL, check=True)

    brokers_rmc = "127.0.0.1:5556,127.0.0.1:5566"
    p = {
        "persistencia": arrancar("persistencia", "capa_persistencia.main"),
        "broker_1": arrancar("broker_1", "capa_negocios.main", "broker"),
        "broker_2": arrancar("broker_2", "capa_negocios.main", "broker",
                             extra={"BROKER_PUERTO_GT": "5569", "BROKER_PUERTO_RMC": "5566"}),
        "gt_1": arrancar("gt_1", "capa_negocios.main", "gt", "--broker", "tcp://127.0.0.1:5559"),
        "gt_2": arrancar("gt_2", "capa_negocios.main", "gt", "--broker", "tcp://127.0.0.1:5569",
                         extra={"GT_PUERTO_CLIENTES": "5565", "GT_PUERTO_CANCELACION": "5567"}),
    }
    for letra in "ABC":
        p[f"rmc_{letra}"] = arrancar(f"rmc_{letra}", "capa_negocios.main", "rmc", "--brokers", brokers_rmc)
    time.sleep(2.5)                     # que todos abran puertos y se conecten

    contexto = zmq.Context()
    try:
        caidos = [n for n, x in p.items() if x.poll() is not None]
        revisar("los 9 procesos arrancaron", not caidos)

        # ---------------------------------------------- los dos GT, 3 RMC
        print("\n--- 30 consultas a gt_1 y 30 a gt_2")
        ok_1 = sum(1 for _ in range(30)
                   if (r := pedir(contexto, 5555, m.CONSULTA, {"mes": "noviembre"})) and r["estado"] == m.EXITO)
        ok_2 = sum(1 for _ in range(30)
                   if (r := pedir(contexto, 5565, m.CONSULTA, {"mes": "noviembre"})) and r["estado"] == m.EXITO)
        revisar(f"gt_1 respondio 30/30 ({ok_1})", ok_1 == 30)
        revisar(f"gt_2 respondio 30/30 ({ok_2})", ok_2 == 30)
        time.sleep(0.3)
        cuenta = {x: atendidas(f"rmc_{x}") for x in "ABC"}
        print(f"      peticiones por RMC: {cuenta}")
        revisar("los 3 RMC recibieron trabajo (reparto round-robin)", all(v > 0 for v in cuenta.values()))
        revisar("entre los 3 atendieron las 60", sum(cuenta.values()) == 60)

        r = pedir(contexto, 5565, m.RESERVA, {"id_evento": "E1", "fecha": "2026/11/01", "n_asientos": 2})
        revisar("una reserva por gt_2 funciona", r is not None and r["estado"] == m.EXITO)

        # ---------------------------------------------- cae un RMC
        print("\n--- se cae el RMC A")
        p["rmc_A"].kill()
        p["rmc_A"].wait()
        time.sleep(0.5)
        ok = sum(1 for _ in range(20)
                 if (r := pedir(contexto, 5555, m.CONSULTA, {"mes": "noviembre"})) and r["estado"] == m.EXITO)
        revisar(f"con 2 RMC, gt_1 sigue respondiendo 20/20 ({ok})", ok == 20)

        # ---------------------------------------------- cae broker_1
        print("\n--- se cae broker_1")
        p["broker_1"].kill()
        p["broker_1"].wait()
        time.sleep(0.5)
        r = pedir(contexto, 5555, m.CONSULTA, {"mes": "noviembre"})
        revisar("gt_1 sin broker responde ERROR NO_DISPONIBLE (no se queda colgado)",
                r is not None and r["estado"] == m.ERROR and r["motivo"] == m.NO_DISPONIBLE)
        ok = sum(1 for _ in range(20)
                 if (r := pedir(contexto, 5565, m.CONSULTA, {"mes": "noviembre"})) and r["estado"] == m.EXITO)
        revisar(f"gt_2 sigue respondiendo 20/20 con broker_2 ({ok})", ok == 20)

        # ---------------------------------------------- vuelve broker_1
        print("\n--- se vuelve a lanzar broker_1")
        p["broker_1"] = arrancar("broker_1b", "capa_negocios.main", "broker")
        time.sleep(1.5)                 # ZeroMQ reconecta solo a GT y RMC
        r = pedir(contexto, 5555, m.CONSULTA, {"mes": "noviembre"})
        revisar("gt_1 vuelve a funcionar sin reiniciar GT ni RMC (reconexion automatica)",
                r is not None and r["estado"] == m.EXITO)
    finally:
        contexto.term()
        for x in p.values():
            x.kill()
        for x in p.values():
            x.wait()
            x.log.close()

    print(f"\n{sum(resultados)}/{len(resultados)} pruebas pasaron")
    sys.exit(0 if all(resultados) else 1)


if __name__ == "__main__":
    main()
