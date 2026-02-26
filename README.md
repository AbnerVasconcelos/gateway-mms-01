# Gateway Modbus IoT

Gateway IoT industrial genérico para integração entre CLPs Modbus e sistemas de nuvem, IA ou interfaces gráficas via Redis pub/sub.

O mapeamento de tags é inteiramente definido por arquivos CSV — nenhuma alteração de código é necessária para adaptar o gateway a uma nova aplicação industrial.

---

## Arquitetura

```
CLP (Modbus TCP)
      │
      ├──► Delfos (leitura) ──► Redis ch2, ch4 ──► [consumidores externos]
      │
      └──◄ Atena  (escrita) ◄── Redis ch3, ch5, ch7 ◄── [UI / IA / nuvem]
                                      │
                               Redis ch1 (estado do usuário)
```

Cada processo roda de forma independente. A comunicação entre eles é feita exclusivamente via Redis pub/sub.

---

## Processos

### Delfos — Leitor do CLP

Lê coils e holding registers do CLP em ciclo contínuo e publica os dados no Redis.

- **Publica:** `channel2` (dados operacionais), `channel4` (alarmes/configuração)
- **Assina:** `channel1` (estado do usuário)
- **Frequência:** 1 Hz quando usuário conectado, 0,033 Hz quando inativo
- **Otimização:** agrupa endereços Modbus contíguos para minimizar roundtrips de rede

### Atena — Escritor do CLP

Recebe comandos via Redis e os escreve nos coils e holding registers do CLP.

- **Assina:** `channel1`, `channel3`, `channel5`, `channel7`
- **Segurança:** só escreve no CLP quando `user_state = True`
- **Modelo:** orientado a eventos via `pubsub.listen()`

---

## Canais Redis

| Canal | Direção | Conteúdo |
|-------|---------|----------|
| `channel1` | ↔ bidirecional | Estado de conexão do usuário |
| `channel2` | Delfos → externos | Dados operacionais do CLP + timestamp |
| `channel3` | externos → Atena | Comandos de escrita no CLP |
| `channel4` | Delfos → externos | Dados de alarmes/configuração + timestamp |
| `channel5` | externos → Atena | Ativação do modo IA |
| `channel7` | externos → Atena | Dados do modelo de IA |

---

## Estrutura do projeto

```
gateway/
├── shared/
│   ├── modbus_functions.py        # setup, leitura e escrita Modbus
│   └── redis_config_functions.py  # setup, publish e subscribe Redis
├── Delfos/
│   ├── delfos.py                  # entry point
│   └── table_filter.py            # agrupamento de endereços contíguos
├── Atena/
│   ├── atena.py                   # entry point
│   ├── data_handle.py             # handlers por canal
│   └── table_filter.py            # lookup reverso por ObjecTag
├── tables/
│   ├── operacao.csv               # mapeamento de tags operacionais
│   └── configuracao.csv           # parâmetros de configuração
├── .env.example                   # template de variáveis de ambiente
├── requirements.txt
└── CLAUDE.md                      # referência técnica completa do projeto
```

---

## Configuração

Copie `.env.example` para `.env` na raiz e ajuste os valores:

```bash
cp .env.example .env
```

```env
MODBUS_HOST=192.168.1.2
MODBUS_PORT=502
MODBUS_UNIT_ID=2
REDIS_HOST=localhost
REDIS_PORT=6379
TABLES_DIR=../tables
```

---

## Instalação

**Linux:**
```bash
python3 -m venv gateway
source gateway/bin/activate
pip install -r requirements.txt
```

**Windows:**
```powershell
python -m venv gateway
gateway\Scripts\activate
pip install -r requirements.txt
```

---

## Execução

Inicie cada processo em um terminal separado:

```bash
# Terminal 1
cd Delfos && python delfos.py

# Terminal 2
cd Atena && python atena.py
```

> Redis deve estar em execução antes de iniciar os processos.

---

## Mapeamento de tags (CSV)

O arquivo `tables/operacao.csv` define o mapeamento entre variáveis do CLP e o JSON publicado no Redis:

| Coluna | Descrição |
|--------|-----------|
| `key` | Namespace lógico (ex: `Motor`, `Sensor`) |
| `ObjecTag` | Nome da variável no JSON |
| `Modbus` | Endereço Modbus |
| `At` | `%MB` = coil, `%MW` = holding register |

Para adaptar o gateway a uma nova aplicação, basta editar os CSVs — sem alteração de código.

---

## Dependências principais

| Pacote | Versão | Uso |
|--------|--------|-----|
| `pyModbusTCP` | 0.2.1 | Cliente Modbus TCP |
| `redis` | 5.0.3 | Pub/sub e store |
| `pandas` | 2.2.1 | Leitura dos CSVs |
| `python-dotenv` | ≥1.0.0 | Variáveis de ambiente |
