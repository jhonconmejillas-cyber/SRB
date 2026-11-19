r"""
Prueba la tolerancia a fallos del cliente (Lazy Pirate + failover gt_1 -> gt_2),
todo en un solo computador:

    clientes --> gt_1 :5555 (primario) --> broker_1 --+
             \                                        +--> RMC x2 --> persistencia
              -> gt_2 :5565 (respaldo) --> broker_2 --+
    servicio de cancelacion: escucha a gt_1 (:5557) y a gt_2 (:5567)

Revisa que:
  - un error de negocio (sin cupo) NO se reintenta: sale al primer intento,
  - mientras un cliente reserva sin parar, se MATA gt_1 (kill) y el cliente
    sigue con gt_2 sin que ninguna operacion termine en error,
  - una cancelacion hecha por gt_2 tambien recibe su aviso,
  - si se caen los dos GT, el cliente se rinde con ERROR NO_DISPONIBLE en un
    tiempo acotado (no se queda colgado).

Uso:  python -m pruebas.prueba_failover
Logs: resultados/failover_*.log
"""
import os
import sqlite3
import subprocess
import sys
import threading
import time

from config import configuracion as config
from config import E_mensaje as m

BD_PRUEBA = "datos/prueba_failover.db"
GTS = ["127.0.0.1:5555", "127.0.0.1:5565"]         # [primario, respaldo]
resultados: list[bool] = []


def revisar(nombre, condicion):
    """Anota si una prueba paso o fallo y lo imprime."""
    resultados.append(condicion)
    print(f"[{'PASA' if condicion else 'FALLA'}] {nombre}")


def arrancar(nombre, modulo, *argumentos, extra=None):
    """Lanza un proceso con su propio log. extra: variables de entorno distintas para el."""
    entorno = dict(os.environ, BD_RUTA=BD_PRUEBA, PYTHONIOENCODING="utf-8", **(extra or {}))
    log = open(config.RAIZ / "resultados" / f"failover_{nombre}.log", "w", encoding="utf-8")
    proceso = subprocess.Popen([sys.executable, "-m", modulo, *argumentos], cwd=config.RAIZ,
                               env=entorno, stdout=log, stderr=subprocess.STDOUT)
    proceso.log = log
    return proceso


def reservas_en_bd(id_cliente):
    """Cuantas reservas activas tiene un cliente en la base (para ver duplicados)."""
    with sqlite3.connect(config.RAIZ / BD_PRUEBA) as conexion:
        return conexion.execute("SELECT COUNT(*) FROM reserva WHERE id_cliente = ? AND activa = 1",
                                (id_cliente,)).fetchone()[0]


