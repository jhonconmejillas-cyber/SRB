"""
Pruebas de la capa de datos (db.py), sin red.

OJO: BORRA la base de datos y la vuelve a cargar con el caso del enunciado.
Para no tocar la base real, se puede usar otra:
    BD_RUTA=datos/prueba.db python -m capa_persistencia.prueba

Uso:  python -m capa_persistencia.prueba
"""
import threading

from capa_persistencia.db import BaseDatos, NoExiste, SinCupo
from capa_persistencia.cargar_datos import generar_eventos

resultados: list[bool] = []


def revisar(nombre, condicion):
    """Anota si una prueba paso o fallo y lo imprime."""
    resultados.append(condicion)
    print(f"[{'PASA' if condicion else 'FALLA'}] {nombre}")


def main():
    bd = BaseDatos()
    bd.reiniciar()
    bd.cargar_eventos(generar_eventos())

    # --- consultas
    noviembre = bd.consultar_mes(11)
    revisar("noviembre tiene 70 ocurrencias", len(noviembre) == 70)
    revisar("octubre no tiene ocurrencias", bd.consultar_mes(10) == [])
    revisar("E1 el 2026/11/01 tiene 100 libres",
            bd.disponibilidad("E1", "2026/11/01")["libres"] == 100)
    try:
        bd.disponibilidad("E99", "2026/11/01")
        revisar("disponibilidad de un evento inexistente lanza NoExiste", False)
    except NoExiste:
        revisar("disponibilidad de un evento inexistente lanza NoExiste", True)

    # --- reservas
    r = bd.agregar_reserva(7, "E1", "2026/11/01", 10)
    revisar("reserva valida devuelve su id", r["id_reserva"] == 1)
    revisar("quedan 90 libres", bd.disponibilidad("E1", "2026/11/01")["libres"] == 90)
    try:
        bd.agregar_reserva(7, "E1", "2026/11/01", 91)
        revisar("91 asientos con 90 libres lanza SinCupo", False)
    except SinCupo:
        revisar("91 asientos con 90 libres lanza SinCupo", True)
    revisar("el rechazo no cambio nada", bd.disponibilidad("E1", "2026/11/01")["libres"] == 90)
    bd.agregar_reserva(8, "E1", "2026/11/01", 90)
    revisar("se pueden reservar exactamente los 90 restantes",
            bd.disponibilidad("E1", "2026/11/01")["libres"] == 0)
    try:
        bd.agregar_reserva(7, "E30", "2026/11/16", 1)
        revisar("fecha en que el evento no ocurre lanza NoExiste", False)
    except NoExiste:
        revisar("fecha en que el evento no ocurre lanza NoExiste", True)

    # --- concurrencia: 8 hilos, cada uno con su conexion, 150 intentos de 1 asiento en E2
    exitos = []

    def comprador(id_cliente, intentos):
        mi_bd = BaseDatos()                      # una conexion por hilo
        ok = 0
        for _ in range(intentos):
            try:
                mi_bd.agregar_reserva(id_cliente, "E2", "2026/11/02", 1)
                ok += 1
            except SinCupo:
                pass
        mi_bd.cerrar()
        exitos.append(ok)

    hilos = [threading.Thread(target=comprador, args=(100 + i, 19 if i < 6 else 18))
             for i in range(8)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    revisar(f"sin sobreventa: {sum(exitos)} reservas aceptadas de 150 intentos", sum(exitos) == 100)
    revisar("E2 quedo lleno", bd.disponibilidad("E2", "2026/11/02")["libres"] == 0)

    revisar("ocupados coinciden con la suma de reservas", bd.verificar())

    # --- modificaciones (E5 ocurre el 05 y el 19; E40 solo el 12)
    r = bd.agregar_reserva(20, "E5", "2026/11/05", 4)
    id_r = r["id_reserva"]
    bd.modificar_reserva(20, id_r, "2026/11/05", 10)
    revisar("modificar a mas asientos ocupa solo la diferencia (10 ocupados)",
            bd.disponibilidad("E5", "2026/11/05")["puestos_ocupados"] == 10)
    bd.modificar_reserva(20, id_r, "2026/11/05", 6)
    revisar("modificar a menos asientos libera los que sobran (6 ocupados)",
            bd.disponibilidad("E5", "2026/11/05")["puestos_ocupados"] == 6)
    m_ok = bd.modificar_reserva(20, id_r, "2026/11/19", 6)
    revisar("cambiar de fecha mueve los asientos: el 05 queda en 0",
            bd.disponibilidad("E5", "2026/11/05")["puestos_ocupados"] == 0)
    revisar("cambiar de fecha mueve los asientos: el 19 queda en 6",
            bd.disponibilidad("E5", "2026/11/19")["puestos_ocupados"] == 6)
    revisar("la modificacion informa como estaba antes",
            m_ok["antes"] == {"fecha": "2026/11/05", "n_asientos": 6})
    bd.modificar_reserva(20, id_r, "2026/11/05", 2)
    revisar("cambiar fecha y asientos a la vez: el 05 queda en 2 y el 19 en 0",
            bd.disponibilidad("E5", "2026/11/05")["puestos_ocupados"] == 2
            and bd.disponibilidad("E5", "2026/11/19")["puestos_ocupados"] == 0)

    bd.agregar_reserva(21, "E5", "2026/11/19", 99)          # deja 1 libre el 19
    try:
        bd.modificar_reserva(20, id_r, "2026/11/19", 2)
        revisar("cambiar a una fecha sin cupo lanza SinCupo", False)
    except SinCupo:
        revisar("cambiar a una fecha sin cupo lanza SinCupo", True)
    revisar("la modificacion rechazada no cambio nada",
            bd.consultar_reserva(20, id_r)["fecha"] == "2026/11/05"
            and bd.disponibilidad("E5", "2026/11/05")["puestos_ocupados"] == 2
            and bd.disponibilidad("E5", "2026/11/19")["puestos_ocupados"] == 99)

    r40 = bd.agregar_reserva(20, "E40", "2026/11/12", 1)
    try:
        bd.modificar_reserva(20, r40["id_reserva"], "2026/11/26", 1)
        revisar("evento con una sola fecha: cambiar de fecha lanza NoExiste", False)
    except NoExiste:
        revisar("evento con una sola fecha: cambiar de fecha lanza NoExiste", True)
    try:
        bd.modificar_reserva(99, id_r, "2026/11/05", 3)
        revisar("un cliente no puede modificar la reserva de otro", False)
    except NoExiste:
        revisar("un cliente no puede modificar la reserva de otro", True)

    # --- cancelaciones
    c = bd.cancelar_reserva(20, id_r)
    revisar("cancelar libera los asientos (E5 el 05 queda en 0)",
            c["asientos_liberados"] == 2 and bd.disponibilidad("E5", "2026/11/05")["puestos_ocupados"] == 0)
    try:
        bd.cancelar_reserva(20, id_r)
        revisar("cancelar dos veces la misma reserva lanza NoExiste", False)
    except NoExiste:
        revisar("cancelar dos veces la misma reserva lanza NoExiste", True)
    try:
        bd.modificar_reserva(20, id_r, "2026/11/05", 1)
        revisar("una reserva cancelada ya no se puede modificar", False)
    except NoExiste:
        revisar("una reserva cancelada ya no se puede modificar", True)
    try:
        bd.cancelar_reserva(99, r40["id_reserva"])
        revisar("un cliente no puede cancelar la reserva de otro", False)
    except NoExiste:
        revisar("un cliente no puede cancelar la reserva de otro", True)

    revisar("al final, ocupados siguen coincidiendo con la suma de reservas", bd.verificar())
    print()
    bd.imprimir_estado(solo_ocupadas=True)
    bd.cerrar()
    print(f"\n{sum(resultados)}/{len(resultados)} pruebas pasaron")


if __name__ == "__main__":
    main()