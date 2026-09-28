from django.urls import path

from integracao.views import HubAtivacaoView


urlpatterns = [
    path("ativacao/", HubAtivacaoView.as_view(), name="hub-ativacao"),
]
