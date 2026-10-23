-- Datos de ejemplo para probar el monitor en local.
USE negocio;

CREATE TABLE pedidos (
  id        INT AUTO_INCREMENT PRIMARY KEY,
  cliente   VARCHAR(50) NOT NULL,
  procesado CHAR(1) NOT NULL DEFAULT 'P',
  fecha     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_procesado_fecha (procesado, fecha)
);

CREATE TABLE facturas (
  id        INT AUTO_INCREMENT PRIMARY KEY,
  importe   DECIMAL(10, 2) NOT NULL,
  procesado CHAR(1) NOT NULL DEFAULT 'P',
  fecha     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_procesado_fecha (procesado, fecha)
);

-- pedidos: 3 pendientes, uno de hace 45 minutos (debe disparar "Cola atascada")
INSERT INTO pedidos (cliente, procesado, fecha) VALUES
  ('ACME',      'S', NOW() - INTERVAL 2 HOUR),
  ('Globex',    'P', NOW() - INTERVAL 45 MINUTE),
  ('Initech',   'P', NOW() - INTERVAL 5 MINUTE),
  ('Umbrella',  'P', NOW());

-- facturas: todo procesado (debe quedar en verde)
INSERT INTO facturas (importe, procesado, fecha) VALUES
  (120.50, 'S', NOW() - INTERVAL 1 DAY),
  (89.99,  'S', NOW() - INTERVAL 3 HOUR);

-- Usuario de monitor: solo lectura + PROCESS (igual que en producción)
CREATE USER 'monitor'@'%' IDENTIFIED BY 'monitor';
GRANT SELECT ON negocio.* TO 'monitor'@'%';
GRANT PROCESS ON *.* TO 'monitor'@'%';
