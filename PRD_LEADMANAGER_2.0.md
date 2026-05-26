# 📋 PRD - Gerenciador de Leads Multi-Tenant
## Produto: LeadManager 2.0
**Data:** Maio 2026 | **Status:** Em Desenvolvimento | **Plataforma:** Django + Vibe Code

---

## 📌 VISÃO GERAL DO PRODUTO

O **LeadManager 2.0** é uma aplicação web multi-tenant que permite usuários gerenciar leads provenientes de planilhas Google Sheets públicas, com notificações em tempo real via Telegram.

**Objetivo Principal:** Centralizar o gerenciamento de leads imobiliários com interface visual intuitiva (Kanban) e notificações instantâneas quando novos leads chegam.

---

## 🎯 OBJETIVOS DO PRODUTO

1. ✅ Permitir que cada usuário gerencie sua própria planilha de leads
2. ✅ Exibir leads em um painel visual tipo Kanban com 6 colunas fixas
3. ✅ Permitir movimento de leads entre colunas (drag & drop)
4. ✅ Notificar o usuário via Telegram quando um novo lead chega
5. ✅ Manter sincronização entre o Kanban e a Google Sheets
6. ✅ Suportar múltiplos usuários com isolamento de dados (multi-tenant)

---

## 👥 PÚBLICOS-ALVO

- **Imobiliários/Construtoras** que recebem leads via Google Forms
- **Equipes de vendas** que precisam acompanhar status de leads
- **Gerentes** que querem visualizar fluxo de leads em tempo real

---

## 💡 PROPOSTA DE VALOR

| Antes | Depois |
|-------|--------|
| ❌ Leads espalhados em planilhas desorganizadas | ✅ Painel centralizado com Kanban visual |
| ❌ Sem notificações de novos leads | ✅ Notificação instantânea no Telegram |
| ❌ Atualizar status manualmente é difícil | ✅ Drag & drop simples no Kanban |
| ❌ Sem rastreamento de progresso | ✅ Visualização clara do pipeline de vendas |

---

## 🏗️ ARQUITETURA TÉCNICA

### Stack Tecnológico

```
Frontend:        Vibe Code (JavaScript)
Backend:         Django (Python)
Database:        SQLite (autenticação + config)
Dados Principais: Google Sheets (pública, CSV export)
Parsing:         BeautifulSoup + Requests (web scraping)
Notificações:    Telegram Bot API
Hospedagem:      (A definir)
```

**Nota:** Como a planilha é pública, não é necessário Google Sheets API ou projeto no Google Cloud Console. Usamos a URL pública da planilha em formato CSV.

### Diagrama de Fluxo

```
┌─────────────────────────────────────────────────────────────────┐
│                    Usuário no Browser                            │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                    [Vibe Code Interface]
                         ↙        ↘
                        ↙          ↘
        ┌──────────────────┐   ┌──────────────────┐
        │  Dashboard       │   │  Kanban          │
        │  - Abas          │   │  - 6 Colunas     │
        │  - Contagem      │   │  - Drag & Drop   │
        │  - Gráfico       │   │  - Cards         │
        └──────────────────┘   └──────────────────┘
                        ↘          ↙
                         ↘        ↙
                    [Django Backend]
                         ↙    ↓    ↘
                        ↙     ↓     ↘
        ┌──────────────────┐  │  ┌──────────────────┐
        │  SQLite Database │  │  │ Telegram Bot     │
        │  - Users         │  │  │ - Notificações   │
        │  - Auth          │  │  │ - Chat IDs       │
        │  - Profiles      │  │  └──────────────────┘
        └──────────────────┘  │
                             ↓
                    [Google Sheets API]
                    - Lê leads
                    - Atualiza status
```

### Estrutura de Dados (SQLite)

