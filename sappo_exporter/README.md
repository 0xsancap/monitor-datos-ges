# Exportador de SAP PO

Consulta SAP PO (Adapter Engine) por SOAP, con el servicio `AdapterMessageMonitoringVi`, y publica métricas para Prometheus en `/metrics`, protegido con usuario y contraseña y, opcionalmente, por HTTPS.

- Consulta PO **cada 60 s en segundo plano**, una llamada por estado. `/metrics` devuelve el último resultado, así que un scrape nunca carga PO.
- Solo lee: no reprocesa, no cancela y no toca ningún mensaje.
- Si la llamada a PO falla, `sappo_consulta_ok` pasa a 0 y vuelve a conectar en el siguiente ciclo.

## Métricas

| Métrica | Significado |
|---|---|
| `sappo_mensajes{estado}` | Mensajes en cada estado en los últimos `SAPPO_VENTANA_DIAS` días |
| `sappo_mensaje_mas_antiguo_segundos{estado}` | Antigüedad del más viejo (0 si no hay) |
| `sappo_errores_nuevos` | Mensajes en `systemError` que empezaron en los últimos 15 min |
| `sappo_mensajes_por_interfaz{estado,interfaz}` | Desglose por interfaz |
| `sappo_resultado_truncado{estado}` | 1 si PO devolvió el máximo de mensajes (el número real es mayor) |
| `sappo_consulta_ok` | 1 si la última consulta a PO funcionó |
| `sappo_consulta_duracion_segundos` | Cuánto tardó PO en responder |

Estados: `systemError` (error), `toBeDelivered` (lo que el monitor de PO llama **Scheduled**), `holding` y `waiting`. Se configuran con `SAPPO_ESTADOS`.

## Despliegue en Azure, junto a PO

Es la opción cuando la VM del monitor (GCP) no llega a PO. De Azure solo salen números, nunca datos de los mensajes.

```
GCP: Prometheus ──HTTPS + usuario/contraseña──► Azure: sappo-exporter ──SOAP──► SAP PO
                  (NSG: solo la IP del monitor)          (red privada)
```

**1. Requisitos**
- Una VM Linux con Docker en la VNet de PO. Sirve una existente o una `B1s`.
- Un usuario técnico en PO con rol de solo lectura de monitorización (normalmente `SAP_XI_API_DISPLAY_J2EE`; confírmalo con Basis).
- La IP de salida **fija** de la VM del monitor en GCP (IP externa estática o Cloud NAT con IP reservada).

**2. Comprobar que la VM llega a PO**

```bash
curl -u PIMONITOR -I "http://<ip-po>:<puerto>/AdapterMessageMonitoring/basic?wsdl"
```

Tiene que devolver 200. Un 401 significa que el usuario o la contraseña no son válidos; un 403, que falta el rol.

**3. Instalar**

Copia esta carpeta a la VM y:

```bash
cp .env.example .env                          # rellena SAPPO_* y METRICS_PASSWORD
openssl rand -base64 24                       # sirve como METRICS_PASSWORD
./generar-certificado.sh <ip-publica-de-esta-vm>
docker compose -f docker-compose.azure.yml up -d --build
docker compose -f docker-compose.azure.yml logs -f    # debe decir "Conectado a SAP PO"
```

**4. Abrir el puerto en Azure**

Regla de entrada en el NSG de la VM: TCP **9105**, con origen **solo la IP del monitor**. Nunca `Any`.

**5. Conectar el monitor (en la VM de GCP)**

```bash
cp prometheus/scrape.d/sappo-azure.yml.example prometheus/scrape.d/sappo.yml
# edita la IP en prometheus/scrape.d/sappo.yml
mkdir -p prometheus/secrets
echo -n 'el METRICS_PASSWORD de Azure' > prometheus/secrets/sappo_password
scp azure-vm:monitor-datos/sappo_exporter/certs/sappo-exporter.crt prometheus/secrets/sappo_ca.crt
docker compose restart prometheus
```

En http://localhost:9090/targets (desde la VM del monitor), el job `sappo` debe aparecer como **UP**.

## Alternativa: en la misma VM del monitor

Si la VM de GCP ya llega a PO (hay VPN entre GCP y Azure), no hace falta nada en Azure: rellena `SAPPO_*` en el `.env` principal, añade `sappo` a `COMPOSE_PROFILES` y usa `prometheus/scrape.d/sappo-local.yml.example`. En este caso el tráfico no sale de la red privada y no hace falta HTTPS.

## Probar sin PO

```bash
SAPPO_MODO=simulado METRICS_PASSWORD=x docker compose -f docker-compose.azure.yml up --build
```

O, desde la raíz del proyecto, `make dev`, que lo arranca simulado junto con todo el monitor. Hay un error reciente y un mensaje scheduled de hace 70 min, para que salten las alertas.

Tests: se ejecutan solos al construir la imagen. Incluyen un servidor SOAP falso con un WSDL de estructura similar a la de PO.

## Si algo no cuadra con vuestro PO

El WSDL varía entre versiones (7.31, 7.4, 7.5). El exportador:
- lee el WSDL real y solo envía los campos del filtro que existen en él;
- busca los mensajes en la respuesta estén donde estén (cualquier objeto con `messageID`);
- llama siempre a `SAPPO_URL`, aunque el WSDL traiga el nombre interno del servidor.

Si aun así falla, `LOG_LEVEL=DEBUG` muestra el detalle. Comparte el error junto con el WSDL (`.../AdapterMessageMonitoring/basic?wsdl&mode=ws_policy&style=document`).
