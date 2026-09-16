from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_tipos_despesa_pdv_hub"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        (
                            "ALTER TABLE core_tipodespesapdvhub "
                            "MODIFY COLUMN natureza_codigo varchar(10) NOT NULL"
                        ),
                        (
                            "ALTER TABLE core_tipodespesapdvhub "
                            "MODIFY COLUMN natureza_descricao varchar(255) NOT NULL"
                        ),
                        (
                            "ALTER TABLE core_tipodespesapdvhub "
                            "MODIFY COLUMN natureza_categoria_principal varchar(50) NOT NULL"
                        ),
                        (
                            "ALTER TABLE core_tipodespesapdvhub "
                            "MODIFY COLUMN natureza_subcategoria varchar(50) NOT NULL"
                        ),
                        (
                            "ALTER TABLE core_tipodespesapdvhub "
                            "MODIFY COLUMN natureza_tipo varchar(20) NOT NULL"
                        ),
                        (
                            "ALTER TABLE core_tipodespesapdvhub "
                            "MODIFY COLUMN natureza_categoria_gerencial varchar(50) NOT NULL"
                        ),
                    ],
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
            state_operations=[],
        ),
    ]
