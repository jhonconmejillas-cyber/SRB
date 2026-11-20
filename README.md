# SRB — Sistema Distribuido de Reserva de Boletos

Proyecto del curso **Introducción a Sistemas Distribuidos** — Pontificia Universidad Javeriana, 2026-30.

Sistema de tres capas (consumidores, negocio y almacenamiento) para consultar, reservar, modificar y
cancelar puestos en eventos culturales y deportivos. Las capas se comunican con **ZeroMQ** y los datos
se guardan en **SQLite**. La arquitectura final es **escalable** (N workers RMC detrás de un broker
ROUTER/DEALER) y **tolerante a fallas** (GT primario/respaldo, cliente Lazy Pirate con failover,
reintentos idempotentes por `uuid` y servicios relanzados por `systemd`).

---

## 1. Arquitectura y distribución en máquinas

| Máquina | IP | Qué corre |
|---|---|---|
| **NBIG291** | 10.43.99.232 | Clientes + `gt_1` (primario) + `broker_1` + RMC |
| **NBIG381** | 10.43.100.84 | `gt_2` (respaldo) + `broker_2` + RMC |
| **NBIG296** | 10.43.99.237 | Persistencia + SQLite + servicio de cancelación (ambos con `systemd`) |

```
                    NBIG291 (10.43.99.232)                      NBIG381 (10.43.100.84)
 clientes --REQ--> gt_1 :5555 --> broker_1 :5559|:5556     gt_2 :5555 --> broker_2 :5559|:5556
    |   \______________________ (failover) ______________________^           |
    |                              \                                        /
    |                               RMC ... RMC  (cada RMC conectado a los DOS brokers)
    |                                      |
    |                         NBIG296 (10.43.99.237)
    |                         persistencia :5558 + SQLite   <-- cancelacion (PULL de gt_1:5557 y gt_2:5557)
    +<--- avisos de cancelacion (PUSH a 10.43.99.232:5600-5699)
```

### Puertos y patrones ZeroMQ

| Puerto | Quién hace `bind` | Quién se conecta | Patrón |
|---|---|---|---|
| 5555 | GT (ROUTER) | clientes (REQ) | síncrono |
| 5557 | GT (PUSH) | servicio de cancelación (PULL), a los dos GT | asíncrono |
| 5559 | broker (ROUTER) | su GT local (DEALER) por 127.0.0.1 | síncrono |
| 5556 | broker (DEALER) | todos los RMC (REP), a los dos brokers | síncrono |
| 5558 | persistencia (REP) | RMC y cancelación (REQ) | síncrono |
| 5600-5699 | buzón del cliente (PULL) | servicio de cancelación (PUSH) | notificación |

### Operaciones

- **Consulta, reserva y modificación (síncronas):** cliente → GT → broker → RMC → persistencia, y la
  respuesta vuelve por el mismo camino.
- **Cancelación (asíncrona):** el GT responde `ACK` de inmediato y envía la petición por PUSH al
  servicio de cancelación; cuando se libera la reserva, el cliente recibe una `NOTIFICACION` en su
  buzón PULL.

---

## 2. Tecnologías

- **Python 3.12+** (en las VM se usó Python 3 de Ubuntu; en el PC, 3.13/3.14)
- **pyzmq** (ZeroMQ): REQ/REP, ROUTER/DEALER, PUSH/PULL y `zmq.proxy`
- **SQLite** (módulo `sqlite3` de la librería estándar)
- **python-dotenv**: configuración por máquina en un archivo `.env`
- **systemd**: reinicio automático de la persistencia y del servicio de cancelación
- **matplotlib** (opcional, solo para las gráficas de rendimiento)

Dependencias en `dependencias.txt` (`pyzmq`, `python-dotenv`).

---

## 3. Estructura del repositorio

