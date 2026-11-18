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


def direccion(host, puerto):
    """Direccion para connect() (ir a buscar a otro): tcp://host:puerto"""
    return f"tcp://{host}:{puerto}"


def direccion_bind(puerto):
    """Direccion para bind() (abrir un puerto propio) en todas las interfaces: tcp://*:puerto"""
    return f"tcp://*:{puerto}"


# --- Maquinas: la IP de cada capa
HOST_CAPA_CONSUMIDORES = texto("HOST_CAPA_CONSUMIDORES")   # donde corren los clientes
HOST_CAPA_SERVICIOS = texto("HOST_CAPA_SERVICIOS")         # GT, RMC y servicio de cancelacion
HOST_CAPA_PERSISTENCIA = texto("HOST_CAPA_PERSISTENCIA")   # servicio de persistencia y la BD

# --- Puertos del Gestor de Transacciones
GT_PUERTO_CLIENTES = valor("GT_PUERTO_CLIENTES")           # aqui llegan los clientes
GT_PUERTO_RMC = valor("GT_PUERTO_RMC")                     # aqui se conectan los RMC
GT_PUERTO_CANCELACION = valor("GT_PUERTO_CANCELACION")     # aqui se conecta el servicio de cancelacion

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


if __name__ == "__main__":
    # python -m config.configuracion  -> muestra la configuracion leida
    print("Configuracion leida de", RAIZ / ".env")
    print("  Consumidores :", HOST_CAPA_CONSUMIDORES)
    print("  Servicios    :", HOST_CAPA_SERVICIOS)
    print("  Persistencia :", HOST_CAPA_PERSISTENCIA)
    print("  GT clientes  :", direccion(HOST_CAPA_SERVICIOS, GT_PUERTO_CLIENTES))
    print("  GT para RMC  :", direccion(HOST_CAPA_SERVICIOS, GT_PUERTO_RMC))
    print("  GT cancelac. :", direccion(HOST_CAPA_SERVICIOS, GT_PUERTO_CANCELACION))
    print("  Persistencia :", direccion(HOST_CAPA_PERSISTENCIA, SP_PUERTO))
    print("  Notificac.   :", f"puertos {CLIENTE_PUERTO_NOTIF_MIN}-{CLIENTE_PUERTO_NOTIF_MAX}")
    print("  Base de datos:", BD_RUTA)
    print("  Timeout      :", TIMEOUT_MS, "ms")
