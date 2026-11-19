"""
Prueba que la persistencia reconoce el uuid: si llega dos veces la MISMA
peticion (porque el cliente reintento), la segunda no se vuelve a hacer y
recibe la misma respuesta que la primera.

No usa la red: el RMC y la persistencia se llaman directamente, en este mismo
proceso, con una base de prueba. Asi se prueba solo la logica.

Uso:  python -m pruebas.prueba_uuid
"""
import sys

from config import configuracion as config
from config import E_mensaje as m
from capa_persistencia.cargar_datos import generar_eventos
from capa_persistencia.db import BaseDatos
from capa_persistencia.servicio_persistencia import ServicioPersistencia
from capa_negocios.rmc import ServicioRMC

resultados: list[bool] = []


def revisar(nombre, condicion):
    """Anota si una prueba paso o fallo y lo imprime."""
    resultados.append(condicion)
    print(f"[{'PASA' if condicion else 'FALLA'}] {nombre}")


class PersistenciaDirecta:
    """Hace de ClientePersistencia pero sin red: llama al servicio directamente."""

    def __init__(self, servicio):
        self.servicio = servicio

    def enviar(self, peticion, reintentar=True):
        return self.servicio.atender(peticion)


def main():
    bd = BaseDatos(config.RAIZ / "datos" / "prueba_uuid.db")
    bd.reiniciar()
    bd.cargar_eventos(generar_eventos())
    persistencia = ServicioPersistencia(bd)
    rmc = ServicioRMC(PersistenciaDirecta(persistencia))

    def ocupados(evento, fecha):
        return bd.disponibilidad(evento, fecha)["puestos_ocupados"]

    def reservas_activas():
        return bd.resumen()["reservas"]

    try:
        # ------------------------------------------------ persistencia sola
        print("--- persistencia: la misma escritura dos veces")
        p = m.crear_peticion(m.AGREGAR_RESERVA, 1, {"id_evento": "E1", "fecha": "2026/11/01", "n_asientos": 3})
        r1 = persistencia.atender(p)
        r2 = persistencia.atender(p)                          # mismo uuid: un reintento
        revisar("AGREGAR_RESERVA repetida: las dos dan EXITO con la misma reserva",
                r1["estado"] == r2["estado"] == m.EXITO
                and r1["datos"]["id_reserva"] == r2["datos"]["id_reserva"])
        revisar("  y en la base quedo UNA reserva de 3 asientos",
                reservas_activas() == 1 and ocupados("E1", "2026/11/01") == 3)
        id_reserva = r1["datos"]["id_reserva"]

        p = m.crear_peticion(m.AGREGAR_RESERVA, 1, {"id_evento": "E1", "fecha": "2026/11/01", "n_asientos": 3})
        persistencia.atender(p)                               # uuid NUEVO
        revisar("con otro uuid SI es otra reserva", reservas_activas() == 2 and ocupados("E1", "2026/11/01") == 6)

        p = m.crear_peticion(m.MODIFICAR_RESERVA, 1, {"id_reserva": id_reserva, "fecha": "2026/11/01",
                                                      "n_asientos": 5})
        r1, r2 = persistencia.atender(p), persistencia.atender(p)
        revisar("MODIFICAR_RESERVA repetida: misma respuesta, el cambio se aplico una vez",
                r1 == r2 and r1["estado"] == m.EXITO and ocupados("E1", "2026/11/01") == 8)

        p = m.crear_peticion(m.CANCELAR_RESERVA, 1, {"id_reserva": id_reserva})
        r1, r2 = persistencia.atender(p), persistencia.atender(p)
        revisar("CANCELAR_RESERVA repetida: la segunda da EXITO (no 'ya estaba cancelada')",
                r1 == r2 and r1["estado"] == m.EXITO and ocupados("E1", "2026/11/01") == 3)

        r = persistencia.atender(m.crear_peticion(m.AGREGAR_RESERVA, 1,
                                                  {"id_evento": "E1", "fecha": "2026/11/01", "n_asientos": 500}))
        revisar("una escritura que FALLA no se anota (no cambio nada)",
                r["estado"] == m.FALLO and bd.operacion_hecha(r["uuid"], m.AGREGAR_RESERVA) is None)

        # ------------------------------------------------ a traves del RMC
        print("\n--- RMC: reintentos del cliente (mismo uuid)")
        # Llenar E2 el 02 hasta 98 con otras reservas
        rmc.atender(m.crear_peticion(m.RESERVA, 2, {"id_evento": "E2", "fecha": "2026/11/02", "n_asientos": 98}))
        p = m.crear_peticion(m.RESERVA, 3, {"id_evento": "E2", "fecha": "2026/11/02", "n_asientos": 2})
        r1 = rmc.atender(p)                                   # llena el evento: 100/100
        r2 = rmc.atender(p)                                   # el reintento NO debe decir "sin cupo"
        revisar("reserva que lleno el evento, reintentada: EXITO con la misma reserva (no SIN_CUPO)",
                r1["estado"] == r2["estado"] == m.EXITO
                and r1["datos"]["id_reserva"] == r2["datos"]["id_reserva"]
                and ocupados("E2", "2026/11/02") == 100)

        id_r = r1["datos"]["id_reserva"]
        p = m.crear_peticion(m.MODIFICACION, 3, {"id_reserva": id_r, "fecha": "2026/11/16"})
        r1 = rmc.atender(p)
        r2 = rmc.atender(p)                                   # el reintento NO debe decir "no cambia nada"
        revisar("modificacion reintentada: EXITO igual (no 'la modificacion no cambia nada')",
                r1["estado"] == r2["estado"] == m.EXITO and r1["datos"] == r2["datos"]
                and ocupados("E2", "2026/11/02") == 98 and ocupados("E2", "2026/11/16") == 2)

        revisar("la base sigue consistente (ocupados = suma de reservas activas)", bd.verificar())
    finally:
        bd.cerrar()

    print(f"\n{sum(resultados)}/{len(resultados)} pruebas pasaron")
    sys.exit(0 if all(resultados) else 1)


if __name__ == "__main__":
    main()