```
SRB/
|-- config/
|   |-- configuracion.py        lee el .env: IPs, puertos, timeouts, reintentos
|   `-- E_mensaje.py            protocolo: tipos, estados, motivos, crear/codificar JSON
|-- capa_cliente/               CAPA DE CONSUMIDORES
|   |-- cliente.py              clase Cliente: REQ + Lazy Pirate + failover + buzon PULL
|   |-- script.py               ejecuta un caso de prueba escrito en .txt
|   |-- carga.py                carga continua (N peticiones/min, % por tipo) -> CSV
|   |-- escenarios.py           lanza los 3 escenarios (3, 5 y 8 clientes)
|   `-- main.py                 punto de entrada: --script o --carga
|-- capa_negocios/              CAPA DE SERVICIOS DE NEGOCIO
|   |-- gestor_transacciones.py GT: ROUTER (clientes), DEALER (broker), PUSH (cancelacion)
|   |-- broker.py               broker ROUTER/DEALER (zmq.proxy) que reparte entre RMC
|   |-- rmc.py                  logica de RESERVA, MODIFICACION y CONSULTA
|   |-- servicio_cancelacion.py logica de cancelacion + Notificador (PUSH al cliente)
|   |-- cliente_persistencia.py REQ hacia la persistencia, con timeout y reintento
|   |-- metricas.py             CSV de metricas medidas en el GT
|   `-- main.py                 arranca: broker | gt | rmc | cancelacion
|-- capa_persistencia/          CAPA DE ALMACENAMIENTO
|   |-- db.py                   SQLite: tablas, transacciones, UPDATE atomico, uuid
|   |-- servicio_persistencia.py traduce peticiones -> operaciones de la base
|   |-- main.py                 proceso REP en el puerto 5558
|   |-- cargar_datos.py         carga los 50 eventos (70 ocurrencias) o un JSON
|   `-- ver_bd.py               muestra el estado de la base
|-- despliegue/
|   |-- NBIG291.env, NBIG381.env, NBIG296.env   configuracion de cada VM
|   |-- negocio.sh              arrancar N | rmc N | estado | parar
|   |-- instalar_servicios.sh   persistencia y cancelacion como servicios systemd
|   `-- DESPLIEGUE.md           guia detallada de despliegue y medicion
|-- herramientas/
|   `-- analizar_metricas.py    TPM, promedio, desviacion, p95 y graficas
|-- pruebas/                    pruebas automaticas y casos de prueba (.txt, .json)
|-- dependencias.txt
`-- .env.example                plantilla de configuracion
```

---

## 4. Instalación (en cada una de las 3 máquinas)

```bash
sudo apt install -y python3-venv git
git clone https://github.com/jhonconmejillas-cyber/SRB.git
cd SRB
python3 -m venv venv
venv/bin/pip install -r dependencias.txt
cp despliegue/NBIG<numero>.env .env        # el de ESA maquina: NBIG291, NBIG381 o NBIG296
venv/bin/python -m config.configuracion    # muestra la configuracion leida (revisar IPs)
```

> Cada máquina usa **el mismo código**; lo único que cambia es su `.env`. Para probar todo en un solo
> computador, copie `.env.example` como `.env` (todo apunta a `127.0.0.1`).

### Firewall (solo si `ufw` está activo)

```bash
# NBIG296
sudo ufw allow 5558/tcp                     # persistencia
# NBIG291 y NBIG381
sudo ufw allow 5555/tcp                     # GT: clientes
sudo ufw allow 5556/tcp                     # broker: RMC de la otra maquina
sudo ufw allow 5557/tcp                     # GT: servicio de cancelacion
# NBIG291 ademas
sudo ufw allow 5600:5699/tcp                # buzon de avisos de los clientes
```

Comprobar conectividad desde NBIG291: `nc -zv 10.43.99.237 5558`

---

## 5. Ejecución (en este orden)

### 5.1 NBIG296 — almacenamiento

```bash
venv/bin/python -m capa_persistencia.cargar_datos     # crea la base: 50 eventos, 70 ocurrencias (borra la anterior)
bash despliegue/instalar_servicios.sh                 # instala y arranca srb-persistencia y srb-cancelacion
systemctl status srb-persistencia srb-cancelacion
```

- Ver logs: `journalctl -u srb-persistencia -f`
- Desinstalar los servicios: `bash despliegue/instalar_servicios.sh quitar`
- Recargar la base antes de una medición:
  `sudo systemctl stop srb-persistencia && venv/bin/python -m capa_persistencia.cargar_datos && sudo systemctl start srb-persistencia`

### 5.2 NBIG381 y NBIG291 — negocio (broker + GT + N RMC)

```bash
bash despliegue/negocio.sh arrancar 2     # broker, GT y 2 RMC en segundo plano (logs en logs/)
bash despliegue/negocio.sh estado         # que esta corriendo
tail -f logs/gt.log                       # ver las peticiones que pasan por el GT
bash despliegue/negocio.sh rmc 1          # ESCALAR: agrega 1 RMC en caliente, sin reiniciar nada
bash despliegue/negocio.sh parar          # detener todo
```

> Arrancar primero NBIG381 (`gt_2`, respaldo) y luego NBIG291 (`gt_1`, primario).
> Los `.sh` se ejecutan con `bash ...` (no necesitan permiso de ejecución).

### 5.3 NBIG291 — clientes

**Caso de prueba escrito en un archivo:**
```bash
venv/bin/python -m capa_cliente.main --id 1 --script pruebas/caso_basico.txt
venv/bin/python -m capa_cliente.main --id 2 --script pruebas/caso_modificacion_cancelacion.txt
```

Formato del archivo de script (una operación por línea, `#` para comentarios):
```
CONSULTA noviembre
RESERVA E1 2026/11/01 3
MODIFICAR R1 5
CANCELAR R1
ESPERAR 1.5
```

