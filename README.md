# Roleta no Telegram — guia rápido

```
roleta/
├── frontend/   → vai para o GitHub Pages (o Mini App que abre dentro do Telegram)
│   ├── index.html
│   ├── style.css
│   └── script.js
└── backend/    → vai para um servidor (API + bot + banco de dados)
    ├── main.py
    ├── prizes.txt        ← AQUI você edita as opções da roleta
    ├── requirements.txt
    └── .env.example      ← lista das variáveis de ambiente
```

## Como funciona

1. O usuário abre o bot no Telegram e toca em **🎰 Abrir roleta** (ou no botão de menu).
2. O site (Mini App) manda o `initData` do Telegram ao backend. O backend **valida a assinatura**, então ninguém consegue se passar por outro ID.
3. O backend confere o saldo, **sorteia o prêmio no servidor** e grava o giro.
4. O **bot envia o prêmio no chat** do usuário. Ele pode fechar o app logo após girar.

## Regras de tokens

| Usuário | Como gira |
|---|---|
| **Comum** | Gasta 1 token comprado por giro, até acabar. |
| **VIP** | Ganha **1 token por dia** (vale o dia do calendário, fuso de Brasília). **Não acumula**: se não usar no dia, perde. Se tiver tokens comprados, o diário é usado primeiro. |

O VIP dura o número de dias que você definir (padrão 30).

## Comandos do bot

Todos:
- `/start` abre a roleta · `/saldo` mostra tokens e status · `/ajuda`

Só administradores (IDs em `ADMIN_IDS`):
- `/addtokens <id> <qtd>` adiciona tokens (negativo remove; o saldo nunca fica abaixo de 0)
- `/vip <id> [dias]` ativa ou estende o VIP (padrão 30 dias)
- `/unvip <id>` remove o VIP
- `/user <id>` consulta um usuário

O usuário recebe uma mensagem no bot quando ganha tokens ou VIP. Para descobrir o ID de um usuário, peça para ele mandar `/saldo` ao bot: a resposta mostra o ID dele (o seu aparece do mesmo jeito).

## Editando as opções (backend/prizes.txt)

```
Nome | chance | cor | ícone | tipo | conteúdo
```

| tipo | o que o bot faz | conteúdo |
|---|---|---|
| `tokens` | soma tokens ao saldo | quantidade (ex: `10`) |
| `texto` | envia uma mensagem | o texto (`\n` quebra linha) |
| `link` | envia um link | a URL |
| `arquivo` | envia um arquivo | `file_id` do Telegram ou URL direta |
| `nada` | "não foi dessa vez" | vazio |

O arquivo é relido sozinho quando muda, sem reiniciar o servidor. Os conteúdos (cupons, links) ficam só no servidor e **não** aparecem na API pública.

## Passo a passo para colocar no ar

### 1. Criar o bot
No Telegram, fale com **@BotFather** → `/newbot` → guarde o **token**. Pegue também o seu ID numérico (por exemplo com @userinfobot) para usar como admin.

### 2. Publicar o frontend (GitHub Pages)
1. Crie um repositório e suba o conteúdo de `frontend/`.
2. Em `script.js`, troque `API_BASE_URL` pela URL do backend (terminando em `/api`) e `SUPPORT_URL` pelo seu `https://t.me/seu_usuario`.
3. Ative Settings → Pages. A URL será algo como `https://seuusuario.github.io/roleta/`.

### 3. Publicar o backend
Pode ser Render, Railway, Fly.io ou uma VPS.
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Configure as variáveis de `.env.example` (principalmente `BOT_TOKEN`, `ADMIN_IDS`, `WEBAPP_URL`, `PUBLIC_URL`, `WEBHOOK_SECRET`, `ALLOWED_ORIGINS`).

Ao iniciar, o backend registra sozinho o webhook do bot e o botão de menu **🎰 Girar**.

> ⚠️ **Banco de dados:** o SQLite é um arquivo. Em hospedagem grátis sem disco persistente (ex.: plano free do Render), ele é **apagado a cada deploy/reinício** e você perde saldos e VIPs. Use um disco persistente (`DB_PATH=/data/roleta.db`) ou uma VPS. Se o projeto crescer, vale migrar para Postgres.

### 4. Testar
1. Abra o bot e mande `/start`.
2. Como admin: `/addtokens SEU_ID 5`.
3. Toque em **🎰 Abrir roleta** e gire. O prêmio chega no chat.

## Teste local (sem Telegram)
```
cd backend
pip install -r requirements.txt
DEV_MODE=1 uvicorn main:app --port 8000
```
Com `DEV_MODE=1` o backend aceita requisições sem `initData` (usuário de teste de ID 1). **Nunca use isso em produção.** No `script.js`, use `API_BASE_URL = 'http://localhost:8000/api'`.

## Segurança — resumo
- O sorteio e o saldo ficam no servidor; o app só desenha a animação.
- Toda requisição é autenticada pela assinatura do Telegram (HMAC com o token do bot) e expira em 24 h.
- O webhook só aceita chamadas com o `WEBHOOK_SECRET`.
- Os giros são serializados por trava, então não dá para gastar o mesmo token duas vezes com cliques rápidos.
- Defina `ALLOWED_ORIGINS` com o domínio do seu GitHub Pages em produção.
