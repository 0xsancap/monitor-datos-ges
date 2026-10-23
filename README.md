# Monitor de datos

Monitoriza **datos de negocio**, no infraestructura:

| Qué | Cómo | Alerta |
|---|---|---|
| Colas MySQL (`procesado = 'P'`) | `sql_exporter` → consulta SQL | Pendiente más antiguo > 30 min · más de 1000 pendientes |
| Consultas largas en MySQL | `sql_exporter` → `PROCESSLIST` | Consulta en ejecución > 5 min |
| Replication lag de Cloud SQL | `stackdriver-exporter` → Cloud Monitoring | Lag > 60 s · sin datos |
| Procesos batch nocturnos | script `jobmon` → Pushgateway | Error · no se ha ejecutado · colgado |
| El propio monitor | Prometheus `up` | Un check no responde |
| SAP PO (en Azure) | [sappo_exporter](sappo_exporter/README.md) → SOAP | Errores nuevos · scheduled > 1 h · más de 500 scheduled · PO no responde |

```
Servidores batch ──jobmon──► Pushgateway ─┐
MySQL / Cloud SQL ◄── sql_exporter ───────┤
Cloud Monitoring ◄── stackdriver-exporter ┼──► Prometheus ──► Grafana ──► email
SAP PO (Azure) ◄── sappo_exporter ─HTTPS──┘   (90 días)      (monitor + alertas)
```

Todo es configuración en este repo: añadir un check, cambiar un umbral o un destinatario es editar un fichero y reiniciar.

---

## Probar en local

Requisito: Docker.

```bash
make dev          # arranca todo + un MySQL de ejemplo + Mailpit
make dev-batch    # simula 3 batch: uno bien, uno con error y uno colgado
```

SAP PO arranca en modo simulado, con un error reciente y un mensaje scheduled de hace 70 min.

- Grafana: http://localhost:3000 (admin / admin) → *Dashboards → Monitor de datos*
- Emails de alerta: http://localhost:8025 (Mailpit, no envía nada de verdad)
- Prometheus: http://localhost:9090

El MySQL de ejemplo trae un pedido pendiente de hace 45 min, así que a los ~5 minutos llega el email de **Cola atascada**. Para provocar una consulta larga:

```bash
docker compose -p monitor-datos-dev exec -d mysql-dev mysql -uroot -proot -e "SELECT SLEEP(900)"
```

En local no hay GCP, así que la alerta de **Replication lag** salta como *sin datos*. Es lo esperado.

```bash
make dev-down     # para y borra el entorno local
```

---

## Puesta en producción

### 1. Lo que hace falta

- **VM en GCP** (`e2-medium`, Debian/Ubuntu con Docker) dentro de la misma VPC que Cloud SQL.
  - Service account de la VM con el rol `roles/monitoring.viewer`.
  - Firewall: puerto **3000** (Grafana) abierto a la red interna, puerto **9091** (Pushgateway) abierto **solo** a los servidores batch.
- **Usuario MySQL de solo lectura**, en el principal y la réplica:

  ```sql
  CREATE USER 'monitor'@'%' IDENTIFIED BY '...';
  GRANT SELECT ON negocio.* TO 'monitor'@'%';
  GRANT PROCESS ON *.* TO 'monitor'@'%';   -- para ver las consultas de otros usuarios
  ```

  Si la contraseña tiene caracteres como `@ : / ? #`, escríbelos codificados en `.env` (`@` → `%40`), porque va dentro de una URL.
- **Datos SMTP** (el de la empresa, Google Workspace, SendGrid…).

### 2. Configurar

```bash
cp .env.example .env    # y rellena los valores
```

Edita [sql_exporter/collectors/colas.collector.yml](sql_exporter/collectors/colas.collector.yml) con vuestras tablas de cola (un bloque `UNION ALL` por tabla). Cada tabla necesita un índice en `(procesado, fecha)`:

```sql
CREATE INDEX idx_procesado_fecha ON pedidos (procesado, fecha);
```

