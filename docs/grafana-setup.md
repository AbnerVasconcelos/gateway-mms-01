# Grafana Setup Guide

Step-by-step instructions for connecting Grafana to the Gateway IoT Industrial and building dashboards.

## Prerequisites

- Grafana 9.x or later
- Network access from the Grafana server to the Hub (default: port 4567)
- Hub running with at least one device producing data
- The **SimpleJSON** or **Infinity** datasource plugin installed in Grafana

## 1. Plugin Installation

### SimpleJSON (Recommended)

The SimpleJSON datasource plugin is the simplest option. Install it via Grafana CLI:

```bash
grafana-cli plugins install grafana-simple-json-datasource
# Restart Grafana after installation
sudo systemctl restart grafana-server
```

Or search for "SimpleJSON" in **Configuration > Plugins** in the Grafana UI.

### Alternative: Infinity Plugin

The Infinity plugin supports JSON APIs natively and can query the same endpoints:

```bash
grafana-cli plugins install yesoreyeram-infinity-datasource
```

## 2. Datasource Configuration

1. Go to **Configuration > Data sources > Add data source**
2. Search for **SimpleJSON** (or **JSON API** for Infinity)
3. Configure:

| Field | Value |
|-------|-------|
| **Name** | `Gateway Modbus` (or any name) |
| **URL** | `http://<hub_host>:4567/api/grafana` |
| **Access** | Server (default) |

4. Click **Save & Test** — should show "Data source is working"

If the test fails, verify:
- The Hub is running (`curl http://<hub_host>:4567/api/grafana/` should return `"OK"`)
- Firewall allows traffic on port 4567
- The URL includes the `/api/grafana` path

## 3. Metric Naming Convention

Metrics follow the pattern:

```
{device_id}.{channel}.{group}.{tag}
```

Examples:
- `simulador.plc_operacao.controle_extrusora.extrusoraFeedBackSpeed` — Extruder speed
- `simulador.plc_alarmes.alarmes.emergencia` — Emergency alarm (boolean → 0/1)
- `west_24z.plc_west1Temperatura.zona1.tempZona1` — Temperature zone 1

**How to find metrics:**
- Use the metric dropdown in Grafana panel editor — the `/search` endpoint returns all available metrics
- Filter by typing part of the metric name (e.g., "temperatura", "extrusora")
- Via API: `curl -X POST http://<hub_host>:4567/api/grafana/search -H 'Content-Type: application/json' -d '{}'`

## 4. Building Dashboards

### Time-Series Panel (Graph)

1. Create a new panel, select **Time series** visualization
2. Select the `Gateway Modbus` datasource
3. In the metric field, select or type the metric name
4. Set the appropriate time range and refresh interval

**Recommended refresh intervals:**
- Real-time monitoring: 1s–5s
- Trend analysis: 30s–1m
- Historical review: No auto-refresh

### Multiple Metrics

Add multiple queries (A, B, C...) to the same panel to overlay metrics:
- A: `simulador.plc_operacao.controle_extrusora.extrusoraFeedBackSpeed`
- B: `simulador.plc_operacao.controle_extrusora.extrusoraRefVelocidade`

### Table Panel

1. Select **Table** visualization
2. In the query, set type to "Table" (if using SimpleJSON)
3. Columns: Time, Metric, Value

### Boolean/Alarm Panels

Coil values (boolean) are automatically converted to `0` or `1`:
- Use **Stat** or **Gauge** visualization
- Set thresholds: 0 = green (off), 1 = red (on)
- Or use **State timeline** for on/off history

## 5. Example Dashboards

### Temperature Monitoring

Create a dashboard with:

| Panel | Metrics | Visualization |
|-------|---------|---------------|
| Zone Temperatures | `west_24z.plc_west1Temperatura.zona*.tempZona*` | Time series |
| Set Points | `west_24z.plc_west1SetPoint.zona*.setPointZona*` | Time series |
| Zone On/Off | `west_24z.plc_west1LigDeslZona.zona*.ligDesZona*` | State timeline |
| Alerts | `west_24z.plc_west1Alertas.zona*.*` | Table |

### Extruder Operations

| Panel | Metrics | Visualization |
|-------|---------|---------------|
| Speed Feedback | `simulador.plc_operacao.controle_extrusora.extrusoraFeedBackSpeed` | Gauge |
| Speed Reference | `simulador.plc_operacao.controle_extrusora.extrusoraRefVelocidade` | Gauge |
| Motor Current | `simulador.plc_operacao.controle_extrusora.extrusoraCorrente` | Time series |
| Running State | `simulador.plc_operacao.controle_extrusora.extrusoraLigadoDesligado` | Stat |

### Alarm Overview

| Panel | Metrics | Visualization |
|-------|---------|---------------|
| Active Alarms | All `*.plc_alarmes.*.*` | State timeline |
| Emergency | `simulador.plc_alarmes.alarmes.emergencia` | Stat (red/green) |
| Alarm History | All `*.plc_alarmes.*.*` | Table |

## 6. Troubleshooting

### "Data source is not working"

- Verify the Hub is running: `curl http://<host>:4567/health`
- Check the URL includes `/api/grafana` (not just the host)
- Check firewall/network connectivity

### No metrics in dropdown

- Verify Delfos is running and publishing data
- Check Redis has history data: `redis-cli LLEN history:plc_alarmes`
- If no history, metrics are derived from configured variables (ensure variables have channels assigned)

### Empty panels / no data

- Verify the time range includes data — Grafana defaults to "Last 6 hours"; if the gateway just started, try "Last 15 minutes"
- Check that the selected metric exists: use the search endpoint to verify
- Ensure the channel is enabled and Delfos is reading it

### Data appears delayed

- The gateway publishes at configured `delay_ms` intervals (default 1000ms)
- Grafana's minimum refresh interval depends on your configuration
- Redis history is bounded; very old data may have been trimmed

### Metric names are empty

- This happens when channels have no history and no variables with assigned channels
- Assign variables to channels in the Hub web panel, then restart Delfos
