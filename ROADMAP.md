# ROADMAP — Gateway IoT Industrial

Planejamento de evolução do sistema. As três iniciativas são incrementais e independentes, mas a Fase 2 (Hub) depende da Fase 1 estar estável.

---

## Visão geral

```
FASE 1 — Leitura segmentada        [Delfos]
FASE 2 — Hub: Socket.IO + Config   [Hub - novo processo]
FASE 3 — Painel Web                [Hub - expansão]
```

---

## FASE 1 — Leitura segmentada por canal com frequência adaptativa

**Objetivo:** substituir a publicação monolítica em `plc_data` por canais Redis separados por grupo de variáveis, cada um com delay configurável.

**Motivação:** o loop atual lê e publica todos os 81 tags de uma vez, na mesma frequência. Alarmes precisam de 200ms; parâmetros de configuração podem esperar 10s. Separar permite otimizar leituras Modbus e reduzir carga no broker Redis.

### 1.1 — Arquivos de configuração

**Novos arquivos em `tables/`:**

- **`group_config.json`** — mapeia cada grupo (`key` do CSV) para canal Redis e delay:

```json
{
  "_meta": {
    "aggregate_channel": "plc_data",
    "backward_compatible": true
  },
  "groups": {
    "alarmes":        { "channel": "plc_alarmes",  "delay_ms": 200,   "history_size": 100 },
    "saidasDigitais": { "channel": "plc_process",  "delay_ms": 500,   "history_size": 100 },
    "Extrusora":      { "channel": "plc_process",  "delay_ms": 1000,  "history_size": 100 },
    "Puxador":        { "channel": "plc_process",  "delay_ms": 1000,  "history_size": 100 },
    "threeJs":        { "channel": "plc_visual",   "delay_ms": 1000,  "history_size": 100 },
    "producao":       { "channel": "plc_process",  "delay_ms": 2000,  "history_size": 100 },
    "dosador":        { "channel": "plc_process",  "delay_ms": 2000,  "history_size": 100 },
    "alimentador":    { "channel": "plc_process",  "delay_ms": 2000,  "history_size": 100 },
    "totalizadores":  { "channel": "plc_config",   "delay_ms": 5000,  "history_size": 100 },
    "_configuracao":  { "channel": "plc_config",   "delay_ms": 10000, "history_size": 100 }
  }
}
```

- **`variable_overrides.json`** — exceções por tag individual (sobrescreve o grupo):

```json
{
  "emergencia":             { "enabled": true,  "channel": "plc_alarmes", "delay_ms": 100  },
  "densidadeMedia":         { "enabled": false, "channel": "plc_config",  "delay_ms": 10000 }
}
```

> Regra de precedência: `variable_overrides` > `group_config` > padrão do grupo no CSV.

**Critério de aceite:**
- [ ] `group_config.json` válido e versionado
- [ ] `variable_overrides.json` válido e versionado
- [ ] Ambos documentados no `CLAUDE.md`

---

### 1.2 — `Delfos/table_filter.py`

Adicionar `extract_parameters_by_group()` que retorna um `dict` keyed por grupo, em vez das listas achatadas atuais. A otimização de contiguidade Modbus passa a ser aplicada dentro de cada grupo.

```python
# retorno esperado:
{
  "Extrusora": GroupData(coil_groups, reg_groups, coil_tags, reg_tags, coil_keys, reg_keys),
  "Puxador":   GroupData(...),
  ...
}
```

A função `extract_parameters_from_csv` existente é mantida para não quebrar testes.

**Critério de aceite:**
- [ ] Nova função retorna dict correto para `operacao.csv` e `configuracao.csv`
- [ ] Testes unitários passando (`tests/test_integration.py`)
- [ ] Nenhuma regressão nos testes existentes

---

### 1.3 — `Delfos/delfos.py`

Substituir o loop monolítico por um **time-tracking loop** de resolução ~50ms.

**Lógica:**
```
last_read[group] = 0  # por grupo

loop (50ms tick):
  now = time.time()
  pending_by_channel = defaultdict({"coils": {}, "registers": {}})

  for group in all_groups:
    if now - last_read[group] >= delay[group] / 1000:
      read coils + registers do grupo via Modbus
      merge em pending_by_channel[channel_do_grupo]
      last_read[group] = now

  for channel, data in pending_by_channel.items():
    data["timestamp"] = now.isoformat()
    publish(channel, data)
    if backward_compatible:
      publish("plc_data", data)   # compatibilidade downstream
```

