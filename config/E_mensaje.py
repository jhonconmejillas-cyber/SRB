"""
Protocolo de mensajes del SRB: el "idioma" que hablan todos los procesos.

Todos los mensajes viajan como texto JSON. Hay dos formas:

    Peticion:  {"uuid": str, "tipo": str, "id_cliente": int, "datos": dict}
    Respuesta: {"uuid": str, "estado": str, "motivo": str | None, "datos": dict}

- uuid: un codigo unico por peticion. La respuesta lleva el MISMO uuid de la
  peticion que responde; asi cada quien sabe a que pregunta corresponde.
- tipo: que se quiere hacer (CONSULTA, RESERVA, ...).
- estado: como salio (EXITO, FALLO, ERROR o ACK).
- motivo: por que no salio bien (solo si el estado es FALLO o ERROR).
- datos: la informacion propia de cada operacion.
"""
import json
import uuid
from datetime import datetime
from typing import Any, Mapping, Optional, TypedDict


# Estas dos clases solo describen la forma de los diccionarios (ayudan al editor
# y a mypy). En tiempo de ejecucion son diccionarios normales.
class Peticion(TypedDict):
    uuid: str
    tipo: str
    id_cliente: int
    datos: dict


class Respuesta(TypedDict):
    uuid: Optional[str]
    estado: str
    motivo: Optional[str]
    datos: dict


# ======================================================================
#  Tipos de peticion que manda el CLIENTE al Gestor de Transacciones
# ======================================================================

# Ver los eventos de un mes.        datos: {"mes": "noviembre"}
CONSULTA = "CONSULTA"

# Reservar asientos.                datos: {"id_evento": "E1", "fecha": "2026/11/01", "n_asientos": 2}
RESERVA = "RESERVA"

# Cambiar una reserva que ya existe. Se puede cambiar el numero de asientos,
# la fecha (si el evento tiene otra fecha ese mes) o las dos cosas.
#                                   datos: {"id_reserva": 3, "n_asientos": 5}
#                                          {"id_reserva": 3, "fecha": "2026/11/15"}
MODIFICACION = "MODIFICACION"

# Cancelar una reserva (ASINCRONA). El cliente dice donde quiere recibir el
# aviso de que la cancelacion ya se hizo.
#                                   datos: {"id_reserva": 3,
#                                           "direccion_notificacion": "tcp://10.0.0.5:5600"}
CANCELACION = "CANCELACION"


# ======================================================================
#  Tipos internos: los usan los servicios de negocio para hablar con la
#  persistencia. Un cliente NO puede enviarlos (el GT los rechaza).
# ======================================================================

# Cuantos puestos libres tiene un evento en una fecha.
#                                   datos: {"id_evento": "E1", "fecha": "2026/11/01"}
CONSULTA_DISPONIBILIDAD = "CONSULTA_DISPONIBILIDAD"

# Guardar una reserva nueva.        datos: igual que RESERVA
AGREGAR_RESERVA = "AGREGAR_RESERVA"

# Leer una reserva (evento, fecha y asientos que tiene ahora).
#                                   datos: {"id_reserva": 3}
CONSULTA_RESERVA = "CONSULTA_RESERVA"

# Guardar el cambio de una reserva. Lleva los valores FINALES que debe quedar.
#                                   datos: {"id_reserva": 3, "fecha": "2026/11/15", "n_asientos": 5}
MODIFICAR_RESERVA = "MODIFICAR_RESERVA"

# Borrar (desactivar) una reserva y liberar sus asientos.
#                                   datos: {"id_reserva": 3}
CANCELAR_RESERVA = "CANCELAR_RESERVA"

# Aviso que el Servicio de Cancelacion le manda al cliente cuando ya termino.
# Tiene forma de respuesta (uuid de la cancelacion, estado, motivo, datos)
# mas el campo "tipo": "NOTIFICACION".
NOTIFICACION = "NOTIFICACION"

TIPOS = {CONSULTA, RESERVA, MODIFICACION, CANCELACION,
         CONSULTA_DISPONIBILIDAD, AGREGAR_RESERVA, CONSULTA_RESERVA,
         MODIFICAR_RESERVA, CANCELAR_RESERVA, NOTIFICACION}


# ======================================================================
#  Estados de una respuesta
# ======================================================================
EXITO = "EXITO"   # se hizo lo pedido
FALLO = "FALLO"   # no se pudo por una regla del negocio (sin cupo, no existe...)
ERROR = "ERROR"   # la peticion estaba mal escrita o un servicio no respondio
ACK = "ACK"       # "recibido": se hara despues (lo usa la cancelacion asincrona)

ESTADOS = {EXITO, FALLO, ERROR, ACK}


