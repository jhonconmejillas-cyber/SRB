"""
Prueba de punta a punta de MODIFICACION y CANCELACION (asincrona con notificacion).

Esta prueba lo hace todo sola:
  1. carga una base de datos DE PRUEBA (datos/prueba_sistema.db; la real no se toca)
  2. arranca persistencia, GT, RMC y servicio de cancelacion como procesos aparte
  3. usa la clase Cliente, igual que un cliente de verdad
  4. al final apaga todo

Requiere que los puertos del .env esten libres (no tener el sistema corriendo).

Uso (desde la raiz del proyecto):
    python -m pruebas.prueba_sistema
"""
import os
import subprocess
import sys
import threading
import time

from config import configuracion as config
from config import E_mensaje as m

BD_PRUEBA = "datos/prueba_sistema.db"
resultados: list[bool] = []


def revisar(nombre, condicion):
    """Anota si una prueba paso o fallo y lo imprime."""
    resultados.append(bool(condicion))
    print(f"[{'PASA' if condicion else 'FALLA'}] {nombre}", flush=True)


def entorno_prueba():
    """Variables de entorno para los procesos hijos: usan la BD de prueba."""
    entorno = dict(os.environ, BD_RUTA=BD_PRUEBA, PYTHONIOENCODING="utf-8")
    return entorno


def arrancar(modulo, *argumentos, log):
    """Arranca 'python -m modulo argumentos' en segundo plano; su salida va al archivo log."""
    return subprocess.Popen([sys.executable, "-m", modulo, *argumentos], cwd=config.RAIZ,
                            env=entorno_prueba(), stdout=log, stderr=subprocess.STDOUT)


def ocupados(bd, evento, fecha):
    """Puestos ocupados de una ocurrencia, leidos directo de la base de prueba."""
    return bd.disponibilidad(evento, fecha)["puestos_ocupados"]


