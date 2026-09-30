# Sysvar Hub Backend

Backend local da loja no Projeto Sysvar.

## Papel no produto

O Sysvar Hub é a camada operacional local da loja. Ele mantém dados e serviços necessários para o funcionamento dos terminais/PDVs na LAN e sincroniza com a retaguarda Central.

Estrutura conceitual:

```text
Sysvar Central
    ↕ Internet / APIs autenticadas
Sysvar Hub da Loja
    ↕ LAN
Terminais / PDVs
```

O Hub não é um ERP independente. Cadastros e autoridades corporativas continuam na Central; o Hub mantém cópias locais, estado operacional e filas necessárias à operação da loja.

## Stack principal

- Python
- Django
- Django REST Framework
- MySQL local

As versões efetivas estão em `requirements.txt`.

## Componentes principais

- `core/`: estado e operação local do Hub
- `integracao/`: comunicação e sincronização com a Central
- `sysvarhub/`: configuração da aplicação
- `deploy/`: recursos de instalação e execução
- `frontend_dist/`: frontend empacotado/servido pelo Hub quando aplicável
- `runtime/`: recursos de runtime
- `docs/`: documentação técnica acoplada à implementação local

## Desenvolvimento

Entrada principal:

```text
manage.py
```

Script de desenvolvimento:

```text
iniciar-hub-dev.ps1
```

## Integração

O Hub usa credenciais próprias para falar com a Central e credenciais de Terminal separadas para chamadas locais.

Fluxos ativos incluem ativação, bootstrap/sincronização, heartbeat, administração de terminal, operação de PDV, vendas, devoluções e vale-troca, conforme o estado atual do código.

## Documentação

Documentação central do Projeto Sysvar fica em:

`FernandoMurashima/sysvar-vault`

Pasta principal:

`takeshi/10 Projetos/Sysvar`

Contexto específico do Hub:

`takeshi/10 Projetos/Sysvar/Contexto do Projeto/Sysvar Hub.md`

O diretório `docs/` deste repositório deve conter somente documentação técnica diretamente ligada ao backend Hub. Atualmente inclui documentação de devolução/troca e vale-troca online.

Antes de alterações relevantes, consultar a documentação vigente no `sysvar-vault`, o backend Central quando o fluxo atravessar a retaguarda e o código atual da branch `main`.