def main():
    # La configuracion de ESTE proceso tambien apunta a la base de prueba
    os.environ["BD_RUTA"] = BD_PRUEBA
    from capa_cliente.cliente import Cliente

    (config.RAIZ / "resultados").mkdir(exist_ok=True)
    subprocess.run([sys.executable, "-m", "capa_persistencia.cargar_datos"], cwd=config.RAIZ,
                   env=dict(os.environ), stdout=subprocess.DEVNULL, check=True)

    # gt_2 y broker_2 usan otros puertos solo porque estamos en un solo PC
    p = {
        "persistencia": arrancar("persistencia", "capa_persistencia.main"),
        "broker_1": arrancar("broker_1", "capa_negocios.main", "broker"),
        "broker_2": arrancar("broker_2", "capa_negocios.main", "broker",
                             extra={"BROKER_PUERTO_GT": "5569", "BROKER_PUERTO_RMC": "5566"}),
        "gt_1": arrancar("gt_1", "capa_negocios.main", "gt", "--broker", "tcp://127.0.0.1:5559"),
        "gt_2": arrancar("gt_2", "capa_negocios.main", "gt", "--broker", "tcp://127.0.0.1:5569",
                         extra={"GT_PUERTO_CLIENTES": "5565", "GT_PUERTO_CANCELACION": "5567"}),
        "cancelacion": arrancar("cancelacion", "capa_negocios.main", "cancelacion",
                                "--gts", "127.0.0.1:5557,127.0.0.1:5567"),
    }
    for letra in "AB":
        p[f"rmc_{letra}"] = arrancar(f"rmc_{letra}", "capa_negocios.main", "rmc",
                                     "--brokers", "127.0.0.1:5556,127.0.0.1:5566")
    time.sleep(2.5)                     # que todos abran puertos y se conecten

    # Tiempos cortos para que la prueba sea rapida: 1 s por intento
    avisos: list[dict] = []
    c1 = Cliente(1, gts=GTS, timeout_ms=1000, al_notificar=avisos.append)
    try:
        revisar("los 8 procesos arrancaron", all(x.poll() is None for x in p.values()))

        # ------------------------------------------- errores esperados
        print("\n--- error de negocio: no se reintenta")
        r, ms = c1.reservar("E1", "2026/11/01", 101)        # capacidad 100
        revisar(f"pedir 101 asientos => {r['estado']} {r['motivo']} al primer intento "
                f"({c1.ultimo_intentos} intento, {ms:.0f} ms)",
                r["estado"] in (m.FALLO, m.ERROR) and c1.ultimo_intentos == 1)
        r, _ = c1.reservar("E1", "2026/11/01", 2)
        revisar("una reserva normal sale por gt_1 (primario)", r["estado"] == m.EXITO and c1.gt_actual == 0)
        id_para_cancelar = r["datos"]["id_reserva"]

        # ------------------------------------------- se cae gt_1 con un cliente trabajando
        print("\n--- cliente 2 reserva sin parar; a los 1.5 s se mata gt_1 (kill)")
        registro: list[tuple[str, float, int]] = []          # (estado, ms, gt)
        detener = threading.Event()

        def trabajador():
            c2 = Cliente(2, gts=GTS, timeout_ms=1000)
            try:
                while not detener.is_set():
                    r, ms = c2.reservar("E2", "2026/11/02", 1)
                    registro.append((r["estado"], ms, c2.gt_actual + 1))
                    time.sleep(0.05)
            finally:
                trabajador.cambios = c2.cambios_de_gt
                c2.cerrar()

        hilo = threading.Thread(target=trabajador)
        hilo.start()
        time.sleep(1.5)
        p["gt_1"].kill()                                     # como kill -9: muere sin avisar
        p["gt_1"].wait()
        momento_caida = len(registro)
        print(f"      gt_1 muerto despues de {momento_caida} reservas")
        time.sleep(4)
        detener.set()
        hilo.join()

        exitos = sum(1 for estado, _, _ in registro if estado == m.EXITO)
        peor = max(ms for _, ms, _ in registro)
        despues = [gt for _, _, gt in registro[momento_caida + 1:]]
        revisar(f"todas las reservas salieron bien: {exitos}/{len(registro)}", exitos == len(registro))
        revisar(f"el cliente cambio de GT una vez ({trabajador.cambios})", trabajador.cambios == 1)
        revisar("despues de la caida todas van por gt_2", bool(despues) and all(g == 2 for g in despues))
        revisar(f"la peor espera durante el failover fue acotada ({peor:.0f} ms < 4000 ms)", peor < 4000)
        # La reserva que iba "en vuelo" cuando murio gt_1 SI se hizo en la base,
        # pero su respuesta se perdio. El cliente la reintento por gt_2 con el
        # mismo uuid y la persistencia la reconocio: no hay duplicados.
        en_bd = reservas_en_bd(2)
        revisar(f"sin duplicados: {exitos} reservas con EXITO y {en_bd} en la base", en_bd == exitos)

        # ------------------------------------------- cancelacion por gt_2
        print("\n--- cliente 1 cancela (gt_1 ya no esta)")
        r, _ = c1.cancelar(id_para_cancelar)
        revisar(f"ACK por gt_2 (gt actual: gt_{c1.gt_actual + 1})", r["estado"] == m.ACK and c1.gt_actual == 1)
        revisar("llega el aviso: el servicio de cancelacion tambien escucha a gt_2",
                c1.esperar_notificaciones(5) == 0 and avisos and avisos[0]["estado"] == m.EXITO)

        # ------------------------------------------- se caen los dos
        print("\n--- se mata tambien gt_2")
        p["gt_2"].kill()
        p["gt_2"].wait()
        r, ms = c1.consultar("noviembre")
        revisar(f"sin ningun GT: ERROR NO_DISPONIBLE tras {c1.ultimo_intentos} intentos en {ms / 1000:.1f} s",
                r["estado"] == m.ERROR and r["motivo"] == m.NO_DISPONIBLE
                and c1.ultimo_intentos == config.CLIENTE_INTENTOS)
    finally:
        c1.cerrar()
        for x in p.values():
            x.kill()
        for x in p.values():
            x.wait()
            x.log.close()

    print(f"\n{sum(resultados)}/{len(resultados)} pruebas pasaron")
    sys.exit(0 if all(resultados) else 1)


if __name__ == "__main__":
    main()
