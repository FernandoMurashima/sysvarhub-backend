# MySQL Community Server

Coloque aqui, apenas no ambiente de build, uma distribuicao oficial extraida do MySQL Community Server 8.0.x x64 (8.0.26 ou posterior).

O diretorio informado ao build por `-MySqlRoot` deve conter:

- `bin\mysqld.exe`
- `bin\mysql.exe`
- `bin\mysqldump.exe`
- `bin\mysqlbinlog.exe`

Os quatro executaveis devem ter a mesma versao do servidor. O build valida presenca e metadados de versao antes de preparar o pacote.

Os binarios do MySQL sao insumos externos de build e nao devem ser commitados neste repositorio.
