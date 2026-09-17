"""
URL configuration for sysvarhub project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.http import Http404, JsonResponse
from django.views.static import serve
from django.urls import include, path


def health_check(_request):
    return JsonResponse({"status": "ok", "service": "sysvar-hub"})


def angular_spa(request, path=""):
    if path.startswith("api/"):
        raise Http404()

    frontend_dir = Path(settings.FRONTEND_DIST_DIR)
    requested_path = frontend_dir / path
    if path and requested_path.is_file():
        return serve(request, path, document_root=frontend_dir)

    index_path = frontend_dir / "index.html"
    if not index_path.is_file():
        raise Http404("Frontend Angular compilado nao encontrado.")
    return serve(request, "index.html", document_root=frontend_dir)


urlpatterns = [
    path('admin/', admin.site.urls),
    path("api/health/", health_check, name="health-check"),
    path("api/terminal/", include("core.urls")),
    path("", angular_spa, name="angular-spa-root"),
    path("<path:path>", angular_spa, name="angular-spa"),
]
