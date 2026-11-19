"""
Generador de carga continua: un cliente envia peticiones a un ritmo fijo
durante un tiempo y guarda cada resultado en un CSV (para las metricas).

La mezcla de operaciones es configurable con porcentajes:
    pct_consultas + pct_modificaciones + pct_cancelaciones  (el resto son reservas)

Las modificaciones y cancelaciones se hacen sobre reservas que ESTE cliente
hizo antes. Si todavia no tiene ninguna, en su lugar hace una reserva.
"""
import csv
import random
import time
from pathlib import Path

from config import E_mensaje as m

# intentos: cuantos envios necesito la peticion (1 = salio a la primera)
# gt: a que GT le hablo al final (1 = primario, 2 = respaldo)
COLUMNAS = ["hora_envio", "id_cliente", "tipo", "estado", "motivo", "ms", "uuid", "intentos", "gt"]


def obtener_catalogo(cliente, mes):
    """
    Consulta el mes una vez para saber que eventos y fechas existen.
    Devuelve {id_evento: [fecha1, fecha2, ...]}.
    """
    respuesta, _ = cliente.consultar(mes)
    if respuesta["estado"] != m.EXITO:
        raise RuntimeError(f"no se pudo obtener el catalogo: {respuesta['motivo']}")
    catalogo: dict[str, list[str]] = {}
    for e in respuesta["datos"]["eventos"]:
        catalogo.setdefault(e["id_evento"], []).append(e["fecha"])
    return catalogo


def elegir_operacion(azar, pct_consultas, pct_modificaciones, pct_cancelaciones):
    """Tira un numero entre 0 y 100 y segun donde caiga elige la operacion."""
    tiro = azar.uniform(0, 100)
    if tiro < pct_consultas:
        return m.CONSULTA
    if tiro < pct_consultas + pct_modificaciones:
        return m.MODIFICACION
    if tiro < pct_consultas + pct_modificaciones + pct_cancelaciones:
        return m.CANCELACION
    return m.RESERVA


def generar_carga(cliente, ruta_csv, duracion_s, por_minuto, pct_consultas,
                  pct_modificaciones=0, pct_cancelaciones=0,
                  mes="noviembre", max_asientos=3, semilla=None):
    """
    Envia peticiones durante duracion_s segundos, por_minuto peticiones por minuto.
    Devuelve cuantas peticiones envio.
    """
    azar = random.Random(semilla)        # con la misma semilla se repite la misma corrida
    catalogo = obtener_catalogo(cliente, mes)
    eventos = list(catalogo)
    intervalo = 60.0 / por_minuto        # segundos entre una peticion y la siguiente

    # Reservas vigentes de este cliente: id_reserva -> (id_evento, fecha, n_asientos)
    mis_reservas: dict[int, tuple[str, str, int]] = {}

    ruta = Path(ruta_csv)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    enviadas = 0
    with open(ruta, "w", newline="", encoding="utf-8") as archivo:
        escritor = csv.writer(archivo)
        escritor.writerow(COLUMNAS)

        inicio = time.time()
        siguiente = inicio
        while time.time() - inicio < duracion_s:
            hora_envio = time.time()
            tipo = elegir_operacion(azar, pct_consultas, pct_modificaciones, pct_cancelaciones)
            if tipo in (m.MODIFICACION, m.CANCELACION) and not mis_reservas:
                tipo = m.RESERVA             # no hay nada que modificar o cancelar aun

            if tipo == m.CONSULTA:
                respuesta, ms = cliente.consultar(mes)

            elif tipo == m.RESERVA:
                evento = azar.choice(eventos)
                fecha = azar.choice(catalogo[evento])
                n = azar.randint(1, max_asientos)
                respuesta, ms = cliente.reservar(evento, fecha, n)
                if respuesta["estado"] == m.EXITO:
                    mis_reservas[respuesta["datos"]["id_reserva"]] = (evento, fecha, n)

            elif tipo == m.MODIFICACION:
                id_reserva = azar.choice(list(mis_reservas))
                evento, fecha, n = mis_reservas[id_reserva]
                otras_fechas = [f for f in catalogo[evento] if f != fecha]
                if otras_fechas and azar.random() < 0.5:
                    # cambiar de fecha (solo si el evento tiene otra en el mes)
                    respuesta, ms = cliente.modificar(id_reserva, fecha=azar.choice(otras_fechas))
                else:
                    # cambiar el numero de asientos (a uno distinto del actual)
                    nuevo = azar.choice([x for x in range(1, max_asientos + 2) if x != n])
                    respuesta, ms = cliente.modificar(id_reserva, n_asientos=nuevo)
                if respuesta["estado"] == m.EXITO:
                    d = respuesta["datos"]
                    mis_reservas[id_reserva] = (d["id_evento"], d["fecha"], d["n_asientos"])

            else:  # CANCELACION (asincrona: aqui solo se mide el ACK)
                id_reserva = azar.choice(list(mis_reservas))
                respuesta, ms = cliente.cancelar(id_reserva)
                if respuesta["estado"] == m.ACK:
                    del mis_reservas[id_reserva]

            escritor.writerow([f"{hora_envio:.3f}", cliente.id_cliente, tipo, respuesta["estado"],
                               respuesta["motivo"] or "", f"{ms:.3f}", respuesta["uuid"],
                               cliente.ultimo_intentos, cliente.gt_actual + 1])
            enviadas += 1

            # Ritmo fijo: la siguiente peticion sale 'intervalo' segundos despues de la
            # anterior, no despues de que llego la respuesta. Asi el ritmo no se atrasa.
            siguiente += intervalo
            espera = siguiente - time.time()
            if espera > 0:
                time.sleep(espera)
            else:
                siguiente = time.time()      # el sistema va lento: no acumular atraso
    return enviadas
