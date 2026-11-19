"""
Logica del Servicio de Persistencia: recibe una peticion (dict) y devuelve una
respuesta (dict). Aqui no hay sockets; de la red se encarga main.py.

Peticiones que atiende:
  CONSULTA                 {"mes": "noviembre"}
  CONSULTA_DISPONIBILIDAD  {"id_evento": "E1", "fecha": "2026/11/01"}
  AGREGAR_RESERVA          {"id_evento": "E1", "fecha": "2026/11/01", "n_asientos": 2}
  CONSULTA_RESERVA         {"id_reserva": 3}
  MODIFICAR_RESERVA        {"id_reserva": 3, "fecha": "2026/11/15", "n_asientos": 5}
  CANCELAR_RESERVA         {"id_reserva": 3}
  CONSULTA_OPERACION       {"tipo": "AGREGAR_RESERVA"}   (con el uuid de la peticion original)

Reintentos: las escrituras (agregar, modificar, cancelar) se anotan con su
uuid. Si llega otra vez la misma (mismo uuid y tipo), NO se vuelve a hacer:
se responde lo mismo que la primera vez.
"""
import sqlite3

from config import E_mensaje as mensaje
from capa_persistencia.db import NoExiste, SinCupo


class DatoInvalido(Exception):
    """Un dato de la peticion falta o no tiene el formato esperado."""


class ServicioPersistencia:
    def __init__(self, db):
        self.db = db
        # "Menu" del servicio: tipo de peticion -> metodo que la atiende
        self.operaciones = {
            mensaje.CONSULTA: self.consultar_eventos,
            mensaje.CONSULTA_DISPONIBILIDAD: self.consultar_disponibilidad,
            mensaje.AGREGAR_RESERVA: self.agregar_reserva,
            mensaje.CONSULTA_RESERVA: self.consultar_reserva,
            mensaje.MODIFICAR_RESERVA: self.modificar_reserva,
            mensaje.CANCELAR_RESERVA: self.cancelar_reserva,
            mensaje.CONSULTA_OPERACION: self.consultar_operacion,
        }

    def atender(self, peticion):
        """
        Recibe una peticion y SIEMPRE devuelve una respuesta.
        Cada problema conocido (excepcion) se traduce a un estado y un motivo.
        """
        operacion = self.operaciones.get(peticion.get("tipo"))
        if operacion is None:
            return mensaje.crear_respuesta(peticion, mensaje.ERROR, mensaje.PETICION_INVALIDA,
                                           {"razon": f"tipo no atendido: {peticion.get('tipo')}"})
        # Si es una escritura repetida, responder lo que se respondio la primera vez
        if peticion["tipo"] in mensaje.ESCRITURAS:
            anterior = self.db.operacion_hecha(peticion.get("uuid"), peticion["tipo"])
            if anterior is not None:
                return mensaje.crear_respuesta(peticion, mensaje.EXITO, datos=anterior)
        try:
            return operacion(peticion)
        except DatoInvalido as e:     # algo mal escrito -> ERROR
            return mensaje.crear_respuesta(peticion, mensaje.ERROR, mensaje.DATO_INVALIDO,
                                           {"razon": str(e)})
        except NoExiste as e:         # reglas del negocio -> FALLO
            return mensaje.crear_respuesta(peticion, mensaje.FALLO, mensaje.NO_EXISTE,
                                           {"razon": str(e)})
        except SinCupo as e:
            return mensaje.crear_respuesta(peticion, mensaje.FALLO, mensaje.SIN_CUPO,
                                           {"razon": str(e)})
        except sqlite3.Error as e:    # problema de la base de datos -> ERROR
            return mensaje.crear_respuesta(peticion, mensaje.ERROR, mensaje.NO_DISPONIBLE,
                                           {"razon": f"error de base de datos: {e}"})

    # Cada metodo: 1) lee y valida los datos, 2) llama a la base, 3) arma la respuesta EXITO.
    # Si algo sale mal, la excepcion sube hasta atender(), que la convierte en FALLO/ERROR.

    def consultar_eventos(self, peticion):
        numero_mes = leer_mes(peticion)
        eventos = self.db.consultar_mes(numero_mes)
        return mensaje.crear_respuesta(peticion, mensaje.EXITO,
                                       datos={"mes": mensaje.MESES[numero_mes - 1], "eventos": eventos})

    def consultar_disponibilidad(self, peticion):
        id_evento, fecha = leer_evento_y_fecha(peticion)
        return mensaje.crear_respuesta(peticion, mensaje.EXITO,
                                       datos=self.db.disponibilidad(id_evento, fecha))

    def agregar_reserva(self, peticion):
        id_evento, fecha = leer_evento_y_fecha(peticion)
        n_asientos = leer_asientos(peticion)
        reserva = self.db.agregar_reserva(peticion["id_cliente"], id_evento, fecha, n_asientos,
                                          uuid=peticion.get("uuid"))
        return mensaje.crear_respuesta(peticion, mensaje.EXITO, datos=reserva)

    def consultar_reserva(self, peticion):
        id_reserva = leer_id_reserva(peticion)
        reserva = self.db.consultar_reserva(peticion["id_cliente"], id_reserva)
        reserva.pop("ocurrencia_id")      # dato interno de la base, no le sirve a nadie afuera
        return mensaje.crear_respuesta(peticion, mensaje.EXITO, datos=reserva)

    def modificar_reserva(self, peticion):
        id_reserva = leer_id_reserva(peticion)
        fecha = leer_fecha(peticion)
        n_asientos = leer_asientos(peticion)
        reserva = self.db.modificar_reserva(peticion["id_cliente"], id_reserva, fecha, n_asientos,
                                            uuid=peticion.get("uuid"))
        return mensaje.crear_respuesta(peticion, mensaje.EXITO, datos=reserva)

    def cancelar_reserva(self, peticion):
        id_reserva = leer_id_reserva(peticion)
        resultado = self.db.cancelar_reserva(peticion["id_cliente"], id_reserva,
                                             uuid=peticion.get("uuid"))
        return mensaje.crear_respuesta(peticion, mensaje.EXITO, datos=resultado)

    def consultar_operacion(self, peticion):
        """Dice si la escritura datos.tipo con este uuid ya se hizo (y que dio)."""
        tipo = peticion["datos"].get("tipo")
        if tipo not in mensaje.ESCRITURAS:
            raise DatoInvalido(f"tipo de escritura invalido: {tipo}")
        anterior = self.db.operacion_hecha(peticion.get("uuid"), tipo)
        if anterior is None:
            return mensaje.crear_respuesta(peticion, mensaje.FALLO, mensaje.NO_EXISTE,
                                           {"razon": "esa operacion no se ha hecho"})
        return mensaje.crear_respuesta(peticion, mensaje.EXITO, datos=anterior)


