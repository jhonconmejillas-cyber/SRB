"""
Base de datos del SRB (SQLite: toda la base vive en un solo archivo).

Tablas:
  evento      (id, nombre, tipo_evento)                 -> E1, E2, ...
  ocurrencia  (id, evento_id, fecha, capacidad, puestos_ocupados)
              -> cada fecha en que se hace un evento. E1 puede tener dos.
  reserva     (id, id_cliente, ocurrencia_id, numero_puestos, activa)
              -> quien reservo cuantos puestos y en que ocurrencia.
                 activa = 1 si sigue vigente, 0 si se cancelo.
  operacion   (uuid, tipo, id_cliente, resultado)
              -> las escrituras que ya se hicieron, con su resultado. Sirve
                 para reconocer un reintento: si el cliente repite una
                 peticion (mismo uuid) porque no le llego la respuesta, se
                 devuelve el resultado guardado en vez de hacerla otra vez.

Las fechas se guardan como texto 'AAAA/MM/DD', el mismo formato del protocolo.

Regla de oro: en cada ocurrencia, puestos_ocupados debe ser igual a la suma
de los puestos de sus reservas activas. Todos los cambios se hacen dentro de
una transaccion ("with self.conexion:"), asi que si algo falla a mitad de
camino SQLite deshace todo y la base queda como estaba.
"""
import json
import sqlite3
from config import configuracion

# Instrucciones SQL que crean las tablas (solo si todavia no existen)
ESQUEMA = """
CREATE TABLE IF NOT EXISTS evento (
    id          TEXT PRIMARY KEY,
    nombre      TEXT NOT NULL,
    tipo_evento TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ocurrencia (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    evento_id        TEXT    NOT NULL REFERENCES evento(id),
    fecha            TEXT    NOT NULL,
    capacidad        INTEGER NOT NULL DEFAULT 100,
    puestos_ocupados INTEGER NOT NULL DEFAULT 0,
    UNIQUE (evento_id, fecha),
    CHECK (puestos_ocupados >= 0 AND puestos_ocupados <= capacidad)
);

CREATE TABLE IF NOT EXISTS reserva (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    id_cliente     INTEGER NOT NULL,
    ocurrencia_id  INTEGER NOT NULL REFERENCES ocurrencia(id),
    numero_puestos INTEGER NOT NULL CHECK (numero_puestos > 0),
    activa         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS operacion (
    uuid       TEXT    NOT NULL,
    tipo       TEXT    NOT NULL,
    id_cliente INTEGER NOT NULL,
    resultado  TEXT    NOT NULL,          -- JSON con los datos de la respuesta EXITO
    PRIMARY KEY (uuid, tipo)              -- la misma peticion no se puede anotar dos veces
);
"""