**Carga continua (un cliente):**
```bash
venv/bin/python -m capa_cliente.main --id 1 --carga --duracion 150 --por-minuto 20 \
    --consultas 50 --modificaciones 30 --cancelaciones 0 --semilla 7
```
`--consultas`, `--modificaciones` y `--cancelaciones` son porcentajes; el resto son reservas.
El resultado queda en `resultados/cliente_<id>.csv` (una fila por petición).

**Escenarios de rendimiento (3, 5 y 8 clientes, cada uno un proceso):**
```bash
venv/bin/python -m capa_cliente.escenarios --escenario 1 --etiqueta rmc1    # 3 clientes
venv/bin/python -m capa_cliente.escenarios --escenario 2 --etiqueta rmc1    # 5 clientes
venv/bin/python -m capa_cliente.escenarios --escenario 3 --etiqueta rmc1    # 8 clientes
```

| Escenario | Clientes | Solo consultan | Reservan y modifican |
|---|---|---|---|
| 1 | 3 | 2 | 1 (consultas y reservas) |
| 2 | 5 | 3 | 2 (70 % reservas, 30 % modificaciones) |
| 3 | 8 | 4 | 4 (70 % reservas, 30 % modificaciones) |

Prueba de estrés (sin pausa entre peticiones): agregar `--por-minuto 100000`.

### 5.4 Analizar resultados

```bash
venv/bin/python -m herramientas.analizar_metricas "resultados/rmc*_*clientes"
# graficas (necesita matplotlib, mejor en el PC con la carpeta resultados/ copiada):
python -m herramientas.analizar_metricas "resultados/rmc*_*clientes" --graficas
```
Calcula TPM total y por tipo, tiempo promedio, desviación estándar, p95, máximo, éxitos, fallos,
reintentos y peticiones atendidas por cada GT. Guarda `resultados/resumen_metricas.csv`.

> El número de RMC se lee del nombre de la carpeta (`rmc<N>_<M>clientes`).

---

## 6. Pruebas de tolerancia a fallas

