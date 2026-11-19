"""
Logica del RMC (Reservas, Modificaciones y Consultas).

Recibe una peticion del GT (dict) y devuelve una respuesta (dict). Para
resolverla le pregunta a la persistencia. Sigue los pasos de la tabla del
enunciado. Aqui no hay sockets; de la red se encarga capa_negocios/main.py.
"""
from config import E_mensaje as m
from capa_negocios.cliente_persistencia import PersistenciaNoDisponible


class ServicioRMC:
    def __init__(self, persistencia):
        self.persistencia = persistencia     # objeto para hablar con la persistencia
        # "Menu" del servicio: tipo de peticion -> metodo que la atiende
        self.operaciones = {
            m.CONSULTA: self.consultar_eventos,
            m.RESERVA: self.reservar,
            m.MODIFICACION: self.modificar,
        }

    def atender(self, peticion):
        """Recibe una peticion y SIEMPRE devuelve una respuesta."""
        operacion = self.operaciones.get(peticion.get("tipo"))
        if operacion is None:
            return m.crear_respuesta(peticion, m.ERROR, m.PETICION_INVALIDA,
                                     {"razon": f"tipo no atendido: {peticion.get('tipo')}"})
        try:
            return operacion(peticion)
        except PersistenciaNoDisponible as e:
            # la persistencia no contesto: se le avisa al cliente con un ERROR
            return m.crear_respuesta(peticion, m.ERROR, m.NO_DISPONIBLE, {"razon": str(e)})

    def _preguntar(self, tipo, peticion, datos):
        """
        Le hace una pregunta a la persistencia en nombre del cliente.
        Usa el mismo uuid de la peticion original: sirve para seguirla en los
        logs y, en las escrituras, para que la persistencia reconozca un reintento.
        Por eso ahora TODAS se pueden reintentar sin miedo a duplicar.
        """
        consulta = m.crear_peticion(tipo, peticion["id_cliente"], datos, peticion["uuid"])
        return self.persistencia.enviar(consulta)

    def _ya_hecha(self, peticion, escritura):
        """
        Reintentos: si el cliente repite una peticion (mismo uuid) porque no le
        llego la respuesta, puede que la primera SI se haya hecho. Se pregunta
        primero a la persistencia; si ya se hizo, se responde lo mismo que la
        primera vez. Hay que preguntarlo ANTES de revisar el cupo: si no, una
        reserva ya hecha podria responder "sin cupo" (porque ella misma lleno el
        evento) o una modificacion ya hecha "no cambia nada".
        Devuelve la respuesta a enviar, o None si hay que hacer la operacion.
        """
        previa = self._preguntar(m.CONSULTA_OPERACION, peticion, {"tipo": escritura})
        if previa["estado"] == m.EXITO:
            return m.crear_respuesta(peticion, m.EXITO, datos=previa["datos"])
        return None

    # ------------------------------------------------------------ CONSULTA
    # datos: {"mes": "noviembre"}
    def consultar_eventos(self, peticion):
        # 1. recibir  2. enviar a la persistencia  3. devolver el resultado
        return self.persistencia.enviar(peticion)

    # ------------------------------------------------------------ RESERVA
    # datos: {"id_evento": "E1", "fecha": "2026/11/01", "n_asientos": 2}
    def reservar(self, peticion):
        datos = peticion.get("datos", {})
        n_asientos = datos.get("n_asientos")
        if not m.es_entero_positivo(n_asientos):
            return m.crear_respuesta(peticion, m.ERROR, m.DATO_INVALIDO,
                                     {"razon": f"n_asientos debe ser un entero mayor que 0: {n_asientos}"})

        # 1b. si es un reintento de una reserva que ya se hizo, devolver la misma
        repetida = self._ya_hecha(peticion, m.AGREGAR_RESERVA)
        if repetida:
            return repetida

        # 2. consultar la disponibilidad en tiempo real
        disponibilidad = self._preguntar(m.CONSULTA_DISPONIBILIDAD, peticion,
                                         {"id_evento": datos.get("id_evento"), "fecha": datos.get("fecha")})
        if disponibilidad["estado"] != m.EXITO:
            return disponibilidad          # NO_EXISTE o DATO_INVALIDO: ya trae el uuid correcto

        libres = disponibilidad["datos"]["libres"]
        if libres < n_asientos:
            return m.crear_respuesta(peticion, m.FALLO, m.SIN_CUPO,
                                     {"razon": f"hay {libres} puestos libres, se pidieron {n_asientos}"})

        # 3. confirmar. La persistencia vuelve a revisar el cupo en una sola
        #    instruccion, porque otro cliente pudo reservar entre el paso 2 y este.
        #    Si se pierde la respuesta y se reintenta, la persistencia reconoce
        #    el uuid y no la hace dos veces.
        # 4. responder lo que diga la persistencia (EXITO o FALLO SIN_CUPO)
        return self._preguntar(m.AGREGAR_RESERVA, peticion, datos)

    # ------------------------------------------------------------ MODIFICACION
    # datos: {"id_reserva": 3, "n_asientos": 5}            cambia los asientos
    #        {"id_reserva": 3, "fecha": "2026/11/15"}      cambia la fecha (conserva los asientos)
    #        {"id_reserva": 3, "fecha": ..., "n_asientos": ...}   las dos cosas
    def modificar(self, peticion):
        datos = peticion.get("datos", {})

        # 1. recibir la solicitud y revisar que los datos tengan sentido
        id_reserva = datos.get("id_reserva")
        if not m.es_entero_positivo(id_reserva):
            return m.crear_respuesta(peticion, m.ERROR, m.DATO_INVALIDO,
                                     {"razon": f"id_reserva debe ser un entero mayor que 0: {id_reserva}"})
        if "fecha" not in datos and "n_asientos" not in datos:
            return m.crear_respuesta(peticion, m.ERROR, m.DATO_INVALIDO,
                                     {"razon": "hay que indicar la nueva fecha, el nuevo numero de asientos o ambos"})
        if "n_asientos" in datos and not m.es_entero_positivo(datos["n_asientos"]):
            return m.crear_respuesta(peticion, m.ERROR, m.DATO_INVALIDO,
                                     {"razon": f"n_asientos debe ser un entero mayor que 0: {datos['n_asientos']}"})
        if "fecha" in datos:
            try:
                fecha_pedida = m.normalizar_fecha(datos["fecha"])
            except ValueError:
                return m.crear_respuesta(peticion, m.ERROR, m.DATO_INVALIDO,
                                         {"razon": f"fecha invalida: {datos['fecha']} (formato AAAA/MM/DD)"})

        # 1b. si es un reintento de una modificacion que ya se hizo, devolver la misma
        repetida = self._ya_hecha(peticion, m.MODIFICAR_RESERVA)
        if repetida:
            return repetida

        # 2a. traer la reserva actual (de que evento es, que fecha y cuantos asientos)
        actual = self._preguntar(m.CONSULTA_RESERVA, peticion, {"id_reserva": id_reserva})
        if actual["estado"] != m.EXITO:
            return actual                  # no existe, ya se cancelo o es de otro cliente
        reserva = actual["datos"]

        # Lo que no se pidio cambiar se queda igual
        nueva_fecha = fecha_pedida if "fecha" in datos else reserva["fecha"]
        nuevos_asientos = datos.get("n_asientos", reserva["n_asientos"])
        if nueva_fecha == reserva["fecha"] and nuevos_asientos == reserva["n_asientos"]:
            return m.crear_respuesta(peticion, m.ERROR, m.DATO_INVALIDO,
                                     {"razon": "la modificacion no cambia nada de la reserva"})

        # 2b. consultar la disponibilidad en la ocurrencia destino (misma u otra fecha).
        #     Si el evento solo ocurre una vez en el mes, la otra fecha no existe
        #     y la persistencia responde FALLO NO_EXISTE: solo se pueden cambiar asientos.
        disponibilidad = self._preguntar(m.CONSULTA_DISPONIBILIDAD, peticion,
                                         {"id_evento": reserva["id_evento"], "fecha": nueva_fecha})
        if disponibilidad["estado"] != m.EXITO:
            return disponibilidad

        # Cuantos asientos NUEVOS hacen falta en la ocurrencia destino:
        #  - misma fecha: solo la diferencia (si pide menos, no hace falta ninguno)
        #  - otra fecha:  todos los asientos que quiere
        if nueva_fecha == reserva["fecha"]:
            necesarios = nuevos_asientos - reserva["n_asientos"]
        else:
            necesarios = nuevos_asientos
        libres = disponibilidad["datos"]["libres"]
        if necesarios > libres:
            # 3. no hay cupo: se rechaza y la reserva queda como estaba
            return m.crear_respuesta(peticion, m.FALLO, m.SIN_CUPO,
                                     {"razon": f"hay {libres} puestos libres en {reserva['id_evento']} "
                                               f"el {nueva_fecha}, hacen falta {necesarios}"})

        # 3. hacer el cambio a traves de la persistencia (que revisa el cupo otra
        #    vez, todo en una transaccion). 4. responder EXITO o FALLO.
        return self._preguntar(m.MODIFICAR_RESERVA, peticion,
                               {"id_reserva": id_reserva, "fecha": nueva_fecha,
                                "n_asientos": nuevos_asientos})