# ---------------------------------------------------------------- lectura de datos
# Funciones pequenas que sacan un dato de la peticion y revisan que este bien.

def leer_mes(peticion):
    """'noviembre' (o ' Noviembre ') -> 11"""
    nombre = peticion["datos"].get("mes")
    if not isinstance(nombre, str) or nombre.strip().lower() not in mensaje.MESES:
        raise DatoInvalido(f"mes invalido: {nombre}")
    return mensaje.MESES.index(nombre.strip().lower()) + 1


def leer_fecha(peticion):
    """Fecha en formato AAAA/MM/DD, siempre escrita igual ('2026/11/1' -> '2026/11/01')."""
    texto = peticion["datos"].get("fecha")
    try:
        return mensaje.normalizar_fecha(texto)
    except ValueError:
        raise DatoInvalido(f"fecha invalida: {texto} (formato AAAA/MM/DD)")


def leer_evento_y_fecha(peticion):
    id_evento = peticion["datos"].get("id_evento")
    if not isinstance(id_evento, str) or not id_evento:
        raise DatoInvalido(f"id_evento invalido: {id_evento}")
    return id_evento, leer_fecha(peticion)


def leer_asientos(peticion):
    n = peticion["datos"].get("n_asientos")
    if not mensaje.es_entero_positivo(n):
        raise DatoInvalido(f"n_asientos debe ser un entero mayor que 0: {n}")
    return n


def leer_id_reserva(peticion):
    n = peticion["datos"].get("id_reserva")
    if not mensaje.es_entero_positivo(n):
        raise DatoInvalido(f"id_reserva debe ser un entero mayor que 0: {n}")
    return n
