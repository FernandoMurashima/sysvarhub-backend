from django.test import TestCase

from core.models import HubConfig


class HubConfigTests(TestCase):
    def test_preserva_hub_uuid_ao_salvar(self):
        hub = HubConfig.objects.create(retaguarda_url="http://central.test")
        original_uuid = hub.hub_uuid

        hub.nome = "Sysvar Hub Loja 1"
        hub.save()

        hub.refresh_from_db()
        self.assertEqual(hub.hub_uuid, original_uuid)

    def test_str_nao_expoe_token(self):
        hub = HubConfig.objects.create(
            nome="Sysvar Hub",
            loja_id=1,
            retaguarda_url="http://central.test",
            retaguarda_token="TOKEN-SECRETO",
        )

        self.assertNotIn("TOKEN-SECRETO", str(hub))
