DEV = docker compose --env-file .env.dev -f docker-compose.yml -f docker-compose.dev.yml -p monitor-datos-dev

.PHONY: up down logs dev dev-down dev-logs dev-batch check test-sappo

## Producción (usa .env)
up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

## Entorno local de pruebas (usa .env.dev)
dev:
	$(DEV) up -d
	@echo ""
	@echo "Grafana:  http://localhost:3000  (admin / admin)"
	@echo "Emails:   http://localhost:8025"
	@echo "Prometheus: http://localhost:9090"

dev-down:
	$(DEV) down -v

dev-logs:
	$(DEV) logs -f --tail=100

## Simula tres batch en local: uno bien, uno con error y uno colgado
dev-batch:
	JOBMON_PUSHGATEWAY=http://localhost:9091 ./jobmon/jobmon cierre_diario sleep 2 || true
	JOBMON_PUSHGATEWAY=http://localhost:9091 ./jobmon/jobmon export_sap sh -c 'exit 3' || true
	JOBMON_PUSHGATEWAY=http://localhost:9091 JOBMON_MAX_DURATION=30 ./jobmon/jobmon backup_nocturno sleep 600 >/dev/null 2>&1 &

## Valida la configuración de Prometheus y sql_exporter
check:
	docker run --rm -v $(PWD)/prometheus:/etc/prometheus:ro --entrypoint promtool prom/prometheus:v3.5.0 check config /etc/prometheus/prometheus.yml
	docker run --rm -v $(PWD)/sql_exporter:/config:ro -e 'SQLEXPORTER_TARGET_DSN=mysql://u:p@h:3306/db' burningalchemist/sql_exporter:0.24.9 -config.file=/config/primary.yml -config.check

## Tests del exportador de SAP PO (se ejecutan al construir la imagen)
test-sappo:
	docker build -q -t monitor-datos/sappo-exporter ./sappo_exporter