```python
# Tabela: User
- id (PK)
- email (unique)
- password_hash
- is_active
- created_at

# Tabela: UserProfile
- id (PK)
- user_id (FK)
- sheet_id (Google Sheets ID)
- telegram_chat_id (para notificações)
- telegram_enabled (bool)
- created_at
- updated_at

# Tabela: SheetMetadata
- id (PK)
- user_id (FK)
- sheet_id (Google Sheets ID)
- sheet_names (JSON array com nomes das abas)
- last_sync (timestamp do último sync)
```

---

## 📱 INTERFACE DO USUÁRIO

### 1. Página de Login

**Funcionalidades:**
- Login com email + senha (criados pelo admin no Django Admin)
- Acesso apenas para usuários cadastrados
- Link para Django Admin (/admin) para admins

---

### 2. Página de Perfil ("Meu Perfil")
```
┌─────────────────────────────────────────┐
│     LeadManager 2.0 - Meu Perfil        │
│                                         │
│  Google Sheets (Planilha Pública)       │
│  ─────────────────────────────────     │
│  ID da Planilha:                        │
│  [1947wNxGvwhugX4MTaqctFij741gG0...] │
│                                         │
│  Cole o ID de:                          │
│  https://docs.google.com/spreadsheets   │
│  /d/{ID}/edit                           │
│                                         │
│  Telegram                               │
│  ─────────────────────────────────     │
│  ☐ Ativar Notificações Telegram        │
│                                         │
│  Chat ID do Telegram:                   │
│  [________________]                     │
│  (Envie /start para o bot para pegar)   │
│                                         │
│  [Salvar]  [Testar Notificação]        │
│                                         │
└─────────────────────────────────────────┘
```

**Funcionalidades:**
- Configurar ID da planilha Google Sheets (pública)
- Ativar/desativar notificações Telegram
- Configurar Chat ID do Telegram
- Testar notificação Telegram
- Salvar configurações

**Nota:** A planilha deve ser compartilhada como "Qualquer pessoa com link pode visualizar"

---

### 3. Dashboard
```
┌──────────────────────────────────────────────────────────┐
│ LeadManager 2.0 - Dashboard                  [Perfil][Sair]│
└──────────────────────────────────────────────────────────┘

┌──────────┐
│ Total: 20│
└──────────┘

Abas da Planilha
┌──────────────────┬──────────────────┬──────────────────┬──────────────────┐
│ Casa Blue 1580mi │Casa Enseada 5890 │ Casa Amare E11   │Casa Villas       │
│ Total: 5         │ Total: 5         │ Total: 6         │ Total: 4         │
│                  │                  │                  │                  │
│   [Clicável]     │   [Clicável]     │   [Clicável]     │   [Clicável]     │
└──────────────────┴──────────────────┴──────────────────┴──────────────────┘

Leads Recebidos nos Últimos 30 Dias
┌────────────────────────────────────────────────────────┐
│ 4 │                                                    │
│ 2 │                                                    │
│ 0 ├──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬│
│    25 27 29 01 03 05 07 09 11 13 15 17 19 21 23 25   │
└────────────────────────────────────────────────────────┘
```

**Funcionalidades:**
- Exibir total de leads
- Exibir cartões de abas (sheet names) com contagem
- Cartões são clicáveis e levam para o Kanban da aba
- Exibir gráfico de leads recebidos nos últimos 30 dias

---