class BaseDatos:
    def __init__(self, ruta=configuracion.BD_RUTA):
        ruta.parent.mkdir(parents=True, exist_ok=True)   # crea la carpeta datos/ si no existe
        self.ruta = ruta
        # SQLite deja escribir a uno solo a la vez: si alguien esta escribiendo,
        # esta conexion espera hasta 10 s antes de rendirse.
        self.conexion = sqlite3.connect(ruta, timeout=10)
        self.conexion.row_factory = sqlite3.Row             # leer columnas por nombre: fila["fecha"]
        self.conexion.execute("PRAGMA foreign_keys = ON")   # SQLite ignora las llaves foraneas si no se activan
        self.conexion.executescript(ESQUEMA)                # crea las tablas si no existen

    # ================================================================ carga inicial
    def reiniciar(self):
        """Borra todos los datos y deja las tablas vacias."""
        with self.conexion:
            self.conexion.executescript("""
                DROP TABLE IF EXISTS operacion;
                DROP TABLE IF EXISTS reserva;
                DROP TABLE IF EXISTS ocurrencia;
                DROP TABLE IF EXISTS evento;
            """)
            self.conexion.executescript(ESQUEMA)

    def cargar_eventos(self, eventos):
        """
        eventos: lista de diccionarios
          {"id": "E1", "nombre": "...", "tipo_evento": "...",
           "fechas": ["2026/11/01", "2026/11/15"], "capacidad": 100}
        Todo se inserta en una sola transaccion: o entra todo o no entra nada.
        """
        with self.conexion:
            for ev in eventos:
                self.conexion.execute(
                    "INSERT INTO evento (id, nombre, tipo_evento) VALUES (?, ?, ?)",
                    (ev["id"], ev["nombre"], ev["tipo_evento"]))
                for fecha in ev["fechas"]:      # una fila de ocurrencia por cada fecha
                    self.conexion.execute(
                        "INSERT INTO ocurrencia (evento_id, fecha, capacidad) VALUES (?, ?, ?)",
                        (ev["id"], fecha, ev.get("capacidad", 100)))

    # ================================================================ consultas
    def consultar_mes(self, numero_mes):
        """Todas las ocurrencias de un mes (1 a 12), ordenadas por fecha."""
        # SUBSTR(fecha, 6, 2) saca el mes de '2026/11/01' -> '11'
        filas = self.conexion.execute("""
            SELECT e.id AS id_evento, e.nombre, e.tipo_evento,
                   o.fecha, o.capacidad, o.puestos_ocupados,
                   o.capacidad - o.puestos_ocupados AS libres
            FROM ocurrencia o
            JOIN evento e ON e.id = o.evento_id
            WHERE CAST(SUBSTR(o.fecha, 6, 2) AS INTEGER) = ?
            ORDER BY o.fecha, CAST(SUBSTR(e.id, 2) AS INTEGER)
        """, (numero_mes,)).fetchall()
        return [dict(f) for f in filas]

    def disponibilidad(self, id_evento, fecha):
        """Capacidad y ocupacion de una ocurrencia. Lanza NoExiste si no existe."""
        fila = self.conexion.execute("""
            SELECT capacidad, puestos_ocupados,
                   capacidad - puestos_ocupados AS libres
            FROM ocurrencia
            WHERE evento_id = ? AND fecha = ?
        """, (id_evento, fecha)).fetchone()
        if fila is None:
            raise NoExiste(f"{id_evento} no tiene ocurrencia el {fecha}")
        return dict(fila)

    def consultar_reserva(self, id_cliente, id_reserva):
        """
        Datos de una reserva ACTIVA del cliente:
          {"id_reserva", "id_cliente", "id_evento", "fecha", "n_asientos"}
        Lanza NoExiste si no existe, si ya se cancelo o si es de otro cliente
        (un cliente no puede ver ni tocar las reservas de otro).
        """
        fila = self.conexion.execute("""
            SELECT r.id AS id_reserva, r.id_cliente, o.evento_id AS id_evento,
                   o.fecha, r.numero_puestos AS n_asientos, r.ocurrencia_id
            FROM reserva r
            JOIN ocurrencia o ON o.id = r.ocurrencia_id
            WHERE r.id = ? AND r.id_cliente = ? AND r.activa = 1
        """, (id_reserva, id_cliente)).fetchone()
        if fila is None:
            raise NoExiste(f"el cliente {id_cliente} no tiene una reserva activa con id {id_reserva}")
        return dict(fila)

    # ================================================================ reintentos (idempotencia)
    def operacion_hecha(self, uuid, tipo):
        """
        Si la escritura 'tipo' con ese uuid ya se hizo, devuelve el resultado que
        dio entonces (dict). Si no se ha hecho, devuelve None.
        """
        if not uuid:
            return None
        fila = self.conexion.execute(
            "SELECT resultado FROM operacion WHERE uuid = ? AND tipo = ?", (uuid, tipo)).fetchone()
        return json.loads(fila["resultado"]) if fila else None

    def _anotar(self, uuid, tipo, id_cliente, resultado):
        """
        Guarda que esta escritura ya se hizo. Se llama DENTRO de la misma
        transaccion de la escritura: o quedan las dos cosas o ninguna. Asi nunca
        hay una reserva hecha sin anotar (que se duplicaria al reintentar), ni
        una anotada sin hacer.
        """
        if uuid:
            self.conexion.execute(
                "INSERT INTO operacion (uuid, tipo, id_cliente, resultado) VALUES (?, ?, ?, ?)",
                (uuid, tipo, id_cliente, json.dumps(resultado)))

    # ================================================================ cambios
    def _ocupar(self, ocurrencia_id, n_asientos, id_evento, fecha):
        """
        Suma n_asientos a los ocupados SOLO si caben. Revisar y sumar es una
        sola instruccion SQL, asi dos clientes a la vez nunca pueden pasarse
        de la capacidad. Lanza SinCupo si no caben.
        """
        cursor = self.conexion.execute("""
            UPDATE ocurrencia
            SET puestos_ocupados = puestos_ocupados + ?
            WHERE id = ? AND puestos_ocupados + ? <= capacidad
        """, (n_asientos, ocurrencia_id, n_asientos))
        if cursor.rowcount == 0:      # no se actualizo ninguna fila: no cabian
            libres = self.conexion.execute(
                "SELECT capacidad - puestos_ocupados FROM ocurrencia WHERE id = ?",
                (ocurrencia_id,)).fetchone()[0]
            raise SinCupo(f"hay {libres} puestos libres en {id_evento} el {fecha}, "
                          f"se pidieron {n_asientos}")

    def _liberar(self, ocurrencia_id, n_asientos):
        """Resta n_asientos a los ocupados de una ocurrencia."""
        self.conexion.execute(
            "UPDATE ocurrencia SET puestos_ocupados = puestos_ocupados - ? WHERE id = ?",
            (n_asientos, ocurrencia_id))

    def _buscar_ocurrencia(self, id_evento, fecha):
        """id de la ocurrencia de un evento en una fecha. Lanza NoExiste si no hay."""
        fila = self.conexion.execute(
            "SELECT id FROM ocurrencia WHERE evento_id = ? AND fecha = ?",
            (id_evento, fecha)).fetchone()
        if fila is None:
            raise NoExiste(f"{id_evento} no tiene ocurrencia el {fecha}")
        return fila["id"]

    def agregar_reserva(self, id_cliente, id_evento, fecha, n_asientos, uuid=None):
        """
        Ocupa n_asientos y registra la reserva, todo en una transaccion.
        Lanza NoExiste o SinCupo; en ese caso no cambia nada.
        uuid: si se da, la operacion queda anotada (ver operacion_hecha).
        """
        with self.conexion:
            ocurrencia_id = self._buscar_ocurrencia(id_evento, fecha)
            self._ocupar(ocurrencia_id, n_asientos, id_evento, fecha)
            cursor = self.conexion.execute("""
                INSERT INTO reserva (id_cliente, ocurrencia_id, numero_puestos)
                VALUES (?, ?, ?)
            """, (id_cliente, ocurrencia_id, n_asientos))
            resultado = {"id_reserva": cursor.lastrowid, "id_cliente": id_cliente,
                         "id_evento": id_evento, "fecha": fecha, "n_asientos": n_asientos}
            self._anotar(uuid, "AGREGAR_RESERVA", id_cliente, resultado)
            return resultado

    def modificar_reserva(self, id_cliente, id_reserva, nueva_fecha, nuevos_asientos, uuid=None):
        """
        Cambia una reserva para que quede en 'nueva_fecha' (del MISMO evento) con
        'nuevos_asientos'. Todo en una transaccion: si no hay cupo o la fecha no
        existe, se lanza la excepcion y la reserva queda exactamente como estaba.

        Casos:
          - misma fecha: solo se ocupan (o liberan) los asientos de diferencia.
          - otra fecha:  se ocupan los asientos en la nueva fecha y se liberan
                         todos los de la fecha vieja.
        Devuelve la reserva como quedo, con los valores anteriores en "antes".
        """
        with self.conexion:
            actual = self.consultar_reserva(id_cliente, id_reserva)
            id_evento = actual["id_evento"]
            vieja_id = actual["ocurrencia_id"]
            viejos_asientos = actual["n_asientos"]
            # Un evento con una sola fecha no tiene "otra fecha": aqui sale NoExiste
            nueva_id = self._buscar_ocurrencia(id_evento, nueva_fecha)

            if nueva_id == vieja_id:
                diferencia = nuevos_asientos - viejos_asientos
                if diferencia > 0:            # pide mas asientos: hay que ver si caben
                    self._ocupar(vieja_id, diferencia, id_evento, nueva_fecha)
                elif diferencia < 0:          # pide menos: se liberan los que sobran
                    self._liberar(vieja_id, -diferencia)
            else:
                # Primero ocupar en la nueva fecha (puede fallar con SinCupo y
                # entonces no se toco nada); despues liberar la vieja.
                self._ocupar(nueva_id, nuevos_asientos, id_evento, nueva_fecha)
                self._liberar(vieja_id, viejos_asientos)

            self.conexion.execute(
                "UPDATE reserva SET ocurrencia_id = ?, numero_puestos = ? WHERE id = ?",
                (nueva_id, nuevos_asientos, id_reserva))
            resultado = {"id_reserva": id_reserva, "id_cliente": id_cliente, "id_evento": id_evento,
                         "fecha": nueva_fecha, "n_asientos": nuevos_asientos,
                         "antes": {"fecha": actual["fecha"], "n_asientos": viejos_asientos}}
            self._anotar(uuid, "MODIFICAR_RESERVA", id_cliente, resultado)
            return resultado

    def cancelar_reserva(self, id_cliente, id_reserva, uuid=None):
        """
        Desactiva la reserva y libera sus asientos, en una transaccion.
        Lanza NoExiste si la reserva no existe, ya estaba cancelada o es de otro cliente.
        """
        with self.conexion:
            actual = self.consultar_reserva(id_cliente, id_reserva)
            # "AND activa = 1" evita cancelar dos veces la misma reserva
            cursor = self.conexion.execute(
                "UPDATE reserva SET activa = 0 WHERE id = ? AND activa = 1", (id_reserva,))
            if cursor.rowcount == 0:
                raise NoExiste(f"la reserva {id_reserva} ya estaba cancelada")
            self._liberar(actual["ocurrencia_id"], actual["n_asientos"])
            resultado = {"id_reserva": id_reserva, "id_cliente": id_cliente,
                         "id_evento": actual["id_evento"], "fecha": actual["fecha"],
                         "asientos_liberados": actual["n_asientos"]}
            self._anotar(uuid, "CANCELAR_RESERVA", id_cliente, resultado)
            return resultado

    # ================================================================ revision y vista
    def verificar(self):
        """True si en cada ocurrencia los ocupados coinciden con la suma de sus reservas activas."""
        malas = self.conexion.execute("""
            SELECT o.evento_id, o.fecha, o.puestos_ocupados,
                   COALESCE(SUM(r.numero_puestos), 0) AS suma_reservas
            FROM ocurrencia o
            LEFT JOIN reserva r ON r.ocurrencia_id = o.id AND r.activa = 1
            GROUP BY o.id
            HAVING o.puestos_ocupados != suma_reservas
        """).fetchall()
        return len(malas) == 0

    def resumen(self):
        """Totales de la base: eventos, ocurrencias, asientos, ocupados y reservas activas."""
        fila = self.conexion.execute("""
            SELECT (SELECT COUNT(*) FROM evento)                          AS eventos,
                   (SELECT COUNT(*) FROM ocurrencia)                      AS ocurrencias,
                   (SELECT COALESCE(SUM(capacidad), 0) FROM ocurrencia)   AS capacidad,
                   (SELECT COALESCE(SUM(puestos_ocupados), 0) FROM ocurrencia) AS ocupados,
                   (SELECT COUNT(*) FROM reserva WHERE activa = 1)        AS reservas
        """).fetchone()
        return dict(fila)

    def imprimir_estado(self, solo_ocupadas=False):
        """Imprime una tabla con cada ocurrencia y sus asientos (para la sustentacion)."""
        filas = self.conexion.execute("""
            SELECT evento_id, fecha, capacidad, puestos_ocupados
            FROM ocurrencia
            ORDER BY CAST(SUBSTR(evento_id, 2) AS INTEGER), fecha
        """).fetchall()
        print(f"{'evento':<8}{'fecha':<12}{'capacidad':>10}{'ocupados':>10}{'libres':>8}")
        for f in filas:
            if solo_ocupadas and f["puestos_ocupados"] == 0:
                continue
            libres = f["capacidad"] - f["puestos_ocupados"]
            print(f"{f['evento_id']:<8}{f['fecha']:<12}{f['capacidad']:>10}"
                  f"{f['puestos_ocupados']:>10}{libres:>8}")
        r = self.resumen()
        print(f"-- {r['eventos']} eventos, {r['ocurrencias']} ocurrencias, "
              f"{r['capacidad']} asientos, {r['ocupados']} ocupados, {r['reservas']} reservas activas")

    def cerrar(self):
        self.conexion.close()


class NoExiste(Exception):
    """El evento no tiene ocurrencia en esa fecha, o la reserva no existe."""


class SinCupo(Exception):
    """No hay suficientes puestos libres."""