En [grafana/dashboards/detalle.json](grafana/dashboards/detalle.json), el panel *Pendientes más antiguos · pedidos* es un ejemplo: cambia la tabla o duplícalo desde Grafana (*Export → JSON*) para cada cola.

### 3. Arrancar

```bash
make check   # valida la configuración
make up
```

---

## Procesos batch: `jobmon`

Copia el script a cada servidor batch:

```bash
sudo install -m 755 jobmon/jobmon /usr/local/bin/jobmon
echo 'JOBMON_PUSHGATEWAY=http://IP-DEL-MONITOR:9091' | sudo tee /etc/default/jobmon
```

En el crontab, antepón `jobmon <nombre>` a cada comando:

```cron
# antes
0 2 * * * /opt/batch/cierre.sh >> /var/log/batch/cierre.log 2>&1
# después
0 2 * * * JOBMON_MAX_DURATION=7200 /usr/local/bin/jobmon cierre_diario /opt/batch/cierre.sh >> /var/log/batch/cierre.log 2>&1
```

| Variable | Por defecto | Significado |
|---|---|---|
| `JOBMON_MAX_INTERVAL` | 93600 (26 h) | Máximo entre dos ejecuciones correctas. Para un job horario, pon 5400. |
| `JOBMON_MAX_DURATION` | 10800 (3 h) | Máximo que puede durar una ejecución. |

- Si el monitor no responde, el batch se ejecuta igual y devuelve su propio código de salida: **jobmon nunca hace fallar un job**.
- ⚠️ Un job que nunca ha pasado por `jobmon` no existe para el monitor. Al darlo de alta, ejecútalo una vez a mano.
- Para dar de baja un job: `curl -X DELETE http://IP-DEL-MONITOR:9091/metrics/job/<nombre>/instance/<servidor>`

---

## Cambiar umbrales y alertas

Las alertas están en [grafana/provisioning/alerting/rules.yml](grafana/provisioning/alerting/rules.yml). Cada una tiene:
- una consulta `A` (PromQL),
- un umbral `C` (`params: [1800]`),
- `for:`, que es el tiempo que debe mantenerse antes de avisar.

Los destinatarios están en `.env` (`ALERT_EMAILS`, separados por `;`). La frecuencia de recordatorio de una alerta que sigue activa (4 h) está en [contact-points.yml](grafana/provisioning/alerting/contact-points.yml).

Tras cualquier cambio:

```bash
docker compose restart grafana        # alertas y dashboards
docker compose restart sql-exporter-primary sql-exporter-replica   # consultas SQL
```

Los dashboards están bloqueados para editar desde la web, para que el repo sea la única fuente de verdad. Para modificarlos, edita el panel en Grafana, usa *Export → JSON* y guárdalo en `grafana/dashboards/`.

---

## Estructura

```
docker-compose.yml          servicios de producción
docker-compose.dev.yml      añadidos para local (MySQL de ejemplo, Mailpit)
prometheus/                 scrapes y reglas de recording
sql_exporter/collectors/    consultas SQL → métricas   ← aquí se añaden checks
sappo_exporter/             exportador de SAP PO (contenedor propio, desplegable en Azure)
prometheus/scrape.d/        jobs opcionales (SAP PO): se activan copiando un .example
grafana/provisioning/       datasources, alertas, email
grafana/dashboards/         Estado general y Detalle
jobmon/jobmon               wrapper para los batch
dev/mysql-init/             datos de ejemplo para local
```

## SAP PO

SAP PO está en VMs de Azure y el monitor en GCP. El exportador puede ir en dos sitios:

- **En Azure, junto a PO** (si la VM del monitor no llega a PO). Expone `/metrics` por HTTPS con usuario y contraseña, y solo a la IP del monitor.
- **En la VM del monitor** (si ya hay VPN entre GCP y Azure).

Para saber cuál toca, desde la VM del monitor:

```bash
curl -u USUARIO -I "http://<ip-privada-po>:<puerto>/AdapterMessageMonitoring/basic?wsdl"
```

Responde → en la VM del monitor. Timeout → en Azure. Los pasos de cada opción están en [sappo_exporter/README.md](sappo_exporter/README.md).