### 4. Kanban
```
┌─────────────────────────────────────────────────────────────┐
│ LeadManager 2.0 - Casa Blue 3 dorms 1580mi    [← Dashboard] │
│ [Atualizar]                                                  │
└─────────────────────────────────────────────────────────────┘

┌──────────────┬──────────────┬──────────────┬──────────────┬──────────────┬──────────────┐
│   Criado(5)  │Em Análise(0) │Qualif.(0)   │Não Qualif(0) │ Convertido(0)│  Perdido(0)  │
├──────────────┼──────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│              │              │              │              │              │              │
│┌────────────┐│              │              │              │              │              │
││ João Silva ││              │              │              │              │              │
││+11987654321││              │              │              │              │              │
││24/05 14:30 ││              │              │              │              │              │
│└────────────┘│              │              │              │              │              │
│              │              │              │              │              │              │
│┌────────────┐│              │              │              │              │              │
││ Maria      ││              │              │              │              │              │
││+11912345678││              │              │              │              │              │
││23/05 10:15 ││              │              │              │              │              │
│└────────────┘│              │              │              │              │              │
│              │              │              │              │              │              │
│  [+ novo]    │              │              │              │              │              │
│              │              │              │              │              │              │
└──────────────┴──────────────┴──────────────┴──────────────┴──────────────┴──────────────┘
```

**Funcionalidades:**
- Exibir 6 colunas fixas SEMPRE (Criado, Em Análise, Qualificado, Não Qualificado, Convertido, Perdido)
- Exibir contagem de leads por coluna
- Cada card mostra: Nome, Telefone, Data
- Drag & drop funcional entre colunas
- Botão de atualização para sincronizar com Google Sheets
- Botão para voltar ao Dashboard

---

## 🔄 FLUXO DE FUNCIONAMENTO

### 1. Novo Usuário (Onboarding)

```
1. Admin cria usuário no Django Admin (/admin)
   ├─ Email
   └─ Senha temporária
   ↓
2. Usuário acessa LeadManager 2.0
   ↓
3. Faz login com Email + Senha
   ↓
4. Usuário é redirecionado para [Meu Perfil]
   ↓
5. Configura:
   - ID da Planilha Google Sheets (link público)
   - Ativa Notificações Telegram
   - Configura Chat ID do Telegram
   ↓
6. Sistema detecta automaticamente as abas
   ↓
7. Usuário é redirecionado ao Dashboard
```

---

### 2. Novo Lead Chega (Fluxo com Notificação)

```
1. Novo lead é adicionado à Google Sheets pelo usuário/form
   ↓
2. Sistema faz polling a cada X minutos (recomendado: 5 min)
   ↓
3. Sistema detecta novo lead:
   - Compara com último sync
   - Identifica dados: Nome, Telefone, Aba (Sheet Name)
   ↓
4. Sistema envia notificação Telegram:
   
   📢 Novo Lead!
   📌 Aba: Casa Blue 3 dorms 1580mi
   👤 Nome: João Silva
   📱 Telefone: +55 11 98765-4321
   
   ↓
5. Sistema atualiza cache/metadata
   ↓
6. Usuário vê novo lead no Kanban (coluna "Criado")
```

---

### 3. Usuário Move Card no Kanban

```
1. Usuário arrasta card de "Criado" para "Em Análise"
   ↓
2. Frontend envia requisição ao backend:
   {
     "lead_row_index": 5,
     "new_column_id": "em_analise",
     "new_status": "EM_ANALISE"
   }
   ↓
3. Backend atualiza a Google Sheets:
   - Encontra a linha do lead (row 5)
   - Atualiza coluna de status para "EM_ANALISE"
   ↓
4. Frontend atualiza visualmente o Kanban
   ↓
5. Google Sheets fica sincronizada ✅
```

---

## 🔔 SISTEMA DE NOTIFICAÇÕES TELEGRAM

### Requisitos Funcionais

1. **Integração com Telegram Bot API**
   - Criar um Telegram Bot (via BotFather)
   - Armazenar token do bot no backend
   - Armazenar Chat ID de cada usuário

2. **Notificação ao Novo Lead**
   - Trigger: Novo lead detectado na Google Sheets
   - Conteúdo da mensagem:
     ```
     📢 Novo Lead!
     📌 Aba: [nome_da_aba]
     👤 Nome: [nome_lead]
     📱 Telefone: [telefone_lead]
     ```
   - Enviada ao Chat ID do usuário

