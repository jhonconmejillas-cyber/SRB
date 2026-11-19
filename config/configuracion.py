"""
Configuracion del SRB.

Lee el archivo .env que esta en la raiz del proyecto y deja cada valor en una
constante (HOST_CAPA_SERVICIOS, SP_PUERTO, ...). Cada maquina tiene su propia
copia del .env con las IP de las demas; el codigo es el mismo en todas.
"""
from pathlib import Path
from dotenv import load_dotenv
import os

CARPETA_CONFIG = Path(__file__).resolve().parent   # .../config
RAIZ = CARPETA_CONFIG.parent                       # carpeta raiz del proyecto

# Carga las variables del .env como si fueran variables de entorno
load_dotenv(RAIZ / ".env")


def texto(nombre):
    """Lee una variable obligatoria del .env. Si falta, se detiene con un mensaje claro."""
    v = os.getenv(nombre)
    if not v:
        raise RuntimeError(f"No se puede obtener el valor {nombre}, no esta presente en el .env")
    return v


def valor(nombre):
    """Lee una variable obligatoria que debe ser un numero entero."""
    v = texto(nombre)
    try:
        return int(v)
    except ValueError:
        raise RuntimeError(f"La variable {nombre} debe ser un numero entero, pero vale '{v}'")


def valor_opcional(nombre, por_defecto):
    """Como valor(), pero si la variable no esta en el .env usa un valor por defecto."""
    return valor(nombre) if os.getenv(nombre) else por_defecto


def lista(nombre, por_defecto):
    """
    Lee una variable con varios valores separados por coma, por ejemplo
    GT_HOSTS=10.43.99.1,10.43.100.84  ->  ["10.43.99.1", "10.43.100.84"].
    Si la variable no esta en el .env devuelve por_defecto.
    """
    v = os.getenv(nombre)
    if not v:
        return list(por_defecto)
    return [parte.strip() for parte in v.split(",") if parte.strip()]


def direccion(host, puerto):
    """Direccion para connect() (ir a buscar a otro): tcp://host:puerto"""
    return f"tcp://{host}:{puerto}"


def direccion_de(entrada, puerto):
    """
    Como direccion(), pero la entrada puede traer su propio puerto:
      "10.43.100.84"       -> tcp://10.43.100.84:<puerto>
      "127.0.0.1:5569"     -> tcp://127.0.0.1:5569   (util para probar todo en un PC)
    """
    if ":" in entrada:
        return f"tcp://{entrada}"
    return direccion(entrada, puerto)


def direccion_bind(puerto):
    """Direccion para bind() (abrir un puerto propio) en todas las interfaces: tcp://*:puerto"""
    return f"tcp://*:{puerto}"


# --- Maquinas: la IP de cada capa
HOST_CAPA_CONSUMIDORES = texto("HOST_CAPA_CONSUMIDORES")   # donde corren los clientes
HOST_CAPA_SERVICIOS = texto("HOST_CAPA_SERVICIOS")         # GT, RMC y servicio de cancelacion
HOST_CAPA_PERSISTENCIA = texto("HOST_CAPA_PERSISTENCIA")   # servicio de persistencia y la BD

# --- Puertos del Gestor de Transacciones
GT_PUERTO_CLIENTES = valor("GT_PUERTO_CLIENTES")           # aqui llegan los clientes
GT_PUERTO_CANCELACION = valor("GT_PUERTO_CANCELACION")     # aqui se conecta el servicio de cancelacion

# --- Replicas del GT: primero el primario (gt_1), luego el respaldo (gt_2).
#     Los usan los clientes (para el failover) y el servicio de cancelacion
#     (que escucha a todos los GT). Si no esta, hay un solo GT en HOST_CAPA_SERVICIOS.
GT_HOSTS = lista("GT_HOSTS", [HOST_CAPA_SERVICIOS])