**Decisão de design — single-threaded:**
A conexão `pyModbusTCP` não é thread-safe. Single-threaded com time-tracking resolve o problema sem mutex. Leituras serializadas no pior caso: ~50ms de latência adicional por grupo em rodadas concorrentes. Aceitável para a frequência mínima de 200ms.

**Escuta de `config_reload`:**
Adicionar `config_reload` aos canais assinados. Quando recebido, recarregar `group_config.json` e `variable_overrides.json` sem reiniciar o processo.

**Critério de aceite:**
- [ ] Cada grupo publica no canal correto com o delay configurado (±50ms de tolerância)
- [ ] Canal `plc_data` continua recebendo tudo (compatibilidade)
- [ ] Hot-reload de config via `config_reload` funcionando
- [ ] Testes de integração passando

---

### 1.4 — Testes

| Arquivo | O que testar |
|---------|-------------|
| `tests/test_segmented_reading.py` | Cada grupo publica no canal esperado |
| `tests/test_segmented_reading.py` | Delay por grupo respeitado com margem de 50ms |
| `tests/test_segmented_reading.py` | Hot-reload: alterar `group_config.json` → Delfos atualiza sem reiniciar |
| `tests/test_integration.py` | Nenhuma regressão nos 15 testes existentes |

---

## FASE 2 — Hub: Socket.IO bridge + Config Panel

**Objetivo:** novo processo `Hub/` que faz a ponte Redis ↔ WebSocket. Permite que qualquer frontend (browser, Node.js, dashboard) consuma dados do CLP em tempo real e envie comandos de escrita sem acesso direto ao Redis.

**Arquitetura:**
```
Redis (plc_*, alarms, plc_commands, user_status)
          ↕
       Hub/main.py
   FastAPI + python-socketio (ASGI)
          ↕
      Browser / Frontend
```

### 2.1 — Estrutura de arquivos

```
Hub/
├── main.py              # FastAPI + Socket.IO app + startup tasks
├── redis_bridge.py      # background task: Redis psubscribe → sio.emit
├── config_store.py      # lê/escreve group_config.json e variable_overrides.json
├── templates/
│   └── index.html       # painel de configuração (Fase 3)
├── .env
└── .env.example
```

**Dependências novas (`requirements.txt`):**
```
python-socketio==5.x
uvicorn[standard]
redis[asyncio]
openpyxl
fastapi
```

### 2.2 — `Hub/redis_bridge.py`

Background task assíncrono. Usa `redis.asyncio` com `psubscribe('plc_*')` para capturar todos os canais de dados do Delfos.

```
psubscribe('plc_*', 'alarms')
  → mensagem recebida
    → channel = "plc_alarmes"
    → room    = "alarmes"   (remove prefixo "plc_")
    → sio.emit("plc:data", {channel, data}, room=room)
```

O uso de **rooms** garante que cada cliente receba apenas os grupos que subscreveu, sem flooding.

### 2.3 — Eventos Socket.IO

**Server → Client:**

| Evento | Payload | Quando |
|--------|---------|--------|
| `plc:data` | `{channel: str, data: {coils, registers, timestamp}}` | A cada publicação Delfos |
| `config:updated` | objeto de configuração | Após `config:save` bem-sucedido |
| `connection_ack` | `{status, available_rooms}` | Na conexão inicial |

**Client → Server:**

| Evento | Payload | Ação no Hub |
|--------|---------|-------------|
| `join` | `{rooms: ["alarmes", "process"]}` | Entra nos rooms Redis correspondentes |
| `plc:write` | `{tag: str, value: any}` | Publica em Redis `plc_commands` |
| `user:status` | `{user_state: bool}` | Publica em Redis `user_status` |
| `config:save` | objeto de configuração | Salva arquivos + publica `config_reload` |
| `config:get` | — | Retorna config atual |
| `history:set` | `{channel: str, size: int}` | Atualiza `history_size` do canal + publica `config_reload` |
| `history:get` | — | Retorna `{channel: history_size}` para todos os canais |

### 2.4 — Segurança de escrita