3. **Notificação de Teste**
   - Usuário pode testar a notificação em "Meu Perfil"
   - Botão [Testar Notificação] envia mensagem de teste
   - Útil para validar que o Chat ID está correto

4. **Desabilitar/Habilitar Notificações**
   - Checkbox "Ativar Notificações Telegram"
   - Se desativado, não envia notificações
   - Se ativado e Chat ID está vazio, exibe aviso

### Implementação Técnica

**Backend (Django):**
```python
# tasks.py (para execução periódica)
@periodic_task(run_every=crontab(minute='*/5'))
def check_new_leads():
    """
    A cada 5 minutos:
    1. Para cada usuário ativo
    2. Busca dados da Google Sheets
    3. Compara com último sync
    4. Se tem novos leads:
       - Envia notificação Telegram
       - Atualiza metadata
    """
    pass

# telegram_service.py
def send_telegram_notification(chat_id, sheet_name, lead_name, lead_phone):
    """
    Envia notificação para o usuário via Telegram
    """
    message = f"""
📢 Novo Lead!
📌 Aba: {sheet_name}
👤 Nome: {lead_name}
📱 Telefone: {lead_phone}
    """
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
    )
```

---

## 📊 ESTRUTURA DO BANCO DE DADOS (SQLite)

### Tabela: Users
```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

### Tabela: UserProfiles
```sql
CREATE TABLE user_profiles (
    id INTEGER PRIMARY KEY,
    user_id INTEGER UNIQUE NOT NULL,
    sheet_id VARCHAR(255) NOT NULL,
    telegram_chat_id VARCHAR(255),
    telegram_enabled BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

### Tabela: SheetMetadata
```sql
CREATE TABLE sheet_metadata (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    sheet_id VARCHAR(255) NOT NULL,
    sheet_names TEXT,  -- JSON array
    last_sync TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

### Tabela: SyncLog
```sql
CREATE TABLE sync_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    sheet_name VARCHAR(255) NOT NULL,
    lead_count INTEGER,
    last_lead_row_index INTEGER,
    synced_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

---

## 📥 ACESSO À GOOGLE SHEETS PÚBLICA (CSV)

Como a planilha é pública, não é necessário autenticação da Google. Apenas exportamos como CSV:

### URL de Exportação CSV
```
https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}
```

**Onde:**
- `{SHEET_ID}`: ID da planilha (ex: `1947wNxGvwhugX4MTaqctFij741gG0SDVmGRlNgKKQpY`)
- `{GID}`: ID da aba/sheet (ex: `0` para primeira aba, `738340972` para outra aba)

### Exemplo Real
```
https://docs.google.com/spreadsheets/d/1947wNxGvwhugX4MTaqctFij741gG0SDVmGRlNgKKQpY/export?format=csv&gid=0
```

### No Django
```python
def get_sheet_csv(sheet_id, gid=0):
    """Busca CSV da Google Sheets pública"""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    response = requests.get(url)
    return response.text

def parse_csv_to_leads(csv_text):
    """Converte CSV para lista de dicts"""
    import csv
    from io import StringIO
    
    reader = csv.DictReader(StringIO(csv_text))
    leads = []
    for row in reader:
        leads.append({
            'nome': row.get('nome', ''),
            'telefone': row.get('telefone', ''),
            'status': row.get('status', ''),
            'aba': row.get('aba', ''),
            # ... outros campos
        })
    return leads
```

---

## 🔐 SEGURANÇA

1. **Autenticação:**
   - Login com email + senha
   - Senhas hasheadas com bcrypt
   - Sessões seguras (Django)
   - Usuários criados apenas pelo admin

2. **Autorização:**
   - Isolamento por usuário (@login_required)
   - Cada usuário só vê sua própria planilha
   - Validação de sheet_id antes de qualquer operação

3. **Google Sheets (Pública):**
   - Planilhas são públicas (sem auth necessária)
   - Exportação via CSV (sem API key necessária)
   - Rate limiting para evitar abuso de requisições

4. **Telegram API:**
   - Token do bot armazenado como env variable
   - Chat IDs armazenados no SQLite
   - Validação de Chat ID antes de enviar

---

## 📈 ROADMAP (Fases)

### Fase 1: MVP (v1.0) - Prioridade Alta
- [x] Login (usuários criados via admin)
- [x] Página "Meu Perfil" com config Google Sheets
- [x] Dashboard com abas
- [x] Kanban com 6 colunas fixas
- [x] Drag & Drop funcional
- [x] Sincronização com Google Sheets (via CSV)
- [x] Notificação Telegram para novo lead

### Fase 2: Melhorias (v1.1)
- [ ] Sistema de papéis (admin, gerente, vendedor)
- [ ] Histórico de alterações de status
- [ ] Filtros por data, status, aba
- [ ] Exportar dados para Excel
- [ ] Buscar/filtrar leads no Kanban
- [ ] Dark mode

### Fase 3: Avançado (v2.0)
- [ ] Integração com WhatsApp
- [ ] Automação de workflows
- [ ] Análise avançada de dados
- [ ] API pública para integrações
- [ ] Aplicativo mobile

---

## 🚀 ENDPOINTS DA API

### Autenticação
```
POST   /api/auth/login/           - Login
POST   /api/auth/logout/          - Logout
POST   /api/auth/refresh-token/   - Renovar sessão
```

**Nota:** Cadastro de usuários é feito via Django Admin (/admin)

### Perfil do Usuário
```
GET    /api/profile/              - Buscar perfil
PUT    /api/profile/              - Atualizar perfil
POST   /api/profile/test-telegram/ - Testar notificação
```

### Dashboard
```
GET    /api/dashboard/            - Dados do dashboard
GET    /api/sheets/               - Lista de abas
```

### Kanban
```
GET    /api/kanban/<sheet_name>/  - Dados do Kanban
PATCH  /api/lead/<row_index>/     - Atualizar status do lead
```

### Telegram
```
POST   /api/telegram/webhook/     - Webhook do bot (receber msgs)
```

---

## 📋 CHECKLIST DE DESENVOLVIMENTO

### Backend (Django)

**Autenticação:**
- [ ] Criar modelo User (Django padrão)
- [ ] Criar modelo UserProfile
- [ ] Implementar login
- [ ] Implementar @login_required
- [ ] Usar Django Admin para criar usuários (padrão)

**Google Sheets:**
- [ ] Criar função parse_google_sheets_csv()
- [ ] Criar função detect_sheet_names_from_csv()
- [ ] Criar função update_cell_in_sheet() (via Selenium ou requests)
- [ ] Implementar cache/metadata
- [ ] Criar função compare_leads_for_changes()

**Telegram:**
- [ ] Criar telegram_service.py
- [ ] Implementar send_notification()
- [ ] Criar webhook para bot (se necessário)
- [ ] Implementar teste de notificação

**API:**
- [ ] POST /api/auth/login/
- [ ] POST /api/auth/logout/
- [ ] GET /api/profile/
- [ ] PUT /api/profile/
- [ ] POST /api/profile/test-telegram/
- [ ] GET /api/dashboard/
- [ ] GET /api/sheets/
- [ ] GET /api/kanban/<sheet_name>/
- [ ] PATCH /api/lead/<row_index>/

**Tarefas Agendadas:**
- [ ] Configurar Celery ou APScheduler
- [ ] Criar task check_new_leads() (a cada 5 min)
- [ ] Testar com múltiplos usuários

### Frontend (Vibe Code)

**Páginas:**
- [ ] Página de Login
- [ ] Página de Cadastro
- [ ] Página de Perfil ("Meu Perfil")
- [ ] Página de Dashboard
- [ ] Página de Kanban

**Componentes:**
- [ ] Card de Aba (Dashboard)
- [ ] Card de Lead (Kanban)
- [ ] Coluna do Kanban
- [ ] Gráfico de leads (últimos 30 dias)

**Funcionalidades:**
- [ ] Drag & Drop
- [ ] Sincronização com backend
- [ ] Notificações visuais de sucesso/erro
- [ ] Formulários com validação

### Testes

- [ ] Testar com 1 usuário
- [ ] Testar com múltiplos usuários
- [ ] Testar novo lead chegando
- [ ] Testar notificação Telegram
- [ ] Testar drag & drop
- [ ] Testar sincronização com Google Sheets
- [ ] Testar em diferentes navegadores

---

## 🔧 CONFIGURAÇÃO INICIAL

### URL da Google Sheets
1. Compartilhar planilha como "Qualquer pessoa com link pode visualizar"
2. Copiar o ID da planilha da URL
   - URL: `https://docs.google.com/spreadsheets/d/{ID}/edit`
   - ID: `1947wNxGvwhugX4MTaqctFij741gG0SDVmGRlNgKKQpY`
3. Para CSV: `https://docs.google.com/spreadsheets/d/{ID}/export?format=csv&gid={SHEET_ID}`

### Telegram Bot
1. Conversar com @BotFather no Telegram
2. Criar novo bot `/newbot`
3. Obter token
4. Armazenar em .env

### Django Admin
1. Criar superuser: `python manage.py createsuperuser`
2. Acessar `/admin` para criar usuários

### Variáveis de Ambiente
```
DEBUG=True
SECRET_KEY=xxx
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_BOT_NAME=xxx
DATABASE_URL=sqlite:///db.sqlite3
```

---

## 📱 INTERFACE - DETALHES ADICIONAIS

### Notificação Telegram (Exemplo Real)

```
📢 Novo Lead!
📌 Aba: Formulário Casa Blue 3 dorms 1580mi
👤 Nome: João da Silva
📱 Telefone: +55 (11) 98765-4321
```

---

## ⚡ PERFORMANCE

- **Polling de novos leads:** A cada 5 minutos
- **Cache de abas:** Atualizar a cada 1 hora ou on-demand
- **Limite de requisições Google Sheets:** 100 requisições/minuto (limite gratuito)
- **Limite de notificações Telegram:** 30 mensagens/segundo por bot

---

## 🎨 DESIGN SYSTEM

- **Cores principais:** Verde (ação), Azul (info), Vermelho (erro)
- **Tipografia:** Roboto ou Inter (fonts system)
- **Ícones:** Usar Feather Icons ou similares
- **Layout:** Mobile-first responsive

---

## 📞 SUPORTE E DOCUMENTAÇÃO

- **API Docs:** /api/docs/ (Swagger)
- **Guia do usuário:** Wiki ou conhecimento base
- **FAQ:** Perguntas frequentes

---

## ✅ CRITÉRIOS DE SUCESSO (MVP)

1. ✅ Usuário consegue fazer login
2. ✅ Usuário consegue configurar sua planilha em "Meu Perfil"
3. ✅ Dashboard exibe abas com contagem correta
4. ✅ Kanban exibe 6 colunas fixas
5. ✅ Drag & drop move o lead entre colunas
6. ✅ Status no Google Sheets é atualizado após mover
7. ✅ Notificação Telegram é enviada quando novo lead chega
8. ✅ Sistema funciona para múltiplos usuários isoladamente

---

## 📝 NOTAS FINAIS

- Este é um PRD para desenvolvimento do zero
- Recomenda-se usar Django REST Framework para API
- Usar pytest para testes
- Configurar CI/CD (GitHub Actions, GitLab CI)
- Documentar API com Swagger/OpenAPI
- Considerar usar Django Celery Beat para tarefas agendadas

---

**Versão:** 1.0 | **Última atualização:** Maio 2026 | **Responsável:** Desenvolvimento
