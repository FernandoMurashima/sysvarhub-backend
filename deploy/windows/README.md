# Sysvar Hub no Windows

O runtime de producao do Sysvar Hub roda em um unico processo Windows com Django, Waitress e o frontend Angular compilado servido pelo proprio Hub.

## Arquitetura

- `http://<ip-da-loja>:8000/` atende o frontend Angular.
- `http://<ip-da-loja>:8000/api/...` atende a API local do Hub.
- `GET /api/health/` retorna o estado basico do servico para instalador e monitoramento.
- O banco local esperado e MySQL.

## Diretorios previstos

- `C:\Program Files\Sysvar Hub\app`: backend empacotado.
- `C:\Program Files\Sysvar Hub\frontend`: Angular compilado.
- `C:\ProgramData\SysvarHub\config`: arquivo `.env` da instalacao.
- `C:\ProgramData\SysvarHub\logs`: `hub.log` com rotacao.
- `C:\ProgramData\SysvarHub\backup`: backups futuros.
- `C:\ProgramData\SysvarHub\data`: dados auxiliares futuros.

Os caminhos podem ser alterados por parametros ou variaveis de ambiente.

## Configuracao

Use `sysvarhub.env.example` como modelo e preencha segredo, banco, hosts permitidos e origens confiaveis. Em producao, `DJANGO_DEBUG=False` exige `DJANGO_SECRET_KEY` real.

## Scripts

- `install-runtime.ps1`: cria diretorios base e copia o template de configuracao.
- `start-hub.ps1`: inicia `runtime/run_hub.py` com Waitress.
- `stop-hub.ps1`: encerra o processo ouvindo na porta informada.
- `check-hub.ps1`: consulta `/api/health/` e retorna exit code `0` quando saudavel.
- `build-frontend.ps1`: compila o Angular e copia o resultado para `frontend_dist`.
- `prepare-package.ps1`: monta `deploy\windows\staging\` com app, frontend, template e scripts.
- `build-installer.ps1`: gera o runtime PyInstaller, monta staging com frontend e MySQL e chama o Inno Setup.
- `install-hub.ps1`: cria configuracao segura, registra os servicos `SysvarHubMySQL` e `SysvarHub`, aplica migrations e libera firewall apenas para TCP 8000.
- `configurar-hub.ps1`: executa somente `ativar_hub`; depois disso o Hub fica conectado aguardando a primeira sincronizacao comandada pela Central.
- `diagnostico.ps1`: mostra estado dos servicos e portas sem exibir segredos.
- `uninstall-hub.ps1`: remove servicos e binarios preservando `C:\ProgramData\SysvarHub`.
- `purge-data.ps1`: remove dados preservados somente quando executado explicitamente.

## MySQL

O instalador usa MySQL Community Server 8.x x64 em ZIP extraido, informado no build por `-MySqlRoot`. Ele sera instalado em `C:\Program Files\Sysvar Hub\mysql` e configurado para escutar somente `127.0.0.1:3307`.

## Build e instalador

O build do frontend nao e versionado. O instalador final devera embutir runtime Python, dependencias, app, frontend compilado, scripts e template de configuracao, sem exigir Python, Node.js, Angular CLI, Git ou VS Code na maquina da loja.

## Fluxo de primeira sincronizacao

1. Instalar o Hub.
2. Ativar o Hub com a URL da Central e o codigo de ativacao.
3. O servico fica online enviando heartbeat para a Central.
4. A Central solicita a primeira sincronizacao pelo painel.
5. O Hub recebe o comando no heartbeat, executa a carga completa e informa o resultado.
