import os

from waitress import serve


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sysvarhub.settings")

    import django
    from django.core.wsgi import get_wsgi_application

    django.setup()

    host = os.environ.get("HUB_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("HUB_PORT", "8000"))
    application = get_wsgi_application()

    serve(application, host=host, port=port)


if __name__ == "__main__":
    main()
