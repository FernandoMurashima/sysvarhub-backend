from django.core.management import BaseCommand, call_command
from django.db import connection


class Command(BaseCommand):
    help = "Valida a instalacao local do Sysvar Hub sem alterar ou limpar dados."

    def handle(self, *args, **options):
        self.stdout.write("Verificando configuracao Django...")
        call_command("check", verbosity=0)

        self.stdout.write("Verificando conexao com o banco...")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

        self.stdout.write(self.style.SUCCESS("Instalacao validada com sucesso."))