**Caída del GT primario (failover):** en NBIG291, una terminal con carga y a los ~15 s matar `gt_1`:
```bash
venv/bin/python -m capa_cliente.main --id 1 --carga --duracion 60 --por-minuto 60 & sleep 15; kill -9 $(cat logs/gt.pid); wait
```
El cliente no recibe respuesta en 2,5 s, reintenta con espera exponencial y tras 2 fallos pasa a
`gt_2`, sin errores ni reservas duplicadas (el reintento lleva el mismo `uuid`).

**Caída de un RMC:** `kill -9 $(cat logs/rmc_1.pid)` durante la carga: los demás RMC siguen atendiendo.

**Caída de la persistencia** (NBIG296):
```bash
sudo kill -9 $(systemctl show -p MainPID --value srb-persistencia)
journalctl -u srb-persistencia -f          # systemd la relanza en ~1 s
```

**Revisar la consistencia de la base:**
```bash
venv/bin/python -m capa_persistencia.ver_bd --verificar
```

---

## 7. Pruebas automáticas (en un solo computador, con `.env` local)

Cada una arranca los procesos que necesita e imprime `PASA`/`FALLA` por comprobación.

| Comando | Qué verifica |
|---|---|
| `python -m capa_persistencia.prueba` | base de datos: carga, cupo, modificación, cancelación, concurrencia |
| `python -m pruebas.prueba_uuid` | reintentos con el mismo `uuid` no duplican escrituras |
| `python -m pruebas.prueba_sistema` | punta a punta: modificación y cancelación con notificación |
| `python -m pruebas.prueba_broker` | 2 brokers, 2 GT, 3 RMC: reparto de carga y caídas |
| `python -m pruebas.prueba_failover` | Lazy Pirate, kill de `gt_1`, sin duplicados |

---

## 8. Protocolo de mensajes (JSON)

```json
Peticion:  {"uuid": "9f1c...", "tipo": "RESERVA", "id_cliente": 1,
            "datos": {"id_evento": "E1", "fecha": "2026/11/01", "n_asientos": 2}}
Respuesta: {"uuid": "9f1c...", "estado": "EXITO", "motivo": null, "datos": {"id_reserva": 7}}
```

| Estado | Significado | ¿El cliente reintenta? |
|---|---|---|
| `EXITO` | se hizo lo pedido | no |
| `ACK` | recibido, se hará después (cancelación) | no |
| `FALLO` | regla de negocio: `SIN_CUPO`, `NO_EXISTE` | no |
| `ERROR` | dato inválido (`DATO_INVALIDO`) o servicio caído (`NO_DISPONIBLE`) | solo `NO_DISPONIBLE` |

---

## 9. Configuración (`.env`)

| Variable | Descripción |
|---|---|
| `HOST_CAPA_CONSUMIDORES`, `HOST_CAPA_SERVICIOS`, `HOST_CAPA_PERSISTENCIA` | IP de cada capa |
| `GT_HOSTS` | GT separados por coma: primero el primario, luego el respaldo |
| `BROKERS` | brokers a los que se conectan los RMC |
| `SP_PUERTO`, `BD_RUTA` | puerto y archivo de la persistencia |
| `TIMEOUT_MS` | espera máxima de una respuesta interna |
| `CLIENTE_TIMEOUT_MS`, `CLIENTE_INTENTOS`, `CLIENTE_FALLOS_PARA_CAMBIAR` | Lazy Pirate del cliente |
| `BACKOFF_BASE_MS`, `BACKOFF_MAX_MS` | espera exponencial entre reintentos |
| `CLIENTE_PUERTO_NOTIF_MIN`, `CLIENTE_PUERTO_NOTIF_MAX` | rango de puertos del buzón de avisos |

Más detalle del despliegue y de las mediciones en [`despliegue/DESPLIEGUE.md`](despliegue/DESPLIEGUE.md).

---

## Autores

- David Felipe Mannios Romero
- Jhon Sebastian Mejia Alvarez