# --- Broker ROUTER/DEALER (uno en cada maquina de negocio)
#     El GT se conecta al broker de SU maquina (BROKER_HOST : BROKER_PUERTO_GT).
#     Los RMC se conectan a TODOS los brokers (BROKERS : BROKER_PUERTO_RMC).
BROKER_PUERTO_GT = valor_opcional("BROKER_PUERTO_GT", 5559)
BROKER_PUERTO_RMC = valor_opcional("BROKER_PUERTO_RMC", valor_opcional("GT_PUERTO_RMC", 5556))
BROKER_HOST = os.getenv("BROKER_HOST") or "127.0.0.1"      # normalmente la misma maquina del GT
BROKERS = lista("BROKERS", [HOST_CAPA_SERVICIOS])

# --- Persistencia
SP_PUERTO = valor("SP_PUERTO")
BD_RUTA = RAIZ / texto("BD_RUTA")

# --- Notificaciones: rango de puertos donde cada cliente abre su "buzon" para
#     recibir los avisos de cancelacion. Hay que permitirlos en el firewall de
#     la maquina de clientes. Son opcionales en el .env.
CLIENTE_PUERTO_NOTIF_MIN = valor_opcional("CLIENTE_PUERTO_NOTIF_MIN", 5600)
CLIENTE_PUERTO_NOTIF_MAX = valor_opcional("CLIENTE_PUERTO_NOTIF_MAX", 5699)

# --- Tiempo maximo que se espera una respuesta (milisegundos)
TIMEOUT_MS = valor("TIMEOUT_MS")

# --- Tolerancia a fallos del cliente (patron Lazy Pirate). Todas opcionales.
#     CLIENTE_TIMEOUT_MS: cuanto espera el cliente CADA intento antes de darlo por perdido.
#     CLIENTE_INTENTOS: cuantos intentos en total como maximo (contando el primero).
#     CLIENTE_FALLOS_PARA_CAMBIAR: fallos seguidos con un GT antes de pasar al siguiente.
#     BACKOFF_BASE_MS / BACKOFF_MAX_MS: espera entre intentos: 100, 200, 400... hasta el maximo.
CLIENTE_TIMEOUT_MS = valor_opcional("CLIENTE_TIMEOUT_MS", TIMEOUT_MS)
CLIENTE_INTENTOS = valor_opcional("CLIENTE_INTENTOS", 6)
CLIENTE_FALLOS_PARA_CAMBIAR = valor_opcional("CLIENTE_FALLOS_PARA_CAMBIAR", 2)
BACKOFF_BASE_MS = valor_opcional("BACKOFF_BASE_MS", 100)
BACKOFF_MAX_MS = valor_opcional("BACKOFF_MAX_MS", 2000)


if __name__ == "__main__":
    # python -m config.configuracion  -> muestra la configuracion leida
    print("Configuracion leida de", RAIZ / ".env")
    print("  Consumidores :", HOST_CAPA_CONSUMIDORES)
    print("  Servicios    :", HOST_CAPA_SERVICIOS)
    print("  Persistencia :", HOST_CAPA_PERSISTENCIA)
    print("  GT (clientes):", [direccion_de(h, GT_PUERTO_CLIENTES) for h in GT_HOSTS])
    print("  GT cancelac. :", [direccion_de(h, GT_PUERTO_CANCELACION) for h in GT_HOSTS])
    print("  Broker del GT:", direccion(BROKER_HOST, BROKER_PUERTO_GT))
    print("  Brokers (RMC):", [direccion_de(h, BROKER_PUERTO_RMC) for h in BROKERS])
    print("  Persistencia :", direccion(HOST_CAPA_PERSISTENCIA, SP_PUERTO))
    print("  Notificac.   :", f"puertos {CLIENTE_PUERTO_NOTIF_MIN}-{CLIENTE_PUERTO_NOTIF_MAX}")
    print("  Base de datos:", BD_RUTA)
    print("  Timeout      :", TIMEOUT_MS, "ms")
    print("  Cliente      :", f"{CLIENTE_TIMEOUT_MS} ms por intento, {CLIENTE_INTENTOS} intentos, "
                              f"cambia de GT tras {CLIENTE_FALLOS_PARA_CAMBIAR} fallos, "
                              f"espera {BACKOFF_BASE_MS}-{BACKOFF_MAX_MS} ms")