# ======================================================================
#  Motivos (por que algo salio FALLO o ERROR)
# ======================================================================
SIN_CUPO = "SIN_CUPO"                    # no hay suficientes asientos libres
NO_EXISTE = "NO_EXISTE"                  # el evento, la fecha o la reserva no existen
DATO_INVALIDO = "DATO_INVALIDO"          # un dato tiene un formato incorrecto
PETICION_INVALIDA = "PETICION_INVALIDA"  # el mensaje no se entiende o no va a ese servicio
NO_IMPLEMENTADO = "NO_IMPLEMENTADO"      # ese tipo de peticion no se atiende ahi
NO_DISPONIBLE = "NO_DISPONIBLE"          # un servicio no respondio a tiempo

MOTIVOS = {SIN_CUPO, NO_EXISTE, DATO_INVALIDO, PETICION_INVALIDA, NO_IMPLEMENTADO, NO_DISPONIBLE}

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]

FORMATO_FECHA = "%Y/%m/%d"   # ejemplo: "2026/11/01"


def crear_peticion(tipo: str, id_cliente: int, datos: Optional[dict] = None,
                   uuid_peticion: Optional[str] = None) -> Peticion:
    """
    Arma una peticion nueva. Si no se da un uuid, se inventa uno al azar.
    (Se reutiliza el uuid cuando un servicio reenvia la misma peticion a otro,
    asi se puede seguir su recorrido en los registros.)
    """
    # el tipo tiene que ser uno de los que conocemos
    if tipo not in TIPOS:
        raise ValueError(f"Tipo de peticion no valido: {tipo}")
    # el id del cliente tiene que ser un numero entero positivo
    if not isinstance(id_cliente, int) or id_cliente <= 0:
        raise ValueError(f"ID de cliente no valido: {id_cliente}")
    return {
        "uuid": uuid_peticion or str(uuid.uuid4()),
        "tipo": tipo,
        "id_cliente": id_cliente,
        "datos": datos or {}
    }


def crear_respuesta(peticion: Mapping[str, Any], estado: str, motivo: Optional[str] = None,
                    datos: Optional[dict] = None) -> Respuesta:
    """Arma la respuesta a una peticion, copiando su uuid."""
    # el estado tiene que ser uno de los que conocemos
    if estado not in ESTADOS:
        raise ValueError(f"Estado no valido: {estado}")
    # si algo salio mal, es obligatorio decir por que
    if estado in (FALLO, ERROR) and motivo not in MOTIVOS:
        raise ValueError(f"Estado: {estado}, necesita un motivo valido, no: {motivo}")
    # si salio bien no hay motivo que dar
    if estado in (EXITO, ACK):
        motivo = None
    uuid_peticion = peticion.get("uuid") if isinstance(peticion, dict) else None
    return {
        "uuid": uuid_peticion,
        "estado": estado,
        "motivo": motivo,
        "datos": datos or {}
    }


def normalizar_fecha(texto: Any) -> str:
    """
    Revisa que una fecha tenga el formato AAAA/MM/DD y la devuelve escrita
    siempre igual: '2026/11/1' -> '2026/11/01'. Lanza ValueError si no sirve.
    """
    fecha = datetime.strptime(str(texto), FORMATO_FECHA)
    return fecha.strftime(FORMATO_FECHA)


def es_entero_positivo(valor: Any) -> bool:
    """True si el valor es un numero entero mayor que 0 (True/False no cuentan)."""
    return isinstance(valor, int) and not isinstance(valor, bool) and valor > 0


def codificar(mensaje: Mapping[str, Any]) -> bytes:
    """Convierte un diccionario en bytes (texto JSON) para poderlo enviar por la red."""
    # ensure_ascii=False conserva las tildes tal cual
    return json.dumps(mensaje, ensure_ascii=False).encode("utf-8")


def decodificar(datos: bytes) -> dict:
    """Hace lo contrario de codificar: bytes recibidos -> diccionario."""
    mensaje = json.loads(datos)      # lanza ValueError si no es JSON
    # un JSON valido podria ser una lista o un numero; solo aceptamos objetos {}
    if not isinstance(mensaje, dict):
        raise ValueError("El mensaje debe ser un objeto JSON")
    return mensaje


if __name__ == "__main__":
    # python -m config.E_mensaje  -> pequena demostracion
    p = crear_peticion(RESERVA, 7, {"id_evento": "E1", "fecha": "2026/11/01", "n_asientos": 2})
    print("Peticion       :", p)
    print("Codificada     :", codificar(p))
    r = crear_respuesta(p, FALLO, SIN_CUPO, {"libres": 1})
    print("Respuesta      :", r)
    print("Mismo uuid     :", r["uuid"] == p["uuid"])
    print("Ida y vuelta   :", decodificar(codificar(p)) == p)