def main():
    # La configuracion de ESTE proceso tambien debe apuntar a la base de prueba
    os.environ["BD_RUTA"] = BD_PRUEBA
    config.BD_RUTA = config.RAIZ / BD_PRUEBA
    from capa_persistencia.db import BaseDatos
    from capa_cliente.cliente import Cliente

    # 1. base de prueba recien cargada con el caso del enunciado
    subprocess.run([sys.executable, "-m", "capa_persistencia.cargar_datos"], cwd=config.RAIZ,
                   env=entorno_prueba(), stdout=subprocess.DEVNULL, check=True)

    # 2. arrancar los servicios (sus logs quedan en resultados/prueba_*.log)
    carpeta_logs = config.RAIZ / "resultados"
    carpeta_logs.mkdir(exist_ok=True)
    logs = {n: open(carpeta_logs / f"prueba_{n}.log", "w", encoding="utf-8")
            for n in ("persistencia", "gt", "rmc", "cancelacion")}
    procesos = {
        "persistencia": arrancar("capa_persistencia.main", log=logs["persistencia"]),
        "gt": arrancar("capa_negocios.main", "gt", log=logs["gt"]),
        "rmc": arrancar("capa_negocios.main", "rmc", log=logs["rmc"]),
        "cancelacion": arrancar("capa_negocios.main", "cancelacion", log=logs["cancelacion"]),
    }
    time.sleep(2)                       # darles tiempo de abrir sus puertos

    bd = BaseDatos(config.RAIZ / BD_PRUEBA)
    avisos_c1: list[dict] = []
    c1 = Cliente(1, al_notificar=avisos_c1.append)    # guarda los avisos en vez de imprimirlos
    c2 = Cliente(2, al_notificar=lambda aviso: None)
    try:
        caidos = [n for n, p in procesos.items() if p.poll() is not None]
        if caidos:
            print(f"No arrancaron: {caidos}. Revise resultados/prueba_*.log (puertos ocupados?)")
            return

        # ================================================= MODIFICACION
        print("\n--- MODIFICACION (sincrona)")
        r, _ = c1.reservar("E1", "2026/11/01", 3)
        a = r["datos"]["id_reserva"]
        revisar("reserva inicial E1 2026/11/01 x3", r["estado"] == m.EXITO)

        r, _ = c1.modificar(a, n_asientos=5)
        revisar("cambiar a 5 asientos: EXITO", r["estado"] == m.EXITO and r["datos"]["n_asientos"] == 5)
        revisar("  la BD tiene 5 ocupados el 01", ocupados(bd, "E1", "2026/11/01") == 5)

        r, _ = c1.modificar(a, fecha="2026/11/15")
        revisar("cambiar a la otra fecha del evento (15): EXITO y conserva 5 asientos",
                r["estado"] == m.EXITO and r["datos"]["fecha"] == "2026/11/15" and r["datos"]["n_asientos"] == 5)
        revisar("  la BD movio los asientos: 01 -> 0, 15 -> 5",
                ocupados(bd, "E1", "2026/11/01") == 0 and ocupados(bd, "E1", "2026/11/15") == 5)

        r, _ = c1.modificar(a, fecha="2026/11/1", n_asientos=2)
        revisar("cambiar fecha y asientos a la vez (fecha sin cero '2026/11/1'): EXITO",
                r["estado"] == m.EXITO and r["datos"]["fecha"] == "2026/11/01")

        r, _ = c1.modificar(a, n_asientos=200)
        revisar("pedir 200 asientos: FALLO SIN_CUPO", r["estado"] == m.FALLO and r["motivo"] == m.SIN_CUPO)
        revisar("  la reserva quedo como estaba (2 asientos el 01)", ocupados(bd, "E1", "2026/11/01") == 2)

        r, _ = c1.reservar("E30", "2026/11/02", 1)
        b = r["datos"]["id_reserva"]
        r, _ = c1.modificar(b, fecha="2026/11/16")
        revisar("E30 solo ocurre una vez: cambiar de fecha da FALLO NO_EXISTE",
                r["estado"] == m.FALLO and r["motivo"] == m.NO_EXISTE)
        r, _ = c1.modificar(b, n_asientos=4)
        revisar("E30 si permite cambiar el numero de asientos", r["estado"] == m.EXITO)

        r, _ = c2.modificar(a, n_asientos=1)
        revisar("otro cliente no puede modificar mi reserva: FALLO NO_EXISTE",
                r["estado"] == m.FALLO and r["motivo"] == m.NO_EXISTE)
        r, _ = c1.modificar(99999, n_asientos=1)
        revisar("reserva inexistente: FALLO NO_EXISTE", r["estado"] == m.FALLO and r["motivo"] == m.NO_EXISTE)
        r, _ = c1.modificar(a, n_asientos=2)
        revisar("modificacion que no cambia nada: ERROR DATO_INVALIDO",
                r["estado"] == m.ERROR and r["motivo"] == m.DATO_INVALIDO)
        r, _ = c1.modificar(a)
        revisar("modificacion sin fecha ni asientos: ERROR DATO_INVALIDO",
                r["estado"] == m.ERROR and r["motivo"] == m.DATO_INVALIDO)
        r, _ = c1.modificar(a, fecha="15-11-2026")
        revisar("fecha mal escrita: ERROR DATO_INVALIDO", r["estado"] == m.ERROR and r["motivo"] == m.DATO_INVALIDO)

        # Cambio de fecha justo hasta el limite del cupo
        c2.reservar("E2", "2026/11/16", 98)                     # deja 2 libres el 16
        r, _ = c1.reservar("E2", "2026/11/02", 2)
        d = r["datos"]["id_reserva"]
        r, _ = c1.modificar(d, fecha="2026/11/16")
        revisar("mover 2 asientos a una fecha con exactamente 2 libres: EXITO", r["estado"] == m.EXITO)
        r, _ = c1.modificar(d, n_asientos=3)
        revisar("subir a 3 cuando ya no queda cupo: FALLO SIN_CUPO",
                r["estado"] == m.FALLO and r["motivo"] == m.SIN_CUPO)

        # ================================================= CANCELACION
        print("\n--- CANCELACION (asincrona con notificacion)")
        r, ms = c1.cancelar(a)
        revisar(f"el GT acepta la cancelacion de inmediato con ACK ({ms:.1f} ms)", r["estado"] == m.ACK)
        quedan = c1.esperar_notificaciones(5)
        revisar("llega la NOTIFICACION al buzon del cliente", quedan == 0 and len(avisos_c1) == 1)
        aviso = avisos_c1[-1] if avisos_c1 else {}
        revisar("  la notificacion es de esa cancelacion (mismo uuid) y dice EXITO",
                aviso.get("tipo") == m.NOTIFICACION and aviso.get("uuid") == r["uuid"]
                and aviso.get("estado") == m.EXITO and aviso["datos"].get("id_reserva") == a)
        revisar("  la BD libero los asientos (E1 el 01 queda en 0)", ocupados(bd, "E1", "2026/11/01") == 0)

        r, _ = c1.cancelar(a)
        c1.esperar_notificaciones(5)
        revisar("cancelar otra vez la misma: ACK, y luego aviso FALLO NO_EXISTE",
                r["estado"] == m.ACK and avisos_c1[-1]["estado"] == m.FALLO
                and avisos_c1[-1]["motivo"] == m.NO_EXISTE)

        r, _ = c1.modificar(a, n_asientos=1)
        revisar("una reserva cancelada ya no se puede modificar", r["estado"] == m.FALLO)

        antes = len(avisos_c1)
        r, _ = c1.cancelar(0)
        revisar("id_reserva invalido: el GT responde ERROR DATO_INVALIDO de inmediato (sin aviso)",
                r["estado"] == m.ERROR and r["motivo"] == m.DATO_INVALIDO)

        r, _ = c2.cancelar(d)                    # d es de c1
        c2.esperar_notificaciones(5)
        revisar("otro cliente no puede cancelar mi reserva: su aviso dice FALLO",
                c2.notificaciones and c2.notificaciones[-1]["estado"] == m.FALLO)
        revisar("  y mi reserva sigue ahi (E2 el 16 sigue lleno)", ocupados(bd, "E2", "2026/11/16") == 100)
        revisar("  a mi no me llego ningun aviso de eso", len(avisos_c1) == antes)

        # Varias cancelaciones seguidas: el cliente no espera entre una y otra
        ids = [c1.reservar("E7", "2026/11/07", 1)[0]["datos"]["id_reserva"] for _ in range(5)]
        inicio = time.perf_counter()
        acks = [c1.cancelar(i)[0]["estado"] for i in ids]
        ms_total = (time.perf_counter() - inicio) * 1000
        revisar(f"5 cancelaciones seguidas, todas con ACK ({ms_total:.1f} ms en total)",
                acks == [m.ACK] * 5)
        revisar("  llegan los 5 avisos", c1.esperar_notificaciones(5) == 0)
        revisar("  E7 el 07 queda en 0", ocupados(bd, "E7", "2026/11/07") == 0)

        # ================================================= CONCURRENCIA
        print("\n--- CONCURRENCIA: 6 clientes reservan, modifican y cancelan a la vez sobre E3")
        errores: list[str] = []

        def trabajador(id_cliente):
            cliente = Cliente(id_cliente, al_notificar=lambda aviso: None)
            try:
                for _ in range(8):
                    r, _ = cliente.reservar("E3", "2026/11/03", 5)
                    if r["estado"] != m.EXITO:
                        continue
                    i = r["datos"]["id_reserva"]
                    cliente.modificar(i, fecha="2026/11/17")
                    cliente.modificar(i, n_asientos=8)
                    if i % 2 == 0:
                        cliente.cancelar(i)
                if cliente.esperar_notificaciones(10):
                    errores.append(f"cliente {id_cliente}: faltaron avisos")
            except Exception as e:
                errores.append(f"cliente {id_cliente}: {e}")
            finally:
                cliente.cerrar()

        hilos = [threading.Thread(target=trabajador, args=(10 + n,)) for n in range(6)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        for e in errores:
            print("   ", e)
        revisar("todos los clientes terminaron y recibieron sus avisos", not errores)
        revisar("nunca se paso de la capacidad (E3 el 03 y el 17 <= 100)",
                ocupados(bd, "E3", "2026/11/03") <= 100 and ocupados(bd, "E3", "2026/11/17") <= 100)
        revisar("la BD es consistente: ocupados = suma de reservas activas", bd.verificar())

        # ================================================= SERVICIO CAIDO
        print("\n--- si el servicio de cancelacion no esta")
        procesos["cancelacion"].terminate()
        procesos["cancelacion"].wait()
        time.sleep(0.5)
        r, _ = c1.reservar("E9", "2026/11/09", 1)
        r, _ = c1.cancelar(r["datos"]["id_reserva"])
        revisar("el GT responde ERROR NO_DISPONIBLE en vez de ACK",
                r["estado"] == m.ERROR and r["motivo"] == m.NO_DISPONIBLE)
    finally:
        c1.cerrar()
        c2.cerrar()
        bd.cerrar()
        for p in procesos.values():
            p.terminate()
        for p in procesos.values():
            p.wait()
        for f in logs.values():
            f.close()

    print(f"\n{sum(resultados)}/{len(resultados)} pruebas pasaron")
    print("Logs de los servicios en resultados/prueba_*.log")
    sys.exit(0 if all(resultados) else 1)


if __name__ == "__main__":
    main()