O Hub **não valida** `user_state` — essa responsabilidade é da Atena, que já implementa a guarda `if user_state`. O Hub apenas encaminha o payload recebido. Isso mantém a separação de responsabilidades.

**Critério de aceite:**
- [ ] Hub conecta ao Redis e escuta `plc_*` sem erros
- [ ] Cliente Socket.IO recebe `plc:data` em <100ms após publicação do Delfos
- [ ] Evento `plc:write` resulta em escrita no CLP (via Atena)
- [ ] Rooms isolam dados corretamente (cliente em "alarmes" não recebe "process")
- [ ] Hub inicia com `uvicorn Hub.main:asgi_app --port 8000`

---

### 2.5 — Testes

| Arquivo | O que testar |
|---------|-------------|
| `tests/test_hub.py` | Bridge Redis → Socket.IO com cliente de teste |
| `tests/test_hub.py` | Evento `plc:write` chega no Redis `plc_commands` |
| `tests/test_hub.py` | Rooms: cliente isolado não recebe eventos de outro room |
| `tests/test_hub.py` | `config:save` → arquivo atualizado + `config_reload` publicado |

---

## FASE 3 — Painel Web de configuração de variáveis

**Objetivo:** interface gráfica servida pelo Hub para visualizar, configurar e exportar as variáveis do sistema. Integra-se à Fase 1 (edita `group_config.json`) e à Fase 2 (usa Socket.IO para preview em tempo real).

### 3.1 — Funcionalidades

| Feature | Detalhe |
|---------|---------|
| **Upload de planilha** | Aceita `.xlsx` com colunas compatíveis com `operacao.csv`; exibe preview antes de confirmar |
| **Tabela de variáveis** | Exibe todos os tags com filtro por grupo, tipo (`%MB`/`%MW`), canal, status habilitado |
| **Edição inline** | Editar canal Redis de destino, delay (ms), flag `enabled` por variável ou por grupo |
| **Configuração de histórico** | Definir `history_size` (número de publicações armazenadas) por canal Redis; alteração aplica `ltrim` imediato no Redis e persiste em `group_config.json` |
| **Preview em tempo real** | Coluna "último valor" atualizada via Socket.IO sem refresh |
| **Exportação** | Download de `.xlsx` com configurações atuais; opção de exportar CSV compatível com o projeto |
| **Diff visual** | Destaca variáveis com override individual em relação ao padrão do grupo |

### 3.2 — Stack

- **Backend:** FastAPI (já no Hub), endpoints REST para upload/export
- **Frontend:** HTML + AG Grid (CDN, sem build step) + Socket.IO client (CDN)
- **Armazenamento:** `tables/group_config.json` + `tables/variable_overrides.json`

### 3.3 — Endpoints REST do Hub

| Método | Rota | Função |
|--------|------|--------|
| `GET` | `/api/variables` | Lista todos os tags com config mesclada (grupo + overrides) |
| `PATCH` | `/api/variables/{tag}` | Atualiza override de um tag específico |
| `POST` | `/api/upload` | Processa `.xlsx` enviado e retorna preview |
| `POST` | `/api/upload/confirm` | Aplica o `.xlsx` carregado como nova base |
| `GET` | `/api/export` | Retorna `.xlsx` com configurações atuais |
| `GET` | `/api/channels` | Lista canais com `delay_ms` e `history_size` configurados |
| `PATCH` | `/api/channels/{channel}/history` | Atualiza `history_size`; aplica `ltrim` imediato no Redis + persiste |
| `GET` | `/api/groups` | Lista grupos e seus delimitadores configurados |

