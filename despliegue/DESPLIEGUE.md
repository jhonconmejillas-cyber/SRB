# Despliegue en las 3 maquinas

| Maquina | IP | Que corre |
|---|---|---|
| NBIG296 | 10.43.99.237 | Persistencia + SQLite + servicio de Cancelaciones (con systemd) |
| NBIG291 | 10.43.99.232 | Clientes + gt_1 (primario) + broker_1 + RMC |
| NBIG381 | 10.43.100.84 | gt_2 (respaldo) + broker_2 + RMC |

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

## 1. Preparar cada maquina (las 3)

```bash
sudo apt install -y python3-venv git
git clone https://github.com/jhonconmejillas-cyber/SRB.git      # repo privado: pide usuario y token
cd SRB
python3 -m venv venv
venv/bin/pip install -r dependencias.txt
cp despliegue/NBIG<numero>.env .env       # el de ESA maquina: NBIG296, NBIG291 o NBIG381
venv/bin/python -m config.configuracion   # revisar que las IP salgan bien
```

## 2. Abrir puertos en el firewall

```bash
# NBIG296
sudo ufw allow 5558/tcp                    # persistencia

# NBIG291 y NBIG381
sudo ufw allow 5555/tcp                    # GT: clientes
sudo ufw allow 5556/tcp                    # broker: RMC de la otra maquina
sudo ufw allow 5557/tcp                    # GT: servicio de cancelacion
# (5559 no hace falta abrirlo: el GT se conecta a su broker por 127.0.0.1)

# NBIG291 ademas
sudo ufw allow 5600:5699/tcp               # buzon de avisos de los clientes
```

Para probar que se ven: `nc -zv 10.43.99.237 5558` desde NBIG291.

## 3. Arrancar, en este orden

**NBIG296** (persistencia y cancelacion quedan como servicios: si se caen, Linux los relanza):
```bash
venv/bin/python -m capa_persistencia.cargar_datos       # solo la primera vez (borra la base)
bash despliegue/instalar_servicios.sh
systemctl status srb-persistencia srb-cancelacion
```

**NBIG381** y **NBIG291** (broker + GT + 2 RMC en cada una):
```bash
bash despliegue/negocio.sh arrancar 2
bash despliegue/negocio.sh estado
tail -f logs/gt.log                       # ver las peticiones que pasan
```

**NBIG291**, clientes:
```bash
venv/bin/python -m capa_cliente.main --id 1 --script pruebas/caso_basico.txt
venv/bin/python -m capa_cliente.main --id 2 --script pruebas/caso_modificacion_cancelacion.txt
```

Para escalar en caliente: `bash despliegue/negocio.sh rmc 1` (agrega un RMC; nadie se reinicia).
Para detener: `bash despliegue/negocio.sh parar`.

## 4. Prueba de caida del GT (failover)

En NBIG291, terminal 1: un cliente con carga continua.
```bash
venv/bin/python -m capa_cliente.main --id 1 --carga --duracion 60 --por-minuto 60
```
Terminal 2, a los ~15 s:
```bash
kill -9 $(cat logs/gt.pid)                # mata gt_1 sin avisar
```
En la terminal 1 se ve:
```
[C1] RESERVA: gt_1 (tcp://10.43.99.232:5555) no respondio en 2500 ms; reintento 1 en 120 ms
[C1] RESERVA: gt_1 (tcp://10.43.99.232:5555) no respondio en 2500 ms; reintento 2 en 280 ms
[C1] FAILOVER: gt_1 (...) no responde -> ahora uso gt_2 (tcp://10.43.100.84:5555)
```
y el cliente termina sin errores. En `resultados/cliente_1.csv` la columna `gt` pasa de 1 a 2 y
`intentos` muestra la peticion que se reintento. En `logs/gt.log` de NBIG381 empiezan a llegar
las peticiones. Gracias al uuid, la peticion que iba en vuelo no queda duplicada en la base.

## 5. Prueba de caida de la persistencia (systemd)

En NBIG296:
```bash
journalctl -u srb-persistencia -f &                                   # ver el log
sudo kill -9 $(systemctl show -p MainPID --value srb-persistencia)    # matarla
```
En ~1 s systemd la relanza ("Scheduled restart job" en el log). Los datos siguen en
`datos/srb.db`. Un cliente que estaba trabajando, como mucho reintenta una peticion.

## 6. Medir TPM y tiempo de respuesta (3, 5, 8 clientes x 1, 2, 3 RMC)

Cuantos RMC hay en total se controla con `negocio.sh` en las dos maquinas de negocio:

| RMC en total | NBIG291 | NBIG381 |
|---|---|---|
| 1 | `arrancar 1` | `arrancar 0` |
| 2 | `arrancar 1` | `arrancar 1` |
| 3 | `arrancar 2` | `arrancar 1` |

Antes de cada corrida: `negocio.sh parar` en las dos y recargar la base en NBIG296
(`sudo systemctl stop srb-persistencia && venv/bin/python -m capa_persistencia.cargar_datos && sudo systemctl start srb-persistencia`).

Luego, en NBIG291 (cada corrida dura 2.5 min):
```bash
venv/bin/python -m capa_cliente.escenarios --escenario 1 --etiqueta rmc1    # 3 clientes
venv/bin/python -m capa_cliente.escenarios --escenario 2 --etiqueta rmc1    # 5 clientes
venv/bin/python -m capa_cliente.escenarios --escenario 3 --etiqueta rmc1    # 8 clientes
```
Repetir con `--etiqueta rmc2` y `rmc3`. Los CSV quedan en `resultados/rmc<N>_<M>clientes/`.

### Analizar

```bash
venv/bin/python -m herramientas.analizar_metricas "resultados/rmc*_*clientes" --gt resultados/gt_metricas.csv
```
Imprime una tabla (TPM, tiempo promedio, p95, maximo, exitos, fallos, reintentos, peticiones
por gt_2) y guarda `resultados/resumen_metricas.csv`. Para las graficas (mejor en tu PC,
copiando la carpeta `resultados/`):
```bash
pip install matplotlib
python -m herramientas.analizar_metricas "resultados/rmc*_*clientes" --graficas
```
Genera `resultados/grafica_tpm.png` y `resultados/grafica_tiempo.png`.

### Ojo al interpretar
Con el ritmo del enunciado (20 peticiones por minuto por cliente) el sistema esta lejos de
saturarse: el TPM es igual a la carga que mandan los clientes y casi no cambia con 1, 2 o 3 RMC.
Para ver el efecto de agregar RMC hace falta una corrida de **estres**, mandando lo mas rapido
posible (cada cliente envia la siguiente peticion apenas recibe la respuesta):
```bash
venv/bin/python -m capa_cliente.escenarios --escenario 3 --etiqueta rmc1 --por-minuto 100000
```
En ese caso el TPM mide la capacidad maxima. Como la persistencia es un solo proceso (SQLite
admite un solo escritor), a partir de cierto punto ella es el cuello de botella: es un
resultado valido para discutir en el informe.
