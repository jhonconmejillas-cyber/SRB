"""
Ejecuta un caso de prueba escrito en un archivo de texto, una operacion por linea:

    # comentario
    CONSULTA  noviembre
    RESERVA   E1 2026/11/01 2         (evento, fecha, numero de asientos)
    MODIFICAR R1 5                    (cambia la reserva a 5 asientos)
    MODIFICAR R1 2026/11/15           (cambia la reserva a otra fecha, mismos asientos)
    MODIFICAR R1 2026/11/15 5         (cambia las dos cosas)
    CANCELAR  R1                      (cancelacion asincrona: llega un aviso despues)
    ESPERAR   1.5                     (pausa en segundos)

Como nombrar una reserva en MODIFICAR y CANCELAR:
    R1, R2, ...  -> la 1.a, 2.a ... reserva que salio bien EN ESTE ARCHIVO
    7            -> la reserva con id 7 en la base de datos
"""
import time

from config import E_mensaje as m


def interpretar_reserva(texto):
    """'R2' -> ("R", 2) (reserva del script);  '7' -> ("ID", 7) (id real)."""
    if texto.upper().startswith("R"):
        return ("R", int(texto[1:]))
    return ("ID", int(texto))


def interpretar_linea(linea):
    """
    Convierte una linea del archivo en una tupla con la operacion:
      ("CONSULTA", mes)
      ("RESERVA", evento, fecha, n)
      ("MODIFICAR", reserva, n_asientos | None, fecha | None)
      ("CANCELAR", reserva)
      ("ESPERAR", segundos)
    Lanza ValueError si la linea no tiene el formato esperado.
    """
    partes = linea.split()
    orden = partes[0].upper()
    if orden == "CONSULTA" and len(partes) == 2:
        return ("CONSULTA", partes[1])
    if orden == "RESERVA" and len(partes) == 4:
        return ("RESERVA", partes[1], partes[2], int(partes[3]))
    if orden in ("MODIFICAR", "MODIFICACION") and len(partes) in (3, 4):
        n_asientos = fecha = None
        for valor in partes[2:]:          # lo que tiene "/" es fecha; lo demas, asientos
            if "/" in valor:
                fecha = valor
            else:
                n_asientos = int(valor)
        return ("MODIFICAR", interpretar_reserva(partes[1]), n_asientos, fecha)
    if orden in ("CANCELAR", "CANCELACION") and len(partes) == 2:
        return ("CANCELAR", interpretar_reserva(partes[1]))
    if orden == "ESPERAR" and len(partes) == 2:
        return ("ESPERAR", float(partes[1]))
    raise ValueError(f"no se entiende '{linea}'")


def mostrar(cliente, descripcion, respuesta, ms, detalle=False):
    """Imprime una linea con el resultado de una operacion."""
    texto = f"{descripcion} => {respuesta['estado']}"
    datos = respuesta.get("datos", {})
    if respuesta["estado"] not in (m.EXITO, m.ACK):
        texto += f" {respuesta['motivo']}: {datos.get('razon', '')}"
    elif respuesta["estado"] == m.ACK:
        texto += " (recibida; el aviso llegara despues)"
    elif "antes" in datos:                # respuesta de una modificacion
        antes = datos["antes"]
        texto += (f" (reserva {datos['id_reserva']}: {antes['fecha']} x{antes['n_asientos']}"
                  f" -> {datos['fecha']} x{datos['n_asientos']})")
    elif "id_reserva" in datos:           # respuesta de una reserva
        texto += f" (reserva {datos['id_reserva']})"
    elif "eventos" in datos:              # respuesta de una consulta
        con_reservas = sum(1 for e in datos["eventos"] if e["puestos_ocupados"] > 0)
        texto += f" ({len(datos['eventos'])} ocurrencias, {con_reservas} con reservas)"
    print(f"[{time.strftime('%H:%M:%S')}] [C{cliente.id_cliente}] {texto} ({ms:.1f} ms)", flush=True)

    # Con --detalle, las consultas muestran las fechas que tienen reservas
    if detalle and "eventos" in datos:
        for e in datos["eventos"]:
            if e["puestos_ocupados"] > 0:
                print(f"      {e['id_evento']:<6}{e['fecha']}  ocupados {e['puestos_ocupados']:>3}"
                      f"  libres {e['libres']:>3}")


def ejecutar_script(cliente, ruta, detalle=False, espera_avisos_s=10):
    """
    Ejecuta el archivo linea por linea y devuelve un resumen {estado: cantidad}.
    Al final espera (hasta espera_avisos_s) los avisos de las cancelaciones.
    """
    resumen: dict[str, int] = {}
    reservas_script: list[int] = []      # ids de las reservas hechas en este archivo (R1, R2...)

    def resolver(referencia):
        """Convierte ("R", 2) o ("ID", 7) en un id real de la base de datos."""
        clase, numero = referencia
        if clase == "ID":
            return numero
        if not 1 <= numero <= len(reservas_script):
            raise ValueError(f"R{numero} no existe: este archivo lleva {len(reservas_script)} reservas")
        return reservas_script[numero - 1]

    with open(ruta, encoding="utf-8") as archivo:
        for numero, linea in enumerate(archivo, start=1):
            linea = linea.strip()
            if not linea or linea.startswith("#"):      # lineas vacias y comentarios
                continue
            try:
                operacion = interpretar_linea(linea)
                if operacion[0] in ("MODIFICAR", "CANCELAR"):
                    id_reserva = resolver(operacion[1])
            except (ValueError, IndexError) as e:
                print(f"  linea {numero} ignorada: {e}")
                continue

            if operacion[0] == "ESPERAR":
                time.sleep(operacion[1])
                continue
            if operacion[0] == "CONSULTA":
                respuesta, ms = cliente.consultar(operacion[1])
                descripcion = f"CONSULTA {operacion[1]}"
            elif operacion[0] == "RESERVA":
                _, evento, fecha, n = operacion
                respuesta, ms = cliente.reservar(evento, fecha, n)
                descripcion = f"RESERVA {evento} {fecha} x{n}"
                if respuesta["estado"] == m.EXITO:
                    reservas_script.append(respuesta["datos"]["id_reserva"])
            elif operacion[0] == "MODIFICAR":
                _, _, n, fecha = operacion
                respuesta, ms = cliente.modificar(id_reserva, n_asientos=n, fecha=fecha)
                cambios = " ".join(str(x) for x in (fecha, n and f"x{n}") if x)
                descripcion = f"MODIFICAR reserva {id_reserva} -> {cambios}"
            else:  # CANCELAR
                respuesta, ms = cliente.cancelar(id_reserva)
                descripcion = f"CANCELAR reserva {id_reserva}"

            mostrar(cliente, descripcion, respuesta, ms, detalle)
            resumen[respuesta["estado"]] = resumen.get(respuesta["estado"], 0) + 1

    # Las cancelaciones son asincronas: hay que darles tiempo a que llegue su aviso
    sin_aviso = cliente.esperar_notificaciones(espera_avisos_s)
    if cliente.notificaciones:
        resumen["NOTIFICACIONES"] = len(cliente.notificaciones)
    if sin_aviso:
        print(f"  {sin_aviso} cancelacion(es) sin aviso despues de {espera_avisos_s} s")
        resumen["SIN_AVISO"] = sin_aviso
    return resumen