### 3.4 — Layout do painel

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Gateway Config Panel                         [Upload .xlsx] [Export]    │
├──────────────────────────────────────────────────────────────────────────┤
│  Canais Redis                                                            │
│  Canal          │ Delay    │ History size  │ Uso atual  │ Ação           │
│  plc_alarmes    │  200ms   │ [100    ] pub │  87/100    │ [Aplicar]      │
│  plc_process    │ 1000ms   │ [100    ] pub │  100/100   │ [Aplicar]      │
│  plc_visual     │ 1000ms   │ [ 50    ] pub │  50/50     │ [Aplicar]      │
│  plc_config     │ 10000ms  │ [200    ] pub │  12/200    │ [Aplicar]      │
├──────────────────────────────────────────────────────────────────────────┤
│  Variáveis                                                               │
│  Filtros: Grupo ▼   Tipo ▼   Canal ▼        Status: 79/81 habilitados   │
│                                                                          │
│  Tag              │ Grupo     │ Canal       │ Delay   │ En.│ Últ. valor  │
│  extrusoraErro    │ Extrusora │ plc_process │ 1000ms  │ ✓  │ false 500ms │
│  emergencia     * │ alarmes   │ plc_alarmes │  200ms  │ ✓  │ false 180ms │
│  densidadeMedia * │ producao  │ plc_config  │ 10000ms │ ✗  │ —           │
└──────────────────────────────────────────────────────────────────────────┘
  * = override individual ativo
```

### 3.5 — Critério de aceite

- [ ] Upload de `.xlsx` parseia e exibe preview sem erros
- [ ] Edição inline salva via Socket.IO `config:save` e reflete em `variable_overrides.json`
- [ ] Coluna "Último valor" atualiza em tempo real via Socket.IO
- [ ] Exportação gera `.xlsx` com todas as colunas originais + `canal`, `delay_ms`, `enabled`
- [ ] Filtros por grupo/tipo/canal funcionando sem latência perceptível
- [ ] Painel de canais exibe `history_size` atual e uso real (`llen history:{channel}`)
- [ ] Editar `history_size` → salva em `group_config.json` + aplica `ltrim` imediato no Redis + confirma via Socket.IO `config:updated`
- [ ] Reduzir `history_size` trunca o histórico existente imediatamente (não apenas nas próximas publicações)

---

## Ordem de implementação recomendada

```
1. tables/group_config.json            (30 min)
2. tables/variable_overrides.json      (15 min)
3. Delfos/table_filter.py              (nova função por grupo)
4. Delfos/delfos.py                    (loop adaptativo + config_reload)
5. tests/test_segmented_reading.py     (validar Fase 1)
─────────────────────────── Fase 1 estável ───────────────────
6. Hub/redis_bridge.py                 (bridge async Redis → sio)
7. Hub/main.py                         (FastAPI + sio + endpoints REST)
8. tests/test_hub.py                   (validar Fase 2)
─────────────────────────── Fase 2 estável ───────────────────
9. Hub/config_store.py                 (leitura/escrita de configs)
10. Hub/templates/index.html           (AG Grid + Socket.IO client)
11. Testes E2E do painel               (validar Fase 3)
```

---

## Dependências entre fases

```
Fase 1  ──precede──→  Fase 2  ──precede──→  Fase 3
  │                      │
  │ group_config.json     │ Hub/config_store.py
  │ variable_overrides    │ Socket.IO events
  └── hot-reload          └── endpoints REST
```

Fase 2 pode ser iniciada antes de a Fase 1 estar 100% completa — o bridge Redis→Socket.IO funciona com os canais antigos (`plc_data`, `alarms`) enquanto a Fase 1 não estiver pronta.

---

## Impacto em processos existentes

| Processo | Fase 1 | Fase 2 | Fase 3 |
|----------|--------|--------|--------|
| Delfos   | Modificado | +1 canal subscrito (`config_reload`) | Nenhum |
| Atena    | Nenhum | Nenhum | Nenhum |
| Hub      | —      | Novo processo | Expansão |

---

## Canais Redis após implementação completa

| Canal | Produtor | Consumidor | Freq. típica |
|-------|----------|------------|-------------|
| `plc_alarmes` | Delfos | Hub, externos | 200ms |
| `plc_process` | Delfos | Hub, externos | 500ms–2s |
| `plc_visual`  | Delfos | Hub, externos | 1s |
| `plc_config`  | Delfos | Hub, externos | 5s–10s |
| `plc_data`    | Delfos | Legado | igual ao grupo mais rápido |
| `alarms`      | Delfos | Hub, externos | mesma freq. que `plc_config` |
| `plc_commands`| Hub, externos | Atena | sob demanda |
| `user_status` | Hub, UI | Delfos, Atena | sob demanda |
| `config_reload`| Hub | Delfos | sob demanda |
| `ia_status`   | IA/Cloud | Atena | sob demanda |
| `ia_data`     | IA/Cloud | Atena | sob demanda |
